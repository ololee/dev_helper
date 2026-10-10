"""Shared browser motion behavior using an isolated DOM; no device/service changes."""
from pathlib import Path
from contextlib import redirect_stdout
import importlib.util
import io
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

try:
    from desktop.web_assets import asset_path, render_knowledge_ui, render_notes_ui
except ImportError:
    from web_assets import asset_path, render_knowledge_ui, render_notes_ui


DOM = r"""
const assert=require('node:assert/strict'),animations=[],listeners={},observers=[];
class Element {
 constructor(tag='div',id='',css=''){this.nodeType=1;this.tagName=tag.toUpperCase();this.id=id;this.className=css;this.children=[];this.parentElement=null;this.hidden=false;this.attributes={};this.style={};this.dataset={};this.textContent='';this.isConnected=true;this.rect={left:100,top:100,width:300,height:200};this.classList={contains:c=>this.className.split(' ').includes(c),add:c=>this.className+=' '+c};}
 matches(selectors){return selectors.split(',').some(s=>{s=s.trim();if(s==='*')return true;if(s==='[hidden]')return this.hidden;if(s==='[aria-selected]')return this.attributes['aria-selected']!==undefined;if(s.startsWith('[role='))return this.attributes.role===s.slice(6,-1);if(s.startsWith('#'))return this.id===s.slice(1);if(s.startsWith('.'))return this.classList.contains(s.slice(1));return this.tagName===s.toUpperCase();});}
 closest(s){for(let node=this;node;node=node.parentElement)if(node.matches(s))return node;return null;}
 querySelectorAll(s){return this.children.flatMap(c=>[...(c.matches(s)?[c]:[]),...c.querySelectorAll(s)]);}
 querySelector(s){return this.querySelectorAll(s)[0]||null;}
 getAttribute(key){return this.attributes[key]??null;}
 setAttribute(key,value){this.attributes[key]=String(value);if(key==='id')this.id=String(value);}
 removeAttribute(key){delete this.attributes[key];if(key==='id')this.id='';}
 appendChild(c){c.parentElement=this;c.isConnected=true;this.children.push(c);return c;}
 remove(){if(this.parentElement)this.parentElement.children=this.parentElement.children.filter(c=>c!==this);this.parentElement=null;this.isConnected=false;}
 getBoundingClientRect(){return this.rect;}
 cloneNode(deep){const c=new Element(this.tagName,this.id,this.className);c.attributes={...this.attributes};c.textContent=this.textContent;c.style={...this.style};if(deep)for(const child of this.children)c.appendChild(child.cloneNode(true));return c;}
 animate(frames,options){const a={element:this,frames,options,cancelled:false,cancel(){this.cancelled=true;this.oncancel?.();}};animations.push(a);return a;}
}
const body=new Element('body'),preference={matches:false,addEventListener:(name,cb)=>listeners.motion=cb};
const document={body,readyState:'complete',createElement:tag=>new Element(tag),getElementById:id=>[body,...body.querySelectorAll('*')].find(n=>n.id===id)||null,querySelectorAll:s=>body.querySelectorAll(s),querySelector:s=>body.querySelector(s),addEventListener:(name,cb)=>listeners[name]=cb};
const window={document,matchMedia:()=>preference,getComputedStyle:element=>({transform:element.baseTransform||'none'})};
class MutationObserver{constructor(cb){observers.push(cb);}observe(){}}
const group=body.appendChild(new Element('nav','','tabs'));
const first=group.appendChild(new Element('button','tab-first')),second=group.appendChild(new Element('button','tab-second'));
first.setAttribute('aria-selected','true');first.setAttribute('aria-controls','page-first');second.setAttribute('aria-selected','false');second.setAttribute('aria-controls','page-second');
const pageFirst=body.appendChild(new Element('section','page-first')),pageSecond=body.appendChild(new Element('section','page-second'));pageSecond.hidden=true;
const list=pageSecond.appendChild(new Element('div','owned-list','item-list'));
const modal=body.appendChild(new Element('div','owned-modal','modal-backdrop'));modal.hidden=true;
const panel=modal.appendChild(new Element('section','owned-panel','modal'));panel.setAttribute('role','dialog');
const video=panel.appendChild(new Element('video','owned-video'));video.setAttribute('src','/synthetic-source.mp4');
const fullscreen=body.appendChild(new Element('div','owned-editor','modal-backdrop media-workspace'));fullscreen.hidden=true;
const editor=fullscreen.appendChild(new Element('section','owned-canvas-parent','modal media-editor-modal'));editor.appendChild(new Element('canvas'));
const status=body.appendChild(new Element('div','owned-status','badge'));status.textContent='正在导出 20%';
function emit(...records){observers[0](records);}
function attribute(element,key,oldValue){return {type:'attributes',target:element,attributeName:key,oldValue};}
function open(element){element.hidden=false;emit(attribute(element,'hidden',''));}
function close(element){element.hidden=true;emit(attribute(element,'hidden',null));}
function switchTo(next){const old=next===second?first:second;old.setAttribute('aria-selected','false');next.setAttribute('aria-selected','true');pageFirst.hidden=next!==first;pageSecond.hidden=next!==second;emit(attribute(next,'aria-selected','false'));}
"""


