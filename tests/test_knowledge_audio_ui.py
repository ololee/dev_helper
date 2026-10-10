"""Legacy resource and Markdown audio compatibility without media-edit regression."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest

try:
    from desktop.web_assets import asset_path, render_knowledge_ui
except ImportError:
    from web_assets import asset_path, render_knowledge_ui


FAKE_DOM = """
class Element {
 constructor(tag,css,text){this.tag=tag;this.children=[];this.textContent=text||'';this.className=css||'';}
 appendChild(child){this.children.push(child);return child;}
 addEventListener(){} getAttribute(key){return this[key];}
}
const document={createElement:tag=>new Element(tag),createTextNode:text=>new Element('#text','',text)};
const node=(tag,css,text)=>new Element(tag,css,text);const text=(element,value)=>element.textContent=value;
const bytesLabel=size=>String(size);const toast=()=>{};
const UUID_PATTERN='[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}';
const UUID_RE=new RegExp('^'+UUID_PATTERN+'$');const mediaState={metadata:new Map()};
const identifier='12345678-abcd-1234-abcd-123456789abc';
const path=API+'/attachments/'+identifier+'/content';
const rawAudio={id:identifier,name:'会议.wav',kind:'audio',mediaType:'audio',mimeType:'audio/wav',bytes:32044,contentPath:path};
function all(element){return [element,...element.children.flatMap(all)];}
"""


class KnowledgeAudioTests(unittest.TestCase):
    def setUp(self):
        self.source = asset_path('knowledge.html').read_text()

    def functions(self, *names, source=None):
        value = self.source if source is None else source
        return '\n'.join(re.search(r'function ' + name + r'\([\s\S]*?\n\}', value).group()
                         for name in names)

    def node(self, program):
        if not shutil.which('node'):
            self.skipTest('Node is needed for browser function verification')
        result = subprocess.run(['node'], input=program, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_delayed_waveform_does_not_replace_restored_completed_job_status(self):
        self.node("""
const fields={},$=id=>fields[id]||(fields[id]={hidden:true,value:'',textContent:''});
const text=(element,value)=>element.textContent=value;
const audioEditState={epoch:0,wave:{gl:true,upload(){},draw(){}},spectrum:{upload(){},draw(){}}};
const mediaEditState={kind:null},attachmentMetadata=x=>x;
const openModal=()=>{},closeModal=()=>true,insertionTarget=()=>null,audioEditControls=()=>{},audioEditWindow=()=>{},audioEditSchedulePoll=()=>{},videoTime=String;
const api=async()=>({id:'owned',kind:'audio',durationSeconds:1,editable:true});
const audioEditAdoptJob=async()=>{audioEditState.job={state:'completed'};text($('audio-edit-status'),'剪辑副本已保存，原录音保留。');};
const audioEditRequestWaveform=async()=>({id:'owned',peaks:[.1]});
""" + 'async ' + self.functions('openAudioEditor') + """
(async()=>{await openAudioEditor({id:'owned',name:'Synthetic recording',contentPath:'/owned'});
if($('audio-edit-status').textContent!=='剪辑副本已保存，原录音保留。')process.exit(1);
if(!audioEditState.fullPeaks)process.exit(2);})();
""")

    def test_audio_metadata_and_owned_resource_urls_are_validated(self):
        for device in ('mac', 'android'):
            prefix = '/device-api/' + device + '/api/knowledge'
            script = "const API=" + json.dumps(prefix) + ';\n' + FAKE_DOM
            script += self.functions('attachmentMetadata', 'resourceItem')
            script += """
