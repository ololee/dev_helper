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
            self.node("const ORIGIN=" + json.dumps(origin) + ";\nconst API="+json.dumps("/device-api/"+origin+"/api/knowledge")+";\n" + """
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
""" + self.function('recordingValue') + '\n' + self.function('applyRecording') + """
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

    def recording_program(self, origin='mac'):
        """Synthetic permission/MediaRecorder/timers; never opens a microphone."""
        functions=('recordingValue','recordingClock','recordingVisible','clearRecordingTimer',
                   'releaseBrowserRecording','finishRecordingCancellation',
                   'requestRecordingCancellation','cancelRecordingCountdown','tickRecordingSession',
                   'applyRecording','renderRecording','loadRecording','browserRecordingError',
                   'startRecording','stopRecording','handleNativeRecordingResult','recordingPageHidden')
        return 'const ORIGIN='+json.dumps(origin)+";const API='/owned';\n"+r"""
const assert=require('node:assert/strict');
const state={tab:'recordings',note:null,noteKey:1,recording:{state:'idle',elapsedMillis:0},recordBusy:false,recordTarget:null,recordingGeneration:0,recordSession:null,recordStatusPending:false,recordingOwnerId:null,recordingSeen:new Set(),recordStartedAt:0,recorder:null,stream:null,chunks:[],pendingBlob:null,uploading:false};
const elements={},$=id=>elements[id]||(elements[id]={value:'',textContent:'',classList:{toggle(){}},removeAttribute(){}});
let clock=0,nextTimer=1,starts=0,saves=0,stopTracks=0,visibleFrame=true,nativeEnabled=false,permissionResolve;
const timers=new Map(),messages=[],sent=[];
function setTimeout(fn,delay){const id=nextTimer++;timers.set(id,{fn,due:clock+delay});return id;}
function clearTimeout(id){timers.delete(id);}
function advance(ms){const target=clock+ms;for(;;){const next=[...timers].filter(([,t])=>t.due<=target).sort((a,b)=>a[1].due-b[1].due)[0];if(!next)break;clock=next[1].due;timers.delete(next[0]);next[1].fn();}clock=target;}
const performance={now:()=>clock},document={hidden:false},window={isSecureContext:true,frameElement:{getClientRects:()=>visibleFrame?[{}]:[]},DevHelperMotion:{enter(){}}};
const stream=()=>({getTracks:()=>[{stop(){stopTracks++;}}]});
Object.defineProperty(globalThis,'navigator',{value:{mediaDevices:{getUserMedia:()=>new Promise(resolve=>permissionResolve=resolve)}},configurable:true});
class MediaRecorder{static isTypeSupported(){return true;}constructor(stream,options){this.stream=stream;this.mimeType=options?.mimeType||'audio/webm';this.state='inactive';}start(){starts++;this.state='recording';}stop(){this.state='inactive';this.onstop?.();}}
const native=()=>nativeEnabled,message=(id,value)=>messages.push(value),errorText=()=>'',attachment=value=>value,insertAudio=()=>true,toast=()=>{},loadAudio=()=>{};
let api=async(method,path,body)=>{sent.push({method,path,body});return {state:'cancelled'};};
async function saveBrowserRecording(){assert.ok(state.pendingBlob.size>0);saves++;state.pendingBlob=null;applyRecording({state:'saved',attachment:{id:'synthetic-'+saves}},true);}
"""+'\n'.join(self.function(name) for name in functions)+'\n'

    def test_browser_counts_three_seconds_before_start_and_only_saves_real_audio(self):
        self.node(self.recording_program()+"""
(async()=>{const pending=startRecording();assert.equal(state.recording.state,'starting');assert.equal(starts,0);
permissionResolve(stream());await pending;assert.equal(state.recording.state,'countdown');assert.equal($('record-circle').textContent,'3');assert.equal($('record-stop').textContent,'取消倒计时');
advance(999);assert.equal(starts,0);assert.equal($('record-circle').textContent,'3');advance(1);assert.equal($('record-circle').textContent,'2');
advance(1000);assert.equal($('record-circle').textContent,'1');advance(999);assert.equal(starts,0);advance(1);assert.equal(starts,1);assert.equal(state.recording.state,'recording');assert.equal($('record-stop').textContent,'停止并保存');
state.recorder.ondataavailable({data:new Blob(['synthetic pcm'])});await stopRecording();assert.equal(saves,1);assert.equal(stopTracks,1);assert.equal(state.recording.state,'saved');
})().catch(error=>{console.error(error);process.exit(1);});
""")

    def test_browser_countdown_cancel_releases_stream_and_produces_no_attachment(self):
        self.node(self.recording_program()+"""
(async()=>{const pending=startRecording();permissionResolve(stream());await pending;advance(1000);await stopRecording();
assert.equal(state.recording.state,'cancelled');assert.equal(stopTracks,1);assert.equal(state.pendingBlob,null);assert.equal(state.recordSession,null);
advance(10000);assert.equal(starts,0);assert.equal(saves,0);assert.equal(timers.size,0);
})().catch(error=>{console.error(error);process.exit(1);});
""")

    def test_late_permission_after_cancel_cannot_start_or_replace_new_session(self):
        self.node(self.recording_program()+"""
(async()=>{const first=startRecording(),resolveFirst=permissionResolve;cancelRecordingCountdown();
const second=startRecording(),resolveSecond=permissionResolve,newSession=state.recordSession;
resolveFirst(stream());await first;assert.equal(stopTracks,1);assert.equal(state.recordSession,newSession);assert.equal(state.recording.state,'starting');assert.equal(state.recordBusy,true);
resolveSecond(stream());await second;advance(3000);assert.equal(starts,1);assert.equal(saves,0);
})().catch(error=>{console.error(error);process.exit(1);});
""")

    def test_background_hidden_iframe_and_leaving_cancel_pending_browser_start(self):
        for transition in ("document.hidden=true;recordingPageHidden();",
                           "visibleFrame=false;advance(100);",
                           "state.tab='notes';advance(100);",
                           "recordingPageHidden(true);"):
            self.node(self.recording_program()+"""
(async()=>{const pending=startRecording();permissionResolve(stream());await pending;advance(1500);
"""+transition+"""
advance(5000);assert.equal(starts,0);assert.equal(saves,0);assert.equal(stopTracks,1);assert.equal(state.recordSession,null);
})().catch(error=>{console.error(error);process.exit(1);});
""")

    def test_permission_denial_or_empty_recording_does_not_upload(self):
        self.node(self.recording_program()+"""
(async()=>{navigator.mediaDevices.getUserMedia=async()=>{throw Object.assign(Error('denied'),{name:'NotAllowedError'});};await startRecording();assert.equal(state.recording.state,'failed');assert.equal(timers.size,0);assert.equal(starts,0);
navigator.mediaDevices.getUserMedia=async()=>stream();await startRecording();advance(3000);assert.equal(starts,1);await stopRecording();assert.equal(saves,0);assert.equal(state.pendingBlob,null);assert.equal(state.recording.state,'failed');assert.equal(stopTracks,1);
})().catch(error=>{console.error(error);process.exit(1);});
""")

    def test_background_permission_result_and_page_exit_discard_owned_resources(self):
        self.node(self.recording_program()+"""
(async()=>{const pending=startRecording();document.hidden=true;recordingPageHidden();permissionResolve(stream());await pending;
advance(5000);assert.equal(starts,0);assert.equal(stopTracks,1);assert.equal(saves,0);
document.hidden=false;navigator.mediaDevices.getUserMedia=async()=>stream();await startRecording();advance(3000);assert.equal(starts,1);
state.recorder.ondataavailable({data:new Blob(['synthetic'])});recordingPageHidden(true);assert.equal(stopTracks,2);assert.equal(state.pendingBlob,null);assert.equal(state.recordSession,null);assert.equal(saves,0);assert.equal(timers.size,0);
})().catch(error=>{console.error(error);process.exit(1);});
""")

    def test_phone_start_has_one_backend_countdown_and_cancel_is_guarded_by_owner(self):
        self.node(self.recording_program('android')+"""
(async()=>{let resolveStart;api=(method,path,body)=>{sent.push({method,path,body});return path==='/recording/start'?new Promise(resolve=>resolveStart=resolve):Promise.resolve({state:'cancelled',recordingId:'owned'});};
const pending=startRecording();await stopRecording();assert.equal(sent.length,1);resolveStart({state:'countdown',recordingId:'owned',countdownRemaining:3});await pending;
assert.deepEqual(sent[1].body,{recordingId:'owned',cancelCountdown:true});assert.equal(state.recording.state,'cancelled');assert.equal(starts,0);assert.equal(saves,0);
api=async(method,path,body)=>{sent.push({method,path,body});return {state:'countdown',recordingId:'new-owned',countdownRemaining:3};};await startRecording();assert.equal(state.recording.state,'countdown');assert.equal(state.recordSession.deadline,undefined);
applyRecording({state:'recording',recordingId:'new-owned',elapsedMillis:0});assert.equal(state.recording.state,'recording');assert.equal(timers.size,0);
})().catch(error=>{console.error(error);process.exit(1);});
""")

    def test_native_pending_permission_cancel_and_late_start_are_owned(self):
        self.node(self.recording_program('android')+"""
(async()=>{nativeEnabled=true;const cancelled=[];window.NativeKnowledge={startAudioRecording(){},cancelAudioRecordingCountdown(id){cancelled.push(id);},stopAudioRecording(){throw Error('normal stop cannot cancel a countdown');}};
await startRecording();assert.equal(state.recording.state,'starting');cancelRecordingCountdown();assert.deepEqual(cancelled,['']);
handleNativeRecordingResult({status:200,body:{state:'countdown',recordingId:'owned-late',countdownRemaining:3}});assert.deepEqual(cancelled,['','owned-late']);
handleNativeRecordingResult({status:200,body:{state:'cancelled',recordingId:'owned-late'}});assert.equal(state.recordSession,null);assert.equal(state.recordBusy,false);assert.equal(starts,0);assert.equal(saves,0);
})().catch(error=>{console.error(error);process.exit(1);});
""")

    def test_cancellation_race_keeps_started_recording_and_never_cancels_foreign_id(self):
        self.node(self.recording_program('android')+"""
const own={kind:'remote',recordingId:'owned',cancelled:true};state.recordSession=own;state.recordBusy=true;
finishRecordingCancellation(own,{state:'recording',recordingId:'owned',elapsedMillis:10});assert.equal(state.recording.state,'recording');assert.equal(own.cancelled,false);assert.equal(state.recordBusy,false);
own.cancelled=true;finishRecordingCancellation(own,{state:'countdown',recordingId:'foreign',countdownRemaining:3});assert.equal(state.recordSession,null);assert.equal(own.recordingId,'owned');assert.equal(sent.length,0);
""")

    def test_phone_stop_uses_displayed_recording_id_for_http_and_native(self):
        self.node(self.recording_program('android')+"""
(async()=>{const id='11111111-2222-4333-8444-555555555555';state.recording={state:'recording',recordingId:id};
api=async(method,path,body)=>{sent.push({method,path,body});return {state:'saved',recordingId:id};};await stopRecording();
assert.deepEqual(sent[0],{method:'POST',path:'/recording/stop',body:{recordingId:id}});assert.equal(state.recordBusy,false);
nativeEnabled=true;const stopped=[];window.NativeKnowledge={stopAudioRecording(){throw Error('global stop must not be used');},stopAudioRecordingById(value){stopped.push(value);}};
state.recording={state:'recording',recordingId:id};await stopRecording();assert.deepEqual(stopped,[id]);assert.equal(state.recordBusy,true);
handleNativeRecordingResult({status:200,body:{state:'saved',recordingId:id}});assert.equal(state.recordBusy,false);
delete window.NativeKnowledge.stopAudioRecordingById;state.recording={state:'recording',recordingId:id};await stopRecording();assert.deepEqual(sent[1].body,{recordingId:id});assert.equal(state.recordBusy,false);
})().catch(error=>{console.error(error);process.exit(1);});
""")

    def test_phone_stop_without_id_refreshes_and_never_calls_global_stop(self):
        self.node(self.recording_program('android')+"""
(async()=>{state.recording={state:'recording'};api=async(method,path,body)=>{sent.push({method,path,body});return {state:'recording',recordingId:'owned'};};
await stopRecording();assert.equal(sent.length,1);assert.equal(sent[0].method,'GET');assert.equal(sent[0].path,'/recording/status');assert.ok(messages.some(value=>value.includes('录音标识')));
})().catch(error=>{console.error(error);process.exit(1);});
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
