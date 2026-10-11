import { loadRuntime, pickRuntime, RUNTIMES } from './spine-runtime.js';

// Derive the same-device API from this served module, including desktop proxy scope.
export const API_ROOT = new URL(import.meta.url).pathname.replace(/\/assets\/spine\/spine-player\.js$/, '');
export { RUNTIMES };

export function normalizePath(path, base = '') {
  const value = String(path || '');
  if (!value || value.startsWith('/') || value.includes('\\') || /^[a-z][a-z\d+.-]*:/i.test(value) || /[\u0000-\u001f]/.test(value)) {
    throw new Error('资源路径无效。');
  }
  const parts = base ? base.split('/') : [];
  for (const part of value.split('/')) {
    if (!part || part === '.') continue;
    if (part === '..') {
      if (!parts.length) throw new Error('资源路径超出导入目录。');
      parts.pop();
    } else parts.push(part);
  }
  if (!parts.length) throw new Error('资源路径无效。');
  return parts.join('/');
}

export function bundleFileUrl(bundleId, path) {
  if (!/^[\da-f]{8}-[\da-f]{4}-[\da-f]{4}-[\da-f]{4}-[\da-f]{12}$/i.test(String(bundleId))) throw new Error('资源包标识无效。');
  return API_ROOT + '/spine/bundles/' + bundleId + '/files/' + normalizePath(path).split('/').map(encodeURIComponent).join('/');
}

function disposeObjects(objects) {
  for (const object of objects) {
    try { object?.dispose?.(); } catch { /* Release the remaining objects even after an upstream failure. */ }
  }
}

async function readResource(url, binary, signal) {
  const response = await fetch(url, { credentials: 'same-origin', signal });
  if (!response.ok) throw new Error('资源读取失败（HTTP ' + response.status + '）：' + decodeURIComponent(url.split('/files/')[1] || ''));
  return binary ? new Uint8Array(await response.arrayBuffer()) : response.text();
}

async function readImage(url, signal) {
  const response = await fetch(url, { credentials: 'same-origin', signal });
  if (!response.ok) throw new Error('纹理读取失败（HTTP ' + response.status + '）：' + decodeURIComponent(url.split('/files/')[1] || ''));
  const objectUrl = URL.createObjectURL(await response.blob());
  try {
    return await new Promise((resolve, reject) => {
      const image = new Image();
      const finish = (error) => { image.onload = image.onerror = null; signal.removeEventListener('abort', abort); error ? reject(error) : resolve(image); };
      const abort = () => { image.src = ''; finish(new DOMException('Aborted', 'AbortError')); };
      image.onload = () => finish();
      image.onerror = () => finish(new Error('无法解码纹理：' + decodeURIComponent(url.split('/files/')[1] || '')));
      signal.addEventListener('abort', abort, { once: true });
      if (signal.aborted) abort(); else image.src = objectUrl;
    });
  } finally { URL.revokeObjectURL(objectUrl); }
}

export class SpinePlayer {
  constructor(canvas) {
    this.canvas = canvas;
    this.generation = 0;
    this.visible = true;
    this.disposed = false;
    this.raf = 0;
    this.lastFrame = 0;
    this.lastNotify = 0;
    this.zoom = 1;
    this.pan = { x: 0, y: 0 };
    this.pointers = new Map();
    this.textures = [];
    this.state = { initialized: true, ready: false, name: '', runtime: '', animations: [], skins: [], animation: '', skin: '', playing: true, loop: true, speed: 1, time: 0, duration: 0, pma: false, debug: false, tracks: [], error: '' };
    this.listeners = [];
    this.listen(document, 'visibilitychange', () => this.syncVisibility());
    this.listen(window, 'pagehide', () => this.dispose());
    this.listen(canvas, 'webglcontextlost', event => {
      event.preventDefault();
      this.contextLost = true;
      this.stopFrame();
      this.state.error = '画布暂时不可用，正在等待恢复。';
      this.emit();
    });
    this.listen(canvas, 'webglcontextrestored', () => {
      this.contextLost = false;
      if (this.metadata && !this.disposed) this.load(this.metadata);
    });
    this.listen(canvas, 'pointerdown', event => {
      if (event.pointerType === 'mouse' && event.button !== 0) return;
      event.preventDefault();
      canvas.setPointerCapture?.(event.pointerId);
      this.pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    });
    this.listen(canvas, 'pointermove', event => this.movePointer(event));
    const removePointer = event => this.pointers.delete(event.pointerId);
    this.listen(canvas, 'pointerup', removePointer);
    this.listen(canvas, 'pointercancel', removePointer);
    this.listen(canvas, 'lostpointercapture', removePointer);
    this.listen(canvas, 'wheel', event => {
      event.preventDefault();
      const rect = canvas.getBoundingClientRect();
      this.zoomAt(Math.exp(-Math.max(-300, Math.min(300, event.deltaY)) * .002), event.clientX - rect.left, event.clientY - rect.top);
    }, { passive: false });
    this.listen(canvas, 'dblclick', () => this.resetView());
    this.resizeObserver = typeof ResizeObserver === 'function' ? new ResizeObserver(() => this.requestFrame()) : null;
    this.resizeObserver?.observe(canvas);
    queueMicrotask(() => this.emit());
  }

