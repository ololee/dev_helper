import json
from html.parser import HTMLParser
from pathlib import Path
import re
import shutil
import subprocess
import unittest

try:
    from desktop.web_assets import KNOWLEDGE_UI, _scope_paths, render_knowledge_ui
except ImportError:
    from web_assets import KNOWLEDGE_UI, _scope_paths, render_knowledge_ui


class Elements(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.by_id = {}
        self.scripts = []
        self.inline = None
        self.feed(source)
        self.close()

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if "id" in attributes:
            assert attributes["id"] not in self.by_id
            self.by_id[attributes["id"]] = (tag, attributes)
        if tag == "script" and "src" not in attributes:
            self.inline = []

    def handle_data(self, data):
        if self.inline is not None:
            self.inline.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.inline is not None:
            self.scripts.append("".join(self.inline))
            self.inline = None


class WebAssetsTests(unittest.TestCase):
    def test_plain_regex_and_double_escaped_paths_are_scoped(self):
        for device in ("mac", "android"):
            for escaping in (0, 1, 2):
                separator = "\\" * escaping + "/"
                for path in ("/api/knowledge/attachments/123/content", "/artifacts/123"):
                    original = path.replace("/", separator)
                    expected = ("/device-api/" + device + path).replace("/", separator)
                    self.assertEqual(_scope_paths(original, device), expected)

    def test_android_keeps_capture_and_all_original_dom_ids(self):
        source = KNOWLEDGE_UI.read_text(encoding="utf-8")
        rendered = render_knowledge_ui("android", Path("/unused"))
        self.assertEqual(set(Elements(source).by_id), set(Elements(rendered).by_id))
        self.assertIn("const API='/device-api/android/api/knowledge';", rendered)
        self.assertIn("'/device-api/android/artifacts/'", rendered)
        self.assertIn('value="/sdcard"', rendered)
        self.assertNotIn("MEDIA_EDITING_SUPPORTED=false", rendered)
        self.assertNotIn("desktop-capability-style", rendered)
        self.assertIn('aria-label="手机屏幕捕获"', rendered)

    def test_mac_uses_shared_directory_and_preserves_event_binding_nodes(self):
        shared = Path("/tmp/DevHelper's \"资料\" <test>&</script>")
        rendered = render_knowledge_ui("mac", shared)
        nodes = Elements(rendered)
        self.assertEqual(nodes.by_id["resource-path"][1]["value"], str(shared.resolve()))
        self.assertIn("const SCREEN_CAPTURE_SUPPORTED=false;", rendered)
        self.assertIn("if(['video','audio'].includes(metadata.mediaType))controls.appendChild(edit)", rendered)
        style = re.search(r'<style id="desktop-capability-style">(.*?)</style>', rendered, re.S).group(1)
        self.assertNotIn("#video-edit-modal", style)
        self.assertNotIn("#video-export-banner", style)
        self.assertIn("button.hidden=true;button.disabled=true;", rendered)
        for identifier in ("capture-screen-insert", "capture-record-start", "image-edit-modal", "video-edit-modal"):
            self.assertIn(identifier, nodes.by_id)
            self.assertIn(identifier, rendered)
        self.assertIn("desktop-capability-style", nodes.by_id)
        self.assertNotIn("手机", rendered)
        self.assertNotIn("/sdcard", rendered)
        self.assertEqual(len(nodes.scripts), 1)
        self.assertIn("\\u003c/script>", nodes.scripts[0])
        self.assertNotIn("</script>", nodes.scripts[0])

    @unittest.skipUnless(shutil.which("node"), "Node is required for JS syntax verification")
    def test_rendered_scripts_parse_and_recognize_scoped_markdown_attachments(self):
        identifier = "12345678-abcd-1234-abcd-123456789abc"
        for device in ("mac", "android"):
            rendered = render_knowledge_ui(device, Path("/tmp/DevHelper's \"资料\" <test>&</script>"))
            script = Elements(rendered).scripts[0]
            syntax = subprocess.run(["node", "--check"], input=script, text=True, capture_output=True)
            self.assertEqual(syntax.returncode, 0, syntax.stderr)
            pattern = re.search(r"const UUID_PATTERN=.*?;", script).group()
            function = re.search(r"function attachmentReferences\(content\)\{[\s\S]*?\n\}", script).group()
            content = "![测试](/device-api/" + device + "/api/knowledge/attachments/" + identifier + "/content)"
            program = pattern + "\n" + function + "\nconst refs=attachmentReferences(" + json.dumps(content) + ");\n"
            program += "if(refs.length!==1||refs[0].id!==" + json.dumps(identifier) + ")process.exit(1);"
            result = subprocess.run(["node"], input=program, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_unknown_device_is_rejected(self):
        with self.assertRaises(ValueError):
            render_knowledge_ui("../android", Path("/tmp"))

    @unittest.skipUnless(shutil.which("node"), "Node is required for JS behavior verification")
    def test_failed_clipboard_response_preserves_unsent_text(self):
        source = (Path(__file__).resolve().parents[1] / "static/index.html").read_text(encoding="utf-8")
        function = re.search(r"function renderClipboard\(data, replaceText=true\)[\s\S]*?\n  \}", source).group()
        program = """
const fields={};
const $=id=>fields[id]||(fields[id]={value:'',textContent:'',replaceChildren(){},append(){}});
const state={clipboardDirty:true,config:{}};
const make=()=>({});
const formatTime=()=>'';
const renderSync=()=>{};
const showError=(id,text)=>$(id).textContent=text;
$('clipboard-text').value='尚未成功发送的草稿';
""" + function + """
renderClipboard({text:'服务缓存的旧文字',error:'手机未连接',source:'manual'});
if($('clipboard-text').value!=='尚未成功发送的草稿'||!state.clipboardDirty)process.exit(1);
if($('clipboard-error').textContent!=='手机未连接')process.exit(2);
renderClipboard({text:'成功发送的文字',source:'manual'});
if($('clipboard-text').value!=='成功发送的文字'||state.clipboardDirty)process.exit(3);
"""
        result = subprocess.run(["node"], input=program, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
