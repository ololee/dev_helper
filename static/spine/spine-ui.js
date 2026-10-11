import { exposePlayer, API_ROOT } from './spine-player.js';
import { pickRuntime } from './spine-runtime.js';
import { makeStoredZip } from './zip-store.js';

const $ = id => document.getElementById(id);
const ui = { bundles: [], bundle: null, skeleton: null, importBusy: false, listRequest: 0, selectionRequest: 0, existingRequest: 0, existingMode: 'files', existingOffset: 0, existingPath: '', existingParent: null, existingHasMore: false, dragging: false, latest: null, expanded: false };
const player = exposePlayer($('spine-canvas'));
const uuid = /^[\da-f]{8}-[\da-f]{4}-[\da-f]{4}-[\da-f]{4}-[\da-f]{12}$/i;
const importControls = ['choose-zip', 'choose-folder', 'choose-files', 'browse-files', 'browse-attachments', 'refresh'];
if (document.body.dataset.device === 'android') $('directory-path').value = '/sdcard';

function node(tag, className, value) { const element = document.createElement(tag); if (className) element.className = className; if (value !== undefined) element.textContent = String(value); return element; }
function message(text = '', error = false) { $('message').textContent = text; $('message').hidden = !text; $('message').classList.toggle('error', error); }
function note(text = '') { $('selection-note').textContent = text; }
function timeLabel(seconds) { const value = Math.max(0, Number(seconds) || 0); return Math.floor(value / 60) + ':' + (value % 60).toFixed(1).padStart(4, '0'); }
function setOptions(select, options, placeholder, value = '') {
  select.replaceChildren();
  if (placeholder) { const option = node('option', '', placeholder); option.value = ''; select.append(option); }
  for (const item of options) { const option = node('option', '', item.label ?? item.name ?? item); option.value = item.value ?? item.name ?? item; select.append(option); }
  if (options.some(item => String(item.value ?? item.name ?? item) === String(value))) select.value = value;
}

async function api(path, method = 'GET', body, timeoutMs = 45000) {
  const controller = new AbortController(), timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const options = { method, credentials: 'same-origin', headers: { Accept: 'application/json' }, signal: controller.signal };
    if (body !== undefined) { options.body = JSON.stringify(body); options.headers['Content-Type'] = 'application/json'; }
    const response = await fetch(API_ROOT + path, options), raw = await response.text();
    let data; try { data = raw ? JSON.parse(raw) : {}; } catch { throw new Error('服务返回的数据格式不正确。'); }
    if (!response.ok) throw new Error(typeof data.error === 'string' ? data.error : data.error?.message || data.message || '操作未完成（HTTP ' + response.status + '）。');
    return data;
  } catch (error) { if (error.name === 'AbortError') throw new Error('操作超时，请检查服务连接后重试。'); if (error instanceof TypeError) throw new Error('无法连接设备服务，请检查连接后重试。'); throw error; }
  finally { clearTimeout(timer); }
}

function renderBundles() {
  $('bundle-list').replaceChildren();
  $('library-empty').hidden = ui.bundles.length > 0;
  $('bundle-count').textContent = String(ui.bundles.length);
  for (const bundle of ui.bundles) {
    const row = node('div', 'bundle' + (ui.bundle?.id === bundle.id ? ' active' : ''));
    const choose = node('button'); choose.type = 'button';
    choose.append(node('strong', '', bundle.name || 'Spine 资源包'), node('small', '', (bundle.skeletons || []).length + ' 个骨架'));
    choose.addEventListener('click', () => selectBundle(bundle));
    const remove = node('button', 'delete-bundle', '×'); remove.type = 'button'; remove.setAttribute('aria-label', '移除资源包：' + (bundle.name || 'Spine 资源包')); remove.title = '移除已导入资源，保留原 ZIP'; remove.disabled = ui.importBusy;
    remove.addEventListener('click', async () => {
      if (!window.confirm('移除「' + (bundle.name || 'Spine 资源包') + '」的已导入资源？原 ZIP 附件会保留。')) return;
      remove.disabled = true;
      try { await api('/spine/bundles/' + bundle.id, 'DELETE'); if (ui.bundle?.id === bundle.id) { ui.bundle = ui.skeleton = null; ui.selectionRequest++; player.unload(); } await refreshBundles(); message('已移除导入资源，原 ZIP 保留。'); }
      catch (error) { message(error.message, true); remove.disabled = false; }
    });
    row.append(choose, remove); $('bundle-list').append(row);
  }
}

