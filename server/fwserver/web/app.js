// aoi-flywheel web pages: plain ES modules, no build step. Every page reads the same /api/v1 as the fw client,
// with the account's token (kept in this browser's localStorage). Text from the platform is only ever put into
// the page as text (el() / textContent), never as HTML.

const TOKEN_KEY = 'fw-token';
const IMAGE_EXTS = new Set(['.bmp', '.png', '.jpg', '.jpeg', '.tif', '.tiff', '.webp']);  // = fw.contract
const PAGE = 60;
const main = typeof document !== 'undefined' ? document.getElementById('main') : null;  // null under node (tests)

// --- small helpers ----------------------------------------------------------------------------------------
export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'style') node.style.cssText = v;  // through the CSSOM: the page's policy blocks style attributes
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat(Infinity)) {
    if (c === undefined || c === null || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

function present(nodes) {
  return nodes.flat().filter((n) => n !== null && n !== undefined && n !== false);
}

function paint(...nodes) {
  main.replaceChildren(...present(nodes));
  window.scrollTo(0, 0);
}

// Every page starts with `const show = live();` before its first await: what it shows later is dropped when
// another page was opened meanwhile (a slow page must not cover the one the user went on to).
let seq = 0;
let timer = null;  // the one auto refresh of the page shown now (render clears it)
let keyNav = null;  // arrow keys of the page shown now (render clears it)
function live() {
  const mine = seq;
  return (...nodes) => { if (mine === seq) paint(...nodes); };
}


export function sizeText(n) {
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  return i === 0 ? `${v} B` : `${v.toFixed(v >= 100 ? 0 : 1)} ${units[i]}`;
}

export function when(stamp) {
  if (!stamp) return '';
  const d = new Date(stamp);
  const p = (x) => String(x).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function ext(path) {
  const name = path.slice(path.lastIndexOf('/') + 1);
  const dot = name.lastIndexOf('.');
  return dot > 0 ? name.slice(dot).toLowerCase() : '';
}

function stem(path) {
  const e = ext(path);
  return e ? path.slice(0, path.length - e.length) : path;
}

export function hrefOf(parts, query) {
  const q = query ? new URLSearchParams(Object.entries(query).filter(([, v]) => v !== '' && v != null)).toString() : '';
  return '#/' + parts.map(encodeURIComponent).join('/') + (q ? `?${q}` : '');
}

// --- the API ----------------------------------------------------------------------------------------------
class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

function token() {
  try { return localStorage.getItem(TOKEN_KEY) || ''; } catch { return ''; }
}

async function call(path, { method = 'GET', body, blob, raw = false, stay = false } = {}) {
  const headers = { Authorization: `Bearer ${token()}` };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (blob !== undefined) headers['Content-Type'] = 'application/octet-stream';
  const r = await fetch(`/api/v1${path}`, { method, headers, body: blob !== undefined ? blob : body === undefined ? undefined : JSON.stringify(body) });
  if (r.status === 401 && !stay) {
    location.hash = '#/login';
    throw new ApiError(401, '要先登录');
  }
  if (!r.ok) {
    let detail = `${r.status} ${r.statusText}`;
    try { detail = (await r.json()).detail || detail; } catch { /* not JSON */ }
    throw new ApiError(r.status, typeof detail === 'string' ? detail : JSON.stringify(detail));
  }
  return raw ? r : r.json();
}

export const api = {
  me: (stay = false) => call('/me', { stay }),  // stay: on the login page, say why instead of going there
  datasets: () => call('/datasets'),
  dataset: (name) => call(`/datasets/${encodeURIComponent(name)}`),
  versions: (name) => call(`/datasets/${encodeURIComponent(name)}/versions`),
  version: (name, number) => call(`/datasets/${encodeURIComponent(name)}/versions/${encodeURIComponent(number)}`),
  diff: (oldId, newId) => call(`/diff?old=${oldId}&new=${newId}`),
  object: (digest) => call(`/objects/${digest}`, { raw: true }),
  models: () => call('/models'),
  model: (name) => call(`/models/${encodeURIComponent(name)}`),
  modelVersions: (name) => call(`/models/${encodeURIComponent(name)}/versions`),
  modelVersion: (name, number) => call(`/models/${encodeURIComponent(name)}/versions/${encodeURIComponent(number)}`),
  evals: (model = '') => call(`/evals?model=${encodeURIComponent(model)}`),
  eval: (id) => call(`/evals/${Number(id)}`),
  chain: (ref) => call(`/chain?ref=${encodeURIComponent(ref)}`),
  runners: async () => (await call('/runners')).runners,
  jobs: async (status = '', limit = 100) => (await call(`/jobs?status=${encodeURIComponent(status)}&limit=${limit}`)).jobs,
  job: (id) => call(`/jobs/${Number(id)}`),
  jobLog: (id, attempt = 0, after = -1) => call(`/jobs/${Number(id)}/log?attempt=${attempt}&after=${after}`),
  cancelJob: (id) => call(`/jobs/${Number(id)}/cancel`, { method: 'POST', body: {} }),
  submitJob: (kind, runner, params) => call('/jobs', { method: 'POST', body: { kind, runner, params } }),
  predictionSets: async (model = '') => (await call(`/predictions?model=${encodeURIComponent(model)}`)).prediction_sets,
  predictionSet: (id) => call(`/predictions/${Number(id)}`),
  createDataset: (body) => call('/datasets', { method: 'POST', body }),
  folderCheck: (paths, allowOtherFiles, force) => call('/folder-check', { method: 'POST', body: { paths, allow_other_files: allowOtherFiles, force } }),
  missing: async (objects) => (await call('/objects/missing', { method: 'POST', body: { objects } })).missing,
  putObject: (digest, blob) => call(`/objects/${digest}`, { method: 'PUT', blob }),
  createVersion: (name, body) => call(`/datasets/${encodeURIComponent(name)}/versions`, { method: 'POST', body }),
};

// The signed-in account (null before /me answered): pages show forms only to roles that may use them.
let me = null;
const can = (...roles) => !!me && roles.includes(me.role);

// Files of a version, read once per page view from its manifest (path \t sha256 \t size per line).
const manifests = new Map();

export function parseManifest(text) {
  const lines = text.split('\n');
  if (!lines[0].startsWith('fw-manifest')) throw new Error('不是 fw 清单');
  const out = [];
  for (const line of lines.slice(1)) {
    if (!line) continue;
    const [path, sha256, size] = line.split('\t');
    out.push({ path, sha256, size: Number(size) });
  }
  return out;
}

async function filesOf(version) {
  if (!manifests.has(version.manifest_sha256)) {
    const r = await api.object(version.manifest_sha256);
    manifests.set(version.manifest_sha256, parseManifest(await r.text()));
  }
  return manifests.get(version.manifest_sha256);
}

// The JSON label of an image: the same path with .json (the extension in any case), as fw pairs them.
export function labelIndex(files) {
  const labels = new Map();
  for (const f of files) if (ext(f.path) === '.json') labels.set(stem(f.path), f);
  return (image) => labels.get(stem(image.path));
}

// Object URLs of fetched files, the most recently used last; the oldest are let go past BLOBS (images are
// shown at full size, a field BMP is a few MB).
const BLOBS = 150;
const blobs = new Map();

async function blobUrl(digest) {
  let got = blobs.get(digest);
  if (got) blobs.delete(digest);
  else got = api.object(digest).then((r) => r.blob()).then((b) => URL.createObjectURL(b));
  blobs.set(digest, got);
  while (blobs.size > BLOBS) {
    const [old, url] = blobs.entries().next().value;
    blobs.delete(old);
    url.then((u) => URL.revokeObjectURL(u)).catch(() => {});
  }
  got.catch(() => blobs.delete(digest));
  return got;
}

// Chrome and Firefox show no TIFF; such images get a note instead of a broken picture.
export const shownByBrowsers = (path) => !['.tif', '.tiff'].includes(ext(path));
const NO_TIFF = '浏览器显示不了 TIFF；下载后用 LiteAnno 看';

// --- pages ------------------------------------------------------------------------------------------------
function errorBox(e) {
  return el('div', { class: 'error' }, e instanceof Error ? e.message : String(e));
}

function loginPage() {
  const show = live();
  const input = el('input', { type: 'password', placeholder: '凭据（fw-server token create 给的那一串）', autocomplete: 'off' });
  const msg = el('div');
  const go = async (ev) => {
    ev.preventDefault();
    try { localStorage.setItem(TOKEN_KEY, input.value.trim()); } catch { /* private window */ }
    try {
      renderWho(await api.me(true));
      location.hash = '#/';
    } catch (e) {
      msg.replaceChildren(errorBox(e.status === 401 ? '凭据不对、已吊销或账号已停用' : e));
    }
  };
  show(el('form', { class: 'panel login', onsubmit: go },
    el('h1', {}, '登录'),
    el('p', { class: 'muted' }, '用你的账号凭据登录，和 fw login 用的是同一个。凭据只存在这个浏览器里。'),
    msg, input, el('button', { class: 'primary', type: 'submit' }, '登录')));
  input.focus();
}

async function datasetsPage() {
  const show = live();
  const rows = await api.datasets();
  const body = rows.map((d) => el('tr', {},
    el('td', {}, el('a', { href: hrefOf(['d', d.name]) }, d.name)),
    el('td', {}, d.kind === 'trainset' ? el('span', { class: 'tag' }, '训练集快照') : d.kind),
    el('td', {}, [d.site, d.line].filter(Boolean).join(' / ')),
    el('td', {}, String(d.version_count)),
    el('td', {}, d.latest ? el('a', { href: hrefOf(['v', d.name, String(d.latest.number)]) }, d.latest.ref) : '-'),
    el('td', {}, d.latest ? `${d.latest.file_count} 个文件，${sizeText(d.latest.total_bytes)}` : ''),
    el('td', {}, d.latest ? when(d.latest.created_at) : ''),
    el('td', { class: 'muted' }, d.note)));
  show(el('h1', {}, '数据集'),
    rows.length ? el('table', {}, el('tr', {}, ['名称', '类型', '现场 / 产线', '版本数', '最新版本', '大小', '最新版本时间', '备注']
      .map((h) => el('th', {}, h))), body) : el('p', { class: 'muted' }, '还没有数据集：在下面新建一个数据池再上传目录，或者在服务器上用 fw dataset create 和 fw dataset import。'),
    el('div', { style: 'margin-top:16px' }, newDatasetForm()));
}

async function datasetPage(name) {
  const show = live();
  const [d, versions] = await Promise.all([api.dataset(name), api.versions(name)]);
  const list = versions.slice().reverse();
  const pick = (id) => el('select', { id }, list.map((v) => el('option', { value: String(v.number) }, v.ref)));
  const a = pick('old');
  const b = pick('new');
  if (list.length > 1) a.value = String(list[1].number);
  const compare = el('button', {
    onclick: () => { location.hash = hrefOf(['diff', d.name], { old: a.value, new: b.value }); },
    disabled: list.length < 2,
  }, '比较');
  show(el('div', { class: 'crumbs' }, el('a', { href: '#/' }, '数据集'), ' / ', d.name),
    el('h1', {}, d.name),
    el('dl', { class: 'kv' }, el('dt', {}, '类型'), el('dd', {}, d.kind), el('dt', {}, '现场 / 产线'),
      el('dd', {}, [d.site, d.line].filter(Boolean).join(' / ') || '-'), el('dt', {}, '建立'),
      el('dd', {}, `${d.created_by}，${when(d.created_at)}`), el('dt', {}, '备注'), el('dd', {}, d.note || '-')),
    el('h2', {}, '版本'),
    el('table', {}, el('tr', {}, ['版本', '父版本', '文件', '大小', '来源', '登记人', '时间', '备注'].map((h) => el('th', {}, h))),
      list.map((v) => el('tr', {},
        el('td', {}, el('a', { href: hrefOf(['v', d.name, String(v.number)]) }, v.ref)),
        el('td', {}, v.parent ? v.parent.ref : '-'),
        el('td', {}, String(v.file_count)), el('td', {}, sizeText(v.total_bytes)),
        el('td', { class: 'muted' }, v.source), el('td', {}, v.created_by), el('td', {}, when(v.created_at)),
        el('td', { class: 'muted' }, v.note)))),
    d.kind === 'trainset' ? null : el('div', { class: 'row', style: 'margin-top:12px' },
      el('a', { class: 'button primary', href: hrefOf(['upload', d.name]) }, '从浏览器上传新版本')),
    el('h2', {}, '比较两个版本'),
    el('div', { class: 'row' }, '从', a, '到', b, compare));
}

function crumbsFor(version, dir) {
  const parts = dir ? dir.split('/') : [];
  const out = [el('a', { href: '#/' }, '数据集'), ' / ', el('a', { href: hrefOf(['d', version.dataset]) }, version.dataset),
    ' / ', el('a', { href: hrefOf(['v', version.dataset, String(version.number)]) }, `@${version.number}`)];
  parts.forEach((p, i) => {
    out.push(' / ');
    out.push(el('a', { href: hrefOf(['v', version.dataset, String(version.number)], { dir: parts.slice(0, i + 1).join('/') }) }, p));
  });
  return el('div', { class: 'crumbs' }, out);
}

// The folders right below dir (with how many images each holds, at any depth) and the images directly in it.
export function listDir(files, dir) {
  const prefix = dir ? `${dir}/` : '';
  const folders = new Map();
  const images = [];
  for (const f of files) {
    if (!f.path.startsWith(prefix) || !IMAGE_EXTS.has(ext(f.path))) continue;
    const rest = f.path.slice(prefix.length);
    const slash = rest.indexOf('/');
    if (slash < 0) images.push(f);
    else {
      const name = rest.slice(0, slash);
      folders.set(name, (folders.get(name) || 0) + 1);
    }
  }
  return { folders: [...folders.entries()].sort((x, y) => x[0].localeCompare(y[0])), images };
}

function lazyThumb(img, file) {
  if (!shownByBrowsers(file.path)) { img.alt = 'TIFF（浏览器显示不了）'; return; }
  const digest = file.sha256;
  const io = new IntersectionObserver((seen) => {
    if (seen.some((s) => s.isIntersecting)) {
      io.disconnect();
      blobUrl(digest).then((u) => { img.src = u; }).catch(() => { img.alt = '读不了'; });
    }
  });
  io.observe(img);
}

async function versionPage(name, number, query) {
  const show = live();
  const version = await api.version(name, number);
  const files = await filesOf(version);
  const dir = (query.get('dir') || '').replace(/^\/+|\/+$/g, '');
  const page = Math.max(0, Number(query.get('page') || 0));
  const { folders, images } = listDir(files, dir);
  const labelOf = labelIndex(files);
  const pages = Math.max(1, Math.ceil(images.length / PAGE));
  const shown = images.slice(page * PAGE, (page + 1) * PAGE);
  const vparts = ['v', version.dataset, String(version.number)];
  const pager = pages > 1 ? el('div', { class: 'pager' },
    el('button', { disabled: page === 0, onclick: () => { location.hash = hrefOf(vparts, { dir, page: page - 1 }); } }, '上一页'),
    `第 ${page + 1} / ${pages} 页`,
    el('button', { disabled: page + 1 >= pages, onclick: () => { location.hash = hrefOf(vparts, { dir, page: page + 1 }); } }, '下一页')) : null;
  const labeled = images.filter((f) => labelOf(f)).length;
  show(crumbsFor(version, dir),
    el('h1', {}, version.ref + (dir ? ` · ${dir}` : '')),
    dir ? null : el('dl', { class: 'kv' },
      el('dt', {}, '文件'), el('dd', {}, `${version.file_count} 个，${sizeText(version.total_bytes)}`),
      el('dt', {}, '父版本'), el('dd', {}, version.parent ? version.parent.ref : '-'),
      el('dt', {}, '登记'), el('dd', {}, `${version.created_by}，${when(version.created_at)}`),
      el('dt', {}, '来源'), el('dd', {}, version.source || '-'),
      el('dt', {}, '清单哈希'), el('dd', { class: 'mono' }, version.manifest_sha256)),
    folders.length ? el('div', { class: 'folders' }, folders.map(([f, n]) =>
      el('a', { href: hrefOf(vparts, { dir: dir ? `${dir}/${f}` : f }) }, `📁 ${f}`, el('span', { class: 'muted' }, `  ${n}`)))) : null,
    images.length ? el('p', { class: 'muted' }, `这一层 ${images.length} 张图，${labeled} 张有 JSON 标注`) : null,
    pager,
    el('div', { class: 'grid' }, shown.map((f) => {
      const img = el('img', { alt: '' });
      lazyThumb(img, f);
      return el('a', { class: 'tile', href: hrefOf(['img', version.dataset, String(version.number), f.path]) },
        el('div', { class: 'thumb' }, img),
        el('div', { class: 'name', title: f.path }, el('span', { class: labelOf(f) ? 'dot on' : 'dot' }), f.path.slice(f.path.lastIndexOf('/') + 1)));
    })),
    pager ? pager.cloneNode(true) : null);
  if (pager) {  // the copy at the bottom needs its own handlers
    const bottom = main.lastElementChild.querySelectorAll('button');
    bottom[0].onclick = () => { location.hash = hrefOf(vparts, { dir, page: page - 1 }); };
    bottom[1].onclick = () => { location.hash = hrefOf(vparts, { dir, page: page + 1 }); };
  }
}

// The points of a LiteAnno / X-AnyLabeling shape as a closed outline (rectangles and polygons) or not.
export function outline(shape) {
  const pts = (shape.points || []).filter((p) => Array.isArray(p) && p.length >= 2).map((p) => [Number(p[0]), Number(p[1])]);
  const type = shape.shape_type || 'polygon';
  if (type === 'rectangle' && pts.length === 2) {
    const [[x1, y1], [x2, y2]] = pts;
    return { pts: [[x1, y1], [x2, y1], [x2, y2], [x1, y2]], closed: true };
  }
  if (type === 'circle' && pts.length === 2) return { circle: pts, closed: true };
  if (type === 'point') return { pts, point: true };
  return { pts, closed: !['line', 'linestrip'].includes(type) };
}

const COLORS = ['#ef4444', '#22c55e', '#3b82f6', '#eab308', '#a855f7', '#06b6d4', '#f97316', '#ec4899'];

function colorOf(name, names) {
  if (!names.includes(name)) names.push(name);
  return COLORS[names.indexOf(name) % COLORS.length];
}

function drawShape(g, s, color, lw, dashed) {
  const o = outline(s);
  g.strokeStyle = color; g.fillStyle = color; g.lineWidth = lw * 2;
  g.setLineDash(dashed ? [lw * 6, lw * 4] : []);
  g.beginPath();
  if (o.circle) {
    const [[cx, cy], [px, py]] = o.circle;
    g.arc(cx, cy, Math.hypot(px - cx, py - cy), 0, 2 * Math.PI);
    g.stroke();
  } else if (o.point) {
    for (const [x, y] of o.pts) { g.moveTo(x + lw * 3, y); g.arc(x, y, lw * 3, 0, 2 * Math.PI); }
    g.fill();
  } else if (o.pts.length) {
    g.moveTo(...o.pts[0]);
    for (const p of o.pts.slice(1)) g.lineTo(...p);
    if (o.closed) g.closePath();
    g.stroke();
  }
  const first = o.circle ? o.circle[0] : o.pts[0];
  if (first) {
    g.font = `${lw * 12}px sans-serif`;
    const text = String(s.label) + (typeof s.score === 'number' ? ` ${s.score.toFixed(2)}` : '');
    g.fillText(text, first[0] + lw * 2, Math.max(lw * 12, first[1] - lw * 3));
  }
}

// A prediction set's predictions.json, read once per page view.
const predictionDocs = new Map();

async function predictionsOf(set) {
  if (!predictionDocs.has(set.id)) {
    const f = set.files.find((x) => x.path === 'predictions.json');
    predictionDocs.set(set.id, api.object(f.sha256).then((r) => r.json()));
  }
  return predictionDocs.get(set.id);
}

// The boxes of one image of a prediction set at a threshold, as rectangles named by the model's classes.
export function predictedShapes(doc, path, threshold) {
  const row = doc.images[path];
  if (!row) return [];
  return row.boxes.filter((b) => b[4] >= threshold).map(([x1, y1, x2, y2, score, cls]) => ({
    label: doc.classes[cls] ?? String(cls), score, shape_type: 'rectangle', points: [[x1, y1], [x2, y2]] }));
}

async function imagePage(name, number, path, query) {
  const show = live();
  const version = await api.version(name, number);
  const files = await filesOf(version);
  const image = files.find((f) => f.path === path);
  if (!image) throw new Error(`${version.ref} 里没有 ${path}`);
  const label = labelIndex(files)(image);
  const dir = path.includes('/') ? path.slice(0, path.lastIndexOf('/')) : '';
  const predId = query.get('pred');
  const set = predId ? await api.predictionSet(predId) : null;
  const pdoc = set ? await predictionsOf(set) : null;
  const threshold = Number(query.get('thr') || 0.3);
  // with a prediction set: page through its images; otherwise through the folder
  const siblings = pdoc ? Object.keys(pdoc.images).sort().map((p) => files.find((f) => f.path === p)).filter(Boolean)
    : listDir(files, dir).images;
  const at = siblings.findIndex((f) => f.path === path);
  const keep = set ? { pred: set.id, thr: query.get('thr') } : null;
  const go = (f) => { location.hash = hrefOf(['img', version.dataset, String(version.number), f.path], keep); };
  const canvas = el('canvas');
  const side = el('div', { class: 'panel' }, el('p', { class: 'muted' }, '加载中…'));
  const slider = set ? el('input', { type: 'range', min: String(set.conf), max: '1', step: '0.01', value: String(Math.max(threshold, set.conf)),
    onchange: (ev) => { location.hash = hrefOf(['img', version.dataset, String(version.number), path], { pred: set.id, thr: ev.target.value }); } }) : null;
  show(set ? el('div', { class: 'crumbs' }, el('a', { href: '#/predictions' }, '预测集'), ' / ',
    el('a', { href: hrefOf(['p', String(set.id)], { thr: query.get('thr') }) }, `#${set.id}`)) : crumbsFor(version, dir),
  el('div', { class: 'row' }, el('h1', { style: 'flex:1' }, path.slice(path.lastIndexOf('/') + 1)),
    slider ? el('label', { class: 'row' }, '阈值', slider, el('span', {}, Math.max(threshold, set.conf).toFixed(2))) : null,
    el('button', { disabled: at <= 0, onclick: () => go(siblings[at - 1]) }, '上一张'),
    el('span', { class: 'muted' }, `${at + 1} / ${siblings.length}`),
    el('button', { disabled: at + 1 >= siblings.length, onclick: () => go(siblings[at + 1]) }, '下一张')),
  el('div', { class: 'viewer' }, el('div', {}, canvas), side));
  keyNav = (key) => {
    if (key === 'ArrowLeft' && at > 0) go(siblings[at - 1]);
    if (key === 'ArrowRight' && at + 1 < siblings.length) go(siblings[at + 1]);
  };

  const img = new Image();
  const problems = [];
  let size = null;  // [w, h] when the browser could show the image, else from the predictions if any
  if (shownByBrowsers(path)) {
    try {
      img.src = await blobUrl(image.sha256);
      await img.decode();
      size = [img.naturalWidth, img.naturalHeight];
    } catch (e) { problems.push(`图片显示不了：${e.message}`); }
  } else problems.push(NO_TIFF);
  if (!size && pdoc && pdoc.images[path]) size = pdoc.images[path].size;
  let doc = null;
  if (label) {
    try { doc = JSON.parse(await (await api.object(label.sha256)).text()); } catch (e) { problems.push(`标注读不了：${e.message}`); }
  }
  if (!size && doc && doc.imageWidth > 0 && doc.imageHeight > 0) size = [doc.imageWidth, doc.imageHeight];
  [canvas.width, canvas.height] = size || [800, 600];
  const g = canvas.getContext('2d');
  if (img.naturalWidth) g.drawImage(img, 0, 0);
  const names = [];
  const lw = Math.max(1, Math.round(Math.max(canvas.width, canvas.height) / 500));
  const shapes = doc && Array.isArray(doc.shapes) ? doc.shapes : [];
  for (const s of shapes) drawShape(g, s, colorOf(String(s.label), names), lw, false);
  const predicted = pdoc ? predictedShapes(pdoc, path, Math.max(threshold, set.conf)) : [];
  for (const s of predicted) drawShape(g, s, colorOf(String(s.label), names), lw, true);
  const count = (list) => {
    const m = new Map();
    for (const s of list) m.set(String(s.label), (m.get(String(s.label)) || 0) + 1);
    return el('ul', { class: 'shapes' }, [...m.entries()].map(([n, k]) =>
      el('li', {}, el('span', { class: 'dot on', style: `background:${colorOf(n, names)}` }), `${n} × ${k}`)));
  };
  side.replaceChildren(...present([
    el('dl', { class: 'kv' },
      el('dt', {}, '尺寸'), el('dd', {}, size ? `${size[0]} × ${size[1]}` : '-'),
      el('dt', {}, '大小'), el('dd', {}, sizeText(image.size)),
      el('dt', {}, 'sha256'), el('dd', { class: 'mono' }, image.sha256),
      el('dt', {}, '标注'), el('dd', {}, label ? label.path.slice(label.path.lastIndexOf('/') + 1) : '没有（正常图）'),
      doc ? [el('dt', {}, '确认'), el('dd', {}, doc.checked === true ? '已确认' : '未确认')] : null),
    problems.map((x) => errorBox(x)),
    shapes.length ? [el('h2', {}, '标注（实线）'), count(shapes)] : null,
    set ? [el('h2', {}, `预测（虚线，分数 ≥ ${Math.max(threshold, set.conf).toFixed(2)}）`),
      predicted.length ? count(predicted) : el('p', { class: 'muted' }, '没有框')] : null]));
}

async function diffPage(name, query) {
  const show = live();
  const [a, b] = await Promise.all([api.version(name, query.get('old')), api.version(name, query.get('new'))]);
  const d = await api.diff(a.id, b.id);
  const LIMIT = 500;
  const link = (v, path) => (IMAGE_EXTS.has(ext(path)) ? el('a', { href: hrefOf(['img', v.dataset, String(v.number), path]) }, path) : path);
  const section = (title, cls, rows) => (rows.length ? [el('h2', {}, `${title}（${rows.length}）`),
    el('table', {}, rows.slice(0, LIMIT).map((r) => el('tr', { class: cls }, el('td', { class: 'mark' }), el('td', {}, r)))),
    rows.length > LIMIT ? el('p', { class: 'muted' }, `只列出前 ${LIMIT} 个；全部用 fw dataset diff --list 看`) : null] : []);
  show(el('div', { class: 'crumbs' }, el('a', { href: '#/' }, '数据集'), ' / ', el('a', { href: hrefOf(['d', name]) }, name), ' / 比较'),
    el('h1', {}, `${a.ref} → ${b.ref}`),
    el('p', {}, `新增 ${d.added.length}，删除 ${d.deleted.length}，修改 ${d.modified.length}，改名 ${d.renamed.length}`),
    section('新增', 'change-added', d.added.map((p) => link(b, p))),
    section('删除', 'change-deleted', d.deleted.map((p) => link(a, p))),
    section('修改', 'change-modified', d.modified.map((p) => (IMAGE_EXTS.has(ext(p)) ? el('span', {}, link(a, p), '  →  ',
      el('a', { href: hrefOf(['img', b.dataset, String(b.number), p]) }, '新版本')) : p))),
    section('改名', 'change-modified', d.renamed.map(([o, n]) => el('span', {}, link(a, o), '  →  ', link(b, n)))));
}


// --- models, evaluations, jobs, prediction sets -----------------------------------------------------------
const TASK_NAMES = { det: '检测', seg: '分割', obb: '旋转框', pose: '关键点', cls: '分类' };

// The numbers of a metrics JSON as [dotted key, value], in file order (= fw.ops.flatten_metrics).
export function flattenMetrics(doc, prefix = '') {
  const out = [];
  for (const [k, v] of Object.entries(doc || {})) {
    if (typeof v === 'boolean') continue;
    if (typeof v === 'number') out.push([prefix + k, v]);
    else if (v && typeof v === 'object' && !Array.isArray(v)) out.push(...flattenMetrics(v, `${prefix}${k}.`));
  }
  return out;
}

export function numberText(v) {
  if (v === undefined || v === null) return '-';
  return Number.isInteger(v) ? String(v) : v.toFixed(4).replace(/0+$/, '').replace(/\.$/, '');
}

function metricsTable(metrics, limit = 60) {
  const rows = flattenMetrics(metrics);
  if (!rows.length) return el('p', { class: 'muted' }, '没有数值指标');
  return [el('table', { class: 'narrow' }, rows.slice(0, limit).map(([k, v]) =>
    el('tr', {}, el('td', { class: 'mono' }, k), el('td', { class: 'num' }, numberText(v))))),
  rows.length > limit ? el('p', { class: 'muted' }, `只列出前 ${limit} 项，全部见 metrics.json`) : null];
}

function fileTable(files) {
  return el('table', {}, el('tr', {}, ['文件', 'sha256', '大小', ''].map((h) => el('th', {}, h))),
    files.map((f) => el('tr', {}, el('td', {}, f.path), el('td', { class: 'mono' }, f.sha256.slice(0, 12)),
      el('td', {}, sizeText(f.size)),
      el('td', {}, el('button', { onclick: (ev) => download(f, ev.currentTarget) }, '下载')))));
}

// Downloads go through the API with the token, so the file is fetched whole and handed to the browser; the
// object URL is let go right after (an ONNX can be hundreds of MB).
async function download(f, button) {
  button.disabled = true;
  try {
    const blob = await (await api.object(f.sha256)).blob();
    const url = URL.createObjectURL(blob);
    const a = el('a', { href: url, download: f.path.slice(f.path.lastIndexOf('/') + 1) });
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (e) {
    button.after(el('span', { class: 'bad' }, ` 下载不了：${e.message}`));
  } finally {
    button.disabled = false;
  }
}

const vref = (ref) => {  // NAME@N -> link to the dataset version page
  const [name, number] = ref.split(':')[0].split('@');
  return el('a', { href: hrefOf(['v', name, number]) }, ref);
};
const mref = (ref) => {
  const [name, number] = ref.split('@');
  return el('a', { href: hrefOf(['mv', name, number]) }, ref);
};
const jobLink = (id) => (id ? el('a', { href: hrefOf(['job', String(id)]) }, `任务 ${id}`) : '-');

async function modelsPage() {
  const show = live();
  const rows = await api.models();
  show(el('h1', {}, '模型'),
    rows.length ? el('table', {}, el('tr', {}, ['名称', '现场 / 产线', '任务', '版本数', '最新', '建立', '备注'].map((h) => el('th', {}, h))),
      rows.map((m) => el('tr', {},
        el('td', {}, el('a', { href: hrefOf(['m', m.name]) }, m.name)),
        el('td', {}, `${m.site} / ${m.line}`), el('td', {}, TASK_NAMES[m.task] || m.task),
        el('td', {}, String(m.version_count)), el('td', {}, m.latest ? mref(m.latest) : '-'),
        el('td', {}, `${m.created_by}，${when(m.created_at)}`), el('td', { class: 'muted' }, m.note))))
      : el('p', { class: 'muted' }, '还没有模型：用 fw model create 建，再 fw model register 登记版本。'));
}

async function modelPage(name) {
  const show = live();
  const [m, versions, evals] = await Promise.all([api.model(name), api.modelVersions(name), api.evals(name)]);
  const list = versions.slice().reverse();
  show(el('div', { class: 'crumbs' }, el('a', { href: '#/models' }, '模型'), ' / ', m.name),
    el('h1', {}, m.name),
    el('dl', { class: 'kv' }, el('dt', {}, '现场 / 产线'), el('dd', {}, `${m.site} / ${m.line}`),
      el('dt', {}, '任务'), el('dd', {}, TASK_NAMES[m.task] || m.task),
      el('dt', {}, '建立'), el('dd', {}, `${m.created_by}，${when(m.created_at)}`), el('dt', {}, '备注'), el('dd', {}, m.note || '-')),
    el('h2', {}, '版本'),
    list.length ? el('table', {}, el('tr', {}, ['版本', '训练集快照', 'ONNX', '代码提交', '标签', '登记人', '时间', '备注'].map((h) => el('th', {}, h))),
      list.map((v) => el('tr', {}, el('td', {}, mref(v.ref)), el('td', {}, vref(v.trainset.ref)),
        el('td', { class: 'mono' }, `${v.onnx_sha256.slice(0, 12)}（${sizeText(v.onnx_size || 0)}）`),
        el('td', { class: 'mono' }, v.code_commit ? v.code_commit.slice(0, 10) + (v.code_dirty ? '*' : '') : '-'),
        el('td', {}, v.tags.map((t) => el('span', { class: 'tag' }, t))),
        el('td', {}, v.created_by), el('td', {}, when(v.created_at)), el('td', { class: 'muted' }, v.note))))
      : el('p', { class: 'muted' }, '还没有版本；用 fw model register 登记。'),
    el('h2', {}, '评估'), evalTable(evals.slice().reverse(), true));
}

function evalTable(rows, withModel) {
  if (!rows.length) return el('p', { class: 'muted' }, '还没有评估报告。');
  const pick = new Set();
  const compare = el('button', { disabled: true, onclick: () => {
    const [a, b] = [...pick];
    location.hash = hrefOf(['evals'], { a, b });
  } }, '比较选中的两个');
  const head = ['', '编号', withModel ? '模型版本' : null, '测试集', '目录', '工具', '任务', '登记', '备注'].filter((h) => h !== null);
  return [el('table', {}, el('tr', {}, head.map((h) => el('th', {}, h))),
    rows.map((e) => el('tr', {},
      el('td', {}, el('input', { type: 'checkbox', onchange: (ev) => {
        if (ev.target.checked) pick.add(String(e.id)); else pick.delete(String(e.id));
        compare.disabled = pick.size !== 2;
      } })),
      el('td', {}, el('a', { href: hrefOf(['eval', String(e.id)]) }, `#${e.id}`)),
      withModel ? el('td', {}, mref(e.model_version.ref)) : null,
      el('td', {}, vref(e.testset.ref)), el('td', {}, e.dirs.join('、') || '全部'),
      el('td', {}, `${e.tool} ${e.tool_version}`.trim() || '-'), el('td', {}, jobLink(e.job_id)),
      el('td', {}, `${e.created_by}，${when(e.created_at)}`), el('td', { class: 'muted' }, e.note)))),
  el('div', { class: 'row', style: 'margin-top:8px' }, compare)];
}

async function modelVersionPage(name, number) {
  const show = live();
  const mv = await api.modelVersion(name, number);
  const [chain, sets] = await Promise.all([api.chain(mv.ref), api.predictionSets(mv.model)]);
  const mine = sets.filter((p) => p.model_version.id === mv.id).reverse();
  show(el('div', { class: 'crumbs' }, el('a', { href: '#/models' }, '模型'), ' / ', el('a', { href: hrefOf(['m', mv.model]) }, mv.model), ' / ', `@${mv.number}`),
    el('h1', {}, mv.ref, ...mv.tags.map((t) => [' ', el('span', { class: 'tag' }, t)])),
    el('dl', { class: 'kv' },
      el('dt', {}, '训练集快照'), el('dd', {}, vref(mv.trainset.ref)),
      el('dt', {}, '来源数据池'), el('dd', {}, chain.sources.length ? chain.sources.map((v, i) => [i ? '、' : '', vref(v.ref)]) : '-'),
      el('dt', {}, '类别'), el('dd', {}, (mv.classes || []).join('、') || '-'),
      el('dt', {}, 'ONNX'), el('dd', { class: 'mono' }, `${mv.onnx_sha256}（${sizeText(mv.onnx_size || 0)}）`),
      el('dt', {}, '训练代码'), el('dd', {}, mv.code_commit ? `${mv.code_repo || ''} @ ${mv.code_commit}${mv.code_dirty ? '（有未提交的改动）' : ''}` : '没登记'),
      el('dt', {}, '发布'), el('dd', {}, chain.releases.length ? chain.releases.map((r) => r.ref).join('、') : '还没发布'),
      el('dt', {}, '登记'), el('dd', {}, `${mv.created_by}，${when(mv.created_at)}`),
      el('dt', {}, '备注'), el('dd', {}, mv.note || '-')),
    flattenMetrics(mv.metrics).length ? [el('h2', {}, '训练时的指标（metrics.json）'), metricsTable(mv.metrics, 20)] : null,
    el('h2', {}, '文件'), fileTable(mv.files),
    el('h2', {}, '评估'), evalTable(mv.evals.slice().reverse(), false),
    el('h2', {}, '预测集'), predictionTable(mine, false),
    can('admin', 'algo') ? await submitForms(mv) : null);
}

// Forms for an evaluation or a prediction job of this model version on a GPU runner.
async function submitForms(mv) {
  const runners = (await api.runners()).filter((r) => r.framework);
  if (!runners.length) {
    return [el('h2', {}, '提交任务'), el('p', { class: 'muted' }, '还没有能推理的推理环境：在 GPU 机上用 worker 账号运行 fw runner register --framework rfdetr --onnx2engine <程序> 登记。')];
  }
  const runnerSelect = () => el('select', {}, runners.map((r) => el('option', { value: r.ref }, `${r.ref}（${r.worker}，${r.conda_env}）`)));
  const form = (title, kind, fields, params, hint) => {
    const msg = el('div');
    const runner = runnerSelect();
    const go = async (ev) => {
      ev.preventDefault();
      const button = ev.target.querySelector('button');
      button.disabled = true;
      try {
        const job = await api.submitJob(kind, runner.value, params());
        location.hash = hrefOf(['job', String(job.id)], job.reused ? { reused: '1' } : null);
      } catch (e) {
        msg.replaceChildren(errorBox(e));
        button.disabled = false;
      }
    };
    return el('form', { class: 'panel form', onsubmit: go }, el('h2', {}, title), el('p', { class: 'muted' }, hint), msg,
      el('label', {}, '推理环境', runner), ...fields, el('button', { class: 'primary', type: 'submit' }, '提交'));
  };
  const dirsOf = (input) => input.value.split(/[,，\n]/).map((d) => d.trim()).filter(Boolean);
  const evalData = el('input', { type: 'text', placeholder: `例如 ${mv.trainset.ref}:test 或 某数据池@3` });
  evalData.value = `${mv.trainset.ref}:test`;
  const evalDirs = el('input', { type: 'text', placeholder: '数据池版本里的目录，逗号分开；空 = 全部' });
  const predData = el('input', { type: 'text', placeholder: '数据池版本，例如 SES-TX@5' });
  const predDirs = el('input', { type: 'text', placeholder: '目录，逗号分开；空 = 全部图片' });
  const predConf = el('input', { type: 'text', value: '0.1' });
  return [el('h2', {}, '提交任务'), el('div', { class: 'forms' },
    form('评估', 'eval', [el('label', {}, '测试数据', evalData), el('label', {}, '目录', evalDirs)],
      () => ({ model: mv.ref, data: evalData.value.trim(), dirs: dirsOf(evalDirs) }),
      '在 GPU 机上用 TensorRT 跑这个模型，用 aoi-deteval 算指标，结果登记成评估报告。快照写划分（:test），数据池可选目录。'),
    form('预标注（推理）', 'predict', [el('label', {}, '数据', predData), el('label', {}, '目录', predDirs), el('label', {}, '最低分数 conf', predConf)],
      () => ({ model: mv.ref, data: predData.value.trim(), dirs: dirsOf(predDirs), conf: Number(predConf.value) }),
      '对数据池版本的图片推理，结果存成预测集；同样的模型、推理环境、图片和 conf 已经跑过就直接用上次的。'))];
}

async function evalPage(id) {
  const show = live();
  const e = await api.eval(id);
  show(el('div', { class: 'crumbs' }, el('a', { href: '#/models' }, '模型'), ' / ', mref(e.model_version.ref), ' / ', `评估 #${e.id}`),
    el('h1', {}, `评估 #${e.id}`),
    el('dl', { class: 'kv' },
      el('dt', {}, '模型版本'), el('dd', {}, mref(e.model_version.ref)),
      el('dt', {}, '测试集'), el('dd', {}, vref(e.testset.ref)),
      el('dt', {}, '目录'), el('dd', {}, e.dirs.join('、') || '全部'),
      el('dt', {}, '工具'), el('dd', {}, `${e.tool} ${e.tool_version}`.trim() || '-'),
      el('dt', {}, '任务'), el('dd', {}, e.job_id ? [jobLink(e.job_id), `，推理环境 ${e.runner}`] : '手动登记'),
      el('dt', {}, '登记'), el('dd', {}, `${e.created_by}，${when(e.created_at)}`),
      el('dt', {}, '备注'), el('dd', {}, e.note || '-')),
    el('h2', {}, '指标'), metricsTable(e.metrics),
    el('h2', {}, '文件'), fileTable(e.files));
}

async function evalComparePage(query) {
  const show = live();
  const [a, b] = await Promise.all([api.eval(query.get('a')), api.eval(query.get('b'))]);
  const ma = new Map(flattenMetrics(a.metrics));
  const mb = new Map(flattenMetrics(b.metrics));
  const keys = [...ma.keys(), ...[...mb.keys()].filter((k) => !ma.has(k))];
  const delta = (k) => {
    if (!ma.has(k) || !mb.has(k)) return el('td', {}, '');
    const d = mb.get(k) - ma.get(k);
    return el('td', { class: `num ${d > 0 ? 'ok' : d < 0 ? 'bad' : ''}` }, (d > 0 ? '+' : '') + numberText(d));
  };
  const head = (e) => [el('a', { href: hrefOf(['eval', String(e.id)]) }, `#${e.id}`), el('div', { class: 'muted' }, `${e.model_version.ref} 在 ${e.testset.ref}`)];
  show(el('div', { class: 'crumbs' }, el('a', { href: '#/models' }, '模型'), ' / 比较评估'),
    el('h1', {}, '比较评估'),
    a.testset.ref !== b.testset.ref || a.dirs.join() !== b.dirs.join()
      ? el('div', { class: 'error' }, '两份报告的测试集或目录不一样，数字不能直接比') : null,
    el('table', { class: 'narrow' }, el('tr', {}, el('th', {}, '指标'), el('th', {}, ...head(a)), el('th', {}, ...head(b)), el('th', {}, '变化')),
      keys.map((k) => el('tr', {}, el('td', { class: 'mono' }, k), el('td', { class: 'num' }, numberText(ma.get(k))),
        el('td', { class: 'num' }, numberText(mb.get(k))), delta(k)))));
}

const STATUS_CLASS = { done: 'ok', failed: 'bad', cancelled: 'muted', running: 'warn', queued: '' };

function refreshLater(fn, ms) {
  clearTimeout(timer);
  const at = location.hash;
  timer = setTimeout(() => { if (location.hash === at) fn().catch(() => {}); }, ms);
}

function jobWhat(j) {
  const p = j.params || {};
  if (j.kind === 'check') return '';
  return [p.model && mref(p.model.ref), ' 在 ', p.data && vref(p.data.ref + (p.data.split ? `:${p.data.split}` : '')),
    p.data && p.data.dirs && p.data.dirs.length ? `（${p.data.dirs.join('、')}）` : '', p.data ? `，${p.data.images} 张` : ''];
}

async function jobsPage(query) {
  const show = live();
  const status = query.get('status') || '';
  const rows = await api.jobs(status);
  const pick = el('select', { onchange: (ev) => { location.hash = hrefOf(['jobs'], { status: ev.target.value }); } },
    [['', '全部'], ['queued', '排队'], ['running', '运行中'], ['done', '完成'], ['failed', '失败'], ['cancelled', '已取消']]
      .map(([v, t]) => el('option', { value: v, selected: v === status }, t)));
  show(el('div', { class: 'row' }, el('h1', { style: 'flex:1' }, '任务'), '状态', pick),
    rows.length ? el('table', {}, el('tr', {}, ['编号', '类型', '内容', '推理环境', '状态', '提交', '开始', '结束'].map((h) => el('th', {}, h))),
      rows.map((j) => el('tr', {},
        el('td', {}, jobLink(j.id)), el('td', {}, j.kind_text), el('td', {}, jobWhat(j)), el('td', {}, j.runner || '-'),
        el('td', { class: STATUS_CLASS[j.status] }, j.status_text + (j.queued_ahead ? `（前面 ${j.queued_ahead} 个）` : '')
          + (j.cancel_requested && j.status === 'running' ? '（正在取消）' : '')),
        el('td', {}, `${j.submitted_by}，${when(j.created_at)}`), el('td', {}, when(j.started_at)), el('td', {}, when(j.finished_at)))))
      : el('p', { class: 'muted' }, '没有任务。'),
    el('p', { class: 'muted' }, '每 10 秒自动刷新。'));
  if (rows.some((j) => j.status === 'queued' || j.status === 'running')) refreshLater(() => jobsPage(query), 10000);
}

function jobResult(j) {
  const r = j.result || {};
  if (j.kind === 'eval' && r.eval_id) return el('a', { href: hrefOf(['eval', String(r.eval_id)]) }, `评估报告 #${r.eval_id}`);
  if (j.kind === 'predict' && r.prediction_set) return el('a', { href: hrefOf(['p', String(r.prediction_set)]) }, `预测集 #${r.prediction_set}`);
  if (j.kind === 'check') return `环境${r.same_env ? '一致' : '不一致'}，源码${r.same_src ? '一致' : '不一致'}`;
  return Object.keys(r).length ? el('span', { class: 'mono' }, JSON.stringify(r)) : '-';
}

async function jobPage(id, query) {
  const show = live();
  let j = await api.job(id);
  const log = el('pre', { class: 'log' });
  const status = el('span');
  const result = el('dd');
  const attempt = j.attempts || 0;
  let after = -1;
  const cancel = el('button', { onclick: async () => {
    cancel.disabled = true;
    try { j = await api.cancelJob(j.id); paint(); } catch (e) { log.before(errorBox(e)); }
  } }, '取消任务');
  const paint = () => {
    status.replaceChildren(el('span', { class: STATUS_CLASS[j.status] }, j.status_text),
      j.queued_ahead ? `（前面还有 ${j.queued_ahead} 个）` : '', j.cancel_requested && j.status === 'running' ? '（正在取消）' : '',
      j.attempts ? `，第 ${j.attempts} 次领取（最多 ${j.max_attempts} 次）` : '', j.worker ? `，在 ${j.worker} 上` : '');
    result.replaceChildren(j.status === 'done' ? jobResult(j) : j.error ? el('span', { class: 'bad' }, j.error) : '-');
    cancel.hidden = !can('admin', 'algo') || !(j.status === 'queued' || j.status === 'running') || j.cancel_requested;
  };
  show(el('div', { class: 'crumbs' }, el('a', { href: '#/jobs' }, '任务'), ' / ', `任务 ${j.id}`),
    query.get('reused') ? el('p', { class: 'panel' }, '同样的模型版本、推理环境、图片和 conf 已经有一个任务，用的就是它，没有再跑。') : null,
    el('div', { class: 'row' }, el('h1', { style: 'flex:1' }, `任务 ${j.id}：${j.kind_text}`), cancel),
    el('dl', { class: 'kv' },
      el('dt', {}, '内容'), el('dd', {}, jobWhat(j) || '-'),
      el('dt', {}, '推理环境'), el('dd', {}, j.runner || '-'),
      el('dt', {}, '状态'), el('dd', {}, status),
      el('dt', {}, '结果'), result,
      el('dt', {}, '提交'), el('dd', {}, `${j.submitted_by}，${when(j.created_at)}`),
      el('dt', {}, '开始 / 结束'), el('dd', {}, `${when(j.started_at) || '-'} / ${when(j.finished_at) || '-'}`)),
    (j.params && j.params.warnings || []).map((w) => el('div', { class: 'error' }, w)),
    el('h2', {}, attempt ? `日志（第 ${attempt} 次）` : '日志'), log);
  paint();
  const poll = async () => {
    if (j.attempts) {
      const got = await api.jobLog(j.id, j.attempts, after);
      for (const piece of got.pieces) { log.append(piece.text); after = piece.seq; }
    }
    if (!log.textContent) log.textContent = j.status === 'queued' ? '排队中，还没有日志。' : '';
    if (j.status === 'queued' || j.status === 'running') {
      refreshLater(async () => {
        const before = j.attempts;
        j = await api.job(j.id);
        if (j.attempts !== before) { await jobPage(id, query); return; }
        paint();
        await poll();
      }, 3000);
    }
  };
  await poll();
}

function predictionTable(rows, withModel) {
  if (!rows.length) return el('p', { class: 'muted' }, '还没有预测集。');
  return el('table', {}, el('tr', {}, ['编号', withModel ? '模型版本' : null, '数据', '目录', 'conf', '图片', '框', '推理环境', '任务', '时间']
    .filter((h) => h !== null).map((h) => el('th', {}, h))),
  rows.map((p) => el('tr', {},
    el('td', {}, el('a', { href: hrefOf(['p', String(p.id)]) }, `#${p.id}`)),
    withModel ? el('td', {}, mref(p.model_version.ref)) : null,
    el('td', {}, vref(p.data.ref)), el('td', {}, p.dirs.join('、') || '全部'), el('td', {}, numberText(p.conf)),
    el('td', {}, String(p.images)), el('td', {}, String(p.boxes)), el('td', {}, p.runner), el('td', {}, jobLink(p.job_id)),
    el('td', {}, `${p.created_by}，${when(p.created_at)}`))));
}

async function predictionsPage() {
  const show = live();
  const rows = await api.predictionSets();
  show(el('h1', {}, '预测集'),
    el('p', { class: 'muted' }, '模型在数据池图片上的推理结果。下载成预标注：fw predict get <编号> <目录> --threshold 0.3。'),
    predictionTable(rows.slice().reverse(), true));
}

async function predictionPage(id, query) {
  const show = live();
  const set = await api.predictionSet(id);
  const [name, number] = set.data.ref.split('@');
  const [version, doc] = await Promise.all([api.version(name, number), predictionsOf(set)]);
  const files = await filesOf(version);
  const byPath = new Map(files.map((f) => [f.path, f]));
  const threshold = Math.max(Number(query.get('thr') || 0.3), set.conf);
  const all = Object.keys(doc.images).sort();
  const page = Math.max(1, Number(query.get('page') || 1));
  const pages = Math.max(1, Math.ceil(all.length / PAGE));
  const shown = all.slice((page - 1) * PAGE, page * PAGE);
  const counted = all.map((p) => predictedShapes(doc, p, threshold).length);
  const total = counted.reduce((a, b) => a + b, 0);
  const empty = counted.filter((n) => n === 0).length;
  const keep = (extra) => hrefOf(['p', String(set.id)], { thr: query.get('thr'), ...extra });
  const slider = el('input', { type: 'range', min: String(set.conf), max: '1', step: '0.01', value: String(threshold),
    onchange: (ev) => { location.hash = keep({ thr: ev.target.value, page: null }); } });
  show(el('div', { class: 'crumbs' }, el('a', { href: '#/predictions' }, '预测集'), ' / ', `#${set.id}`),
    el('h1', {}, `预测集 #${set.id}`),
    el('dl', { class: 'kv' },
      el('dt', {}, '模型版本'), el('dd', {}, mref(set.model_version.ref)),
      el('dt', {}, '数据'), el('dd', {}, vref(set.data.ref), set.dirs.length ? `（${set.dirs.join('、')}）` : ''),
      el('dt', {}, '推理环境'), el('dd', {}, set.runner, '，', jobLink(set.job_id)),
      el('dt', {}, '类别'), el('dd', {}, doc.classes.join('、')),
      el('dt', {}, '存下的框'), el('dd', {}, `${set.boxes} 个（分数 ≥ ${numberText(set.conf)}），${set.images} 张图`),
      el('dt', {}, '下载预标注'), el('dd', { class: 'mono' }, `fw predict get ${set.id} <目录> --threshold ${numberText(threshold)}`)),
    el('div', { class: 'row panel' }, '阈值', slider, el('b', {}, numberText(threshold)),
      el('span', {}, `：${total} 个框，${all.length - empty} 张图有框，${empty} 张没有`)),
    el('div', { class: 'grid', style: 'margin-top:12px' }, shown.map((p) => {
      const img = el('img', { alt: '' });
      const f = byPath.get(p);
      if (f) lazyThumb(img, f);
      const n = predictedShapes(doc, p, threshold).length;
      return el('a', { class: 'tile', href: hrefOf(['img', version.dataset, String(version.number), p], { pred: set.id, thr: query.get('thr') }) },
        el('div', { class: 'thumb' }, img), el('div', { class: 'name' }, el('span', { class: n ? 'dot on' : 'dot' }), `${n} 框  ${p}`));
    })),
    pages > 1 ? el('div', { class: 'pager' },
      el('button', { disabled: page <= 1, onclick: () => { location.hash = keep({ page: page - 1 }); } }, '上一页'),
      el('span', {}, `${page} / ${pages}`),
      el('button', { disabled: page >= pages, onclick: () => { location.hash = keep({ page: page + 1 }); } }, '下一页')) : null);
}


// --- upload a folder from the browser (the same checks, dedupe and freezing as fw dataset import) ------------
// The path of each picked file inside the picked folder (the browser puts the folder's own name first).
export function relPaths(files) {
  return files.map((f) => {
    const p = f.webkitRelativePath || f.name;
    return p.slice(p.indexOf('/') + 1);
  });
}

const utf8 = new TextEncoder();

function byteOrder(a, b) {  // the order of the manifest: by the UTF-8 bytes of the path (= fw.contract.path_key)
  const x = utf8.encode(a);
  const y = utf8.encode(b);
  for (let i = 0; i < Math.min(x.length, y.length); i++) if (x[i] !== y[i]) return x[i] - y[i];
  return x.length - y.length;
}

// Manifest format 1 (fw.contract.manifest_bytes): header, then path \t sha256 \t size sorted by path bytes.
export function manifestText(entries) {
  const rows = entries.slice().sort((a, b) => byteOrder(a.path, b.path));
  return ['fw-manifest 1', ...rows.map((e) => `${e.path}\t${e.sha256}\t${e.size}`)].join('\n') + '\n';
}

export function diffManifests(old, entries) {
  const before = new Map(old.map((f) => [f.path, f.sha256]));
  const now = new Set(entries.map((e) => e.path));
  const added = entries.filter((e) => !before.has(e.path)).map((e) => e.path);
  const modified = entries.filter((e) => before.has(e.path) && before.get(e.path) !== e.sha256).map((e) => e.path);
  const deleted = old.filter((f) => !now.has(f.path)).map((f) => f.path);
  return { added, deleted, modified };
}

// Hash files in a few workers; onProgress(done bytes, total bytes).
async function hashAll(files, onProgress) {
  const n = Math.max(1, Math.min(navigator.hardwareConcurrency || 4, 6, files.length));
  const total = files.reduce((a, f) => a + f.size, 0);
  const out = new Array(files.length);
  let next = 0;
  let done = 0;
  const workers = Array.from({ length: n }, () => new Worker('/web/sha256.js'));
  try {
    await Promise.all(workers.map((w) => new Promise((resolve, reject) => {
      const give = () => {
        if (next >= files.length) { resolve(); return; }
        const id = next++;
        w.onmessage = (ev) => {
          if (ev.data.error) { reject(new Error(`${files[id].name}：${ev.data.error}`)); return; }
          out[id] = ev.data.sha256;
          done += files[id].size;
          onProgress(done, total);
          give();
        };
        w.postMessage({ id, file: files[id] });
      };
      w.onerror = (e) => reject(new Error(e.message || '算哈希出错'));
      give();
    })));
  } finally {
    for (const w of workers) w.terminate();
  }
  return out;
}

async function inParallel(items, n, fn) {
  let next = 0;
  await Promise.all(Array.from({ length: Math.min(n, items.length) }, async () => {
    while (next < items.length) { const i = next++; await fn(items[i], i); }
  }));
}

function bar() {
  const fill = el('div', { class: 'fill' });
  const text = el('span', { class: 'muted' });
  const node = el('div', { class: 'progress' }, el('div', { class: 'track' }, fill), text);
  node.set = (part, whole, what) => {
    fill.style.width = `${whole ? Math.round((part / whole) * 100) : 100}%`;
    text.textContent = what;
  };
  return node;
}

let uploading = false;
if (typeof window !== 'undefined') {
  window.addEventListener('beforeunload', (ev) => { if (uploading) { ev.preventDefault(); ev.returnValue = ''; } });
}

async function uploadPage(name) {
  const show = live();
  const d = await api.dataset(name);
  if (d.kind === 'trainset') throw new Error(`${d.name} 是训练集快照，不能上传；快照用 fw trainset create 建`);
  const latest = d.latest;
  const picker = el('input', { type: 'file', webkitdirectory: true, multiple: true });
  const allowOther = el('input', { type: 'checkbox' });
  const force = el('input', { type: 'checkbox' });
  const note = el('input', { type: 'text', placeholder: '这批数据是什么（来源、日期、批次），写进版本备注' });
  const result = el('div');
  let files = [];
  let paths = [];

  const listOf = (title, part, cls = '') => (part && part.count ? el('details', { class: cls },
    el('summary', {}, `${title}：${part.count}`),
    el('ul', { class: 'mono' }, part.examples.map((x) => el('li', {}, Array.isArray(x) ? x.join('  ') : x))),
    part.count > part.examples.length ? el('p', { class: 'muted' }, `只列出前 ${part.examples.length} 个`) : null) : null);

  const check = async () => {
    if (!files.length) return;
    result.replaceChildren(el('p', { class: 'muted' }, `检查 ${files.length} 个文件…`));
    const r = await api.folderCheck(paths, allowOther.checked, force.checked);
    const junk = new Set(r.junk);
    const keep = files.map((f, i) => [f, paths[i]]).filter(([, p]) => !junk.has(p));
    const report = [
      el('p', {}, `${r.files} 个文件：图片 ${r.images.count}，标注 ${r.labels.count}，说明文件 ${r.meta.count}，其他 ${r.others.count}`
        + (r.junk.length ? `；系统文件 ${r.junk.length} 个不入库` : '')),
      listOf('文件名用不了', r.bad_paths, 'bad'), listOf('路径冲突', r.collisions, 'bad'),
      listOf('同名不同扩展名的图片', r.same_stem_images, 'warn'), listOf('图片和标注以外的文件', r.others, 'warn'),
      listOf('没有标注的图片（当正常图）', r.unlabeled_images), listOf('找不到图片的标注', r.orphan_labels),
      listOf('不同目录里的同名图片（训练时把目录摊平会互相覆盖）', r.same_name_in_dirs), listOf('说明文件（一起入库）', r.meta)];
    if (r.reasons.length) {
      result.replaceChildren(...present([report, el('div', { class: 'error' }, '不能入库：\n' + r.reasons.join('\n'))]));
      return;
    }
    const go = el('button', { class: 'primary', onclick: () => hashAndCompare(keep, report) }, '算哈希，和最新版本比较');
    result.replaceChildren(...present([report, el('div', { class: 'row' }, go)]));
  };

  const hashAndCompare = async (keep, report) => {
    const progress = bar();
    result.replaceChildren(...present([report, progress]));
    uploading = true;
    let digests;
    try {
      digests = await hashAll(keep.map(([f]) => f), (a, b) => progress.set(a, b, `算哈希 ${sizeText(a)} / ${sizeText(b)}`));
    } finally { uploading = false; }
    const entries = keep.map(([f, p], i) => ({ path: p, sha256: digests[i], size: f.size, file: f }));
    const text = manifestText(entries);
    const manifest = new Blob([text]);
    const [manifestDigest] = await hashAll([manifest], () => {});
    const old = latest ? await filesOf(latest) : [];
    const total = entries.reduce((a, e) => a + e.size, 0);
    if (latest && latest.manifest_sha256 === manifestDigest) {
      result.replaceChildren(...present([report, el('p', { class: 'panel' }, `和 ${latest.ref} 完全一样，不用建新版本`)]));
      return;
    }
    const change = diffManifests(old, entries);
    const number = (latest ? latest.number : 0) + 1;
    const start = el('button', { class: 'primary', onclick: () => send(entries, manifest, manifestDigest, number, report) }, `上传并建 ${d.name}@${number}`);
    result.replaceChildren(...present([report,
      el('p', {}, latest ? `相比 ${latest.ref}：新增 ${change.added.length}，删除 ${change.deleted.length}，修改 ${change.modified.length}` : `这是 ${d.name} 的第一个版本`),
      change.deleted.length ? el('div', { class: 'error' }, `会删掉 ${latest.ref} 里的 ${change.deleted.length} 个文件（新版本只含这个目录里的文件），例如：\n`
        + change.deleted.slice(0, 5).join('\n')) : null,
      el('p', {}, `新版本 ${d.name}@${number}：${entries.length} 个文件，${sizeText(total)}`),
      el('div', { class: 'row' }, start)]));
  };

  const send = async (entries, manifest, manifestDigest, number, report) => {
    const progress = bar();
    result.replaceChildren(...present([report, progress]));
    uploading = true;
    try {
      const unique = new Map();
      for (const e of entries) if (!unique.has(e.sha256)) unique.set(e.sha256, e);
      const all = [...unique.values()];
      const missing = new Set();
      for (let i = 0; i < all.length; i += 20000) {
        for (const x of await api.missing(all.slice(i, i + 20000).map((e) => [e.sha256, e.size]))) missing.add(x);
      }
      const todo = all.filter((e) => missing.has(e.sha256));
      const bytes = todo.reduce((a, e) => a + e.size, 0);
      let sent = 0;
      progress.set(0, bytes, `上传 ${todo.length} 个文件（其余 ${all.length - todo.length} 个服务器上已经有了）`);
      await inParallel(todo, 4, async (e) => {
        await api.putObject(e.sha256, e.file);
        sent += e.size;
        progress.set(sent, bytes, `上传 ${sizeText(sent)} / ${sizeText(bytes)}`);
      });
      await api.putObject(manifestDigest, manifest);
      const folder = (files[0].webkitRelativePath || '').split('/')[0];
      const v = await api.createVersion(d.name, { manifest_sha256: manifestDigest, parent_id: latest ? latest.id : null,
        expect_latest_id: latest ? latest.id : null, source: `网页上传 ${folder}`, note: note.value.trim() });
      uploading = false;
      location.hash = hrefOf(['v', d.name, String(v.number)]);
    } catch (e) {
      result.replaceChildren(...present([report, errorBox(e)]));
    } finally { uploading = false; }
  };

  picker.addEventListener('change', () => {
    files = [...picker.files];
    paths = relPaths(files);
    check().catch((e) => result.replaceChildren(errorBox(e)));
  });
  for (const box of [allowOther, force]) box.addEventListener('change', () => check().catch((e) => result.replaceChildren(errorBox(e))));
  show(el('div', { class: 'crumbs' }, el('a', { href: '#/' }, '数据集'), ' / ', el('a', { href: hrefOf(['d', d.name]) }, d.name), ' / 上传'),
    el('h1', {}, `上传到 ${d.name}`),
    el('p', { class: 'muted' }, `选一个目录：目录里的全部文件成为新版本（${latest ? `父版本 ${latest.ref}；目录里没有的文件在新版本里就是删除` : '第一个版本'}）。`
      + '和 fw dataset import 一样检查、按哈希去重（服务器上已有的不再上传），建好后冻结。大批数据（几万张）还是在服务器上用 fw dataset import 快。'),
    el('div', { class: 'panel form' },
      el('label', {}, '目录', picker),
      el('label', {}, '备注', note),
      el('label', { class: 'check' }, allowOther, '图片和标注以外的文件也一起入库（--allow-other-files）'),
      el('label', { class: 'check' }, force, '同名不同扩展名的图片也入库（--force）')),
    result);
}

function newDatasetForm() {
  const name = el('input', { type: 'text', placeholder: '名称，例如 SES-TX' });
  const site = el('input', { type: 'text', placeholder: '现场' });
  const line = el('input', { type: 'text', placeholder: '产线' });
  const note = el('input', { type: 'text', placeholder: '备注' });
  const msg = el('div');
  const go = async (ev) => {
    ev.preventDefault();
    try {
      const d = await api.createDataset({ name: name.value.trim(), site: site.value.trim(), line: line.value.trim(), note: note.value.trim() });
      location.hash = hrefOf(['upload', d.name]);
    } catch (e) { msg.replaceChildren(errorBox(e)); }
  };
  return el('details', { class: 'panel' }, el('summary', {}, '新建数据池'),
    el('form', { class: 'form', onsubmit: go }, msg, el('label', {}, '名称', name), el('label', {}, '现场', site),
      el('label', {}, '产线', line), el('label', {}, '备注', note), el('button', { class: 'primary', type: 'submit' }, '建好后上传')));
}

// --- frame and routing ------------------------------------------------------------------------------------
// [link, text, the roles that see it, the pages it stands for]
const NAV = [
  ['#/', '数据集', ['admin', 'algo'], /^#\/?($|d\/|v\/|img\/|diff\/)/],
  ['#/models', '模型', ['admin', 'algo', 'deploy'], /^#\/(models|m\/|mv\/|eval)/],
  ['#/jobs', '任务', ['admin', 'algo', 'worker'], /^#\/(jobs|job\/)/],
  ['#/predictions', '预测集', ['admin', 'algo'], /^#\/(predictions|p\/)/],
];

function renderNav() {
  const here = location.hash || '#/';
  document.getElementById('nav').replaceChildren(...NAV.filter(([, , roles]) => !me || roles.includes(me.role))
    .map(([href, text, , on]) => el('a', { href, class: on.test(here) ? 'on' : '' }, text)));
}

function renderWho(account) {
  me = account;
  renderNav();
  const who = document.getElementById('who');
  if (!me) { who.replaceChildren(); return; }
  who.replaceChildren(`${me.name}（${me.role}）`, el('button', {
    onclick: () => { try { localStorage.removeItem(TOKEN_KEY); } catch { /* ignore */ } renderWho(null); location.hash = '#/login'; },
  }, '退出'));
}

export function route(hash) {
  const [path, q] = (hash.replace(/^#/, '') || '/').split('?');
  const parts = path.split('/').filter(Boolean).map(decodeURIComponent);
  return { parts, query: new URLSearchParams(q || '') };
}

async function render() {
  seq += 1;
  clearTimeout(timer);
  keyNav = null;
  const show = live();
  const { parts, query } = route(location.hash);
  renderNav();
  if (parts[0] === 'login' || !token()) { loginPage(); return; }
  if (parts.length === 0 && me && !['admin', 'algo'].includes(me.role)) {  // their home: what they may see
    location.hash = me.role === 'worker' ? '#/jobs' : '#/models';
    return;
  }
  try {
    if (parts.length === 0) await datasetsPage();
    else if (parts[0] === 'd' && parts[1]) await datasetPage(parts[1]);
    else if (parts[0] === 'v' && parts[2]) await versionPage(parts[1], parts[2], query);
    else if (parts[0] === 'img' && parts[3]) await imagePage(parts[1], parts[2], parts.slice(3).join('/'), query);
    else if (parts[0] === 'diff' && parts[1]) await diffPage(parts[1], query);
    else if (parts[0] === 'models') await modelsPage();
    else if (parts[0] === 'm' && parts[1]) await modelPage(parts[1]);
    else if (parts[0] === 'mv' && parts[2]) await modelVersionPage(parts[1], parts[2]);
    else if (parts[0] === 'eval' && parts[1]) await evalPage(parts[1]);
    else if (parts[0] === 'evals') await evalComparePage(query);
    else if (parts[0] === 'jobs') await jobsPage(query);
    else if (parts[0] === 'job' && parts[1]) await jobPage(parts[1], query);
    else if (parts[0] === 'predictions') await predictionsPage();
    else if (parts[0] === 'p' && parts[1]) await predictionPage(parts[1], query);
    else if (parts[0] === 'upload' && parts[1]) await uploadPage(parts[1]);
    else show(el('h1', {}, '没有这个页面'), el('a', { href: '#/' }, '回首页'));
  } catch (e) {
    if (e.status === 401) return;
    show(errorBox(e.status === 403 ? `${e.message}（换一个有权限的账号登录）` : e));
  }
}

async function start() {
  if (token()) {
    try { renderWho(await api.me(true)); } catch { /* the pages say why */ }
  }
  window.addEventListener('hashchange', render);
  document.addEventListener('keydown', (ev) => {  // one listener; the page shown now sets keyNav
    if (keyNav && !ev.target.closest('input, select, textarea')) keyNav(ev.key);
  });
  await render();
}

if (main) start();
