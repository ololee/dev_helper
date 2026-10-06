"""Behavior checks for assistant context, durable task races and draft isolation."""
import re
import unittest
import test_notes_ui as notes_tests


class AiUiTests(unittest.TestCase):
    setUp = notes_tests.NotesUiTests.setUp
    function = notes_tests.NotesUiTests.function
    node = notes_tests.NotesUiTests.node
    def test_typed_payload_only_contains_selected_real_tools(self):
        self.node("""
const ai={history:[{role:'user',content:'先前问题'}],documents:new Set(['doc-a']),images:new Set(),videos:new Set(),tools:new Set(['android:read_note','mac:unknown']),capabilities:{tools:[{device:'android',name:'read_note'},{device:'mac',name:'write_file'}]}};
const values={'ai-action':{value:'summary',selectedOptions:[{textContent:'提炼'}]},'ai-message':{value:'当前草稿'},'ai-title':{value:''},'ai-knowledge':{checked:true},'ai-context-device':{value:'android'},'ai-execute':{checked:false}};const $=id=>values[id];
""" + self.function('aiToolKey') + '\n' + self.function('aiPayload') + """
const body=aiPayload();
if(body.type!=='assist'||body.action!=='summary'||body.text!=='当前草稿'||body.executeTools!==false)process.exit(1);
if(body.allowedTools.length!==1||body.allowedTools[0].name!=='read_note')process.exit(2);
if(body.contextDevice!=='android'||body.documentIds[0]!=='doc-a'||'deepseekApiKey' in body)process.exit(3);
""")

    def test_submission_receipt_does_not_clear_a_question_typed_while_waiting(self):
        self.node("""
const ai={task:null,submitting:false,version:1,history:[],savedTask:'',saveGeneration:0};
const values={};const $=id=>values[id]||(values[id]={value:'',disabled:false});
$('ai-message').value='第一条问题';
function aiPayload(){return {type:'chat',message:$('ai-message').value,title:'第一条',history:[]};}
const aiActive=()=>false;function message(){}function aiHistory(){}function renderAiTask(){}async function pollAiTask(){}
let resolve;const workflow=()=>new Promise(done=>{resolve=done;});
""" + self.function('submitAi') + """
(async()=>{const result=submitAi({preventDefault(){}});$('ai-message').value='下一条问题';++ai.version;resolve({id:'task-a',status:'pending'});await result;
if($('ai-message').value!=='下一条问题')process.exit(1);
if(ai.history[0].content!=='第一条问题'||ai.task.id!=='task-a')process.exit(2);
})().catch(()=>process.exit(3));
""")

    def test_an_old_poll_response_cannot_replace_a_different_task(self):
        self.node("""
const ai={task:{id:'task-a'},polling:false};let resolve;const workflow=()=>new Promise(done=>resolve=done);function message(){}function renderAiTask(){throw Error('stale result rendered');}
""" + self.function('pollAiTask') + """
(async()=>{const request=pollAiTask();ai.task={id:'task-b'};resolve({id:'task-a',status:'succeeded'});await request;if(ai.task.id!=='task-b'||ai.polling)process.exit(1);})().catch(()=>process.exit(2));
""")

    def test_save_result_creates_a_new_document_without_modifying_original_editor(self):
        self.node("""
const ai={task:{id:'task-a',result:{content:'生成内容'}},saveGeneration:1,savedTask:''};
const original={id:'original',revision:7,content:'未保存原文'};const state={note:original,dirty:true};
const values={'ai-save-title':{value:'新记忆'},'ai-save-kind':{value:'memory'},'ai-save':{disabled:false}};const $=id=>values[id];
let captured;async function api(method,path,body){captured={method,path,body};return {id:'new-id'};}function toast(){}function message(){}function loadNotes(){}function renderAiTask(){}
""" + self.function('saveAiDraft') + """
(async()=>{await saveAiDraft({preventDefault(){}});if('id' in captured.body||'expectedRevision' in captured.body||captured.body.autoLoad!==false)process.exit(1);if(state.note!==original||!state.dirty||original.content!=='未保存原文')process.exit(2);if(captured.body.content!=='生成内容'||captured.body.kind!=='memory')process.exit(3);})().catch(()=>process.exit(4));
""")

    def test_note_shortcut_uses_unsaved_text_without_saving_or_overwriting(self):
        self.node("""
const state={note:{id:'old-note'},dirty:true};const values={'note-content':{value:'尚未保存的正文'},'note-title':{value:'编辑中的标题'}};const $=id=>values[id];let received;function startAi(value){received=value;}function toast(){throw Error('unexpected');}
""" + self.function('summarizeNote') + """
summarizeNote();if(received.text!=='尚未保存的正文'||received.title!=='编辑中的标题'||!state.dirty)process.exit(1);
""")