async function refreshBundles(preferredId) {
  const request = ++ui.listRequest;
  $('refresh').disabled = true;
  try {
    const data = await api('/spine/bundles');
    if (request !== ui.listRequest) return;
    if (!Array.isArray(data.bundles)) throw new Error('资源包列表格式不正确。');
    ui.bundles = data.bundles.filter(item => item && uuid.test(item.id) && Array.isArray(item.skeletons));
    const selected = ui.bundles.find(item => item.id === (preferredId || ui.bundle?.id));
    if (preferredId || !ui.bundle) selectBundle(selected || ui.bundles[0] || null);
    else if (selected) { ui.bundle = selected; renderBundles(); }
    else selectBundle(null);
  } catch (error) { message(error.message, true); }
  finally { if (request === ui.listRequest) $('refresh').disabled = ui.importBusy; }
}

function selectBundle(bundle) {
  ui.bundle = bundle;
  renderBundles();
  const skeletons = bundle?.skeletons || [];
  setOptions($('skeleton-select'), skeletons.map(item => ({ value: item.skeleton, label: item.name || item.skeleton })), skeletons.length ? '' : '资源包没有骨架');
  $('skeleton-select').disabled = !skeletons.length;
  selectSkeleton(skeletons[0] || null);
}

function selectSkeleton(skeleton) {
  ui.skeleton = skeleton;
  ui.selectionRequest++;
  player.unload();
  $('runtime-select').value = 'auto';
  $('atlas-field').hidden = true;
  $('runtime-select').disabled = false;
  note('');
  if (!skeleton || !ui.bundle) { $('asset-name').textContent = '动画预览'; $('asset-version').textContent = '等待选择资源'; return; }
  $('asset-name').textContent = skeleton.name || skeleton.skeleton;
  $('asset-version').textContent = skeleton.version ? 'Spine ' + skeleton.version : '版本待选择';
  const candidates = Array.isArray(skeleton.atlasCandidates) ? skeleton.atlasCandidates.filter(item => item?.atlas) : [];
  if (skeleton.atlas && !candidates.some(item => item.atlas === skeleton.atlas)) candidates.unshift({ atlas: skeleton.atlas, atlasName: skeleton.atlasName });
  setOptions($('atlas-select'), candidates.map(item => ({ value: item.atlas, label: item.atlas })), '请选择匹配的 .atlas', skeleton.atlas || '');
  $('atlas-field').hidden = candidates.length <= 1 && !!skeleton.atlas;
  try {
    const runtime = pickRuntime(skeleton.version);
    $('runtime-select').disabled = !!runtime;
    if (!runtime) note('未能识别骨架版本，请选择导出时使用的 Spine 版本。');
    if (!skeleton.atlas) note(skeleton.issue || '请选择与这个骨架配套的图集。');
    loadSelection();
  } catch (error) { $('runtime-select').disabled = true; note(error.message); message(error.message, true); }
}

async function loadSelection() {
  const skeleton = ui.skeleton, bundle = ui.bundle;
  if (!skeleton || !bundle) return;
  const atlas = $('atlas-select').value;
  if (!atlas) { note(skeleton.issue || '请选择与这个骨架配套的图集。'); return; }
  let detected;
  try { detected = pickRuntime(skeleton.version); } catch (error) { note(error.message); return; }
  if (!detected && $('runtime-select').value === 'auto') { note('未能识别骨架版本，请选择导出时使用的 Spine 版本。'); return; }
  const request = ++ui.selectionRequest;
  $('canvas-loading').hidden = false;
  message(''); note('');
  const loaded = await player.load({ bundleId: bundle.id, skeleton: skeleton.skeleton, atlas, version: skeleton.version || '', runtime: detected || $('runtime-select').value, name: skeleton.name || skeleton.skeleton, format: skeleton.format });
  if (request !== ui.selectionRequest) return;
  $('canvas-loading').hidden = true;
  if (loaded) { $('asset-version').textContent = 'Spine ' + (skeleton.version || player.state.runtime); note(''); }
}

