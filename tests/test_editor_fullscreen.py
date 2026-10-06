"""Exercise cross-frame editor lifetime and pointer canvas sizing without real data."""
from html.parser import HTMLParser
from pathlib import Path
import re
import shutil
import subprocess
import unittest
from web_assets import asset_path, render_knowledge_ui

SHELL = Path(__file__).resolve().parents[1] / 'static/index.html'


class EditorUiTests(unittest.TestCase):
    def node(self, program):
        if not shutil.which('node'): self.skipTest('Node needed for UI behavior')
        result = subprocess.run(['node'], input=program, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def function(self, source, name):
        match = re.search(r'function ' + name + r'\([^\n]*', source)
        return match.group()

    def test_only_recognized_same_origin_frame_can_expand_and_restore_focus(self):
        source = SHELL.read_text()
        script = self.function(source, 'releaseEditorExpansion') + '\n' + self.function(source, 'handleMediaEditorMessage')
        self.node("""
let expandedEditor=null;
const classes=()=>({values:new Set(),add(v){this.values.add(v)},remove(v){this.values.delete(v)}});
const unrelated={inert:true,contains:()=>false},toolbar={inert:false,contains:()=>false};
const host={classList:classes(),parentElement:{children:[toolbar]},contains:()=>true};host.parentElement.children.push(host);
const frame={hidden:false,contentWindow:{},closest:()=>host};
const other={hidden:false,contentWindow:{},closest:()=>null};
const header={inert:false,contains:()=>false},page={contains:()=>true};
let focused=0;const focus={isConnected:true,focus:()=>focused++};
const document={activeElement:focus,body:{classList:classes()},querySelectorAll:()=>[header,page,unrelated]};
const window={location:{origin:'https://owned.invalid'}};
const $=id=>id==='frame-android'?frame:other;
""" + script + """
const event={origin:window.location.origin,source:frame.contentWindow,data:{type:'devhelper.media-editor',kind:'image',open:true}};
for(const bad of [{...event,origin:'https://other.invalid'},{...event,source:{}},{...event,data:{...event.data,open:'true'}},{...event,data:{...event.data,kind:'other'}}])handleMediaEditorMessage(bad);
if(expandedEditor)process.exit(1);
handleMediaEditorMessage(event);
if(expandedEditor?.frame!==frame||!host.classList.values.has('editor-expanded')||!document.body.classList.values.has('editor-open')||!header.inert||!toolbar.inert)process.exit(2);
handleMediaEditorMessage({...event,source:other.contentWindow,data:{...event.data,open:false}});
if(!expandedEditor)process.exit(3);
handleMediaEditorMessage({...event,data:{...event.data,open:false}});
if(expandedEditor||host.classList.values.has('editor-expanded')||document.body.classList.values.has('editor-open')||header.inert||toolbar.inert||!unrelated.inert||focused!==1)process.exit(4);
document.activeElement=frame;frame.isConnected=true;frame.focus=()=>{process.exit(5)};
handleMediaEditorMessage(event);handleMediaEditorMessage({...event,data:{...event.data,open:false}});
if(expandedEditor||focused!==1)process.exit(6);
""")

    def test_child_notifies_only_media_lifecycle_and_exact_origin(self):
        source = asset_path('knowledge.html').read_text()
        self.node("""
const calls=[];const window={location:{protocol:'https:',origin:'https://owned.invalid'},parent:{postMessage:(body,target)=>calls.push({body,target})}};
""" + self.function(source, 'notifyMediaWorkspace') + """
notifyMediaWorkspace('attachment-library-modal',true);
notifyMediaWorkspace('image-edit-modal',true);notifyMediaWorkspace('image-edit-modal',false);
if(calls.length!==2||calls[0].target!=='https://owned.invalid'||calls[0].body.open!==true||calls[1].body.open!==false)process.exit(1);
window.location.protocol='file:';notifyMediaWorkspace('video-edit-modal',true);if(calls.length!==2)process.exit(2);
window.location.protocol='https:';window.parent=window;notifyMediaWorkspace('image-edit-modal',true);if(calls.length!==2)process.exit(3);
""")

    def test_canvas_fits_actual_work_area_instead_of_small_modal_limit(self):
        source = asset_path('knowledge.html').read_text()
        image = source[source.index('function resizeMediaEditCanvas(){'):source.index('function imageEditorPoint(')]
        video = source[source.index('function resizeVideoEditCanvas(){'):source.index('\nfunction ',source.index('function resizeVideoEditCanvas(){')+10)]
        self.node("""
const mediaEditState={kind:'image',info:{width:2000,height:1000}};
const host={clientWidth:1280,clientHeight:720},canvas={parentNode:host,style:{}};
const stage={parentNode:{clientWidth:1600,clientHeight:1000},style:{}};
const videoCanvas={style:{}};const videoCanvasState={info:{width:1920,height:1080}};
const window={innerWidth:1800,innerHeight:1100};const renderVideoCanvas=()=>{};
const $=id=>id==='image-edit-canvas'?canvas:id==='video-edit-stage'?stage:videoCanvas;
""" + video + '\n' + image + """
resizeMediaEditCanvas();if(parseInt(canvas.style.width)!==1280)process.exit(1);
host.clientWidth=320;host.clientHeight=180;resizeMediaEditCanvas();if(parseInt(canvas.style.width)!==320)process.exit(2);
mediaEditState.kind='video';resizeMediaEditCanvas();if(parseInt(stage.style.width)<=720||parseInt(stage.style.width)>1552)process.exit(3);
""")

    def test_rejected_close_keeps_workspace_and_draft(self):
        source = asset_path('knowledge.html').read_text()
        close = source[source.index('function closeModal(id,force){'):source.index('\nasync function loadAttachmentLibrary')]
        self.node("""
let allow=false;const notifications=[];const modal={hidden:false,querySelectorAll:()=>[]};
let focused=0;const mediaState={focusBeforeModal:{focus:()=>focused++}};const resourceState={};
const document={querySelector:()=>null,body:{classList:{remove:()=>{}}}};
const $=()=>modal;const closeMediaEditor=()=>allow;const notifyMediaWorkspace=(id,open)=>notifications.push(open);
""" + close + """
if(closeModal('image-edit-modal')!==false||modal.hidden||notifications.length||focused)process.exit(1);
allow=true;if(closeModal('image-edit-modal')!==true||!modal.hidden||notifications[0]!==false||focused!==1)process.exit(2);
""")

    def test_controls_still_bind_and_mac_capabilities_remain_honest(self):
        class Page(HTMLParser):
            def __init__(self, source):super().__init__();self.ids=set();self.feed(source)
            def handle_starttag(self, tag, attrs):
                attrs=dict(attrs)
                if 'id' in attrs:
                    if attrs['id'] in self.ids:raise AssertionError('Duplicate UI ID')
                    self.ids.add(attrs['id'])
        for device in ('mac','android'):
            rendered=render_knowledge_ui(device,Path('/tmp/synthetic-shared'))
            ids=Page(rendered).ids
            for target in re.findall(r"\$\('([^']+)'\)", rendered):self.assertIn(target,ids)
            if device=='mac':self.assertIn('MEDIA_EDITING_SUPPORTED=false',rendered)
            scripts=re.findall(r'<script>([\s\S]*?)</script>',rendered)
            for script in scripts:
                if shutil.which('node'):
                    parsed=subprocess.run(['node','--check'],input=script,text=True,capture_output=True)
                    self.assertEqual(parsed.returncode,0,parsed.stderr)


if __name__=='__main__':unittest.main()
