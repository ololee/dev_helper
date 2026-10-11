// Small, dependency-free ZIP writer. Files are stored unchanged; no executable ZIP code is read.
const table = Uint32Array.from({ length: 256 }, (_, index) => {
  let value = index;
  for (let bit = 0; bit < 8; bit++) value = value & 1 ? 0xedb88320 ^ (value >>> 1) : value >>> 1;
  return value >>> 0;
});

async function crc32(bytes) {
  let crc = 0xffffffff;
  for (let offset = 0; offset < bytes.length; offset += 1048576) {
    const end = Math.min(bytes.length, offset + 1048576);
    for (let index = offset; index < end; index++) crc = table[(crc ^ bytes[index]) & 255] ^ (crc >>> 8);
    await new Promise(resolve => setTimeout(resolve, 0));
  }
  return (crc ^ 0xffffffff) >>> 0;
}

function header(size) { const bytes = new Uint8Array(size); return { bytes, view: new DataView(bytes.buffer) }; }
const caseKey = value => value.normalize('NFC').toLowerCase().replace(/ß/g, 'ss').replace(/ς/g, 'σ');

export async function makeStoredZip(files, progress = () => {}) {
  const chosen = Array.from(files).filter(file => /\.(?:json|skel(?:\.bytes)?|atlas(?:\.txt)?|png|jpe?g|webp|bmp)$/i.test(file.name) && !file.webkitRelativePath?.split('/').some(part => part.startsWith('.')));
  if (!chosen.length) throw new Error('请选择骨架、图集与纹理文件，或选择包含它们的目录。');
  if (chosen.length > 10000) throw new Error('一个资源包最多包含 10000 个文件。');
  if (chosen.reduce((sum, file) => sum + file.size, 0) > 256 * 1048576) throw new Error('目录打包后的 ZIP 不能超过 256 MiB。');
  const local = [], central = [], names = new Set(), components = new Map(), encoder = new TextEncoder();
  let offset = 0;
  for (let index = 0; index < chosen.length; index++) {
    const file = chosen[index];
    if (file.size > 128 * 1048576) throw new Error(file.name + ' 超过单个文件 128 MiB 的限制。');
    const path = (file.webkitRelativePath || file.name).normalize('NFC');
    if (path.startsWith('/') || path.includes('\\') || path.split('/').some(part => !part || part === '.' || part === '..') || /[\u0000-\u001f]/.test(path)) throw new Error('文件路径无效：' + path);
    const key = caseKey(path);
    if (names.has(key)) throw new Error('文件路径重复或大小写冲突：' + path);
    names.add(key);
    const segments = path.split('/');
    for (let length = 1; length <= segments.length; length++) {
      const component = segments.slice(0, length).join('/'), normalized = caseKey(component);
      if (components.has(normalized) && components.get(normalized) !== component) throw new Error('目录路径大小写冲突：' + component);
      components.set(normalized, component);
    }
    const name = encoder.encode(path);
    if (name.length > 4096) throw new Error('文件路径过长：' + file.name);
    progress(index + 1, chosen.length, file.name);
    const bytes = new Uint8Array(await file.arrayBuffer()), checksum = await crc32(bytes);
    const date = new Date(file.lastModified || Date.now());
    const dosTime = date.getHours() << 11 | date.getMinutes() << 5 | date.getSeconds() >>> 1;
    const dosDate = (Math.max(1980, Math.min(2107, date.getFullYear())) - 1980) << 9 | (date.getMonth() + 1) << 5 | date.getDate();
    const record = header(30), view = record.view;
    view.setUint32(0, 0x04034b50, true); view.setUint16(4, 20, true); view.setUint16(6, 0x800, true);
    view.setUint16(10, dosTime, true); view.setUint16(12, dosDate, true); view.setUint32(14, checksum, true);
    view.setUint32(18, bytes.length, true); view.setUint32(22, bytes.length, true); view.setUint16(26, name.length, true);
    local.push(record.bytes, name, bytes);
    const directory = header(46), data = directory.view;
    data.setUint32(0, 0x02014b50, true); data.setUint16(4, 20, true); data.setUint16(6, 20, true); data.setUint16(8, 0x800, true);
    data.setUint16(12, dosTime, true); data.setUint16(14, dosDate, true); data.setUint32(16, checksum, true);
    data.setUint32(20, bytes.length, true); data.setUint32(24, bytes.length, true); data.setUint16(28, name.length, true); data.setUint32(42, offset, true);
    central.push(directory.bytes, name);
    offset += record.bytes.length + name.length + bytes.length;
  }
  const centralSize = central.reduce((sum, bytes) => sum + bytes.length, 0), end = header(22);
  end.view.setUint32(0, 0x06054b50, true); end.view.setUint16(8, chosen.length, true); end.view.setUint16(10, chosen.length, true);
  end.view.setUint32(12, centralSize, true); end.view.setUint32(16, offset, true);
  const zip = new Blob([...local, ...central, end.bytes], { type: 'application/zip' });
  if (zip.size > 256 * 1048576) throw new Error('目录打包后的 ZIP 不能超过 256 MiB。');
  const root = chosen[0].webkitRelativePath?.split('/')[0] || 'Spine资源';
  return new File([zip], root + '.zip', { type: 'application/zip' });
}
