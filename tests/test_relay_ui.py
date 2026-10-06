import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest

HTML = Path(__file__).resolve().parents[1] / 'static/index.html'


class RelayUITests(unittest.TestCase):
    def test_connection_and_manual_resource_controls_exist(self):
        source = HTML.read_text()
        for identifier in ('tab-relay', 'page-relay', 'relay-form', 'relay-url', 'relay-code', 'relay-enabled', 'relay-same-lan',
                           'relay-peer', 'relay-transport', 'relay-source', 'relay-file', 'relay-send-file', 'relay-catalog-list',
                           'relay-transfer-list', 'clipboard-transport'):
            self.assertIn('id="' + identifier + '"', source)
        self.assertIn('inputmode="numeric"', source)
        self.assertNotIn('47.88.', source)
        self.assertNotRegex(source, r"relay-file.*addEventListener\(['\"]change")

    def test_catalog_refresh_calls_only_metadata_endpoints(self):
        source = HTML.read_text()
        body = re.search(r'async function loadRelayCatalog\(\)[\s\S]*?(?=\n  async function queueRelayTransfer)', source).group()
        for endpoint in ('/api/relay/local-catalog', '/api/relay/catalog?deviceId='):
            self.assertIn(endpoint, body)
        self.assertNotIn('/content', body)
        self.assertNotIn('/api/relay/transfer', body)
        self.assertIn("addEventListener('click',()=>queueRelayTransfer", body)

    @unittest.skipUnless(shutil.which('node'), 'Node verifies browser behavior')
    def test_javascript_syntax_and_acceptance_never_means_completed(self):
        source = HTML.read_text()
        script = re.search(r'<script>([\s\S]*?)</script>', source).group(1)
        result = subprocess.run(['node', '--check'], input=script, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        function = re.search(r'function relayOutcome\(value\)\{[^\n]+', script).group()
        program = function + "\n" + """
for(const value of [{state:'pending',succeeded:false},{state:'running',succeeded:false},{state:'completed',succeeded:false},{state:'delivery_unknown',succeeded:false}]){
 if(relayOutcome(value)==='已完成')process.exit(1);
}
if(relayOutcome({state:'completed',succeeded:true})!=='已完成')process.exit(2);
"""
        result = subprocess.run(['node'], input=program, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which('node'), 'Node verifies browser behavior')
    def test_manual_queue_posts_exact_identity_and_does_not_display_success(self):
        source = HTML.read_text()
        outcome = re.search(r'function relayOutcome\(value\)\{[^\n]+', source).group()
        queue = re.search(r'async function queueRelayTransfer\(fields\)\{[^\n]+', source).group()
        program = outcome + "\n" + queue + "\n" + """
const $=id=>({value:id==='relay-peer'?'11111111-1111-4111-8111-111111111111':'relay'});
const calls=[],messages=[];
const showError=()=>{};const loadRelay=async()=>{};const showToast=text=>messages.push(text);
const api=async(path,body)=>{calls.push({path,body});return {id:'22222222-2222-4222-8222-222222222222',state:'pending',succeeded:false};};
(async()=>{
 await queueRelayTransfer({kind:'attachment',id:'33333333-3333-4333-8333-333333333333',source:'android',target:'mac'});
 if(calls.length!==2||calls[0].path!=='/api/relay/config'||calls[1].path!=='/api/relay/transfer')process.exit(1);
 if(calls[1].body.transport!=='relay'||calls[1].body.id!=='33333333-3333-4333-8333-333333333333')process.exit(2);
 if(messages.some(text=>text.includes('已完成')))process.exit(3);
})().catch(()=>process.exit(4));
"""
        result = subprocess.run(['node'], input=program, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which('node'), 'Node verifies browser behavior')
    def test_six_digit_pairing_waits_for_phone_without_workspace_or_receipt(self):
        source = HTML.read_text()
        self.assertNotIn('relay-create-code', source)
        self.assertNotIn('fields.workspaceId', source)
        self.assertNotIn('localStorage', source)
        outcome = re.search(r'function pairingOutcome\(value\)\{[^\n]+', source).group()
        pair = re.search(r'async function relayPair\(\)\{[^\n]+', source).group()
        expiry = re.search(r'function pairingExpiry\(value\)\{[^\n]+', source).group()
        program = outcome + "\n" + expiry + "\n" + pair + "\n" + """
let relayBusy=false;
const fields={'relay-code':{value:'123456'},'relay-url':{value:'https://example.invalid'},'relay-name':{value:'Test Mac'},'relay-pair':{},'relay-pair-status':{}};
const $=id=>fields[id];const calls=[];const errors=[];
const showError=(id,text)=>errors.push(text);const loadRelay=async()=>{};
const api=async(path,body)=>{calls.push({path,body});return {id:'owned-request',state:'pendingApproval',succeeded:false};};
(async()=>{
 await relayPair();
 if(calls.length!==1||calls[0].path!=='/api/relay/pair')process.exit(1);
 if(calls[0].body.code!=='123456'||'workspaceId' in calls[0].body||'receiptSecret' in calls[0].body)process.exit(2);
 if(!fields['relay-pair-status'].textContent.includes('手机确认')||fields['relay-code'].value!=='')process.exit(3);
 fields['relay-code'].value='invalid';await relayPair();if(calls.length!==1)process.exit(4);
 if(pairingOutcome({state:'pendingApproval'}).includes('已批准'))process.exit(5);
})().catch(()=>process.exit(6));
"""
        result = subprocess.run(['node'], input=program, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which('node'), 'Node verifies browser behavior')
    def test_no_pair_and_expired_pair_never_claim_request_sent(self):
        source = HTML.read_text()
        outcome = re.search(r'function pairingOutcome\(value\)\{[^\n]+', source).group()
        load = re.search(r'async function loadRelay\(\)\{[^\n]+', source).group()
        expiry = re.search(r'function pairingExpiry\(value\)\{[^\n]+', source).group()
        program = outcome + "\n" + expiry + "\n" + load + "\n" + """
let relayLoading=false,relayApprovedId=null,relayConfigLoaded=false,relayBusy=false;
const elements={};const $=id=>elements[id]||(elements[id]={classList:{toggle:()=>{}}});
let status={pairing:null,config:{enabled:true,workspaceConfigured:true},connected:true};
const formatTime=stamp=>String(stamp);const api=async()=>status;const showError=()=>{};const renderRelayTransfers=()=>{};
const refreshDevices=async()=>{};const relayRefresh=async()=>{};
(async()=>{
 await loadRelay();
 if(!elements['relay-pair-status'].textContent.includes('添加电脑'))process.exit(1);
 if(elements['relay-pair-status'].textContent.includes('请求已发送')||elements['relay-pair-status'].textContent.includes('手机确认'))process.exit(2);
 for(const value of [null,undefined,{}, {state:'waitingNetwork'}, {state:'expired',expiresAt:Date.now()-1000}]){
  if(pairingOutcome(value).includes('请求已发送')||pairingOutcome(value).includes('手机确认'))process.exit(3);
 }
 status.pairing={state:'pendingApproval',expiresAt:Date.now()-60000,localExpiresAt:Date.now()+60000};await loadRelay();
 if(!elements['relay-pair-status'].textContent.includes('手机确认')||elements['relay-pair-status'].textContent.includes('已过期')||!elements['relay-pair-status'].textContent.includes('有效期至'))process.exit(4);
 status.pairing={state:'expired',expiresAt:Date.now()-1000};await loadRelay();
 if(!elements['relay-pair-status'].textContent.includes('已过期'))process.exit(5);
})().catch(error=>{console.error(error);process.exit(6);});
"""
        result = subprocess.run(['node'], input=program, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
