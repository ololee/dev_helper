export const API_ROOT = new URL(import.meta.url).pathname.replace(/\/assets\/minigames\/minigames-ui\.js$/, '');
const $ = id => document.getElementById(id);
const DEVICE = document.body.dataset.device === 'android' ? 'android' : 'mac';
const MAC_API = '/device-api/mac/api/knowledge';
const MODES = {
  raw: ['原始包', '保留设备缓存中的包和清单，便于在电脑继续处理。'],
  unpacked: ['代码与包内容', '解开支持的包格式，整理脚本与目录。'],
  assets: ['资源文件', '整理图片、音频和 Spine 等可识别资源。'],
  'unity-project': ['Unity 项目', '在电脑使用 AssetRipper 导出可编辑项目。']
};
const FINISHED = new Set(['completed', 'succeeded', 'success', 'failed', 'error', 'cancelled']);
const SUCCESS = new Set(['completed', 'succeeded', 'success']);
const ui = { games: [], selected: null, nameDrafts: new Map(), exports: [], offset: 0, limit: 40, total: 0, hasMore: false, mode: 'raw', listRequest: 0, detailRequest: 0, visible: true, jobs: [], jobViews: new Map(), polling: false, timer: null, busy: false, exportsKey: '', tool: null };
const STORE = 'devhelper.minigames.jobs.v1:' + API_ROOT;
let toastTimer;