const metadata=attachmentMetadata(rawAudio);if(metadata.mediaType!=='audio'||metadata.contentPath!==path)process.exit(1);
const item=resourceItem({...rawAudio,source:'attachment',downloadPath:path,referenceDocuments:[{id:identifier,kind:'note',title:'own fixture'}]});
if(item.kind!=='audio'||item.referenceDocuments.length!==1)process.exit(2);
let rejected=false;try{attachmentMetadata({...rawAudio,contentPath:'/other-device/file'});}catch{rejected=true;}
if(!rejected)process.exit(3);
"""
            self.node(script)

    def test_audio_markdown_reference_labels_work_in_each_scoped_editor(self):
        for device in ('mac', 'android'):
            rendered = render_knowledge_ui(device, Path('/tmp/shared'))
            path = '/device-api/' + device + '/api/knowledge/attachments/12345678-abcd-1234-abcd-123456789abc/content'
            self.node("const API=" + json.dumps('/device-api/' + device + '/api/knowledge') + ';\n' +
                      FAKE_DOM + self.functions('attachmentReferences', source=rendered) + '\n' +
                      'const refs=attachmentReferences(' + json.dumps('[音频：会议.wav](' + path + ')\n[录音](' + path + ')') + ");\n" +
                      "if(refs.length!==2||refs.some(item=>item.mediaType!=='audio')||refs[0].name!=='会议.wav')process.exit(1);\n")

    def test_audio_uses_players_and_has_no_image_video_edit_buttons(self):
        self.node("const API='/api/knowledge';\n" + FAKE_DOM +
                  "const openMediaEditor=()=>{throw new Error('should not edit audio')};\n" +
                  self.functions('attachmentMetadata', 'createAttachmentCard', 'resourceMedia') + """
const metadata=attachmentMetadata(rawAudio),card=createAttachmentCard(metadata);
if(!all(card).some(e=>e.tag==='audio'&&e.controls))process.exit(1);
if(all(card).some(e=>e.tag==='video'||e.tag==='img'||['编辑图片','剪辑视频'].includes(e.textContent)))process.exit(2);
const resource=resourceMedia({kind:'audio',name:'fixture',contentPath:path},false);
if(resource.tag!=='audio'||!resource.controls||resource.preload!=='none')process.exit(3);
for(const kind of ['image','video']){
 const card=createAttachmentCard({...metadata,mediaType:kind});
 if(!all(card).some(e=>e.tag==='button'&&e.textContent===(kind==='image'?'编辑图片':'剪辑视频')))process.exit(4);
}
""")

    def test_audio_resource_actions_keep_download_insert_rename_and_delete(self):
        self.node("const API='/api/knowledge';\n" + FAKE_DOM + """
const state={document:{kind:'memory'}};const $=()=>({hidden:false});
const resourceDownloadLink=()=>node('a','','下载');const resourceAction=label=>node('button','',label);
const resourceProtection=()=>'';
""" + self.functions('resourceActions') + """
const actions=resourceActions({...rawAudio,source:'attachment'},false),labels=all(actions).map(e=>e.textContent);
for(const needed of ['下载','插入正文','重命名','删除'])if(!labels.includes(needed))process.exit(1);
if(labels.includes('编辑图片')||labels.includes('剪辑视频'))process.exit(2);
const previewActions=resourceActions({...rawAudio,source:'file',kind:'audio'},true),previewLabels=all(previewActions).map(e=>e.textContent);
for(const needed of ['下载','保存到附件','导入并插入'])if(!previewLabels.includes(needed))process.exit(3);
if(previewLabels.includes('编辑图片')||previewLabels.includes('剪辑视频'))process.exit(4);
""")

    def test_standard_and_custom_audio_links_render_audio_in_markdown(self):
        vendor = json.dumps(str(asset_path('vendor/markdown-it.min.js')))
        self.node("const API='/api/knowledge';\n" + FAKE_DOM + """
const metadata={...rawAudio,mediaType:'audio'};
const ownedAttachment=href=>href===path?{id:identifier,metadata}:null;
const queuePreviewMetadata=()=>{throw new Error('known fixture metadata')};
const safePreviewLink=()=>({href:path,external:false});
""" + self.functions('previewMedia', 'inlineLabel', 'renderInlineTokens') +
                  '\nconst parser=require(' + vendor + ')({html:false});\n' + """
for(const label of ['录音','音频：会议','自定义标签']){
 const parsed=parser.parse('['+label+']('+path+')',{}),root=node('div');
 renderInlineTokens(parsed.find(t=>t.type==='inline').children,root);
 if(!all(root).some(e=>e.tag==='audio'&&e.controls)||all(root).some(e=>e.tag==='video'))process.exit(1);
}
""")


if __name__ == '__main__':
    unittest.main()