window.addEventListener('spine-state', event => {
  const state = event.detail; ui.latest = state;
  $('canvas-empty').hidden = state.ready;
  $('play').disabled = !state.ready; $('play').textContent = state.playing ? '暂停' : '播放';
  $('timeline').disabled = !state.ready || !state.duration;
  $('loop').checked = state.loop; $('pma').checked = state.pma; $('debug').checked = state.debug;
  $('speed-select').value = String(state.speed);
  const animationKey = JSON.stringify(state.animations.map(item => item.name));
  if ($('animation-select').dataset.options !== animationKey) {
    setOptions($('animation-select'), state.animations, state.animations.length ? '' : '暂无动画', state.animation);
    $('animation-select').dataset.options = animationKey;
  }
  $('animation-select').disabled = !state.ready || !state.animations.length;
  const skinsKey = JSON.stringify(state.skins);
  if ($('skin-select').dataset.options !== skinsKey) { setOptions($('skin-select'), state.skins, state.skins.length ? '' : '暂无皮肤', state.skin); $('skin-select').dataset.options = skinsKey; }
  $('skin-select').disabled = !state.ready || !state.skins.length;
  $('skin-select').value = state.skin;
  for (const id of ['apply-animation', 'queue-animation', 'clear-track', 'clear-tracks']) $(id).disabled = !state.ready;
  if (!ui.dragging) { $('timeline').max = String(state.duration || 1); $('timeline').value = String(state.time); $('time-current').textContent = timeLabel(state.time); }
  $('time-duration').textContent = timeLabel(state.duration);
  $('track-list').textContent = state.tracks.length ? state.tracks.map(track => '轨道 ' + track.track + '：' + track.animation + (track.queued.length ? ' → ' + track.queued.join(' → ') : '')).join('；') : '轨道为空';
  if (state.error) { $('canvas-loading').hidden = true; message(state.error, true); note(state.error); }
});

function importBusy(value) {
  ui.importBusy = value;
  importControls.forEach(id => $(id).disabled = value);
  $('existing-list').querySelectorAll('button').forEach(button => button.disabled = value);
  $('bundle-list').querySelectorAll('.delete-bundle').forEach(button => button.disabled = value);
}

async function importAttachment(attachmentId, name) {
  if (!uuid.test(String(attachmentId))) throw new Error('ZIP 附件标识不正确。');
  message('正在整理资源包…');
  const data = await api('/spine/import', 'POST', { attachmentId, name: (name || 'Spine资源').replace(/\.zip$/i, '') }, 180000);
  const bundle = data.bundle || data;
  if (!uuid.test(bundle.id)) throw new Error('导入结果缺少资源包标识。');
  await refreshBundles(bundle.id);
  if (!player.state.error) message('资源包已导入。');
}

