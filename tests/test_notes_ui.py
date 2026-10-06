"""Focused shared Notes UI contracts and draft/media isolation checks."""
import json
from html.parser import HTMLParser
from pathlib import Path
import re
import shutil
import subprocess
import unittest

try:
    from desktop.web_assets import asset_path, render_notes_ui
except ImportError:
    from web_assets import asset_path, render_notes_ui


class Page(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.ids = set()
        self.scripts = []
        self.inline = None
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        value = dict(attrs)
        if 'id' in value:
            assert value['id'] not in self.ids
            self.ids.add(value['id'])
        if tag == 'script' and 'src' not in value:
            self.inline = []

    def handle_data(self, value):
        if self.inline is not None:
            self.inline.append(value)

    def handle_endtag(self, tag):
        if tag == 'script' and self.inline is not None:
            self.scripts.append(''.join(self.inline))
            self.inline = None


class NotesUiTests(unittest.TestCase):
    def setUp(self):
        self.source = asset_path('notes.html').read_text()
        self.script = Page(self.source).scripts[0]

    def function(self, name):
        # Shared UI functions intentionally occupy one logical line.
        return re.search(r'(?:async )?function ' + name + r'\([^\n]*', self.script).group()

    def node(self, program):
        if not shutil.which('node'):
            self.skipTest('Node is needed for browser function verification')
        result = subprocess.run(['node'], input=program, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_each_device_scopes_notes_workflows_vendor_and_media(self):
        for device in ('mac', 'android'):
            rendered = render_notes_ui(device)
            self.assertIn("const ORIGIN='" + device + "';", rendered)
            self.assertIn("const API='/device-api/" + device + "/api/knowledge';", rendered)
            self.assertIn("const WORKFLOWS='/device-api/" + device + "/api/workflows';", rendered)
            self.assertIn('/device-api/' + device + '/api/knowledge/assets/markdown-it.min.js', rendered)
            self.assertNotRegex(rendered, r'192\.168\.\d+\.\d+')
            self.assertEqual(Page(rendered).ids, Page(self.source).ids)
        with self.assertRaises(ValueError):
            render_notes_ui('../android')

    def test_device_scripts_parse_and_all_event_targets_exist(self):
        for source in (self.source, render_notes_ui('mac'), render_notes_ui('android')):
            page = Page(source)
            for script in page.scripts:
                for target in re.findall(r"\$\('([^']+)'\)", script):
                    self.assertIn(target, page.ids)
                if shutil.which('node'):
                    result = subprocess.run(['node', '--check'], input=script, text=True, capture_output=True)
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_keys_are_preserved_when_input_empty_and_explicitly_clearable(self):
        fields = re.search(r'const configFields=[^\n]+', self.script).group()
        self.node("""
const ORIGIN='mac';
const values={};const $=id=>values[id]||(values[id]={value:'',checked:false});
""" + fields + '\n' + self.function('configBody') + """
let body=configBody();
if('asrApiKey' in body || 'deepseekApiKey' in body)process.exit(1);
$('setting-asr-key').value='synthetic-key';
body=configBody();if(body.asrApiKey!=='synthetic-key')process.exit(2);
$('clear-asr-key').checked=true;
body=configBody();if(body.asrApiKey!=='')process.exit(3);
""")

    def test_phone_config_only_sends_phone_owned_fields(self):
        fields = re.search(r'const configFields=[^\n]+', self.script).group()
        self.node("const ORIGIN='android';const $=()=>({value:'',checked:false});\n" + fields + '\n' + self.function('configBody') + """
const value=configBody();
if('asrBackend' in value||'asrApiKey' in value||'scriptsDirectory' in value)process.exit(1);
if(!('desktopUrl' in value)||!('deepseekModel' in value))process.exit(2);
""")

    def test_recording_import_never_inserts_into_a_different_note(self):
        self.node("""
const state={note:{id:'note-b'},noteKey:2};
const toast=()=>{};const $=()=>{throw new Error('another draft was touched');};
""" + self.function('insertAudio') + """
if(insertAudio([{name:'owned fixture'}],1)!==false)process.exit(1);
""")

    def test_audio_buttons_submit_plain_or_summarized_transcription(self):
        for origin in ('mac', 'android'):
            self.node("const ORIGIN=" + json.dumps(origin) + ";\n" + """
const sent=[],messages=[],state={audio:[{id:'owned-audio',name:'录音.wav',contentPath:'/owned-audio'}],audioOffset:0,audioHasMore:false,note:{id:'owned-note'},dirty:false,taskSubmitting:false};
class Element{constructor(tag,css,text){this.tag=tag;this.textContent=text||'';this.children=[];this.handlers={};}append(...items){this.children.push(...items);}replaceChildren(...items){this.children=items;}addEventListener(name,fn){this.handlers[name]=fn;}}
const elements={},$=id=>elements[id]||(elements[id]=new Element('div'));
const make=(...args)=>new Element(...args),date=()=>'',size=()=>'',insertAudio=()=>true,showTab=()=>{};
const toast=message=>messages.push(message),workflow=async(method,path,body)=>{sent.push({method,path,body});};
function all(element){return [element,...element.children.flatMap(all)];}
""" + self.function('renderAudio') + '\n' + self.function('createTranscription') + """
(async()=>{
 renderAudio();const buttons=all($('audio-list')).filter(item=>item.tag==='button');
 const plain=buttons.find(item=>item.textContent==='转写文字'),summary=buttons.find(item=>item.textContent==='转写并提炼');
 if(!plain||!summary)process.exit(1);
 await plain.handlers.click();await summary.handlers.click();
 if(sent.length!==2||sent[0].body.summarize!==false||sent[1].body.summarize!==true)process.exit(2);
 if(sent.some(item=>item.method!=='POST'||item.path!=='/tasks'||item.body.type!=='transcribe'||item.body.origin!==ORIGIN||item.body.attachmentId!=='owned-audio'||item.body.noteId!=='owned-note'))process.exit(3);
 if(!messages[0].includes('文字转写')||!messages[1].includes('提炼')||plain.disabled||summary.disabled||state.taskSubmitting)process.exit(4);
})().catch(error=>{console.error(error);process.exit(5);});
""")

    def test_only_the_recording_started_here_can_auto_insert(self):
        self.node("""
const state={recording:{state:'idle'},recordingOwnerId:null,recordingSeen:new Set(),recordTarget:2,recordStartedAt:0};
let inserts=0;const attachment=value=>value;const insertAudio=()=>inserts++;
const loadAudio=()=>{};const renderRecording=()=>{};const toast=()=>{};const message=()=>{};const errorText=()=>'';
""" + self.function('applyRecording') + """
applyRecording({state:'saved',recordingId:'old-recording',attachment:{id:'old-audio'}});
if(inserts!==0||state.recordTarget!==2)process.exit(1);
applyRecording({state:'recording',recordingId:'owned-recording',elapsedMillis:0},true);
applyRecording({state:'saved',recordingId:'owned-recording',attachment:{id:'owned-audio'}});
if(inserts!==1||state.recordTarget!==null)process.exit(2);
""")

    def test_old_status_response_cannot_replace_a_new_recording(self):
        self.node("""
const ORIGIN='android';const state={recordingGeneration:1};let complete,applied=0;
const api=()=>new Promise(resolve=>complete=resolve);const applyRecording=()=>applied++;
const message=()=>{};
""" + self.function('loadRecording') + """
(async()=>{const pending=loadRecording();state.recordingGeneration++;
complete({state:'saved',recordingId:'old-recording'});await pending;
if(applied!==0)process.exit(1);})();
""")

    def test_scoped_audio_references_and_unsafe_preview_links(self):
        identifier = '12345678-abcd-1234-abcd-123456789abc'
        source = Page(render_notes_ui('mac')).scripts[0]
        declarations = re.search(r"const \$=id[^\n]+", source).group()
        refs = re.search(r'function refs\([^\n]+', source).group()
        safe = re.search(r'function safeLink\([^\n]+', source).group()
        content = '[音频：fixture](/device-api/mac/api/knowledge/attachments/' + identifier + '/content)'
        self.node("const API='/device-api/mac/api/knowledge';\n" + declarations + '\n' + refs + '\n' + safe + '\n' +
                  'const matches=refs(' + json.dumps(content) + ');\n' +
                  'if(matches.length!==1||matches[0].id!==' + json.dumps(identifier) + ')process.exit(1);\n' +
                  "if(safeLink('javascript:alert(1)')!==null||safeLink('//evil.example/image')!==null||safeLink('data:text/html,x')!==null)process.exit(2);\n")

    def test_task_chat_defaults_to_plan_and_tool_script_have_exact_fields(self):
        self.node("""
const ORIGIN='android';const values={};const $=id=>values[id]||(values[id]={value:'',checked:false,querySelectorAll(){return[];}});
""" + self.function('taskBody') + """
$('task-type').value='chat';$('chat-message').value='fixture plan';
let body=taskBody();if(body.executeTools!==false||'origin'in body||body.allowedTools.length)process.exit(1);
$('task-type').value='tool';$('task-tool').value='test_tool';$('task-arguments').value='{}';$('task-device').value='android';
body=taskBody();if(body.device!=='android'||'origin'in body)process.exit(2);
$('task-type').value='script';$('task-script').value='fixture.py';$('task-script-args').value='[]';
body=taskBody();if('origin'in body||body.script!=='fixture.py')process.exit(3);
""")


if __name__ == '__main__':
    unittest.main()