class MotionTests(unittest.TestCase):
    def setUp(self):
        self.script = asset_path('motion.js').read_text()

    def node(self, program):
        if not shutil.which('node'):
            self.skipTest('Node is required for interaction verification')
        result = subprocess.run(['node'], input=DOM + '\n' + self.script + '\n' + program,
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_all_pages_use_shared_assets_and_device_paths_are_scoped(self):
        pages = [(Path(__file__).resolve().parents[1] / 'static/index.html').read_text()]
        for device in ('mac', 'android'):
            for source in (render_knowledge_ui(device, Path('/tmp/owned-shared')), render_notes_ui(device)):
                for asset in ('motion.js', 'motion.css'):
                    self.assertIn('/device-api/' + device + '/api/knowledge/assets/' + asset, source)
                pages.append(source)
        for source in pages:
            self.assertRegex(source, r'<script src="[^"]+/motion\.js" defer>')
            self.assertRegex(source, r'<link rel="stylesheet" href="[^"]+/motion\.css">')
        css = asset_path('motion.css').read_text()
        self.assertIn('prefers-reduced-motion:reduce', css)
        self.assertIn('pointer-events:none!important', css)
        self.assertNotRegex(css, r'animation:[^;}]*infinite|will-change:|transition:all')

    def test_route_direction_and_repeated_poll_updates_do_not_restart_motion(self):
        self.node("""
switchTo(second);assert.equal(animations[0].frames[0].transform,'translateX(10px)');
const count=animations.length;emit(attribute(second,'aria-selected','true'));assert.equal(animations.length,count);
switchTo(first);assert.equal(animations.at(-1).frames[0].transform,'translateX(-10px)');
status.textContent='正在导出 80%';emit({type:'childList',target:status});assert.equal(animations.length,count+1);
status.textContent='导出完成';emit({type:'childList',target:status});assert.equal(animations.length,count+2);
emit({type:'childList',target:status});assert.equal(animations.length,count+2);
""")

    def test_async_list_entrance_is_armed_once_instead_of_every_poll(self):
        self.node("""
switchTo(second);for(let i=0;i<10;i++)list.appendChild(new Element('article'));
emit({type:'childList',target:list});assert.equal(animations.length,7);
assert.equal(animations.at(-1).options.delay,80);
list.children=[];for(let i=0;i<10;i++)list.appendChild(new Element('article'));
emit({type:'childList',target:list});assert.equal(animations.length,7);
""")

    def test_modal_uses_trigger_origin_and_exit_is_inert_and_interruptible(self):
        self.node("""
first.rect={left:50,top:50,width:20,height:20};listeners.pointerdown({target:first});open(modal);
assert.equal(panel.style.transformOrigin,'0% 0%');assert.match(animations[0].frames[0].transform,/scale\(.975\)/);
close(modal);assert.equal(modal.hidden,true);
const proxy=body.querySelector('.motion-exit-proxy');assert.ok(proxy.inert);assert.equal(proxy.getAttribute('aria-hidden'),'true');
assert.equal(proxy.style.pointerEvents,'none');assert.equal(proxy.id,'');assert.equal(proxy.querySelector('video'),null);
open(modal);assert.equal(body.querySelector('.motion-exit-proxy'),null);assert.equal(modal.hidden,false);
""")

    def test_fullscreen_media_never_scales_translates_or_clones_the_canvas(self):
        self.node("""
open(fullscreen);assert.deepEqual(animations[0].frames,[{opacity:0},{opacity:1}]);
close(fullscreen);const proxy=body.querySelector('.motion-exit-proxy');assert.equal(proxy.querySelector('canvas'),null);
assert.deepEqual(animations.at(-1).frames,[{opacity:1},{opacity:0}]);
""")

    def test_iframe_fullscreen_expansion_cancels_ancestor_route_transform(self):
        self.node("""
switchTo(second);const routeAnimation=animations[0],host=pageSecond.appendChild(new Element('div','','frame-shell editor-expanded'));
emit(attribute(host,'class','frame-shell'));assert.ok(routeAnimation.cancelled);
""")

    def test_reduced_motion_cancels_inflight_and_future_animations(self):
        self.node("""
open(modal);close(modal);preference.matches=true;listeners.motion();
assert.ok(animations.every(a=>a.cancelled));assert.equal(body.querySelector('.motion-exit-proxy'),null);
const count=animations.length;open(fullscreen);switchTo(second);assert.equal(animations.length,count);
assert.equal(window.DevHelperMotion.scrollBehavior(),'instant');
""")

    def test_accordion_feedback_leaves_layout_and_media_positions_untouched(self):
        self.node("""
const details=body.appendChild(new Element('details')),summary=details.appendChild(new Element('summary')),content=details.appendChild(new Element('div'));
details.open=true;emit(attribute(details,'open',null));assert.equal(animations.length,1);assert.equal(animations[0].element,content);
assert.equal(animations[0].frames[0].transform,'translateY(0px)');
const canvas=editor.children[0];canvas.hidden=false;emit(attribute(canvas,'hidden',''));assert.equal(animations.length,1);
""")

    def test_release_explicitly_packages_both_motion_resources(self):
        path = Path(__file__).resolve().parents[1] / 'scripts/package_release.py'
        spec = importlib.util.spec_from_file_location('motion_release_fixture', path)
        package = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(package)
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            output = Path(directory) / 'standalone'
            package.prepare(output)
            for name in ('motion.js', 'motion.css'):
                self.assertEqual((output / 'static' / name).read_bytes(), asset_path(name).read_bytes())
            # The standalone release must find its own assets without access to
            # the Android development tree or an existing server installation.
            result = subprocess.run([sys.executable, '-c',
                                     "from web_assets import asset_path;assert all(asset_path(n).parent.name=='static' for n in ('motion.js','motion.css'))"],
                                    cwd=output, text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
        with self.assertRaises(ValueError):
            asset_path('../motion.js')


class MotionServerTests(unittest.TestCase):
    def test_assets_are_local_portable_and_read_only(self):
        try:
            import test_server as fixture
        except ImportError:
            from tests import test_server as fixture
        fixture.ServerTests.setUp(self)
        try:
            for prefix in ('/api/knowledge/assets/', '/device-api/mac/api/knowledge/assets/'):
                for name, mime in (('motion.js', 'text/javascript'), ('motion.css', 'text/css')):
                    response = self.client.get(prefix + name)
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertIn(mime, response.headers['content-type'])
                    self.assertEqual(response.text, asset_path(name).read_text())
                    self.assertEqual(self.client.head(prefix + name).status_code, 200)
                    self.assertEqual(self.client.post(prefix + name, json={}).status_code, 405)
            self.assertEqual(self.client.get('/api/knowledge/assets/../private.txt').status_code, 404)
        finally:
            fixture.ServerTests.tearDown(self)