async function uploadZip(file) {
  if (!/\.zip$/i.test(file.name)) throw new Error('请选择 ZIP 文件。');
  const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 180000);
  try {
    message('正在上传 ' + file.name + '…');
    const response = await fetch(API_ROOT + '/attachments/upload?name=' + encodeURIComponent(file.name), { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/zip', Accept: 'application/json' }, body: file, signal: controller.signal });
    const raw = await response.text(); let data; try { data = JSON.parse(raw); } catch { throw new Error('上传结果格式不正确。'); }
    if (!response.ok) throw new Error(typeof data.error === 'string' ? data.error : data.error?.message || 'ZIP 上传失败（HTTP ' + response.status + '）。');
    const attachment = data.attachment || data;
    await importAttachment(attachment.id, file.name);
  } catch (error) { if (error.name === 'AbortError') throw new Error('ZIP 上传超时，请检查附件库后重试。'); throw error; }
  finally { clearTimeout(timeout); }
}

async function importFiles(files, packed) {
  if (ui.importBusy || !files.length) return;
  importBusy(true);
  try {
    const file = packed ? await makeStoredZip(files, (index, count, name) => message('正在打包 ' + index + ' / ' + count + '：' + name)) : files[0];
    await uploadZip(file);
  } catch (error) { message(error.message || String(error), true); }
  finally { importBusy(false); }
}

async function browseExisting(mode = ui.existingMode, offset = 0, path = ui.existingPath) {
  if (ui.importBusy) return;
  const request = ++ui.existingRequest;
  ui.existingMode = mode; ui.existingOffset = offset;
  $('directory-form').hidden = mode !== 'files';
  $('existing-status').textContent = '正在读取…';
  $('existing-list').replaceChildren();
  try {
    const data = mode === 'files'
      ? await api('/resources/browse?' + new URLSearchParams({ path, offset: String(offset), limit: '40' }))
      : await api('/attachments?' + new URLSearchParams({ offset: String(offset), limit: '40' }));
    if (request !== ui.existingRequest) return;
    const rows = mode === 'files' ? data.entries : data.attachments;
    if (!Array.isArray(rows)) throw new Error('文件列表格式不正确。');
    if (mode === 'files') { ui.existingPath = data.path || path; ui.existingParent = data.parentPath ?? null; $('directory-path').value = ui.existingPath; }
    ui.existingHasMore = data.hasMore === true || data.hasMore !== false && offset + rows.length < Number(data.total || 0);
    const allowed = rows.filter(item => item && (item.kind === 'directory' || /\.zip$/i.test(item.name || item.fileName || '')));
    for (const item of allowed) {
      const row = node('div', 'existing-row'), directory = item.kind === 'directory';
      row.append(node('span', '', (directory ? '▸ ' : '') + (item.name || item.fileName || 'ZIP')));
      const choose = node('button', '', directory ? '打开' : '导入'); choose.type = 'button';
      choose.addEventListener('click', async () => {
        if (directory) { await browseExisting('files', 0, item.path); return; }
        if (ui.importBusy) return;
        importBusy(true);
        try {
          const imported = mode === 'files' ? await api('/resources/import-file', 'POST', { path: item.path }, 180000) : item;
          const attachment = imported.attachment || imported;
          await importAttachment(attachment.id, item.name || item.fileName);
        } catch (error) { message(error.message, true); }
        finally { importBusy(false); }
      });
      row.append(choose); $('existing-list').append(row);
    }
    $('existing-status').textContent = allowed.length ? '选择 ZIP 后会导入资源，原 ZIP 保留。' : '这一页没有 ZIP 文件或子目录。';
    $('existing-parent').hidden = mode !== 'files' || ui.existingParent === null;
    $('existing-prev').hidden = offset === 0; $('existing-next').hidden = !ui.existingHasMore;
  } catch (error) { if (request === ui.existingRequest) $('existing-status').textContent = error.message; }
}

function notifyExpansion(open) { if (window.parent !== window) window.parent.postMessage({ type: 'devhelper.spine-workspace', open }, window.location.origin); }
async function toggleFullscreen() {
  const viewer = $('viewer');
  if (document.fullscreenElement) { await document.exitFullscreen(); return; }
  if (ui.expanded) { ui.expanded = false; viewer.classList.remove('expanded'); document.body.classList.remove('viewer-open'); notifyExpansion(false); $('fullscreen').textContent = '全屏'; return; }
  try { if (!viewer.requestFullscreen) throw new Error(); await viewer.requestFullscreen(); }
  catch { ui.expanded = true; viewer.classList.add('expanded'); document.body.classList.add('viewer-open'); notifyExpansion(true); $('fullscreen').textContent = '退出全屏'; }
}

document.addEventListener('fullscreenchange', () => $('fullscreen').textContent = document.fullscreenElement ? '退出全屏' : '全屏');
document.addEventListener('keydown', event => { if (event.key === 'Escape' && ui.expanded) toggleFullscreen(); });
window.addEventListener('message', event => { if (event.origin === window.location.origin && event.source === window.parent && event.data?.type === 'devhelper.spine-visibility') player.setVisible(event.data.visible === true); });
window.addEventListener('pagehide', () => notifyExpansion(false));
$('fullscreen').addEventListener('click', () => toggleFullscreen().catch(error => message(error.message, true)));
$('reset-view').addEventListener('click', () => player.resetView());
$('refresh').addEventListener('click', () => refreshBundles());
$('choose-zip').addEventListener('click', () => $('zip-input').click());
$('choose-folder').addEventListener('click', () => $('folder-input').click());
$('choose-files').addEventListener('click', () => $('files-input').click());
for (const id of ['zip-input', 'folder-input', 'files-input']) $(id).addEventListener('change', () => { const files = [...($(id).files || [])]; $(id).value = ''; importFiles(files, id !== 'zip-input'); });
$('skeleton-select').addEventListener('change', () => selectSkeleton(ui.bundle?.skeletons.find(item => item.skeleton === $('skeleton-select').value)));
$('atlas-select').addEventListener('change', () => { player.unload(); loadSelection(); });
$('runtime-select').addEventListener('change', () => { player.unload(); loadSelection(); });
$('animation-select').addEventListener('change', () => player.setAnimation($('animation-select').value, Number($('track-select').value), $('loop').checked, Number($('mix').value)));
$('skin-select').addEventListener('change', () => player.setSkin($('skin-select').value));
$('play').addEventListener('click', () => player.setPlaying(!player.state.playing));
$('loop').addEventListener('change', () => player.setLoop($('loop').checked));
$('pma').addEventListener('change', () => player.setPma($('pma').checked));
$('debug').addEventListener('change', () => player.setDebug($('debug').checked));
$('speed-select').addEventListener('change', () => player.setSpeed(Number($('speed-select').value)));
$('timeline').addEventListener('pointerdown', () => ui.dragging = true);
$('timeline').addEventListener('input', () => { ui.dragging = true; $('time-current').textContent = timeLabel($('timeline').value); player.seek(Number($('timeline').value)); });
const endSeek = () => { ui.dragging = false; player.emit(); };
$('timeline').addEventListener('change', endSeek); $('timeline').addEventListener('pointerup', endSeek); $('timeline').addEventListener('pointercancel', endSeek); $('timeline').addEventListener('blur', endSeek);
$('track-select').addEventListener('change', () => { const track = player.animationState?.getCurrent(Number($('track-select').value)); if (track?.animation) $('animation-select').value = track.animation.name; });
$('apply-animation').addEventListener('click', () => player.setAnimation($('animation-select').value, Number($('track-select').value), $('loop').checked, Number($('mix').value)));
$('queue-animation').addEventListener('click', () => player.queueAnimation($('animation-select').value, Number($('track-select').value), $('loop').checked, Number($('mix').value)));
$('clear-track').addEventListener('click', () => player.clearTrack(Number($('track-select').value), Number($('mix').value)));
$('clear-tracks').addEventListener('click', () => player.clearTracks());
$('browse-files').addEventListener('click', () => browseExisting('files', 0, $('directory-path').value));
$('browse-attachments').addEventListener('click', () => browseExisting('attachments', 0));
$('directory-form').addEventListener('submit', event => { event.preventDefault(); browseExisting('files', 0, $('directory-path').value); });
$('existing-parent').addEventListener('click', () => browseExisting('files', 0, ui.existingParent));
$('existing-prev').addEventListener('click', () => browseExisting(ui.existingMode, Math.max(0, ui.existingOffset - 40)));
$('existing-next').addEventListener('click', () => browseExisting(ui.existingMode, ui.existingOffset + 40));
refreshBundles();