  listen(target, type, callback, options) { target.addEventListener(type, callback, options); this.listeners.push(() => target.removeEventListener(type, callback, options)); }

  emit() {
    if (this.disposed) return;
    const current = this.animationState?.getCurrent(0);
    this.state.animation = current?.animation?.name || '';
    this.state.loop = current ? !!current.loop : this.state.loop;
    this.state.duration = current?.animation?.duration || 0;
    const time = Math.max(0, Number(current?.trackTime) || 0);
    this.state.time = this.state.duration ? (current.loop ? time % this.state.duration : Math.min(time, this.state.duration)) : 0;
    this.state.tracks = (this.animationState?.tracks || []).flatMap((track, index) => track?.animation ? [{ track: index, animation: track.animation.name, loop: !!track.loop, queued: this.queueNames(track.next) }] : []);
    const payload = { ...this.state, animations: this.state.animations.map(item => ({ ...item })), skins: [...this.state.skins], tracks: this.state.tracks.map(item => ({ ...item })) };
    window.dispatchEvent(new CustomEvent('spine-state', { detail: payload }));
    try { window.NativeSpine?.onState?.(JSON.stringify(payload)); } catch { /* The native view may already have left the page. */ }
  }

  queueNames(entry) { const result = []; for (let i = 0; entry && i < 30; i++, entry = entry.next) result.push(entry.animation?.name || ''); return result; }

