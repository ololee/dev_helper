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
        self.assertIn('type="password"', source)
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


if __name__ == '__main__':
    unittest.main()