function node(tag, className, text) { const element = document.createElement(tag); if (className) element.className = className; if (text !== undefined) element.textContent = String(text); return element; }
function button(label, work, className = '') { const element = node('button', className, label); element.type = 'button'; element.addEventListener('click', () => busy(element, work)); return element; }
function message(text, error = false) { $('message').textContent = text || ''; $('message').classList.toggle('error', error); $('message').hidden = !text; }
function toast(text, error = false) { clearTimeout(toastTimer); $('toast').textContent = text; $('toast').classList.toggle('error', error); $('toast').hidden = false; toastTimer = setTimeout(() => $('toast').hidden = true, error ? 7000 : 4200); }
async function busy(element, work) { if (element.disabled) return; element.disabled = true; try { await work(); } catch (error) { message(error.message, true); } finally { element.disabled = false; } }
function errorText(data, status) { return typeof data?.error === 'string' ? data.error : data?.error?.message || data?.message || '操作未完成（HTTP ' + status + '）。'; }
async function api(path, body, root = API_ROOT, timeoutMs = 45000) {
  const controller = new AbortController(), timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const options = { method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin', headers: { Accept: 'application/json' }, signal: controller.signal };
    if (body !== undefined) { options.headers['Content-Type'] = 'application/json'; options.body = JSON.stringify(body); }
    const response = await fetch(root + path, options), raw = await response.text(); let data = {};
    if (raw) { try { data = JSON.parse(raw); } catch { throw new Error('服务返回的数据格式不正确。'); } }
    if (!response.ok) throw new Error(errorText(data, response.status)); return data;
  } catch (error) {
    if (error.name === 'AbortError') throw new Error('连接超时，请检查当前设备服务，再刷新任务查看结果。');
    if (error instanceof TypeError) throw new Error('无法连接当前设备，请检查网络和 DevHelper 服务。'); throw error;
  } finally { clearTimeout(timer); }
}
function size(value) { const bytes = Number(value); if (!Number.isFinite(bytes) || bytes < 0) return '未知'; if (bytes < 1024) return bytes + ' B'; if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB'; if (bytes < 1073741824) return (bytes / 1048576).toFixed(1) + ' MB'; return (bytes / 1073741824).toFixed(2) + ' GB'; }
function platformName(value) { return ({ douyin: '抖音', wechat: '微信' })[value] || '来源未确认'; }
function gameName(game) { return String(game?.name || '').trim() || game?.appId || game?.cacheLabel || '名称待补充'; }
function gameTypeName(game) { return ({ game: '小游戏', miniprogram: '小程序' })[game?.gameType] || '类型待确认'; }
function engineName(game, compact = false) {
  const value = String(game?.engine || '').toLowerCase(), variant = String(game?.engineVariant || '').toLowerCase();
  if (['unity', 'tuanjie', '团结'].includes(value)) {
    if (['tuanjie', '团结'].includes(variant) || ['tuanjie', '团结'].includes(value)) return '团结引擎';
    if (variant === 'unity') return 'Unity';
    return compact ? 'Unity 系列' : 'Unity / 团结未区分';
  }
  const labels = { cocos: 'Cocos', 'cocos-creator': 'Cocos Creator', cocoscreator: 'Cocos Creator', laya: 'LayaAir', layaair: 'LayaAir', godot: 'Godot', egret: 'Egret', unreal: 'Unreal', native: '原生引擎' };
  return labels[value] || (value && !['unknown', 'unidentified', 'none'].includes(value) ? game.engine : '未识别引擎');
}
function normalizeGame(data) { const game = data?.game || data; if (!game || typeof game.id !== 'string' || !game.id) throw new Error('游戏详情缺少有效 ID。'); return game; }
function normalizeExport(data) { const value = data?.export || data; const id = value?.id || value?.exportId; return id ? { ...value, id: String(id) } : null; }
function resultExport(job) { const result = job?.result; if (!result) return null; return normalizeExport(result.export || (result.exportId ? result : null)); }
function jobProgress(job) { if (job.progress === null || job.progress === undefined) return null; const value = Number(job.progress); return Number.isFinite(value) ? Math.max(0, Math.min(100, value)) : null; }
function active() { return ui.visible && !document.hidden; }
function safeUrl(path) { const url = new URL(path, window.location.href); if (url.origin !== window.location.origin || !['http:', 'https:'].includes(url.protocol)) throw new Error('资源链接不属于当前 DevHelper 服务。'); return url.pathname + url.search + url.hash; }
function archiveUrl(value, root = API_ROOT) { return root + '/minigames/exports/' + encodeURIComponent(value.id) + '/archive'; }

function renderGames() {
  const search = $('search').value.trim().toLowerCase();
  const games = ui.games.filter(game => !search || [game.name, game.appId, game.cacheLabel, game.id].some(value => String(value || '').toLowerCase().includes(search)));
  const list = $('game-list'); list.replaceChildren();
  for (const game of games) {
    const row = button('', () => selectGame(game), 'game-row'); row.setAttribute('aria-pressed', String(ui.selected?.id === game.id));
    row.append(node('strong', '', gameName(game)), node('div', 'identifier', game.appId || 'AppID 待确认'));
    const meta = node('div', 'game-meta'); meta.append(node('span', '', platformName(game.platform)), node('span', '', gameTypeName(game)), node('span', '', size(game.bytes)), node('span', 'engine-label', engineName(game, true))); row.append(meta); list.append(row);
  }
  $('game-count').textContent = (Number.isFinite(ui.total) ? ui.total : ui.games.length) + ' 个';
  $('library-empty').hidden = games.length > 0;
  const empty = $('library-empty'); empty.querySelector('strong').textContent = search ? '当前页没有匹配游戏' : '还没有找到游戏'; empty.querySelector('p').textContent = search ? '试试其他名称或 ID，或翻页查找。' : DEVICE === 'android' ? '先扫描手机缓存。手机需先运行过游戏，才能留下缓存。' : '从电脑选择已有游戏 ZIP，或切换到手机扫描并导出缓存。';
  $('previous').disabled = ui.offset === 0; $('next').disabled = !ui.hasMore; $('page-label').textContent = '第 ' + (Math.floor(ui.offset / ui.limit) + 1) + ' 页';
}
async function refreshGames(preferredId) {
  const request = ++ui.listRequest; $('refresh').disabled = true; $('game-count').textContent = '读取中';
  try {
    const query = new URLSearchParams({ platform: $('platform').value, offset: String(ui.offset), limit: String(ui.limit) });
    const data = await api('/minigames/games?' + query); if (request !== ui.listRequest) return;
    const values = data.games || data.items; if (!Array.isArray(values)) throw new Error('游戏列表格式不正确。');
    ui.games = values.filter(game => game && typeof game.id === 'string'); ui.total = Number.isFinite(Number(data.total)) ? Number(data.total) : ui.games.length;
    ui.hasMore = typeof data.hasMore === 'boolean' ? data.hasMore : ui.offset + ui.games.length < ui.total; renderGames();
    if (DEVICE === 'android' && data.rootAvailable === false) $('scan-hint').textContent = '当前手机 Root 尚未可用，无法读取宿主缓存。开启 Root 后再扫描，或导入已有 ZIP。';
    if (preferredId) await selectGame(ui.games.find(game => game.id === preferredId) || { id: preferredId });
    else if (ui.selected) { const current = ui.games.find(game => game.id === ui.selected.id); if (current) await selectGame(current, true); }
  } catch (error) { if (request === ui.listRequest) { $('game-count').textContent = '读取失败'; message(error.message, true); } }
  finally { if (request === ui.listRequest) $('refresh').disabled = false; }
}
function facts(game) {
  const source = typeof game.source === 'string' ? ({ imported: '导入的游戏包', cache: '设备缓存', 'android-cache': '手机缓存', desktop: '电脑资料' })[game.source] || game.source : game.source?.label || (DEVICE === 'android' ? '手机缓存' : '电脑资料');
  const values = [['平台', platformName(game.platform)], ['缓存类型', gameTypeName(game)], ['包版本', game.version || '未发现版本'], ['引擎版本', game.engineVersion || '版本未确认'], ['缓存包', (Number(game.packageCount) || 0) + ' 个'], ['总大小', size(game.bytes)], ['资料来源', source]];
  const list = $('game-facts'); list.replaceChildren(); for (const [label, value] of values) { const item = node('div'); item.append(node('dt', '', label), node('dd', '', value)); list.append(item); }
}
function availableModes(game) { game = game || {}; const modes = Array.isArray(game.exportModes) ? game.exportModes.map(value => typeof value === 'string' ? value : value.id || value.mode) : ['raw']; const engine = String(game.engine || '').toLowerCase(); const unityPossible = DEVICE !== 'android' && ['', 'unknown', 'unity', 'tuanjie', '团结'].includes(engine); return [...new Set(['raw', ...modes.filter(value => value !== 'raw' && MODES[value] && (value !== 'unity-project' || unityPossible))])]; }
function renderModes() {
  const modes = availableModes(ui.selected); if (!modes.includes(ui.mode)) ui.mode = modes.includes('assets') ? 'assets' : 'raw';
  const list = $('export-modes'); list.replaceChildren();
  for (const mode of modes) { const item = button('', () => { ui.mode = mode; renderModes(); }, 'mode-button'); item.setAttribute('aria-pressed', String(ui.mode === mode)); item.append(node('strong', '', MODES[mode][0]), node('span', '', MODES[mode][1])); list.append(item); }
  $('export-device').textContent = DEVICE === 'android' ? '手机处理缓存' : '这台电脑处理';
  $('export-note').textContent = DEVICE === 'android' ? ui.mode !== 'raw' ? '手机处理已缓存且支持的包内容。Unity／团结资源请导出原始包到电脑，继续处理。' : API_ROOT.startsWith('/device-api/') ? '手机导出原始缓存包。完成后点击“下载到电脑继续处理”，再切换到电脑查看与导出资源；Unity 处理也在电脑完成。' : '手机导出原始缓存包。下载 ZIP 后，在电脑 DevHelper 工作台点击“导入 ZIP”继续处理资源；Unity 处理也在电脑完成。' : ui.mode === 'unity-project' ? '识别 Unity 资源后由 AssetRipper 处理。导出内容取决于包内资源与版本支持，不等同于原始开发工程。' : '只处理当前游戏已缓存或导入的内容。没有下载到设备的资源不会出现在结果里。';
  $('start-export').disabled = !ui.selected || ui.busy; $('start-export').textContent = '导出' + MODES[ui.mode][0];
}
function renderDetail(game) {
  $('detail-empty').hidden = true; $('detail-content').hidden = false; $('game-name').textContent = gameName(game); $('game-id').textContent = 'AppID：' + (game.appId || '缓存中未提供');
  const label = engineName(game); $('engine-badge').textContent = label; $('engine-badge').classList.toggle('unknown', label === '未识别引擎'); facts(game);
  if (document.activeElement !== $('name-input')) $('name-input').value = ui.nameDrafts.has(game.id) ? ui.nameDrafts.get(game.id) : game.name || '';
  const issues = [...(Array.isArray(game.issues) ? game.issues : []), ...(Array.isArray(game.gameTypeIssues) ? game.gameTypeIssues : []), ...(Array.isArray(game.metadataIssues) ? game.metadataIssues : [])]; $('game-issues').replaceChildren(); $('game-issues').hidden = !issues.length; for (const issue of issues) $('game-issues').append(node('p', '', typeof issue === 'string' ? issue : issue.message || issue.code || '缓存内容不完整。'));
  const evidence = Array.isArray(game.evidence) ? game.evidence : []; $('evidence-list').replaceChildren();
  for (const item of evidence) { const row = node('li', '', item.path || '包内线索'); if (item.match) row.append(node('span', '', item.match)); $('evidence-list').append(row); }
  const unity = ['unity', 'tuanjie', '团结'].includes(String(game.engine || '').toLowerCase());
  $('engine-note').textContent = (evidence.length ? '根据包内文件与标记识别。' : '当前没有足够的文件线索确认引擎。') + (unity ? ' Unity 与团结只有出现明确版本标记时才区分；未出现时保留“未区分”。' : '');
  renderModes(); renderExports();
}
async function selectGame(game, quiet = false) {
  const changed = ui.selected?.id !== game.id, request = ++ui.detailRequest; ui.selected = game; if (changed) { ui.exports = []; ui.exportsKey = ''; } renderGames(); renderDetail(game); if (changed) $('name-input').value = ui.nameDrafts.has(game.id) ? ui.nameDrafts.get(game.id) : game.name || '';
  $('detail-loading').hidden = quiet; document.querySelector('.detail').setAttribute('aria-busy', 'true');
  try {
    const data = await api('/minigames/games/' + encodeURIComponent(game.id)); if (request !== ui.detailRequest) return; ui.selected = normalizeGame(data);
    const exported = data.exports || ui.selected.exports || []; ui.exports = (Array.isArray(exported) ? exported : exported.exports || exported.items || []).map(normalizeExport).filter(Boolean); renderDetail(ui.selected); renderGames();
  } catch (error) { if (request === ui.detailRequest) message(error.message, true); }
  finally { if (request === ui.detailRequest) { $('detail-loading').hidden = true; document.querySelector('.detail').setAttribute('aria-busy', 'false'); } }
}
async function saveName(event) {
  event.preventDefault(); if (!ui.selected) return; const selectedId = ui.selected.id, draft = $('name-input').value, name = draft.trim();
  await busy($('save-name'), async () => { const data = await api('/minigames/games/' + encodeURIComponent(selectedId) + '/metadata', { name }); if (ui.nameDrafts.get(selectedId) === draft) ui.nameDrafts.delete(selectedId); if (ui.selected?.id === selectedId) { ui.selected = { ...ui.selected, ...(data.game || data), name }; renderDetail(ui.selected); } const game = ui.games.find(game => game.id === selectedId); if (game) game.name = name; renderGames(); toast('游戏名称已保存。'); });
}

function exportActions(value, root = API_ROOT, game = ui.selected) {
  const row = node('div', 'row'), download = node('a', 'button-link', '下载 ZIP'); download.href = archiveUrl(value, root); download.setAttribute('download', ''); row.append(download);
  const phone = root.includes('/device-api/android/') || DEVICE === 'android' && root === API_ROOT;
  if (root.includes('/device-api/android/')) row.append(button('下载到电脑继续处理', () => transferToMac(value, root, game)));
  else if (DEVICE === 'android' && root === API_ROOT) row.append(node('span', 'hint', '下载后可在电脑工作台导入 ZIP。'));
  const skeletons = Array.isArray(value.spine) ? value.spine : [];
  const readySkeletons = skeletons.filter(skeleton => skeleton.ready !== false);
  if (!phone || skeletons.length) {
    let selection;
    if (skeletons.length > 1) { selection = node('select'); selection.setAttribute('aria-label', '要打开的 Spine 骨架'); for (const skeleton of skeletons) { const option = node('option', '', (skeleton.name || skeleton.skeleton || '骨架') + (skeleton.ready === false ? '（资源不完整）' : '')); option.value = skeleton.skeleton || ''; option.disabled = skeleton.ready === false; selection.append(option); } selection.value = readySkeletons[0]?.skeleton || ''; row.append(selection); }
    const spine = button('打开 Spine', () => openSpine(value, root, selection?.value || readySkeletons[0]?.skeleton)); spine.disabled = readySkeletons.length === 0; spine.title = readySkeletons.length ? '直接打开导出结果中的 Spine 动画' : '此导出结果没有发现完整 Spine 资源'; row.append(spine);
  }
  if (!phone) row.append(button('DeepSeek 归纳', () => summarizeExport(value, root)));
  return row;
}
function exportNotes(value) {
  const issues = Array.isArray(value.detail?.issues) ? [...value.detail.issues] : [], error = value.summaryError;
  for (const skeleton of Array.isArray(value.spine) ? value.spine : []) if (Array.isArray(skeleton.issues)) for (const issue of skeleton.issues) issues.push({ path: skeleton.skeleton, message: issue });
  if (!issues.length && !error) return null;
  const notes = node('div', 'export-notes'); if (error) notes.append(node('p', '', error));
  for (const issue of issues) notes.append(node('p', '', typeof issue === 'string' ? issue : (issue.path ? issue.path + '：' : '') + (issue.message || '此文件尚未处理。'))); return notes;
}
function renderExports() {
  const merged = new Map(ui.exports.map(value => [value.id, value]));
  for (const job of ui.jobs) { const value = resultExport(job); if (value && job.apiRoot === API_ROOT && (value.gameId === ui.selected?.id || job.gameId === ui.selected?.id)) merged.set(value.id, value); }
  const values = [...merged.values()]; $('export-count').textContent = String(values.length); $('exports-empty').hidden = values.length > 0;
  const key = JSON.stringify(values); if (key === ui.exportsKey) return; ui.exportsKey = key; const list = $('export-list'); list.replaceChildren();
  for (const value of values) {
    const item = node('article', 'export-result'), heading = node('div', 'export-result-header'); heading.append(node('strong', '', value.name || MODES[value.mode]?.[0] || '导出结果')); if (value.bytes !== undefined) heading.append(node('span', 'hint', size(value.bytes))); if (Array.isArray(value.files)) heading.append(node('span', 'hint', value.files.length + ' 个文件'));
    if (value.fileCount !== undefined && !Array.isArray(value.files)) heading.append(node('span', 'hint', value.fileCount + ' 个文件'));
    item.append(heading, exportActions(value), exportedFiles(value)); const notes = exportNotes(value); if (notes) item.append(notes); const summary = typeof value.summary === 'string' ? value.summary : value.summary?.text; if (summary) item.append(node('p', 'export-summary', summary)); list.append(item);
  }
}
function exportedFiles(value, root = API_ROOT) {
  if (value.mode === 'raw' && (root.includes('/device-api/android/') || DEVICE === 'android' && root === API_ROOT)) return node('p', 'hint', '原始包保留缓存文件与清单；请下载 ZIP 查看。');
  const details = node('details', 'export-files'), summary = node('summary', '', '查看导出文件'), list = node('div', 'export-file-list'); details.append(summary, list); let loaded = false;
  details.addEventListener('toggle', async () => {
    if (!details.open || loaded) return; loaded = true; list.textContent = '正在读取文件…';
    try {
      const data = await api('/minigames/exports/' + encodeURIComponent(value.id) + '/files', undefined, root); if (!Array.isArray(data.files)) throw new Error('文件列表格式不正确。'); list.replaceChildren();
      if (!data.files.length) list.append(node('p', 'hint', '此次导出没有文件。'));
      for (const file of data.files) {
        const path = String(file.path || ''); if (!path || path.includes('\\') || path.split('/').some(part => !part || part === '.' || part === '..')) continue;
        const row = node('div', 'export-file'), link = node('a', '', path); link.href = root + '/minigames/exports/' + encodeURIComponent(value.id) + '/files/' + path.split('/').map(encodeURIComponent).join('/'); link.setAttribute('download', ''); row.append(link, node('span', 'hint', size(file.bytes))); list.append(row);
      }
    } catch (error) { loaded = false; list.textContent = error.message; }
  }); return details;
}
async function openSpine(value, root, skeleton) {
  const data = await api('/minigames/exports/' + encodeURIComponent(value.id) + '/spine', skeleton ? { skeleton } : {}, root, 120000);
  if (!data.openUrl) throw new Error('Spine 导入结果没有预览地址。');
  let url = safeUrl(data.openUrl); const targetDevice = root.includes('/android/') ? 'android' : 'mac';
  const bundleId = data.bundle?.id || data.bundleId;
  if (API_ROOT.startsWith('/device-api/') && typeof bundleId === 'string') url = '/device-ui/' + targetDevice + '/spine?entry=' + encodeURIComponent(bundleId) + (skeleton ? '&skeleton=' + encodeURIComponent(skeleton) : '');
  else if (API_ROOT.startsWith('/device-api/') && (url === '/spine' || url.startsWith('/spine?'))) url = '/device-ui/' + targetDevice + url;
  if (window.parent !== window) window.parent.postMessage({ type: 'devhelper.minigames-spine', device: targetDevice, url }, window.location.origin); else window.location.assign(url);
}
async function summarizeExport(value, root) { const data = await api('/minigames/exports/' + encodeURIComponent(value.id) + '/summarize', {}, root); addJob(data, { title: 'DeepSeek 归纳', apiRoot: root, gameId: value.gameId || ui.selected?.id, exportId: value.id }); toast('DeepSeek 归纳已开始，可在任务进度查看结果。'); }
function normalizeJob(data) { const job = data?.job || data; if (!job || !job.id || typeof job.id !== 'string') throw new Error('服务没有返回任务 ID，请刷新游戏列表检查结果。'); return job; }
function persistJobs() { try { localStorage.setItem(STORE, JSON.stringify(ui.jobs.filter(job => !job.local).slice(0, 16).map(({ id, state, title, apiRoot, gameId, exportId, notify, result, type, progress, message, error }) => ({ id, state, title, apiRoot, gameId, exportId, notify, result, type, progress, message, error })))); } catch { /* Storage can be disabled in private browsing. */ } }
function addJob(data, fields = {}) {
  const raw = normalizeJob(data), existing = ui.jobs.find(job => job.id === raw.id && job.apiRoot === (fields.apiRoot || API_ROOT)); const job = { ...existing, ...raw, apiRoot: API_ROOT, title: ({ scan: '扫描缓存', import: '导入游戏包', export: '导出游戏', summarize: 'DeepSeek 归纳' })[raw.type] || '小游戏任务', ...fields };
  if (existing) Object.assign(existing, job); else ui.jobs.unshift(job); ui.jobs = ui.jobs.slice(0, 16); persistJobs(); renderTasks(); renderExports(); schedulePoll(300); return existing || job;
}
function renderTasks() {
  $('tasks-empty').hidden = ui.jobs.length > 0; const running = ui.jobs.filter(job => !FINISHED.has(job.state)).length; $('task-count').textContent = running ? running + ' 个进行中' : ui.jobs.length ? '任务已结束' : '暂无任务';
  const list = $('task-list'), keys = new Set();
  for (const job of ui.jobs) {
    const key = job.apiRoot + ':' + job.id; keys.add(key); let view = ui.jobViews.get(key);
    if (!view) { const item = node('article', 'task'), heading = node('div', 'task-heading'), title = node('strong'), status = node('span', 'task-state'), detail = node('p', 'task-message'), progress = node('progress'), actions = node('div', 'row'); progress.max = 100; heading.append(title, status); item.append(heading, detail, progress, actions); view = { item, title, status, detail, progress, actions, actionKey: '' }; ui.jobViews.set(key, view); }
    view.title.textContent = job.title; const complete = SUCCESS.has(job.state), failed = ['failed', 'error', 'cancelled'].includes(job.state), percentage = jobProgress(job);
    view.item.classList.toggle('completed', complete); view.item.classList.toggle('error', failed); view.status.textContent = complete ? '完成' : failed ? job.state === 'cancelled' ? '已取消' : '失败' : ({ queued: '等待处理', pending: '等待处理', running: '处理中', uploading: '下载与导入中' })[job.state] || '处理中';
    if (!FINISHED.has(job.state) && percentage !== null) view.status.textContent += ' ' + Math.round(percentage) + '%';
    view.detail.textContent = job.error ? typeof job.error === 'string' ? job.error : job.error.message || '任务未完成。' : job.message || (complete ? '任务已完成。' : '正在等待设备返回进度。');
    view.progress.hidden = FINISHED.has(job.state); if (percentage === null) view.progress.removeAttribute('value'); else view.progress.value = percentage;
    const value = resultExport(job), summary = typeof job.result?.summary === 'string' ? job.result.summary : job.result?.summary?.text || job.result?.text;
    const actionKey = JSON.stringify([job.state, value?.id, value?.summaryError, value?.detail?.issues, summary, job.result?.game?.id || job.result?.gameId]);
    if (actionKey !== view.actionKey) {
      view.actionKey = actionKey; view.actions.replaceChildren();
      if (complete && value) view.actions.append(...exportActions(value, job.apiRoot, job.result?.game || { id: job.gameId, platform: value.platform, name: value.name }).children);
      if (complete && value) { const notes = exportNotes(value); if (notes) view.actions.append(notes); }
      const imported = job.result?.game?.id || job.result?.gameId;
      if (complete && imported) view.actions.append(button(job.apiRoot === API_ROOT ? '查看游戏' : '在电脑查看', () => openImported(imported, job.apiRoot)));
      if (failed) view.actions.append(button('刷新任务', () => refreshJob(job)));
      if (!FINISHED.has(job.state) && !job.local) view.actions.append(button('取消任务', () => cancelJob(job)));
      if (summary) { const summaryText = node('p', 'export-summary', summary); view.actions.append(summaryText); }
    }
    if (list.children[ui.jobs.indexOf(job)] !== view.item) list.insertBefore(view.item, list.children[ui.jobs.indexOf(job)] || null);
  }
  for (const [key, view] of ui.jobViews) if (!keys.has(key)) { view.item.remove(); ui.jobViews.delete(key); }
}
async function refreshJob(job) {
  if (job.local) return; const previous = job.state; let data;
  try { data = normalizeJob(await api('/minigames/jobs/' + encodeURIComponent(job.id), undefined, job.apiRoot)); }
  catch (error) {
    if (error.message !== '小游戏任务不存在') throw error;
    Object.assign(job, { state: 'failed', progress: null, error: '设备已找不到这条任务。请刷新游戏列表查看结果，需要时重新执行。' });
    delete job.pollError; persistJobs(); renderTasks(); return;
  }
  Object.assign(job, data); delete job.pollError;
  if (!FINISHED.has(previous) && FINISHED.has(job.state)) await onFinish(job); persistJobs(); renderTasks(); renderExports();
}
async function cancelJob(job) { const data = await api('/minigames/jobs/' + encodeURIComponent(job.id) + '/cancel', {}, job.apiRoot); Object.assign(job, normalizeJob(data)); persistJobs(); renderTasks(); toast('已请求取消任务。'); }
async function restoreServerJobs() {
  try { const data = await api('/minigames/jobs'); if (!Array.isArray(data.jobs)) return; for (const raw of [...data.jobs].reverse()) { if (!raw || typeof raw.id !== 'string') continue; const previous = ui.jobs.find(job => job.id === raw.id && job.apiRoot === API_ROOT); addJob(raw, previous ? { title: previous.title, gameId: previous.gameId, notify: previous.notify } : {}); } }
  catch { /* Older phone services may not expose task history. Existing IDs still poll. */ }
}
async function onFinish(job) {
  const successful = SUCCESS.has(job.state); toast(job.title + (successful ? '已完成。' : '未完成，请查看任务说明。'), !successful);
  if (!successful) return;
  if (job.apiRoot === API_ROOT && ['scan', 'import', 'export'].includes(job.type)) await refreshGames(job.type === 'import' ? job.result?.game?.id || job.result?.gameId : undefined);
  if (job.exportId && job.apiRoot === API_ROOT && ui.selected) await selectGame(ui.selected, true);
  if (job.type === 'assetripper-setup') await checkAssetRipper();
}
function schedulePoll(delay = 1800) { clearTimeout(ui.timer); if (!active() || ui.polling || !ui.jobs.some(job => !job.local && !FINISHED.has(job.state))) return; ui.timer = setTimeout(poll, delay); }
async function poll() {
  if (!active() || ui.polling) return; ui.polling = true;
  try { await Promise.allSettled(ui.jobs.filter(job => !job.local && !FINISHED.has(job.state)).map(async job => { try { await refreshJob(job); } catch (error) { job.pollError = error.message; job.message = error.message + ' 恢复连接后会继续查询同一任务。'; renderTasks(); } })); }
  finally { ui.polling = false; schedulePoll(); }
}
async function startScan() { message(''); const data = await api('/minigames/scan', { platform: $('platform').value }); const platform = $('platform').value === 'all' ? '小游戏' : platformName($('platform').value); addJob(data, { title: DEVICE === 'android' ? '扫描' + platform + '缓存' : '刷新' + platform + '游戏识别' }); toast(DEVICE === 'android' ? '扫描已开始。' : '游戏识别已开始刷新。'); }
async function startExport() {
  if (!ui.selected || ui.busy) return; ui.busy = true; renderModes(); message('');
  try { const body = { gameId: ui.selected.id, mode: ui.mode, notify: $('notify').checked }; if (DEVICE !== 'android') body.summarize = $('summarize').checked; const data = await api('/minigames/export', body); addJob(data, { title: gameName(ui.selected) + '：' + MODES[ui.mode][0], gameId: ui.selected.id, notify: $('notify').checked }); toast('导出已开始，进度会显示在下方。'); }
  catch (error) { message(error.message, true); } finally { ui.busy = false; renderModes(); }
}
async function uploadAttachment(file, root) {
  const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 180000);
  try {
    const response = await fetch(root + '/attachments/upload?name=' + encodeURIComponent(file.name), { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/zip', Accept: 'application/json' }, body: file, signal: controller.signal });
    const raw = await response.text(); let data; try { data = JSON.parse(raw); } catch { throw new Error('ZIP 上传结果格式不正确。'); } if (!response.ok) throw new Error(errorText(data, response.status)); const attachment = data.attachment || data; if (!attachment.id) throw new Error('ZIP 上传结果缺少附件 ID。'); return attachment;
  } catch (error) { if (error.name === 'AbortError') throw new Error('ZIP 上传超时。请查看附件库后重试。'); throw error; } finally { clearTimeout(timer); }
}
async function importZip(file) {
  if (!file || ui.busy) return; if (!/\.zip$/i.test(file.name)) throw new Error('请选择 ZIP 文件。'); ui.busy = true; $('choose-zip').disabled = true; renderModes();
  const root = DEVICE === 'android' && API_ROOT.startsWith('/device-api/') ? MAC_API : API_ROOT;
  try { message('正在上传 ' + file.name + ' 到电脑…'); const attachment = await uploadAttachment(file, root); const data = await api('/minigames/import', { attachmentId: attachment.id, platform: $('platform').value === 'all' ? 'unknown' : $('platform').value, name: file.name.replace(/\.zip$/i, '') }, root); addJob(data, { title: '电脑导入 ' + file.name, apiRoot: root }); message('ZIP 已保存到电脑，正在识别游戏包。'); }
  finally { ui.busy = false; $('choose-zip').disabled = false; renderModes(); }
}
async function transferToMac(value, root, game) {
  const packageName = value.mode === 'raw' ? '原始包' : '导出包';
  const task = { id: 'transfer-' + Date.now(), local: true, apiRoot: MAC_API, title: '下载到电脑继续处理', type: 'transfer', state: 'uploading', progress: null, message: '正在从手机下载' + packageName + '…' }; ui.jobs.unshift(task); renderTasks();
  const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 300000);
  try {
    const response = await fetch(archiveUrl(value, root), { credentials: 'same-origin', signal: controller.signal }); if (!response.ok) throw new Error('手机' + packageName + '下载失败（HTTP ' + response.status + '）。'); const blob = await response.blob();
    task.message = '正在保存到这台电脑的附件库…'; renderTasks(); const file = new File([blob], (value.name || game?.name || '小游戏' + packageName).replace(/\.zip$/i, '') + '.zip', { type: 'application/zip' }); const attachment = await uploadAttachment(file, MAC_API);
    task.message = packageName + '已到电脑，正在导入游戏列表…'; renderTasks();
    const data = await api('/minigames/import', { attachmentId: attachment.id, platform: ['douyin', 'wechat'].includes(game?.platform) ? game.platform : 'unknown', name: game?.name || value.name || '' }, MAC_API);
    task.state = 'completed'; task.progress = 100; task.message = packageName + '已下载到电脑。导入完成后，点击“在电脑查看”选择资源导出。'; renderTasks(); addJob(data, { title: '电脑导入小游戏', apiRoot: MAC_API }); toast(packageName + '已到电脑，正在导入。');
  } catch (error) { task.state = 'failed'; task.error = error.name === 'AbortError' ? '下载超时，请检查手机连接后重试。' : error.message; renderTasks(); throw error; }
  finally { clearTimeout(timer); }
}
async function openImported(id, root) {
  if (root === API_ROOT) { await refreshGames(id); return; }
  const url = '/device-ui/mac/minigames?game=' + encodeURIComponent(id);
  if (window.parent !== window) window.parent.postMessage({ type: 'devhelper.minigames-open', device: 'mac', gameId: id, url }, window.location.origin); else window.location.assign(url);
}
async function checkAssetRipper() {
  $('assetripper-status').textContent = '正在检查…';
  try { const data = await api('/minigames/assetripper/status', undefined, toolApi()); ui.tool = data; $('assetripper-status').textContent = data.available || data.ready ? 'AssetRipper 已可用。' + (data.version ? ' 版本：' + data.version : '') + (data.tuanjie_ready ? ' 团结转换依赖已就绪。' : ' 团结转换依赖尚未就绪。') : data.message || data.reason || '电脑尚未配置可用的 AssetRipper。'; renderToolConfig(); }
  catch (error) { $('assetripper-status').textContent = error.message; }
}
function toolApi() { return API_ROOT.includes('/device-api/') ? MAC_API : API_ROOT; }
function renderToolConfig() {
  const target = $('assetripper-config'); if (target.childElementCount) return; target.hidden = false;
  const configure = node('form'), title = node('h3', '', '已有工具：配置电脑路径'); configure.append(title);
  const fields = [];
  for (const [key, label, placeholder] of [['executable', 'AssetRipper CLI 执行文件', '请输入电脑上的完整路径'], ['dotnet', '.NET 执行文件（可选）', '使用 DLL 时指定 dotnet 路径'], ['python', '团结转换 Python（可选）', '需要已安装 UnityPy 的 Python']]) {
    const input = node('input'); input.id = 'tool-' + key; input.name = key; input.placeholder = placeholder; input.autocomplete = 'off'; input.spellcheck = false; input.required = key === 'executable'; const field = node('label', '', label); field.htmlFor = input.id; configure.append(field, input); fields.push(input);
  }
  const submit = node('button', '', '保存工具配置'); submit.type = 'submit'; configure.append(submit);
  configure.addEventListener('submit', event => { event.preventDefault(); busy(submit, async () => { const body = Object.fromEntries(fields.filter(input => input.value.trim()).map(input => [input.name, input.value.trim()])); await api('/minigames/assetripper/configure', body, toolApi()); toast('AssetRipper 配置已保存。'); await checkAssetRipper(); }); });
  const install = node('div', 'tool-install'); install.append(node('h3', '', '自动安装到电脑'), node('p', 'hint', '下载固定版本 AssetRipper-CLI 源码并编译到电脑私有工具目录。需要 .NET 10 SDK；选中下方选项后，也会下载 SDK 或团结转换依赖。'));
  const options = {};
  for (const [key, label] of [['install_dotnet', '下载并安装 .NET 10 SDK（下载体积较大）'], ['install_tuanjie', '安装团结资源转换依赖']]) { const check = node('label', 'check'), input = node('input'); input.type = 'checkbox'; options[key] = input; check.append(input, document.createTextNode(label)); install.append(check); }
  install.append(button('按以上选项安装 AssetRipper', async () => { const data = await api('/minigames/assetripper/setup', Object.fromEntries(Object.entries(options).map(([key, input]) => [key, input.checked])), toolApi()); addJob(data, { title: '安装 AssetRipper', apiRoot: toolApi() }); toast('AssetRipper 安装已开始，可在任务区查看进度。'); })); target.append(configure, install);
}

$('search').addEventListener('input', renderGames); $('refresh').addEventListener('click', () => { refreshGames(); restoreServerJobs(); }); $('platform').addEventListener('change', () => { ui.offset = 0; message(''); refreshGames(); });
$('name-input').addEventListener('input', () => { if (!ui.selected) return; if ($('name-input').value === (ui.selected.name || '')) ui.nameDrafts.delete(ui.selected.id); else ui.nameDrafts.set(ui.selected.id, $('name-input').value); });
$('previous').addEventListener('click', () => { ui.offset = Math.max(0, ui.offset - ui.limit); refreshGames(); }); $('next').addEventListener('click', () => { ui.offset += ui.limit; refreshGames(); });
$('scan').addEventListener('click', () => busy($('scan'), startScan)); $('name-form').addEventListener('submit', saveName); $('start-export').addEventListener('click', startExport);
$('choose-zip').addEventListener('click', () => $('zip-input').click()); $('zip-input').addEventListener('change', () => { const file = $('zip-input').files?.[0]; $('zip-input').value = ''; importZip(file).catch(error => message(error.message, true)); });
$('assetripper-check').addEventListener('click', () => busy($('assetripper-check'), checkAssetRipper)); $('assetripper').addEventListener('toggle', () => { if ($('assetripper').open && !ui.tool) checkAssetRipper(); });
window.addEventListener('message', event => { if (event.origin !== window.location.origin || event.source !== window.parent || event.data?.type !== 'devhelper.minigames-visibility') return; ui.visible = event.data.visible === true; if (ui.visible) schedulePoll(100); else clearTimeout(ui.timer); });
document.addEventListener('visibilitychange', () => { if (active()) schedulePoll(100); else clearTimeout(ui.timer); }); window.addEventListener('pagehide', () => clearTimeout(ui.timer));
$('device-hint').textContent = DEVICE === 'android' ? '扫描手机缓存，原始包可下载到电脑处理。' : '导入游戏包，整理代码、资源与动画。';
$('summarize').closest('label').hidden = DEVICE === 'android';
if (DEVICE === 'android') { $('choose-zip').textContent = '导入 ZIP 到电脑'; $('choose-zip').hidden = !API_ROOT.startsWith('/device-api/'); }
$('scan-hint').textContent = DEVICE === 'android' ? '扫描手机已有缓存。先运行过游戏，才能找到完整内容。' : '刷新电脑已导入的游戏；手机缓存请切换到手机扫描。';
if (DEVICE !== 'android') $('scan').textContent = '刷新识别';
try { const jobs = JSON.parse(localStorage.getItem(STORE) || '[]'); ui.jobs = Array.isArray(jobs) ? jobs.filter(job => job && typeof job.id === 'string' && [API_ROOT, MAC_API].includes(job.apiRoot)).slice(0, 16) : []; } catch { ui.jobs = []; }
renderTasks(); refreshGames(new URLSearchParams(window.location.search).get('game') || undefined); restoreServerJobs(); schedulePoll();