  async load(metadata) {
    if (this.disposed) return false;
    const generation = ++this.generation;
    this.controller?.abort();
    this.controller = new AbortController();
    const signal = this.controller.signal;
    this.stopFrame();
    this.releaseSkeleton();
    this.state = { ...this.state, ready: false, name: String(metadata?.name || ''), runtime: '', animations: [], skins: [], animation: '', skin: '', time: 0, duration: 0, error: '' };
    this.emit();
    const textures = [];
    let pendingAtlas = null;
    let committed = false;
    try {
      const skeletonPath = normalizePath(metadata?.skeleton);
      const atlasPath = normalizePath(metadata?.atlas);
      const detected = pickRuntime(metadata?.version);
      const choice = metadata?.runtime && metadata.runtime !== 'auto' ? String(metadata.runtime) : detected;
      if (!choice) throw new Error('无法识别骨架版本，请手动选择导出时使用的 Spine 版本。');
      if (detected && choice !== detected) throw new Error(`骨架由 Spine ${metadata.version} 导出，请使用 ${detected} 运行库。`);
      const adapter = await loadRuntime(choice);
      if (generation !== this.generation || this.disposed) return false;
      this.metadata = { ...metadata, skeleton: skeletonPath, atlas: atlasPath };
      this.releaseGL();
      this.rt = adapter;
      this.buildGL();
      const binary = /\.skel(?:\.bytes)?$/i.test(skeletonPath) || metadata.format === 'skel';
      const [atlasText, data] = await Promise.all([
        readResource(bundleFileUrl(metadata.bundleId, atlasPath), false, signal),
        readResource(bundleFileUrl(metadata.bundleId, skeletonPath), binary, signal)
      ]);
      if (generation !== this.generation || this.disposed) return false;
      const pageNames = [];
      if (adapter.id === '3.8') {
        const image = { width: 16, height: 16 };
        pendingAtlas = adapter.newAtlas(atlasText, name => { pageNames.push(name); return adapter.newFakeTexture(image); });
      } else {
        pendingAtlas = adapter.newAtlas(atlasText);
        pageNames.push(...pendingAtlas.pages.map(page => page.name));
      }
      const directory = atlasPath.includes('/') ? atlasPath.slice(0, atlasPath.lastIndexOf('/')) : '';
      // Decode all textures before creating GL objects. A superseded load cannot leave late textures behind.
      const images = await Promise.all(pageNames.map(name => readImage(bundleFileUrl(metadata.bundleId, normalizePath(name, directory)), signal)));
      if (generation !== this.generation || this.disposed) return false;
      for (const image of images) textures.push(adapter.newTexture(this.ctx, image));
      if (adapter.id === '3.8') {
        disposeObjects([pendingAtlas]);
        pendingAtlas = adapter.newAtlas(atlasText, name => textures[pageNames.indexOf(name)]);
      } else pendingAtlas.pages.forEach((page, index) => page.setTexture(textures[index]));
      const skeletonData = adapter.readSkeletonData(pendingAtlas, data, binary, 1);
      // Never let manually selected unknown data silently override a readable incompatible version.
      const parsedRuntime = pickRuntime(skeletonData.version);
      if (parsedRuntime && parsedRuntime !== adapter.id) throw new Error(`骨架版本为 ${skeletonData.version}，请使用 ${parsedRuntime} 运行库。`);
      this.skeleton = adapter.newSkeleton(skeletonData);
      this.animationState = adapter.newAnimationState(skeletonData);
      adapter.setupPose(this.skeleton);
      this.state.animations = (skeletonData.animations || []).map(animation => ({ name: animation.name, duration: Number(animation.duration) || 0 }));
      this.state.skins = (skeletonData.skins || []).map(skin => skin.name);
      const skin = skeletonData.defaultSkin?.name || this.state.skins[0] || '';
      if (skin) { this.skeleton.setSkinByName(skin); adapter.setupPoseSlots(this.skeleton); }
      this.state.skin = skin;
      this.state.pma = /-pma/i.test(atlasPath) || /^\s*pma\s*:\s*true\s*$/im.test(atlasText);
      this.animationState.timeScale = this.state.speed;
      const first = this.state.animations[0]?.name;
      if (first) this.animationState.setAnimation(0, first, this.state.loop);
      this.animationState.apply(this.skeleton);
      adapter.updateWorldTransform(this.skeleton);
      this.bounds = adapter.getBounds(this.skeleton);
      if (!Number.isFinite(this.bounds.size.x) || !Number.isFinite(this.bounds.size.y) || this.bounds.size.x <= 0 || this.bounds.size.y <= 0) {
        this.bounds = { offset: { x: Number(skeletonData.x) || -100, y: Number(skeletonData.y) || -100 }, size: { x: Number(skeletonData.width) || 200, y: Number(skeletonData.height) || 200 } };
      }
      this.atlas = pendingAtlas;
      this.textures = textures;
      committed = true;
      this.state.ready = true;
      this.state.runtime = adapter.id;
      this.resetView();
      this.emit();
      this.requestFrame();
      return true;
    } catch (error) {
      if (generation !== this.generation || this.disposed || error.name === 'AbortError') return false;
      this.controller.abort();
      this.releaseSkeleton();
      this.releaseGL();
      this.state.error = error?.message || String(error);
      this.emit();
      return false;
    } finally {
      if (!committed) { disposeObjects([pendingAtlas]); disposeObjects(textures); }
    }
  }

  buildGL() {
    const rawGL = this.canvas.getContext('webgl2', { alpha: false, premultipliedAlpha: false }) || this.canvas.getContext('webgl', { alpha: false, premultipliedAlpha: false });
    if (!rawGL || this.contextLost) throw new Error('这台设备无法打开 WebGL 画布。');
    // Construct from the GL object so runtime bundles do not attach immortal canvas listeners.
    this.ctx = this.rt.newContext(rawGL);
    this.shader = this.rt.newTexturedShader(this.ctx);
    this.debugShader = this.rt.newColoredShader(this.ctx);
    this.batcher = this.rt.newBatcher(this.ctx);
    this.shapes = this.rt.newShapeRenderer(this.ctx);
    this.renderer = this.rt.newSkeletonRenderer(this.ctx);
    this.debugRenderer = this.rt.newDebugRenderer(this.ctx);
    Object.assign(this.debugRenderer, { drawRegionAttachments: true, drawBoundingBoxes: true, drawMeshHull: true, drawMeshTriangles: true, drawPaths: true });
    this.mvp = this.rt.newMatrix4();
  }

  releaseSkeleton() {
    // Atlas.dispose also disposes page textures; GLTexture.dispose is idempotent across our bundles.
    disposeObjects([this.atlas]);
    disposeObjects(this.textures);
    this.atlas = this.skeleton = this.animationState = this.bounds = null;
    this.textures = [];
    this.state.ready = false;
    this.clearCanvas();
  }

  releaseGL() {
    disposeObjects([this.shader, this.debugShader, this.batcher, this.shapes, this.renderer, this.debugRenderer]);
    if (this.ctx?.restorables) this.ctx.restorables.length = 0;
    this.shader = this.debugShader = this.batcher = this.shapes = this.renderer = this.debugRenderer = this.mvp = this.ctx = null;
  }

  clearCanvas() {
    if (!this.ctx?.gl || this.contextLost) return;
    const gl = this.ctx.gl;
    gl.clearColor(19 / 255, 29 / 255, 43 / 255, 1);
    gl.clear(gl.COLOR_BUFFER_BIT);
  }

  requestFrame() {
    if (!this.raf && !this.disposed && this.visible && !document.hidden && !this.contextLost && this.state.ready) this.raf = requestAnimationFrame(time => this.render(time));
  }
  stopFrame() { if (this.raf) cancelAnimationFrame(this.raf); this.raf = 0; this.lastFrame = 0; }
  syncVisibility() { if (!this.visible || document.hidden) this.stopFrame(); else this.requestFrame(); }
  setVisible(visible) { this.visible = !!visible; this.syncVisibility(); }

  render(time) {
    this.raf = 0;
    if (!this.state.ready || !this.ctx || this.disposed || !this.visible || document.hidden || this.contextLost) return;
    try {
      const delta = this.lastFrame ? Math.min((time - this.lastFrame) / 1000, .1) : 0;
      this.lastFrame = time;
      this.resize();
      this.clearCanvas();
      if (this.state.playing) {
        this.animationState.update(delta);
        this.skeleton.update?.(delta * this.state.speed);
      }
      this.animationState.apply(this.skeleton);
      this.rt.updateWorldTransform(this.skeleton);
      this.shader.bind();
      this.shader.setUniformi(this.rt.samplerUniform, 0);
      this.shader.setUniform4x4f(this.rt.mvpUniform, this.mvp.values);
      this.batcher.begin(this.shader);
      this.renderer.premultipliedAlpha = this.state.pma;
      this.renderer.draw(this.batcher, this.skeleton);
      this.batcher.end();
      this.shader.unbind();
      if (this.state.debug) {
        this.debugShader.bind();
        this.debugShader.setUniform4x4f(this.rt.mvpUniform, this.mvp.values);
        this.debugRenderer.premultipliedAlpha = this.state.pma;
        this.shapes.begin(this.debugShader);
        this.debugRenderer.draw(this.shapes, this.skeleton);
        this.shapes.end();
        this.debugShader.unbind();
      }
      if (time - this.lastNotify > 100) { this.lastNotify = time; this.emit(); }
      this.requestFrame();
    } catch (error) {
      this.state.error = '画布绘制失败：' + (error.message || error);
      this.state.ready = false;
      this.releaseSkeleton();
      this.releaseGL();
      this.emit();
    }
  }

  worldScale() {
    const width = Math.max(1, this.canvas.clientWidth), height = Math.max(1, this.canvas.clientHeight);
    return Math.max((this.bounds?.size.x || 200) / width, (this.bounds?.size.y || 200) / height, .00001) * 1.2 / this.zoom;
  }
  resize() {
    const width = Math.max(1, this.canvas.clientWidth), height = Math.max(1, this.canvas.clientHeight);
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    if (this.canvas.width !== Math.round(width * ratio) || this.canvas.height !== Math.round(height * ratio)) {
      this.canvas.width = Math.round(width * ratio); this.canvas.height = Math.round(height * ratio);
    }
    const scale = this.worldScale(), worldWidth = width * scale, worldHeight = height * scale;
    const centerX = (this.bounds?.offset.x || 0) + (this.bounds?.size.x || 0) / 2 + this.pan.x;
    const centerY = (this.bounds?.offset.y || 0) + (this.bounds?.size.y || 0) / 2 + this.pan.y;
    this.mvp.ortho2d(centerX - worldWidth / 2, centerY - worldHeight / 2, worldWidth, worldHeight);
    this.ctx.gl.viewport(0, 0, this.canvas.width, this.canvas.height);
  }
  resetView() { this.zoom = 1; this.pan = { x: 0, y: 0 }; this.requestFrame(); }
  zoomAt(factor, x, y) {
    const before = this.worldScale();
    this.zoom = Math.min(50, Math.max(.05, this.zoom * factor));
    const after = this.worldScale();
    this.pan.x += (x - this.canvas.clientWidth / 2) * (before - after);
    this.pan.y -= (y - this.canvas.clientHeight / 2) * (before - after);
    this.requestFrame();
  }
  movePointer(event) {
    if (!this.pointers.has(event.pointerId)) return;
    event.preventDefault();
    const before = [...this.pointers.values()];
    this.pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    const after = [...this.pointers.values()];
    const middle = points => ({ x: points.reduce((sum, point) => sum + point.x, 0) / points.length, y: points.reduce((sum, point) => sum + point.y, 0) / points.length });
    const a = middle(before), b = middle(after), scale = this.worldScale();
    this.pan.x -= (b.x - a.x) * scale; this.pan.y += (b.y - a.y) * scale;
    if (before.length === 2) {
      const distance = points => Math.hypot(points[0].x - points[1].x, points[0].y - points[1].y);
      const rect = this.canvas.getBoundingClientRect(), previous = distance(before);
      if (previous > 0) this.zoomAt(distance(after) / previous, b.x - rect.left, b.y - rect.top);
    }
    this.requestFrame();
  }

  control(action) { if (!this.animationState || !this.skeleton) return false; try { action(); this.emit(); this.requestFrame(); return true; } catch (error) { this.state.error = error.message || String(error); this.emit(); return false; } }
  trackIndex(track) { const index = Number(track); if (!Number.isInteger(index) || index < 0 || index > 7) throw new Error('轨道只能选择 0 到 7。'); return index; }
  mixValue(mix) { return Math.max(0, Math.min(10, Number(mix) || 0)); }
  setAnimation(name, track = 0, loop = true, mix = .2) { return this.control(() => { const entry = this.animationState.setAnimation(this.trackIndex(track), String(name), !!loop); entry.mixDuration = this.mixValue(mix); this.state.playing = true; this.state.error = ''; }); }
  queueAnimation(name, track = 0, loop = true, mix = .2, delay = 0) { return this.control(() => { const entry = this.animationState.addAnimation(this.trackIndex(track), String(name), !!loop, Math.max(-10, Math.min(60, Number(delay) || 0))); entry.mixDuration = this.mixValue(mix); this.state.playing = true; }); }
  clearTrack(track = 0, mix = .2) { return this.control(() => this.animationState.setEmptyAnimation(this.trackIndex(track), this.mixValue(mix))); }
  clearTracks() { return this.control(() => { this.animationState.clearTracks(); this.rt.setupPose(this.skeleton); }); }
  setMix(from, to, seconds) { return this.control(() => this.animationState.data.setMix(String(from), String(to), this.mixValue(seconds))); }
  setSkin(name) { return this.control(() => { this.skeleton.setSkinByName(String(name)); this.rt.setupPoseSlots(this.skeleton); this.state.skin = String(name); }); }
  setPlaying(playing) { this.state.playing = !!playing; this.emit(); this.requestFrame(); }
  setLoop(loop) { this.state.loop = !!loop; for (const track of this.animationState?.tracks || []) if (track) track.loop = !!loop; this.emit(); }
  setSpeed(speed) { const value = Number(speed); if (!Number.isFinite(value)) return; this.state.speed = Math.max(0, Math.min(4, value)); if (this.animationState) this.animationState.timeScale = this.state.speed; this.emit(); }
  seek(seconds) { return this.control(() => { const current = this.animationState.getCurrent(0); if (!current) return; const value = Number(seconds); if (!Number.isFinite(value)) return; current.trackTime = Math.max(0, Math.min(current.animation.duration, value)); this.rt.setupPose(this.skeleton); this.animationState.apply(this.skeleton); }); }
  setPma(pma) { this.state.pma = !!pma; this.emit(); this.requestFrame(); }
  setDebug(debug) { this.state.debug = !!debug; this.emit(); this.requestFrame(); }
  unload() {
    if (this.disposed) return;
    this.generation++; this.controller?.abort(); this.stopFrame(); this.releaseSkeleton(); this.releaseGL();
    this.metadata = null;
    this.state = { ...this.state, ready: false, name: '', runtime: '', animations: [], skins: [], animation: '', skin: '', time: 0, duration: 0, error: '' };
    this.emit();
  }
  dispose() {
    if (this.disposed) return;
    this.disposed = true; this.generation++;
    this.controller?.abort(); this.stopFrame(); this.releaseSkeleton(); this.releaseGL();
    this.resizeObserver?.disconnect(); this.listeners.forEach(remove => remove()); this.listeners = [];
    this.pointers.clear(); this.rt = this.metadata = null;
  }
}

export function exposePlayer(canvas) {
  const player = new SpinePlayer(canvas);
  const methods = ['load', 'setAnimation', 'queueAnimation', 'clearTrack', 'clearTracks', 'setMix', 'setSkin', 'setPlaying', 'setLoop', 'setSpeed', 'seek', 'setPma', 'setDebug', 'resetView', 'setVisible', 'dispose'];
  window.DevHelperSpine = Object.fromEntries(methods.map(name => [name, player[name].bind(player)]));
  return player;
}
