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
  const list = present(nodes);
  const tree = list.find((n) => n instanceof Element && n.id === 'places');  // the tree lives in the side bar
  main.replaceChildren(...list.filter((n) => n !== tree));
  if (tree) document.getElementById('side-places').replaceChildren(tree);
  else sideTree();
  window.scrollTo(0, 0);
}

// --- icons: one stroke width, drawn as SVG (no HTML strings) ------------------------------------------------
const SVG = 'http://www.w3.org/2000/svg';
const ICONS = {
  overview: ['M4 4h6.5v6.5H4z', 'M13.5 4H20v6.5h-6.5z', 'M4 13.5h6.5V20H4z', 'M13.5 13.5H20V20h-6.5z'],
  datasets: ['M4 6c0-1.66 3.58-3 8-3s8 1.34 8 3-3.58 3-8 3-8-1.34-8-3z', 'M4 6v6c0 1.66 3.58 3 8 3s8-1.34 8-3V6', 'M4 12v6c0 1.66 3.58 3 8 3s8-1.34 8-3v-6'],
  batches: ['M3.5 12.2V4.5a1 1 0 0 1 1-1h7.7l8.3 8.3a1 1 0 0 1 0 1.4l-7.3 7.3a1 1 0 0 1-1.4 0z', 'M8 8h.01'],
  models: ['M12 2.8l8.5 4.7v9L12 21.2l-8.5-4.7v-9z', 'M3.5 7.5L12 12l8.5-4.5', 'M12 12v9.2'],
  jobs: ['M7 4.5l12 7.5-12 7.5z'],
  predictions: ['M12 3l9 5-9 5-9-5z', 'M3 12.5l9 5 9-5', 'M3 16.5l9 5 9-5'],
  events: ['M9 3.5h6v3H9z', 'M15 5h3a1 1 0 0 1 1 1v13.5a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h3', 'M12 10.5v3.5l2.5 1.5'],
  users: ['M15.5 20v-1.5a4 4 0 0 0-4-4h-5a4 4 0 0 0-4 4V20', 'M9 11a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7z', 'M21.5 20v-1.5a4 4 0 0 0-3-3.87', 'M15.5 4.13a3.5 3.5 0 0 1 0 6.74'],
  line: ['M3.5 3.5v17h17', 'M7.5 15l3.5-4 3 3 5-6'],
  chip: ['M7 7h10v10H7z', 'M10 10h4v4h-4z', 'M9.5 3v4M14.5 3v4M9.5 17v4M14.5 17v4M3 9.5h4M3 14.5h4M17 9.5h4M17 14.5h4'],
  folder: ['M3 6.5a1.5 1.5 0 0 1 1.5-1.5h4.4l2 2.2h8.6A1.5 1.5 0 0 1 21 8.7v9.8a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 18.5z'],
  right: ['M9.5 6l6 6-6 6'],
  left: ['M14.5 6l-6 6 6 6'],
  down: ['M6 9.5l6 6 6-6'],
  more: ['M5.5 12h.01M12 12h.01M18.5 12h.01'],
  open: ['M14 4h6v6', 'M20 4l-8.5 8.5', 'M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5'],
  chart: ['M4 20h16', 'M7.5 16.5v-4', 'M12 16.5V8', 'M16.5 16.5v-6'],
  copy: ['M9 9h11v11H9z', 'M15 5V4H4v11h1'],
  side: ['M4 6h16', 'M4 12h10', 'M4 18h16'],
  user: ['M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z', 'M12 12.5a3 3 0 1 0 0-6 3 3 0 0 0 0 6z', 'M6.2 18.4a7 7 0 0 1 11.6 0'],
  logout: ['M14 4h4a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1h-4', 'M9.5 8l-4 4 4 4', 'M5.5 12H15'],
  arrow: ['M5 12h14', 'M13 6l6 6-6 6'],
};

export function icon(name, cls = '') {
  const svg = document.createElementNS(SVG, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('class', `icon icon-${name}${cls ? ` ${cls}` : ''}`);
  for (const d of ICONS[name] || []) {
    const path = document.createElementNS(SVG, 'path');
    path.setAttribute('d', d);
    svg.append(path);
  }
  return svg;
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
  datasets: (query = {}) => call(`/datasets?${new URLSearchParams(query)}`),
  dataset: (name) => call(`/datasets/${encodeURIComponent(name)}`),
  versions: (name) => call(`/datasets/${encodeURIComponent(name)}/versions`),
  version: (name, number) => call(`/datasets/${encodeURIComponent(name)}/versions/${encodeURIComponent(number)}`),
  sites: () => call('/sites').then((tree) => { treeCache = tree; return tree; }),
  addSite: (ref, name) => call('/sites', { method: 'POST', body: { ref, name } }),
  labels: (name) => call(`/datasets/${encodeURIComponent(name)}/labels`),
  addLabel: (name, number, label, note) => call(`/datasets/${encodeURIComponent(name)}/versions/${encodeURIComponent(number)}/labels`, { method: 'POST', body: { label, note } }),
  modelLabels: (name) => call(`/models/${encodeURIComponent(name)}/labels`),
  addModelLabel: (name, number, label, note) => call(`/models/${encodeURIComponent(name)}/versions/${encodeURIComponent(number)}/labels`, { method: 'POST', body: { label, note } }),
  diff: (oldId, newId) => call(`/diff?old=${oldId}&new=${newId}`),
  object: (digest) => call(`/objects/${digest}`, { raw: true }),
  models: (query = {}) => call(`/models?${new URLSearchParams(query)}`),
  model: (name) => call(`/models/${encodeURIComponent(name)}`),
  modelVersions: (name) => call(`/models/${encodeURIComponent(name)}/versions`),
  modelVersion: (name, number) => call(`/models/${encodeURIComponent(name)}/versions/${encodeURIComponent(number)}`),
  evals: (model = '') => call(`/evals?model=${encodeURIComponent(model)}`),
  eval: (id) => call(`/evals/${Number(id)}`),
  chain: (ref) => call(`/chain?ref=${encodeURIComponent(ref)}`),
  currentRelease: (site, line) => call(`/releases/current?${new URLSearchParams({ site, line })}`),
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
  trainsetLayout: (name, number) => call(`/trainsets/${encodeURIComponent(name)}/versions/${encodeURIComponent(number)}/layout`),
  createVersion: (name, body) => call(`/datasets/${encodeURIComponent(name)}/versions`, { method: 'POST', body }),
  previewChange: (name, body) => call(`/datasets/${encodeURIComponent(name)}/changes/preview`, { method: 'POST', body }),
  createChange: (name, body) => call(`/datasets/${encodeURIComponent(name)}/changes`, { method: 'POST', body }),
  users: () => call('/users'),
  createUser: (name, role) => call('/users', { method: 'POST', body: { name, role } }),
  setRole: (name, role) => call(`/users/${encodeURIComponent(name)}/role`, { method: 'POST', body: { role } }),
  setDisabled: (name, off) => call(`/users/${encodeURIComponent(name)}/${off ? 'disable' : 'enable'}`, { method: 'POST', body: {} }),
  userTokens: async (name) => (await call(`/users/${encodeURIComponent(name)}/tokens`)).tokens,
  createToken: (name, label) => call(`/users/${encodeURIComponent(name)}/tokens`, { method: 'POST', body: { label } }),
  revokeToken: (id) => call(`/tokens/${Number(id)}/revoke`, { method: 'POST', body: {} }),
  myTokens: async () => (await call('/me/tokens')).tokens,
  batches: (query = {}) => call(`/batches?${new URLSearchParams(query)}`),
  batch: (name) => call(`/batches/${encodeURIComponent(name)}`),
  reviewers: async () => (await call('/reviewers')).reviewers,
  createBatch: (body) => call('/batches', { method: 'POST', body }),
  reviseBatch: (name, body) => call(`/batches/${encodeURIComponent(name)}/revisions`, { method: 'POST', body }),
  batchStep: (name, step, body) => call(`/batches/${encodeURIComponent(name)}/${step}`, { method: 'POST', body }),
  batchPrelabels: (name) => call(`/batches/${encodeURIComponent(name)}/prelabel`),
  prelabelBatch: (name, body) => call(`/batches/${encodeURIComponent(name)}/prelabel`, { method: 'POST', body }),
  compareBatch: (name, older, newer) => call(`/batches/${encodeURIComponent(name)}/compare?old=${older}&new=${newer}`),
  events: (query) => call(`/events?${new URLSearchParams(query)}`),
};

// The signed-in account (null before /me answered): pages show forms only to roles that may use them.
let me = null;
// What the signed-in account may do: the capabilities /me lists (auth.CAPS on the server, which decides anyway).
const can = (cap) => !!me && (me.caps || []).includes(cap);

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

// The files of a version as the pages show them. A training-set snapshot is shown in the folders the default
// fw get makes (train/images, neg_train, val/images, …; the server says where each file goes): every file gets
// `show`, its path there, while `path` stays the stored one (links and prediction sets use it). `flat` is false
// when the snapshot cannot be laid out that way (file names collide); then the stored folders are shown.
async function viewFiles(version) {
  const files = await filesOf(version);
  if (version.kind !== 'trainset') return { files, snapshot: false, flat: false, problems: [] };
  const layout = await api.trainsetLayout(version.dataset, version.number);
  return { files: withLayout(files, layout.files), snapshot: true, flat: layout.files.length > 0, problems: layout.problems };
}

// rows: [path as laid out, sha256, size, stored path or null] from the server (fw.contract.trainset_flat).
export function withLayout(files, rows) {
  const shown = new Map();
  for (const [show, , , stored] of rows) if (stored) shown.set(stored, show);
  if (!rows.length) return files;
  const key = (f) => f.show ?? f.path;  // in the order of the folders shown, not of the stored ones
  return files.map((f) => ({ ...f, show: shown.get(f.path) })).sort((x, y) => (key(x) < key(y) ? -1 : key(x) > key(y) ? 1 : 0));
}

const shownPath = (f) => f.show ?? f.path;

// The JSON label of an image: the same path with .json (the extension in any case), as fw pairs them. In a
// training-set snapshot the label of images/<rest> is json/<rest>.
export function labelIndex(files, snapshot = false) {
  const labels = new Map();
  for (const f of files) if (ext(f.path) === '.json') labels.set(stem(f.path), f);
  return (image) => labels.get(stem(snapshot && image.path.startsWith('images/') ? `json/${image.path.slice(7)}` : image.path));
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

// A version's labels (v0.1.0), as chips after its name.
function labelChips(v) {
  return (v.labels || []).map((x) => [' ', el('span', { class: 'tag ver' }, x)]);
}

// The form that gives a version of a dataset, a snapshot or (model true) a model its label. The platform
// proposes the next one after the highest so far; a label is written once and never moved.
async function labelForm(name, versions, model, done) {
  const info = await (model ? api.modelLabels(name) : api.labels(name));
  const which = el('select', { id: 'label-version' }, versions.map((v) => el('option', { value: String(v.number) },
    v.ref + ((v.labels || []).length ? `（${v.labels.join('、')}）` : ''))));
  const label = el('input', { id: 'label-text', placeholder: 'v0.1.0', size: 12 });
  const note = el('input', { id: 'label-note', placeholder: '备注（可不写）', size: 30 });
  const msg = el('div', {});
  const go = el('button', { id: 'label-go', class: 'primary', onclick: async () => {
    go.disabled = true;
    try {
      await (model ? api.addModelLabel : api.addLabel)(name, which.value, label.value.trim(), note.value.trim());
      await done();
    } catch (e) { msg.replaceChildren(errorBox(e)); go.disabled = false; }
  } }, '起版本号');
  const retired = info.labels.filter((r) => r.retired);
  return [el('h2', {}, '版本号'),
    el('p', { class: 'muted' }, '给一个版本起 v主.次.修订 这样的版本号，例如冷启动 v0.0.1、加了数据 v0.1.0、稳定了 v1.0.0。'
      + `起了以后 ${name}@v0.1.0 就是这个版本，哪里都能用。一个版本可以有几个版本号；版本号定了就不能改到别的版本上。`),
    el('div', { class: 'row' }, which, label,
      ['patch', 'minor', 'major'].map((k) => el('button', { id: `label-${k}`, title: { patch: '小改', minor: '加了数据', major: '大版本' }[k],
        onclick: () => { label.value = info.suggest[k]; } }, info.suggest[k])), note, go),
    msg,
    retired.length ? el('p', { class: 'muted' }, '已撤销、不能再用的版本号：'
      + retired.map((r) => `${r.label}（原来指 ${r.ref}，${r.retired_note}）`).join('、')) : null];
}

// --- where a page is: the path from the home page, and the way back ---------------------------------------
const SECTIONS = { datasets: '数据集', batches: '标注批次', models: '模型', jobs: '任务', predictions: '预测集',
  events: '操作记录', users: '账号', me: '我的凭据' };
const PLACED = new Set(['datasets', 'batches', 'models']);  // the lists that are browsed by site and line

// The path to a page as [[text, link or null], ...]: the home page (the overview), the site and line the thing
// belongs to (each leads to the overview of that place), its list (of that place), then the page's own steps
// (a text: where one is; [text, link]: a step one can go back to).
export function trailOf(section, place, steps = []) {
  const at = place && place.site ? { site: place.site, line: place.line || '' } : { site: '', line: '' };
  const out = [['概览', '#/']];
  if (at.site) out.push([at.site, hrefOf([], { site: at.site })]);
  if (at.line) out.push([at.line, hrefOf([], at)]);
  if (section) out.push([SECTIONS[section], hrefOf([section], PLACED.has(section) ? at : null)]);
  if (!steps.length) out[out.length - 1] = [out[out.length - 1][0], null];  // the page itself
  return out.concat(steps.map((x) => (typeof x === 'string' ? [x, null] : x)));
}

// The pages gone through in this tab, so that "back" never leaves the platform: going to the page before the
// last one is a step back, anything else a step on.
export function visited(stack, hash) {
  if (stack.length > 1 && stack[stack.length - 2] === hash) return stack.slice(0, -1);
  return stack[stack.length - 1] === hash ? stack : [...stack, hash];
}
let visits = [];

// The line on top of every page but the home page: back, and the path. Back is the page one came from; on a
// page opened directly (a bookmark, a new tab) it is the step above.
function crumbs(section, place, ...steps) {
  const trail = trailOf(section, place, steps.filter(Boolean));
  const up = trail.slice(0, -1).reverse().find(([, href]) => href);
  return el('nav', { class: 'crumbs' }, sideToggle(),
    el('button', { class: 'tool back', type: 'button', title: '返回刚才的页面', 'aria-label': '返回', onclick: () => {
      if (visits.length > 1) history.back(); else location.hash = up ? up[1] : '#/';
    } }, icon('left')),
    el('span', { class: 'path' }, trail.map(([text, href], i) => [i ? el('span', { class: 'sep' }, '/') : null,
      href ? el('a', { href }, text) : el('span', { class: 'here' }, text)])));
}

// The button that hides and shows the side bar (remembered by the browser).
const SIDE_KEY = 'fw-side';
function sideToggle() {
  return el('button', { class: 'tool side-toggle', type: 'button', title: '收起或展开侧栏', 'aria-label': '收起或展开侧栏',
    'aria-controls': 'side', onclick: () => {
      const off = document.body.classList.toggle('side-off');
      try { localStorage.setItem(SIDE_KEY, off ? 'off' : ''); } catch { /* private window */ }
    } }, icon('side'));
}

// The site and line of a dataset (they never change), for the path of the pages below it.
const placeCache = new Map();
async function placeOfDataset(name) {
  if (!placeCache.has(name)) {
    const d = await api.dataset(name);
    placeCache.set(name, { site: d.site, line: d.line });
  }
  return placeCache.get(name);
}

// --- which sites and lines the tree shows -----------------------------------------------------------------
// Kept in this browser: the sites folded up (closed) and the sites and lines one chose not to see (hidden),
// as lower-case 'site' and 'site/line'. This only tidies the tree and the overview's table of lines: nothing
// is hidden from the lists themselves.
const PLACES_KEY = 'fw-places';
export const placeKey = (site, line) => (line ? `${site}/${line}` : site).toLowerCase();

export function readPlaces(text) {
  let doc = null;
  try { doc = JSON.parse(text || '{}'); } catch { /* not ours: start over */ }
  const list = (x) => (Array.isArray(x) ? x.filter((k) => typeof k === 'string') : []);
  return { closed: list(doc && doc.closed), hidden: list(doc && doc.hidden) };
}

export const toggled = (list, key) => (list.includes(key) ? list.filter((k) => k !== key) : [...list, key]);

// The sites and lines to show: {sites, hidden (how many are left out)}. The one being browsed is always shown.
export function shownPlaces(tree, hidden, at = { site: '', line: '' }) {
  const off = new Set(hidden);
  let left = 0;
  const sites = [];
  for (const s of tree.sites) {
    const here = !!at.site && placeKey(s.code) === placeKey(at.site);
    if (off.has(placeKey(s.code)) && !here) { left += 1; continue; }
    const lines = s.lines.filter((x) => !off.has(placeKey(s.code, x.code)) || (here && !!at.line && placeKey(x.code) === placeKey(at.line)));
    left += s.lines.length - lines.length;
    sites.push({ ...s, lines });
  }
  return { sites, hidden: left };
}

function placePrefs() {
  try { return readPlaces(localStorage.getItem(PLACES_KEY)); } catch { return readPlaces(''); }
}

function savePlaces(prefs) {
  try { localStorage.setItem(PLACES_KEY, JSON.stringify(prefs)); } catch { /* private window: for this page only */ }
}

// --- browsing by site and line ------------------------------------------------------------------------
// The list pages (datasets, batches, models) show what belongs to one site or line, chosen in the tree on the
// left; the choice and the search text are in the address (?site=&line=&q=), and the links at the top keep
// the site and line while one moves between the lists.
let scope = { site: '', line: '' };

export function scopeOf(query) {
  const site = query.get('site') || '';
  return { site, line: site ? (query.get('line') || '') : '', q: query.get('q') || '' };
}

// What the server is asked for: the scope without its empty parts, plus the page's own conditions.
export function scopeQuery(at, more = {}) {
  return Object.fromEntries(Object.entries({ ...more, site: at.site, line: at.line, q: at.q })
    .filter(([, v]) => v !== '' && v !== undefined && v !== null && v !== false));
}

// How many rows choosing a place gives, for the tree: {line(x), site(s), all, loose} (loose: what is under
// no registered place and so only in "all"). By keys: the lists that take a thing on its own line only.
export function countsBy(tree, keys) {
  const n = (x) => keys.reduce((sum, k) => sum + (x[k] || 0), 0);
  const site = (s) => s.lines.reduce((t, x) => t + n(x), 0);
  return { line: n, site, loose: n(tree.other), all: tree.sites.reduce((t, s) => t + site(s), 0) + n(tree.other) };
}

// The datasets list: a training snapshot is listed on every line it took data from, a dataset of a site
// without a line under its site; the server counts by that rule (listed, snapshots, unplaced).
export function datasetCounts(tree) {
  const both = (x) => x.datasets + x.trainsets;
  return { line: (x) => x.datasets + x.snapshots, site: (s) => both(s.listed), loose: both(tree.unplaced), all: both(tree.listed) };
}

// The tree of sites and lines with a count next to each (counts: see countsBy; null: no counts).
// link(site, line): where a choice leads. A site folds up with the arrow in front of it; 「选择」 puts a box in
// front of every site and line, and only the ticked ones are shown afterwards (see shownPlaces).
function placeTree(tree, at, counts, link) {
  const aside = el('aside', { id: 'places' });
  const same = (x, y) => x.toLowerCase() === y.toLowerCase();
  const label = (p) => [p.code, p.name].filter(Boolean).join(' ');  // TX 贴片线
  let choosing = false;
  const draw = () => {
    const prefs = placePrefs();
    const change = (next) => { savePlaces({ ...prefs, ...next }); draw(); };
    const a = (site, line, text, count, on) => el('a', { href: link(site, line), class: on ? 'on' : '', 'aria-current': on ? 'true' : null },
      line ? icon('line') : null, el('span', { class: 'name' }, text), counts ? el('span', { class: 'count' }, String(count())) : null);
    const box = (key, off, what) => (choosing ? el('input', { type: 'checkbox', class: 'pick', checked: !prefs.hidden.includes(key), disabled: off,
      title: `显示 ${what}`, 'data-place': key, onchange: () => change({ hidden: toggled(prefs.hidden, key) }) }) : null);
    const { sites, hidden } = choosing ? { sites: tree.sites, hidden: 0 } : shownPlaces(tree, prefs.hidden, at);
    const whole = (s) => tree.sites.find((x) => x.code === s.code) || s;
    const items = [el('li', { class: 'all' }, a('', '', '全部', () => counts.all, !at.site))];
    for (const s of sites) {
      const here = same(s.code, at.site);
      const key = placeKey(s.code);
      const closed = !choosing && prefs.closed.includes(key);
      const off = prefs.hidden.includes(key);
      const fold = s.lines.length && !choosing ? el('button', { class: 'fold', type: 'button', 'aria-expanded': String(!closed),
        title: closed ? `展开 ${s.code} 的产线` : `收起 ${s.code} 的产线`, onclick: () => change({ closed: toggled(prefs.closed, key) }) }, icon('down'))
        : el('span', { class: 'fold none' });
      items.push(el('li', { class: `site${closed ? ' closed' : ''}${closed && here && at.line ? ' within' : ''}` }, fold, box(key, false, s.code),
        a(s.code, '', label(s), () => counts.site(whole(s)), here && !at.line)));  // the lines not shown count too
      if (!closed) {
        items.push(...s.lines.map((x) => el('li', { class: 'line' }, box(placeKey(s.code, x.code), off, `${s.code} / ${x.code}`),
          a(s.code, x.code, label(x), () => counts.line(x), here && same(x.code, at.line)))));
      }
    }
    aside.className = choosing ? 'choosing' : '';
    aside.replaceChildren(...present([
      el('div', { class: 'head' }, el('span', {}, '现场 / 产线'),
        tree.sites.length ? el('button', { id: 'places-choose', class: 'link', type: 'button',
          title: '选哪些现场、产线显示在这里', onclick: () => {
            if (choosing) { render(); return; }  // done: the page shows the choice too
            choosing = true;
            draw();
          } }, choosing ? '完成' : '选择') : null),
      choosing ? el('p', { class: 'muted small' }, '只有勾上的现场和产线显示在这里和概览的表里；这台电脑的这个浏览器会记住。') : null,
      el('ul', {}, items),
      hidden ? el('p', { class: 'muted small', id: 'places-hidden' }, `有 ${hidden} 个现场 / 产线没显示，点「选择」勾上。`) : null,
      counts && counts.loose ? el('p', { class: 'muted small' }, `另有 ${counts.loose} 个不在任何登记的现场下（没写现场产线的测试集、现场没登记的旧数据），只在「全部」里。`) : null]));
  };
  draw();
  return aside;
}

let treeCache = null;  // the last /sites read (api.sites keeps it)
async function sideTree() {
  const slot = document.getElementById('side-places');
  const mine = seq;
  if (!me || !can('sites.read')) { slot.replaceChildren(); return; }
  if (!treeCache) { try { await api.sites(); } catch { return; } }
  if (mine === seq) slot.replaceChildren(placeTree(treeCache, scope, null, (site, line) => hrefOf([], { site, line })));
}

// The tree, the search box, and the list itself next to them. parts, keep: the page's address and the query
// parts it keeps.
function browse(tree, at, parts, keep, counts, hint, ...list) {
  const q = el('input', { id: 'search', type: 'text', placeholder: hint, value: at.q });
  const go = (ev) => {
    ev.preventDefault();
    location.hash = hrefOf(parts, { ...keep, site: at.site, line: at.line, q: q.value.trim() });
  };
  return [placeTree(tree, at, counts, (site, line) => hrefOf(parts, { ...keep, site, line, q: at.q })), el('div', { class: 'browse' },
    el('div', { class: 'list' },
      el('form', { class: 'row', onsubmit: go }, q, el('button', { type: 'submit' }, '搜索'),
        at.q ? el('a', { href: hrefOf(parts, { ...keep, site: at.site, line: at.line }) }, '清除') : null,
        at.site ? el('span', { class: 'muted' }, `只看 ${[at.site, at.line].filter(Boolean).join(' / ')}`) : null),
      ...list))];
}

async function datasetsPage(query) {
  const show = live();
  const at = scopeOf(query);
  scope = at;
  renderNav();
  const [rows, tree] = await Promise.all([api.datasets(scopeQuery(at)), api.sites()]);
  const table = (list, kindColumn) => el('table', {}, el('tr', {}, ['名称', kindColumn ? '类型' : null, '现场 / 产线', '版本数', '最新版本', '大小', '最新版本时间', '备注']
    .filter(Boolean).map((h) => el('th', {}, h))), list.map((d) => el('tr', {},
    el('td', {}, el('a', { href: hrefOf(['d', d.name]) }, d.name)),
    kindColumn ? el('td', {}, KIND_NAMES[d.kind] || d.kind) : null,
    el('td', {}, [d.site, d.line].filter(Boolean).join(' / ') || (d.kind === 'trainset' ? '跨产线' : '-')),
    el('td', {}, String(d.version_count)),
    el('td', {}, d.latest ? [el('a', { href: hrefOf(['v', d.name, String(d.latest.number)]) }, d.latest.ref), labelChips(d.latest)] : '-'),
    el('td', {}, d.latest ? `${d.latest.file_count} 个文件，${sizeText(d.latest.total_bytes)}` : ''),
    el('td', {}, d.latest ? when(d.latest.created_at) : ''),
    el('td', { class: 'muted' }, d.note))));
  const pools = rows.filter((d) => d.kind !== 'trainset');
  const snapshots = rows.filter((d) => d.kind === 'trainset');
  const narrowed = at.site || at.q;
  show(crumbs('datasets', at), el('h1', {}, '数据集'),
    browse(tree, at, ['datasets'], {}, datasetCounts(tree), '搜名称',
      el('h2', { id: 'pools' }, `数据池和测试集（${pools.length}）`),
      pools.length ? table(pools, true) : el('p', { class: 'muted' }, narrowed ? '这里没有符合的数据池。'
        : '还没有数据集：在下面新建一个数据池再上传目录，或者在服务器上用 fw dataset create 和 fw dataset import。'),
      el('h2', { id: 'snapshots' }, `训练集快照（${snapshots.length}）`),
      snapshots.length ? table(snapshots, false) : el('p', { class: 'muted' }, narrowed ? '这里没有符合的训练集快照。' : '还没有训练集快照：用 fw trainset create 从数据池版本建。'),
      at.line ? el('p', { class: 'muted' }, '训练集快照按它取过数据的产线算：从几条产线取数据的快照，在每条产线下都列出。') : null,
      can('datasets.write') ? el('div', { style: 'margin-top:16px' }, newDatasetForm(tree, at)) : null,
      can('sites.write') ? el('div', { style: 'margin-top:8px' }, newPlaceForm(() => datasetsPage(query))) : null));
}

// A version's source: 'batch:NAME@N' (a labelling batch that was imported) links to the batch.
export function batchOfSource(source) {
  const m = /^batch:([A-Za-z0-9][A-Za-z0-9._-]*)@(\d+)$/.exec(source || '');
  return m ? { name: m[1], revision: Number(m[2]) } : null;
}
function sourceNode(source) {
  const b = batchOfSource(source);
  return b ? ['批次 ', el('a', { href: hrefOf(['b', b.name]) }, b.name), ` 第 ${b.revision} 版`] : (source || '-');
}

async function datasetPage(name) {
  const show = live();
  if ((await api.dataset(name)).kind === 'batch') {  // a batch has its own page: this address does not stay in the way back
    visits = visits.slice(0, -1);
    location.replace(hrefOf(['b', name]));
    return;
  }
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
  placeCache.set(d.name, { site: d.site, line: d.line });
  show(crumbs('datasets', d, d.name),
    el('h1', {}, d.name),
    el('dl', { class: 'kv' }, el('dt', {}, '类型'), el('dd', {}, d.kind), el('dt', {}, '现场 / 产线'),
      el('dd', {}, [d.site, d.line].filter(Boolean).join(' / ') || '-'), el('dt', {}, '建立'),
      el('dd', {}, `${d.created_by}，${when(d.created_at)}`), el('dt', {}, '备注'), el('dd', {}, d.note || '-')),
    el('h2', {}, '版本'),
    el('table', {}, el('tr', {}, ['版本', '版本号', '父版本', '文件', '大小', '目录变化', '来源', '登记人', '时间', '备注'].map((h) => el('th', {}, h))),
      list.map((v) => el('tr', {},
        el('td', {}, el('a', { href: hrefOf(['v', d.name, String(v.number)]) }, v.ref)),
        el('td', {}, labelChips(v)),
        el('td', {}, v.parent ? v.parent.ref : '-'),
        el('td', {}, String(v.file_count)), el('td', {}, sizeText(v.total_bytes)),
        el('td', {}, opsText(v.ops)), el('td', { class: 'muted' }, sourceNode(v.source)), el('td', {}, v.created_by), el('td', {}, when(v.created_at)),
        el('td', { class: 'muted' }, v.note)))),
    d.kind === 'trainset' || !can('datasets.write') ? null : el('div', { class: 'row', style: 'margin-top:12px' },
      el('a', { class: 'button primary', href: hrefOf(['add', d.name]) }, '上传新增目录'),
      d.latest ? el('a', { class: 'button', href: hrefOf(['remove', d.name]) }, '去掉目录') : null,
      el('a', { class: 'button', href: hrefOf(['upload', d.name]) }, '上传完整版本')),
    d.kind === 'trainset' || !can('datasets.write') ? null : el('p', { class: 'muted' }, '平时用「上传新增目录」：只上传新的那个目录，池里原有的不动。'
      + '「上传完整版本」是把一个目录当作新版本的全部内容，目录里没有的文件在新版本里就是删除。'),
    el('h2', {}, '比较两个版本'),
    el('div', { class: 'row' }, '从', a, '到', b, compare),
    list.length && can('datasets.write') ? await labelForm(d.name, list, false, () => datasetPage(name)) : null);
}

// How the server made a version from its parent (version.ops), in a few words.
export function opsText(ops) {
  if (!ops || !ops.length) return '整份导入';
  return ops.map((o) => `${{ add: '新增', replace: '替换', remove: '去掉' }[o.op]} ${o.dir}/`).join('、');
}

function crumbsFor(version, dir, place, leaf = true) {  // leaf: the folder is the page itself, not a step above it
  const parts = dir ? dir.split('/') : [];
  const batch = version.kind === 'batch';
  const at = ['v', version.dataset, String(version.number)];
  const steps = [[version.dataset, hrefOf([batch ? 'b' : 'd', version.dataset])], [`@${version.number}`, hrefOf(at)],
    ...parts.map((p, i) => [p, hrefOf(at, { dir: parts.slice(0, i + 1).join('/') })])];
  if (leaf) steps[steps.length - 1] = steps[steps.length - 1][0];
  return crumbs(batch ? 'batches' : 'datasets', place, ...steps);
}

// The folders right below dir (with how many images each holds, at any depth) and the images directly in it.
export function listDir(files, dir) {
  const prefix = dir ? `${dir}/` : '';
  const folders = new Map();
  const images = [];
  for (const f of files) {
    const path = shownPath(f);
    if (!path.startsWith(prefix) || !IMAGE_EXTS.has(ext(path))) continue;
    const rest = path.slice(prefix.length);
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
  const place = await placeOfDataset(version.dataset);
  const { files, snapshot, flat, problems } = await viewFiles(version);
  const dir = (query.get('dir') || '').replace(/^\/+|\/+$/g, '');
  const page = Math.max(0, Number(query.get('page') || 0));
  const { folders, images } = listDir(files, dir);
  const labelOf = labelIndex(files, snapshot);
  const pages = Math.max(1, Math.ceil(images.length / PAGE));
  const shown = images.slice(page * PAGE, (page + 1) * PAGE);
  const vparts = ['v', version.dataset, String(version.number)];
  const pager = pages > 1 ? el('div', { class: 'pager' },
    el('button', { disabled: page === 0, onclick: () => { location.hash = hrefOf(vparts, { dir, page: page - 1 }); } }, '上一页'),
    `第 ${page + 1} / ${pages} 页`,
    el('button', { disabled: page + 1 >= pages, onclick: () => { location.hash = hrefOf(vparts, { dir, page: page + 1 }); } }, '下一页')) : null;
  const labeled = images.filter((f) => labelOf(f)).length;
  show(crumbsFor(version, dir, place),
    el('h1', {}, version.ref + (dir ? ` · ${dir}` : ''), labelChips(version)),
    dir ? null : el('dl', { class: 'kv' },
      el('dt', {}, '文件'), el('dd', {}, `${version.file_count} 个，${sizeText(version.total_bytes)}`),
      el('dt', {}, '父版本'), el('dd', {}, version.parent ? version.parent.ref : '-'),
      el('dt', {}, '登记'), el('dd', {}, `${version.created_by}，${when(version.created_at)}`),
      el('dt', {}, '来源'), el('dd', {}, sourceNode(version.source)),
      el('dt', {}, '清单哈希'), el('dd', { class: 'mono' }, version.manifest_sha256)),
    !snapshot || dir ? null : el('p', { class: 'muted' }, flat
      ? '目录和 fw get 默认取回的一样：train 里有框的图在 train/images，没有框的在 neg_train；val、test 的图不分有没有框，都在各自的 images 里。'
      : `这个快照有文件平铺后会重名（例如 ${problems.join('；')}），fw get 默认的平铺目录摆不了，这里按存储的目录显示；取回用 fw get --format yolo-tree。`),
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
  const place = await placeOfDataset(version.dataset);
  const { files, snapshot } = await viewFiles(version);
  const image = files.find((f) => f.path === path);
  if (!image) throw new Error(`${version.ref} 里没有 ${path}`);
  const label = labelIndex(files, snapshot)(image);
  const shown = shownPath(image);
  const dir = shown.includes('/') ? shown.slice(0, shown.lastIndexOf('/')) : '';
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
  show(set ? crumbs('predictions', null, [`#${set.id}`, hrefOf(['p', String(set.id)], { thr: query.get('thr') })], path.slice(path.lastIndexOf('/') + 1))
    : crumbsFor(version, dir, place, false),
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
  const place = await placeOfDataset(name);
  const LIMIT = 500;
  const link = (v, path) => (IMAGE_EXTS.has(ext(path)) ? el('a', { href: hrefOf(['img', v.dataset, String(v.number), path]) }, path) : path);
  const section = (title, cls, rows) => (rows.length ? [el('h2', {}, `${title}（${rows.length}）`),
    el('table', {}, rows.slice(0, LIMIT).map((r) => el('tr', { class: cls }, el('td', { class: 'mark' }), el('td', {}, r)))),
    rows.length > LIMIT ? el('p', { class: 'muted' }, `只列出前 ${LIMIT} 个；全部用 fw dataset diff --list 看`) : null] : []);
  show(crumbs('datasets', place, [name, hrefOf(['d', name])], '比较'),
    el('h1', {}, `${a.ref} → ${b.ref}`),
    el('p', {}, `新增 ${d.added.length}，删除 ${d.deleted.length}，修改 ${d.modified.length}，改名 ${d.renamed.length}`),
    section('新增', 'change-added', d.added.map((p) => link(b, p))),
    section('删除', 'change-deleted', d.deleted.map((p) => link(a, p))),
    section('修改', 'change-modified', d.modified.map((p) => (IMAGE_EXTS.has(ext(p)) ? el('span', {}, link(a, p), '  →  ',
      el('a', { href: hrefOf(['img', b.dataset, String(b.number), p]) }, '新版本')) : p))),
    section('改名', 'change-modified', d.renamed.map(([o, n]) => el('span', {}, link(a, o), '  →  ', link(b, n)))));
}


// --- models, evaluations, jobs, prediction sets -----------------------------------------------------------
const KIND_NAMES = { pool: '数据池', test: '测试集', regression: '回归集', other: '其他' };
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
// The button shows how much has come so far: an ONNX of hundreds of MB takes a while on the intranet.
export function downloadText(got, total) {
  return `下载中 ${total ? Math.min(100, Math.floor((got / total) * 100)) : 0}%`;
}

async function readAll(response, onProgress) {
  if (!response.body || !response.body.getReader) return response.blob();
  const reader = response.body.getReader();
  const chunks = [];
  let got = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    got += value.length;
    onProgress(got);
  }
  return new Blob(chunks);
}

async function download(f, button) {
  button.disabled = true;
  const label = button.textContent;
  button.title = `${f.path}，${sizeText(f.size)}`;
  button.textContent = downloadText(0, f.size);
  try {
    const blob = await readAll(await api.object(f.sha256), (got) => { button.textContent = downloadText(got, f.size); });
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
    button.textContent = label;
    button.removeAttribute('title');
  }
}

const vref = (ref) => {  // NAME@N -> link to the dataset version page
  const [name, number] = ref.split(':')[0].split('@');
  return el('a', { class: 'ref', href: hrefOf(['v', name, number]) }, ref);
};
const mref = (ref) => {
  const [name, number] = ref.split('@');
  return el('a', { class: 'ref', href: hrefOf(['mv', name, number]) }, ref);
};
const jobLink = (id) => (id ? el('a', { class: 'ref', href: hrefOf(['job', String(id)]) }, `任务 ${id}`) : '-');

async function modelsPage(query) {
  const show = live();
  const at = scopeOf(query);
  scope = at;
  renderNav();
  const [rows, tree] = await Promise.all([api.models(scopeQuery(at)), api.sites()]);
  show(crumbs('models', at), el('h1', {}, '模型'),
    browse(tree, at, ['models'], {}, countsBy(tree, ['models']), '搜名称',
      rows.length ? el('table', {}, el('tr', {}, ['名称', '现场 / 产线', '任务', '版本数', '最新', '建立', '备注'].map((h) => el('th', {}, h))),
        rows.map((m) => el('tr', {},
          el('td', {}, el('a', { href: hrefOf(['m', m.name]) }, m.name)),
          el('td', {}, `${m.site} / ${m.line}`), el('td', {}, TASK_NAMES[m.task] || m.task),
          el('td', {}, String(m.version_count)), el('td', {}, m.latest ? mref(m.latest) : '-'),
          el('td', {}, `${m.created_by}，${when(m.created_at)}`), el('td', { class: 'muted' }, m.note))))
        : el('p', { class: 'muted' }, at.site || at.q ? '这里没有符合的模型。' : '还没有模型：用 fw model create 建，再 fw model register 登记版本。')));
}

async function modelPage(name) {
  const show = live();
  const [m, versions, evals] = await Promise.all([api.model(name), api.modelVersions(name), api.evals(name)]);
  const list = versions.slice().reverse();
  show(crumbs('models', m, m.name),
    el('h1', {}, m.name),
    el('dl', { class: 'kv' }, el('dt', {}, '现场 / 产线'), el('dd', {}, `${m.site} / ${m.line}`),
      el('dt', {}, '任务'), el('dd', {}, TASK_NAMES[m.task] || m.task),
      el('dt', {}, '建立'), el('dd', {}, `${m.created_by}，${when(m.created_at)}`), el('dt', {}, '备注'), el('dd', {}, m.note || '-')),
    el('h2', {}, '版本'),
    list.length ? el('table', {}, el('tr', {}, ['版本', '版本号', '训练集快照', 'ONNX', '代码提交', '标签', '登记人', '时间', '备注'].map((h) => el('th', {}, h))),
      list.map((v) => el('tr', {}, el('td', {}, mref(v.ref)), el('td', {}, labelChips(v)), el('td', {}, vref(v.trainset.ref)),
        el('td', { class: 'mono' }, `${v.onnx_sha256.slice(0, 12)}（${sizeText(v.onnx_size || 0)}）`),
        el('td', { class: 'mono' }, v.code_commit ? v.code_commit.slice(0, 10) + (v.code_dirty ? '*' : '') : '-'),
        el('td', {}, v.tags.map((t) => el('span', { class: 'tag' }, t))),
        el('td', {}, v.created_by), el('td', {}, when(v.created_at)), el('td', { class: 'muted' }, v.note))))
      : el('p', { class: 'muted' }, '还没有版本；用 fw model register 登记。'),
    el('h2', {}, '评估'), evalTable(evals.slice().reverse(), true),
    list.length && can('models.write') ? await labelForm(m.name, list, true, () => modelPage(name)) : null);
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
  show(crumbs('models', mv, [mv.model, hrefOf(['m', mv.model])], `@${mv.number}`),
    el('h1', {}, mv.ref, labelChips(mv), ...mv.tags.map((t) => [' ', el('span', { class: 'tag' }, t)])),
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
    can('jobs.write') ? await submitForms(mv) : null);
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
  const [model, number] = e.model_version.ref.split('@');
  show(crumbs('models', null, [model, hrefOf(['m', model])], [`@${number}`, hrefOf(['mv', model, number])], `评估 #${e.id}`),
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
  show(crumbs('models', null, '比较评估'),
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
  show(crumbs('jobs'), el('div', { class: 'row' }, el('h1', { style: 'flex:1' }, '任务'), '状态', pick),
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
    cancel.hidden = !can('jobs.write') || !(j.status === 'queued' || j.status === 'running') || j.cancel_requested;
  };
  show(crumbs('jobs', null, `任务 ${j.id}`),
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
  show(crumbs('predictions'), el('h1', {}, '预测集'),
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
  show(crumbs('predictions', null, `#${set.id}`),
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

// The files of a folder dragged onto the page, from its FileSystemDirectoryEntry: the folder's name, the
// files and their paths inside the folder (what relPaths gives for a picked folder).
export async function readDropped(dir) {
  const found = [];
  const walk = async (d) => {
    const reader = d.createReader();
    for (;;) {  // readEntries hands the entries over a batch at a time, then an empty one
      const batch = await new Promise((ok, no) => reader.readEntries(ok, no));
      if (!batch.length) return;
      for (const e of batch) if (e.isDirectory) await walk(e); else if (e.isFile) found.push(e);
    }
  };
  await walk(dir);
  const files = new Array(found.length);
  await inParallel(found, 32, async (e, i) => { files[i] = await new Promise((ok, no) => e.file(ok, no)); });
  const cut = dir.fullPath.length + 1;  // fullPath is "/<folder>/sub/a.png"
  return { folder: dir.name, files, paths: found.map((e) => e.fullPath.slice(cut)) };
}

// Where a folder is given: dragged onto the box, or chosen with the button. Dragging skips the browser's
// folder dialog and its "upload N files?" prompt, which on some Windows machines take seconds whatever the
// folder holds. The box's files / paths / folder hold what was given; a 'change' event says new ones came.
function folderInput() {
  const input = el('input', { type: 'file', webkitdirectory: true, multiple: true });
  const status = el('span', {}, '把目录拖到这里');
  const box = el('div', { class: 'drop' }, icon('folder'), status, el('label', { class: 'choose' }, '或者选择目录', input));
  let disabled = false;
  let shown = status.textContent;
  let reads = 0;  // each choice or drop starts a new read; an older one that finishes later is dropped
  const say = (text, cls = '') => { status.textContent = text; status.className = cls; };
  const reading = () => { shown = status.textContent; box.classList.add('reading'); say('正在读取目录…'); return ++reads; };
  const pick = (files, paths, folder) => {
    Object.assign(box, { files, paths, folder });
    box.classList.remove('reading');
    say(files.length ? `${folder}/ · ${files.length} 个文件` : `${folder}/ 是空的`, files.length ? 'got' : 'bad');
  };
  Object.assign(box, { files: [], paths: [], folder: '' });
  Object.defineProperty(box, 'disabled', {
    get: () => disabled,
    set: (on) => { disabled = on; input.disabled = on; box.classList.toggle('off', on); },
  });
  input.addEventListener('click', reading);
  input.addEventListener('cancel', () => { box.classList.remove('reading'); say(shown); });
  input.addEventListener('change', () => {  // then the event goes on up to the box, where the page listens
    reads++;
    const files = [...input.files];
    pick(files, relPaths(files), files.length ? (files[0].webkitRelativePath || '').split('/')[0] : '');
  });
  box.addEventListener('dragover', (ev) => {
    ev.preventDefault();
    if (!disabled) box.classList.add('over');
  });
  box.addEventListener('dragleave', (ev) => { if (!box.contains(ev.relatedTarget)) box.classList.remove('over'); });
  box.addEventListener('drop', (ev) => {
    ev.preventDefault();
    box.classList.remove('over');
    if (disabled) return;
    const items = [...((ev.dataTransfer && ev.dataTransfer.items) || [])].filter((i) => i.kind === 'file');
    const entries = items.map((i) => (i.webkitGetAsEntry ? i.webkitGetAsEntry() : null)).filter(Boolean);
    if (entries.length !== 1 || !entries[0].isDirectory) {
      say('拖进来的要是一个目录：不是文件，也不是几个目录一起', 'bad');
      return;
    }
    const mine = reading();
    readDropped(entries[0]).then(({ folder, files, paths }) => {
      if (mine !== reads) return;
      pick(files, paths, folder);
      box.dispatchEvent(new Event('change'));
    }).catch((e) => {
      if (mine !== reads) return;
      box.classList.remove('reading');
      say(`读不了这个目录：${e.message}`, 'bad');
    });
  });
  return box;
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
  const picker = folderInput();
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

  // Each pick or check box starts a new round; an answer, hashing or plan of an older round is dropped, and the
  // controls stay locked while hashing and uploading, so what is uploaded is always what was checked.
  let round = 0;
  const lock = (on) => { for (const c of [picker, allowOther, force]) c.disabled = on; };

  const check = async () => {
    if (!files.length) return;
    const mine = ++round;
    const picked = files;
    const pickedPaths = paths;
    result.replaceChildren(el('p', { class: 'muted' }, `检查 ${picked.length} 个文件…`));
    const r = await api.folderCheck(pickedPaths, allowOther.checked, force.checked);
    if (mine !== round) return;
    const junk = new Set(r.junk);
    const keep = picked.map((f, i) => [f, pickedPaths[i]]).filter(([, p]) => !junk.has(p));
    const folder = (picked[0].webkitRelativePath || '').split('/')[0];
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
    const go = el('button', { class: 'primary', onclick: () => hashAndCompare(mine, keep, folder, report)
      .catch((e) => { if (mine === round) result.replaceChildren(...present([report, errorBox(e)])); }) }, '算哈希，和最新版本比较');
    result.replaceChildren(...present([report, el('div', { class: 'row' }, go)]));
  };

  const hashAndCompare = async (mine, keep, folder, report) => {
    if (mine !== round) return;
    const progress = bar();
    result.replaceChildren(...present([report, progress]));
    uploading = true;
    lock(true);
    let digests;
    try {
      digests = await hashAll(keep.map(([f]) => f), (a, b) => progress.set(a, b, `算哈希 ${sizeText(a)} / ${sizeText(b)}`));
    } finally { uploading = false; lock(false); }
    if (mine !== round) return;
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
    if (mine !== round) return;
    const start = el('button', { class: 'primary', onclick: () => send(mine, entries, manifest, manifestDigest, number, folder, report) }, `上传并建 ${d.name}@${number}`);
    result.replaceChildren(...present([report,
      el('p', {}, latest ? `相比 ${latest.ref}：新增 ${change.added.length}，删除 ${change.deleted.length}，修改 ${change.modified.length}` : `这是 ${d.name} 的第一个版本`),
      change.deleted.length ? el('div', { class: 'error' }, `会删掉 ${latest.ref} 里的 ${change.deleted.length} 个文件（新版本只含这个目录里的文件），例如：\n`
        + change.deleted.slice(0, 5).join('\n')) : null,
      el('p', {}, `新版本 ${d.name}@${number}：${entries.length} 个文件，${sizeText(total)}`),
      el('div', { class: 'row' }, start)]));
  };

  const send = async (mine, entries, manifest, manifestDigest, number, folder, report) => {
    if (mine !== round) return;
    const here = location.hash;
    const progress = bar();
    result.replaceChildren(...present([report, progress]));
    uploading = true;
    lock(true);
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
      const v = await api.createVersion(d.name, { manifest_sha256: manifestDigest, parent_id: latest ? latest.id : null,
        expect_latest_id: latest ? latest.id : null, source: `网页上传 ${folder}`, note: note.value.trim() });
      uploading = false;
      if (location.hash === here) location.hash = hrefOf(['v', d.name, String(v.number)]);  // not if they went elsewhere
    } catch (e) {
      result.replaceChildren(...present([report, errorBox(e)]));
    } finally { uploading = false; lock(false); }
  };

  picker.addEventListener('change', () => {
    files = picker.files;
    paths = picker.paths;
    check().catch((e) => result.replaceChildren(errorBox(e)));
  });
  for (const box of [allowOther, force]) box.addEventListener('change', () => check().catch((e) => result.replaceChildren(errorBox(e))));
  show(crumbs('datasets', d, [d.name, hrefOf(['d', d.name])], '上传'),
    el('h1', {}, `上传到 ${d.name}`),
    el('p', { class: 'muted' }, ['只想加一个新目录、不动池里原有的文件？用 ', el('a', { href: hrefOf(['add', d.name]) }, '上传新增目录'), '。']),
    el('p', { class: 'muted' }, `选一个目录：目录里的全部文件成为新版本（${latest ? `父版本 ${latest.ref}；目录里没有的文件在新版本里就是删除` : '第一个版本'}）。`
      + '和 fw dataset import 一样检查、按哈希去重（服务器上已有的不再上传），建好后冻结。大批数据（几万张）还是在服务器上用 fw dataset import 快。'
      + '浏览器看不到读不了的子目录和链接（命令行会拦下），上传前看清「会删掉」的提示。'),
    el('div', { class: 'panel form' },
      el('div', { class: 'label' }, el('span', {}, '目录'), picker),
      el('label', {}, '备注', note),
      el('label', { class: 'check' }, allowOther, '图片和标注以外的文件也一起入库（--allow-other-files）'),
      el('label', { class: 'check' }, force, '同名不同扩展名的图片也入库（--force）')),
    result);
}

// What a change preview (the server's plan for adding, replacing or removing folders) says, as page nodes.
function changeReport(p) {
  const list = (title, part, cls = '', fmt = (x) => (Array.isArray(x) ? x.join('  ') : x)) => (part && part.count ? el('details', { class: cls },
    el('summary', {}, `${title}：${part.count}`),
    el('ul', { class: 'mono' }, part.examples.map((x) => el('li', {}, fmt(x)))),
    part.count > part.examples.length ? el('p', { class: 'muted' }, `只列出前 ${part.examples.length} 个`) : null) : null);
  const boxes = (n) => (n === null || n === undefined ? '没有标注' : `${n} 个目标`);
  const dup = p.duplicates;
  return [
    el('ul', {}, p.ops.map((o) => el('li', {}, o.op === 'add' ? `新增 ${o.dir}/：${o.file_count} 个文件，${sizeText(o.total_bytes)}`
      : o.op === 'replace' ? `替换 ${o.dir}/：原有 ${o.removed_count} 个文件换成这 ${o.file_count} 个，${sizeText(o.total_bytes)}`
        : `去掉 ${o.dir}/：${o.removed_count} 个文件`))),
    list('文件名用不了', p.checks.bad_paths, 'bad'), list('路径冲突', p.checks.collisions, 'bad'),
    list('同名不同扩展名的图片', p.checks.same_stem_images, 'warn'), list('图片和标注以外的文件', p.checks.others, 'warn'),
    list('没有标注的图片（当正常图）', p.checks.unlabeled_images), list('找不到图片的标注', p.checks.orphan_labels),
    list('说明文件（一起入库）', p.checks.meta),
    list('提醒：和池里已有图片内容完全相同（只存一份，但各算一个样本）', dup.in_pool, 'warn', (x) => `${x.new} = ${x.existing}`),
    list('提醒：新增的目录里内容完全相同的图片（组）', dup.in_new, 'warn'),
    list('提醒：图片相同、标注不一样（训练时会互相矛盾）', dup.labels_differ, 'warn',
      (x) => `${x.new}（${boxes(x.new_boxes)}） 和 ${x.existing}（${boxes(x.existing_boxes)}）`),
    dup.uncompared ? el('p', { class: 'muted' }, `还有 ${dup.uncompared} 对相同的图片太多，没有比较标注`) : null,
    p.changes.deleted.count ? el('div', { class: 'error' }, `新版本里会少掉 ${p.parent} 的 ${p.changes.deleted.count} 个文件（${p.parent} 本身不变），例如：\n`
      + p.changes.deleted.examples.slice(0, 5).join('\n')) : null,
    p.reasons.length ? el('div', { class: 'error' }, '不能建版本：\n' + p.reasons.join('\n')) : null,
    el('p', {}, `新版本 ${p.ref}：${p.file_count} 个文件，${sizeText(p.total_bytes)}`
      + (p.parent ? `（在 ${p.parent} 的基础上）` : '（第一个版本）'))];
}

// Add one folder to the pool: only that folder is read and uploaded, the server builds the version.
async function addPage(name) {
  const show = live();
  const d = await api.dataset(name);
  if (d.kind === 'trainset') throw new Error(`${d.name} 是训练集快照，不能上传；快照用 fw trainset create 建`);
  const picker = folderInput();
  const target = el('input', { type: 'text', placeholder: '选了目录以后自动填目录名，可以改，例如 20261104 或 night/20261104' });
  const replace = el('input', { type: 'checkbox' });
  const allowOther = el('input', { type: 'checkbox' });
  const force = el('input', { type: 'checkbox' });
  const note = el('input', { type: 'text', placeholder: '这批数据是什么（来源、日期、批次），写进版本备注' });
  const result = el('div');
  let files = [];
  let paths = [];
  let round = 0;  // as on the upload page: an answer of an older round is dropped
  const controls = [picker, target, replace, allowOther, force];
  const lock = (on) => { for (const c of controls) c.disabled = on; };
  const listOf = (title, part, cls = '') => (part && part.count ? el('details', { class: cls },
    el('summary', {}, `${title}：${part.count}`),
    el('ul', { class: 'mono' }, part.examples.map((x) => el('li', {}, Array.isArray(x) ? x.join('  ') : x)))) : null);

  const check = async () => {
    if (!files.length) return;
    const mine = ++round;
    const picked = files;
    const pickedPaths = paths;
    result.replaceChildren(el('p', { class: 'muted' }, `检查 ${picked.length} 个文件…`));
    const r = await api.folderCheck(pickedPaths, allowOther.checked, force.checked);
    if (mine !== round) return;
    const junk = new Set(r.junk);
    const keep = picked.map((f, i) => [f, pickedPaths[i]]).filter(([, p]) => !junk.has(p));
    const report = [
      el('p', {}, `${r.files} 个文件：图片 ${r.images.count}，标注 ${r.labels.count}，说明文件 ${r.meta.count}，其他 ${r.others.count}`
        + (r.junk.length ? `；系统文件 ${r.junk.length} 个不入库` : '')),
      listOf('文件名用不了', r.bad_paths, 'bad'), listOf('路径冲突', r.collisions, 'bad'),
      listOf('同名不同扩展名的图片', r.same_stem_images, 'warn'), listOf('图片和标注以外的文件', r.others, 'warn')];
    if (r.reasons.length) {
      result.replaceChildren(...present([report, el('div', { class: 'error' }, '不能入库：\n' + r.reasons.join('\n'))]));
      return;
    }
    if (!keep.length) {
      result.replaceChildren(...present([report, el('div', { class: 'error' }, '目录里没有文件')]));
      return;
    }
    const go = el('button', { class: 'primary', onclick: () => hashAndPreview(mine, keep, report)
      .catch((e) => { if (mine === round) result.replaceChildren(...present([report, errorBox(e)])); }) }, '算哈希，预览新版本');
    result.replaceChildren(...present([report, el('div', { class: 'row' }, go)]));
  };

  const hashAndPreview = async (mine, keep, report) => {
    if (mine !== round) return;
    const dir = target.value.trim();
    if (!dir) throw new Error('要写放在池里的哪个目录');
    const progress = bar();
    result.replaceChildren(...present([report, progress]));
    uploading = true;
    lock(true);
    let entries;
    let body;
    let plan;
    try {
      const digests = await hashAll(keep.map(([f]) => f), (a, b) => progress.set(a, b, `算哈希 ${sizeText(a)} / ${sizeText(b)}`));
      entries = keep.map(([f, p], i) => ({ path: p, sha256: digests[i], size: f.size, file: f }));
      const manifest = new Blob([manifestText(entries)]);
      const [manifestDigest] = await hashAll([manifest], () => {});
      await api.putObject(manifestDigest, manifest);  // the folder's file list: the server needs it for the preview
      body = { add: [{ dir, manifest_sha256: manifestDigest, replace: replace.checked }], remove: [],
        allow_other_files: allowOther.checked, force: force.checked };
      plan = await api.previewChange(d.name, body);
      if (plan.duplicates.not_uploaded) {  // same images whose labels the server lacks: send the labels, ask again
        const labels = entries.filter((e) => ['.json', '.txt'].includes(ext(e.path)));
        const lacking = new Set();
        for (let i = 0; i < labels.length; i += 20000) {
          for (const x of await api.missing(labels.slice(i, i + 20000).map((e) => [e.sha256, e.size]))) lacking.add(x);
        }
        const todo = labels.filter((e) => lacking.delete(e.sha256));
        let sent = 0;
        await inParallel(todo, 4, async (e) => {
          await api.putObject(e.sha256, e.file);
          progress.set(++sent, todo.length, `上传标注用于比较 ${sent} / ${todo.length}`);
        });
        plan = await api.previewChange(d.name, body);
      }
    } finally { uploading = false; lock(false); }
    if (mine !== round) return;
    const start = plan.reasons.length ? null : el('button', { class: 'primary', onclick: () => send(mine, entries, body, plan, report) }, `上传并建 ${plan.ref}`);
    result.replaceChildren(...present([report, changeReport(plan), start ? el('div', { class: 'row' }, start) : null]));
  };

  const send = async (mine, entries, body, plan, report) => {
    if (mine !== round) return;
    const here = location.hash;
    const progress = bar();
    result.replaceChildren(...present([report, progress]));
    uploading = true;
    lock(true);
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
      const made = await api.createChange(d.name, { ...body, expect_latest_id: plan.expect_latest_id,
        source: `网页上传新增目录 ${body.add[0].dir}`, note: note.value.trim() });
      uploading = false;
      if (location.hash === here) location.hash = hrefOf(['v', d.name, String(made.version.number)]);
    } catch (e) {
      result.replaceChildren(...present([report, errorBox(e)]));
    } finally { uploading = false; lock(false); }
  };

  const again = () => check().catch((e) => result.replaceChildren(errorBox(e)));
  picker.addEventListener('change', () => {
    files = picker.files;
    paths = picker.paths;
    if (files.length) target.value = picker.folder;
    again();
  });
  for (const box of [replace, allowOther, force]) box.addEventListener('change', again);
  let typing = null;  // the name is typed letter by letter: check once it rests, not on every key
  target.addEventListener('input', () => {
    round++;  // what is shown belongs to the old name
    clearTimeout(typing);
    typing = setTimeout(again, 400);
  });
  show(crumbs('datasets', d, [d.name, hrefOf(['d', d.name])], '上传新增目录'),
    el('h1', {}, `往 ${d.name} 里加一个目录`),
    el('p', { class: 'muted' }, `选一个目录，比如今天采集的那一天：只上传这个目录，新版本 = ${d.latest ? d.latest.ref : '空'} + 这个目录，池里原有的文件不动。`
      + '池里已经有同名目录时会拦下，除非勾选「替换已有目录」。和池里重复的图片、标注不一致的重复图片只提醒，不拦。和 fw dataset add 是同一件事。'),
    el('div', { class: 'panel form' },
      el('div', { class: 'label' }, el('span', {}, '目录'), picker),
      el('label', {}, '放在池里的目录', target),
      el('label', {}, '备注', note),
      el('label', { class: 'check' }, replace, '替换已有目录：池里已有这个目录时，整个换成这一份（--replace）'),
      el('label', { class: 'check' }, allowOther, '图片和标注以外的文件也一起入库（--allow-other-files）'),
      el('label', { class: 'check' }, force, '同名不同扩展名的图片也入库（--force）')),
    result);
}

// Take folders out of the pool: the next version is the newest one without them.
async function removePage(name) {
  const show = live();
  const d = await api.dataset(name);
  if (d.kind === 'trainset') throw new Error(`${d.name} 是训练集快照，不能改；快照用 fw trainset create 建`);
  if (!d.latest) throw new Error(`${d.name} 还没有版本`);
  const latest = d.latest;
  const counts = new Map();
  for (const f of await filesOf(latest)) {
    const slash = f.path.indexOf('/');
    if (slash > 0) counts.set(f.path.slice(0, slash), (counts.get(f.path.slice(0, slash)) || 0) + 1);
  }
  const boxes = [...counts.entries()].sort((x, y) => x[0].localeCompare(y[0]))
    .map(([dir, n]) => [dir, n, el('input', { type: 'checkbox', value: dir })]);
  const deeper = el('input', { type: 'text', placeholder: '更里面的目录，例如 night/20261104；多个用逗号隔开（可不填）' });
  const note = el('input', { type: 'text', placeholder: '为什么去掉，写进版本备注' });
  const result = el('div');
  let round = 0;
  const body = () => ({ add: [], remove: [...boxes.filter(([, , b]) => b.checked).map(([dir]) => dir),
    ...deeper.value.split(/[,，]/).map((x) => x.trim()).filter(Boolean)] });
  const preview = async () => {
    const mine = ++round;
    const asked = body();
    if (!asked.remove.length) { result.replaceChildren(el('div', { class: 'error' }, '先选要去掉的目录')); return; }
    const plan = await api.previewChange(d.name, asked);
    if (mine !== round) return;
    const go = plan.reasons.length ? null : el('button', { class: 'primary', onclick: async () => {
      const here = location.hash;
      try {
        const made = await api.createChange(d.name, { ...asked, expect_latest_id: plan.expect_latest_id,
          source: '网页去掉目录', note: note.value.trim() });
        if (location.hash === here) location.hash = hrefOf(['v', d.name, String(made.version.number)]);
      } catch (e) { result.replaceChildren(errorBox(e)); }
    } }, `建 ${plan.ref}`);
    result.replaceChildren(...present([changeReport(plan), go ? el('div', { class: 'row' }, go) : null]));
  };
  const run = () => preview().catch((e) => result.replaceChildren(errorBox(e)));
  show(crumbs('datasets', d, [d.name, hrefOf(['d', d.name])], '去掉目录'),
    el('h1', {}, `从 ${d.name} 里去掉目录`),
    el('p', { class: 'muted' }, `新版本 = ${latest.ref} 去掉选中的目录。${latest.ref} 和更早的版本不变，用它们建的训练集快照也不变，随时可以取回。和 fw dataset remove 是同一件事。`),
    el('div', { class: 'panel form' },
      boxes.length ? boxes.map(([dir, n, box]) => el('label', { class: 'check' }, box, `${dir}/（${n} 个文件）`))
        : el('p', { class: 'muted' }, `${latest.ref} 的文件都在最上层，没有目录可以去掉`),
      el('label', {}, '其他目录', deeper),
      el('label', {}, '备注', note),
      el('div', { class: 'row' }, el('button', { class: 'primary', onclick: run }, '预览'))),
    result);
}

// --- accounts, tokens, the record of who did what ------------------------------------------------------------
// --- labelling batches: upload, review, import ---------------------------------------------------------
const BATCH_CLASS = { open: '', review: 'warn', rejected: 'bad', approved: 'ok', imported: 'muted', closed: 'muted' };
const bref = (name) => el('a', { class: 'ref', href: hrefOf(['b', name]) }, name);

export function boxChangeText(row) {
  if (row.unreadable) return '标注读不出框，没有逐框比较';
  const parts = [['added', '加框'], ['removed', '删框'], ['moved', '挪动'], ['relabeled', '改类别']]
    .filter(([k]) => row[k]).map(([k, t]) => `${t} ${row[k]}`);
  const text = parts.length ? `${parts.join('、')}（${row.old_boxes} → ${row.new_boxes} 个框）` : '';
  return row.image_changed ? `图片本身换了${text ? `；${text}` : ''}` : text;
}

async function batchesPage(query) {
  const show = live();
  const at = scopeOf(query);
  scope = at;
  renderNav();
  const view = query.get('view') || (can('batches.review') ? 'todo' : 'mine');
  const [{ batches }, tree] = await Promise.all([
    api.batches(scopeQuery(at, view === 'todo' ? { todo: true } : view === 'mine' ? { mine: true } : {})), api.sites()]);
  const tabs = [can('batches.review') ? ['todo', '待我复核'] : null, ['mine', '我上传的'], ['all', '全部']].filter(Boolean)
    .map(([k, t]) => el('a', { href: hrefOf(['batches'], { view: k, site: at.site, line: at.line, q: at.q }), class: k === view ? 'on' : '' }, t));
  const rows = batches.map((b) => el('tr', {},
    el('td', {}, bref(b.name)), el('td', {}, `第 ${b.revision} 版`),
    el('td', {}, el('span', { class: `tag ${BATCH_CLASS[b.status]}` }, b.status_text)),
    el('td', {}, `${b.pool} / ${b.dir}/`), el('td', {}, `${b.file_count} 个文件，${sizeText(b.total_bytes)}`),
    el('td', {}, b.owner), el('td', {}, b.labeler), el('td', {}, b.reviewer || '-'), el('td', {}, when(b.updated_at))));
  const empty = at.site || at.q ? '这里没有符合的批次。'
    : { todo: '没有待你复核的批次。', mine: '你还没有上传过批次。', all: '还没有批次。' }[view];
  show(crumbs('batches', at), el('h1', {}, '标注批次'),
    el('p', { class: 'muted' }, '一个批次是一个要进数据池的目录：上传（每次上传都冻结成一版，不进数据池）、提交给复核人、退回或通过，通过以后由算法工程师入库。'),
    browse(tree, at, ['batches'], { view }, countsBy(tree, ['batches']), '搜批次名称',
      el('div', { class: 'row tabs' }, tabs, can('batches.write') ? el('a', { class: 'button primary', href: '#/batch-new' }, '上传新批次') : null),
      rows.length ? el('table', {}, el('tr', {}, ['批次', '版次', '状态', '入库到', '大小', '上传账号', '标注人', '复核人', '最近变化']
        .map((h) => el('th', {}, h))), rows) : el('p', { class: 'muted' }, empty),
      el('p', { class: 'muted small' }, '左边的数字是还在办的批次（没入库、没关闭的）。')));
}

// Pick a folder, check it, hash it, upload what the server lacks and its file list; done(manifest digest)
// then makes the batch, the revision or the approval. Returns the node to show.
function folderSender({ controls, allowOther, force, onPick, label, ready, done }) {
  const picker = folderInput();
  const result = el('div');
  let files = [];
  let paths = [];
  let round = 0;
  const all = [picker, allowOther, force, ...controls];
  const lock = (on) => { for (const c of all) c.disabled = on; };
  const listOf = (title, part, cls = '') => (part && part.count ? el('details', { class: cls },
    el('summary', {}, `${title}：${part.count}`),
    el('ul', { class: 'mono' }, part.examples.map((x) => el('li', {}, Array.isArray(x) ? x.join('  ') : x)))) : null);

  const check = async () => {
    if (!files.length) return;
    const mine = ++round;
    const picked = files;
    const pickedPaths = paths;
    result.replaceChildren(el('p', { class: 'muted' }, `检查 ${picked.length} 个文件…`));
    const r = await api.folderCheck(pickedPaths, allowOther.checked, force.checked);
    if (mine !== round) return;
    const junk = new Set(r.junk);
    const keep = picked.map((f, i) => [f, pickedPaths[i]]).filter(([, p]) => !junk.has(p));
    const report = [
      el('p', {}, `${r.files} 个文件：图片 ${r.images.count}，标注 ${r.labels.count}，说明文件 ${r.meta.count}，其他 ${r.others.count}`
        + (r.junk.length ? `；系统文件 ${r.junk.length} 个不上传` : '')
        + (r.unlabeled_images.count ? `；没有标注的图片 ${r.unlabeled_images.count} 张` : '')),
      listOf('文件名用不了', r.bad_paths, 'bad'), listOf('路径冲突', r.collisions, 'bad'),
      listOf('同名不同扩展名的图片', r.same_stem_images, 'warn'), listOf('图片和标注以外的文件', r.others, 'warn')];
    const problem = r.reasons.length ? `不能上传：\n${r.reasons.join('\n')}` : !r.images.count ? '目录里没有图片' : null;
    if (problem) {
      result.replaceChildren(...present([report, el('div', { class: 'error' }, problem)]));
      return;
    }
    const go = el('button', { class: 'primary', id: 'send-folder', onclick: () => send(mine, keep, report)
      .catch((e) => { if (mine === round) result.replaceChildren(...present([report, errorBox(e)])); }) }, label);
    result.replaceChildren(...present([report, el('div', { class: 'row' }, go)]));
  };

  const send = async (mine, keep, report) => {
    if (mine !== round) return;
    ready();  // what the form must say, before the folder is hashed and uploaded
    const progress = bar();
    result.replaceChildren(...present([report, progress]));
    uploading = true;
    lock(true);
    try {
      const digests = await hashAll(keep.map(([f]) => f), (a, b) => progress.set(a, b, `算哈希 ${sizeText(a)} / ${sizeText(b)}`));
      const entries = keep.map(([f, p], i) => ({ path: p, sha256: digests[i], size: f.size, file: f }));
      const unique = new Map();
      for (const e of entries) if (!unique.has(e.sha256)) unique.set(e.sha256, e);
      const every = [...unique.values()];
      const missing = new Set();
      for (let i = 0; i < every.length; i += 20000) {
        for (const x of await api.missing(every.slice(i, i + 20000).map((e) => [e.sha256, e.size]))) missing.add(x);
      }
      const todo = every.filter((e) => missing.has(e.sha256));
      const bytes = todo.reduce((a, e) => a + e.size, 0);
      let sent = 0;
      progress.set(0, bytes, `上传 ${todo.length} 个文件（其余 ${every.length - todo.length} 个服务器上已经有了）`);
      await inParallel(todo, 4, async (e) => {
        await api.putObject(e.sha256, e.file);
        sent += e.size;
        progress.set(sent, bytes, `上传 ${sizeText(sent)} / ${sizeText(bytes)}`);
      });
      const manifest = new Blob([manifestText(entries)]);
      const [manifestDigest] = await hashAll([manifest], () => {});
      await api.putObject(manifestDigest, manifest);
      await done(manifestDigest);
    } finally { uploading = false; lock(false); }
  };

  const again = () => check().catch((e) => result.replaceChildren(errorBox(e)));
  picker.addEventListener('change', () => {
    files = picker.files;
    paths = picker.paths;
    if (files.length && onPick) onPick(picker.folder);
    again();
  });
  for (const box of [allowOther, force]) box.addEventListener('change', again);
  return { picker, result };
}

const reviewerSelect = (names, first) => el('select', { id: 'reviewer' },
  el('option', { value: '' }, first), names.map((n) => el('option', { value: n }, n)));

// A new batch (name null), the owner's next revision, or (approve) the reviewer's corrected folder.
async function batchUploadPage(name, query) {
  const show = live();
  const approve = query.get('approve') === '1';
  const b = name ? await api.batch(name) : null;
  if (b && !b.actions.includes(approve ? 'approve' : 'revise')) {
    throw new Error(`${b.name} 现在是「${b.status_text}」，或者不是你的批次，不能${approve ? '通过' : '上传新的一版'}`);
  }
  const pools = b ? [] : (await api.datasets()).filter((d) => d.kind !== 'trainset');
  const names = approve ? [] : await api.reviewers();
  const pool = el('select', { id: 'pool' }, pools.map((d) => el('option', { value: d.name }, d.name)));
  if (query.get('pool')) pool.value = query.get('pool');
  const batchName = el('input', { type: 'text', id: 'batch-name', placeholder: '字母、数字和 . _ -，例如 SES-TX-20261104' });
  const dir = el('input', { type: 'text', id: 'batch-dir', placeholder: '入库后在池里的目录名，选了目录以后自动填' });
  const labeler = el('input', { type: 'text', id: 'labeler', placeholder: approve ? '修改的人的姓名（必填）' : '标注人姓名（必填）' });
  const reviewer = reviewerSelect(names, b && b.reviewer ? '先不提交复核' : '先不提交复核（比如还没标完）');
  if (b && b.reviewer && names.includes(b.reviewer)) reviewer.value = b.reviewer;
  const note = el('input', { type: 'text', placeholder: b ? '这一版改了什么' : '这批数据是什么（来源、日期）' });
  const comment = el('input', { type: 'text', placeholder: '复核意见（可以不写）' });
  const allowOther = el('input', { type: 'checkbox' });
  const force = el('input', { type: 'checkbox' });
  const here = location.hash;
  const ready = () => {
    if (!labeler.value.trim()) throw new Error(approve ? '要写修改的人的姓名' : '要写标注人姓名');
    if (!b && !batchName.value.trim()) throw new Error('要写批次名称');
    if (!b && !dir.value.trim()) throw new Error('要写池里的目录名');
  };
  const done = async (digest) => {
    const common = { manifest_sha256: digest, labeler: labeler.value.trim(), allow_other_files: allowOther.checked, force: force.checked };
    let made;
    if (approve) {
      made = await api.batchStep(b.name, 'approve', { ...common, comment: comment.value.trim(), revision: b.revision, source: '网页复核时上传' });
    } else if (b) {
      made = await api.reviseBatch(b.name, { ...common, note: note.value.trim(), reviewer: reviewer.value || null,
        expect_latest_id: b.latest_id, source: '网页上传批次新的一版' });
    } else {
      made = await api.createBatch({ ...common, name: batchName.value.trim(), pool: pool.value, dir: dir.value.trim(),
        note: note.value.trim(), reviewer: reviewer.value || null, source: '网页上传批次' });
    }
    uploading = false;
    if (location.hash === here) location.hash = hrefOf(['b', made.name]);
  };
  const { picker, result } = folderSender({ controls: [pool, batchName, dir, labeler, reviewer, note, comment], allowOther, force,
    label: approve ? '上传并通过' : b ? '上传新的一版' : '上传批次',
    onPick: b ? null : (folder) => {
      dir.value = folder;
      if (!batchName.value.trim()) batchName.value = `${pool.value}-${folder}`.replace(/[^A-Za-z0-9._-]/g, '');
    },
    ready, done });
  const title = approve ? `通过 ${b.name}，并带上我修改后的内容` : b ? `给 ${b.name} 上传新的一版` : '上传新批次';
  show(crumbs('batches', b, b ? [b.name, hrefOf(['b', b.name])] : null, approve ? '修改后通过' : b ? '新的一版' : '上传'),
    el('h1', {}, title),
    el('p', { class: 'muted' }, approve
      ? `选你修改后的整个目录（${b.dir}/）：它成为第 ${b.revision + 1} 版，记下是你改的，并且直接通过。`
      : b ? `选批次的整个目录现在的样子：冻结成第 ${b.revision + 1} 版，之前的各版都保留。没变的文件不会重新上传。`
        : '选一个目录，原始图片或已经标注的都行：冻结成批次的第 1 版，不会进数据池；复核通过以后由算法工程师入库。'),
    el('div', { class: 'panel form' },
      b ? null : el('label', {}, '进哪个数据池', pool),
      el('div', { class: 'label' }, el('span', {}, '目录'), picker),
      b ? null : el('label', {}, '批次名称', batchName),
      b ? null : el('label', {}, '池里的目录名', dir),
      el('label', {}, approve ? '修改人姓名' : '标注人姓名', labeler),
      approve ? el('label', {}, '复核意见', comment) : el('label', {}, '复核人', reviewer),
      approve ? null : el('label', {}, '说明', note),
      el('label', { class: 'check' }, allowOther, '图片和标注以外的文件也一起上传'),
      el('label', { class: 'check' }, force, '同名不同扩展名的图片也上传')),
    result);
}

function compareTable(c, limit = 200) {
  const [name, number] = c.new.split('@');
  const rows = c.images.examples.slice(0, limit).map((r) => el('tr', {},
    el('td', { class: 'mono' }, el('a', { href: hrefOf(['img', name, number, ...r.image.split('/')]) }, r.image)),
    el('td', {}, boxChangeText(r))));
  const f = c.files;
  return el('div', {},
    el('p', {}, `${c.new} 相比 ${c.old}：文件新增 ${f.added.count}、修改 ${f.modified.count}、删除 ${f.deleted.count}、改名 ${f.renamed.count}；`
      + `图片 ${c.images.old} → ${c.images.new} 张（新增 ${c.images.added}，去掉 ${c.images.removed}）。`),
    el('p', { id: 'box-changes' }, `标注有变化的图片 ${c.images.changed} 张：加框 ${c.boxes.added}、删框 ${c.boxes.removed}、挪动 ${c.boxes.moved}、改类别 ${c.boxes.relabeled}。`,
      ' ', el('a', { href: hrefOf(['diff', name], { old: String(c.old_id), new: String(c.new_id) }) }, '看全部文件变化')),
    rows.length ? el('table', {}, el('tr', {}, ['图片', '标注变化'].map((h) => el('th', {}, h))), rows) : null,
    c.images.changed > rows.length ? el('p', { class: 'muted' }, `只列了前 ${rows.length} 张；全部用 fw batch diff ${name} 看。`) : null,
    c.uncompared ? el('p', { class: 'muted' }, `还有 ${c.uncompared} 张图片的标注太多，没有比较。`) : null);
}

async function batchPage(name, query) {
  const show = live();
  const msg = el('div');
  const say = (e) => msg.replaceChildren(errorBox(e));
  const draw = async () => {
    const b = await api.batch(name);
    const has = (a) => b.actions.includes(a);
    const newer = Number(query.get('new')) || b.revision;
    const older = Number(query.get('old')) || newer - 1;
    const compare = older >= 1 && older !== newer ? await api.compareBatch(b.name, older, newer) : null;
    const act = (fn) => async () => { try { msg.replaceChildren(); await fn(); await draw(); } catch (e) { say(e); } };
    const forms = [];
    if (has('revise')) {
      forms.push(el('div', { class: 'row' }, el('a', { class: 'button', href: hrefOf(['batch-up', b.name]) }, '上传新的一版'),
        el('span', { class: 'muted' }, `或者：fw batch get ${b.name}，用 LiteAnno 改好后 fw batch update ${b.name} <目录> --labeler <姓名>`)));
    }
    const pre = await api.batchPrelabels(b.name);
    if (pre.prelabels.some((r) => ['queued', 'running'].includes(r.status))) {
      const tick = async () => {  // while something is being typed: not now, but ask again
        if (['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement?.tagName)) refreshLater(tick, 10000);
        else await draw();
      };
      refreshLater(tick, 10000);
    }
    if (has('prelabel')) {
      const d = pre.defaults;
      const model = el('input', { type: 'text', id: 'prelabel-model', value: d.model ? d.model.ref : '',
        placeholder: '模型版本，例如 det-ses@3' });
      const runner = el('select', { id: 'prelabel-runner' }, d.runners.map((r) => el('option', { value: r }, r)));
      const conf = el('input', { type: 'text', id: 'prelabel-conf', placeholder: '0.1', style: 'width:5em' });
      forms.push(el('div', { class: 'panel form' },
        el('p', { class: 'muted' }, `预标注：让模型先给第 ${b.revision} 版的图画框，在 GPU 机上排队跑；跑完用 fw batch get ${b.name} --prelabel 取到本地改。`
          + (d.model ? `默认用 ${d.site}/${d.line} 现在发布的模型（${d.model.release}）。` : `${d.site}/${d.line} 还没有登记过发布，要写用哪个模型版本。`)),
        el('label', {}, '模型版本', model),
        d.runners.length ? el('label', {}, '推理环境', runner) : el('p', { class: 'bad' }, '还没有能推理的推理环境，请算法工程师在 GPU 机上登记。'),
        el('label', {}, '最低分数', conf),
        el('div', { class: 'row' }, el('button', { id: 'prelabel-go', disabled: !d.runners.length, onclick: act(async () => {
          const c = conf.value.trim();
          if (c && !(Number(c) > 0 && Number(c) < 1)) throw new Error('最低分数要在 0 和 1 之间');
          await api.prelabelBatch(b.name, { model: model.value.trim() || null, runner: runner.value || null,
            conf: c ? Number(c) : null, revision: b.revision });
        }) }, '跑预标注'))));
    }
    if (has('submit')) {
      const names = await api.reviewers();
      const who = reviewerSelect(names, '选复核人');
      if (b.reviewer && names.includes(b.reviewer)) who.value = b.reviewer;
      forms.push(el('div', { class: 'row' }, who, el('button', { class: 'primary', id: 'submit-review', onclick: act(async () => {
        if (!who.value) throw new Error('要选复核人');
        await api.batchStep(b.name, 'submit', { reviewer: who.value, revision: b.revision });
      }) }, `把第 ${b.revision} 版提交复核`)));
    }
    if (has('import')) {
      const replace = el('input', { type: 'checkbox', id: 'import-replace' });
      const out = el('div');
      const preview = async () => {
        try {
          const body = { replace: replace.checked };
          const plan = await api.batchStep(b.name, 'import/preview', body);
          const go = plan.reasons.length ? null : el('button', { class: 'primary', id: 'import-go', onclick: act(() => api.batchStep(b.name, 'import',
            { ...body, expect_latest_id: plan.expect_latest_id })) }, `入库，建 ${plan.ref}`);
          out.replaceChildren(...present([changeReport(plan), go ? el('div', { class: 'row' }, go) : null]));
        } catch (e) { out.replaceChildren(errorBox(e)); }
      };
      forms.push(el('div', { class: 'panel form' },
        el('p', {}, `复核通过了。入库：把第 ${b.revision} 版作为 ${b.dir}/ 加进 ${b.pool}，和「上传新增目录」是同样的检查，文件不用再上传。`),
        el('label', { class: 'check' }, replace, `替换已有目录：${b.pool} 里已有 ${b.dir}/ 时，整个换成这一份`),
        el('div', { class: 'row' }, el('button', { id: 'import-preview', onclick: preview }, '预览入库')), out));
    }
    if (has('approve') || has('reject')) {
      const comment = el('textarea', { id: 'review-comment', rows: 3, placeholder: '复核意见：退回时必填，写清楚哪里要改' });
      const images = el('textarea', { id: 'review-images', rows: 3, class: 'mono', placeholder: '有问题的图片，一行一个（批次目录里的相对路径），可以不填' });
      forms.push(el('div', { class: 'panel form' },
        el('p', { class: 'muted' }, has('approve') ? `复核第 ${b.revision} 版：` : `已经通过；入库之前发现问题还可以退回第 ${b.revision} 版：`, el('a', { href: hrefOf(['v', b.name, String(b.revision)]) }, '在网页里看图片和框'),
          `，或者 fw batch get ${b.name} 取到本地用 LiteAnno 打开。`),
        el('label', {}, '意见', comment), el('label', {}, '问题图片', images),
        el('div', { class: 'row' },
          has('approve') ? el('button', { class: 'primary', id: 'approve', onclick: act(() => api.batchStep(b.name, 'approve',
            { comment: comment.value.trim(), revision: b.revision })) }, '通过') : null,
          has('approve') ? el('a', { class: 'button', href: hrefOf(['batch-up', b.name], { approve: '1' }) }, '我改过了：上传修改后的目录并通过') : null,
          el('button', { id: 'reject', onclick: act(() => api.batchStep(b.name, 'reject', { comment: comment.value.trim(),
            images: images.value.split('\n').map((x) => x.trim()).filter(Boolean), revision: b.revision })) }, '退回'))));
    }
    if (has('close')) {
      forms.push(el('div', { class: 'row' }, el('button', { id: 'close-batch', onclick: act(async () => {
        const why = window.prompt(`关闭 ${b.name}？它不会再复核或入库，各版内容保留。可以写原因：`, '');
        if (why === null) return;
        await api.batchStep(b.name, 'close', { comment: why.trim() });
      }) }, '关闭这个批次')));
    }
    const revisions = b.revisions.slice().reverse().map((r) => el('tr', {},
      el('td', {}, el('a', { href: hrefOf(['v', b.name, String(r.number)]) }, `第 ${r.number} 版`)),
      el('td', {}, `${r.file_count} 个文件，${sizeText(r.total_bytes)}`),
      el('td', {}, r.labeler + (r.by_reviewer ? '（复核人修改）' : '')), el('td', {}, r.created_by), el('td', {}, when(r.created_at)),
      el('td', { class: 'muted' }, r.note),
      el('td', {}, r.number > 1 ? el('a', { href: hrefOf(['b', b.name], { old: String(r.number - 1), new: String(r.number) }) }, '和上一版比') : '')));
    const steps = b.steps.map((s) => el('tr', {},
      el('td', {}, when(s.at)), el('td', {}, s.actor),
      el('td', {}, s.step_text + (s.revision ? `第 ${s.revision} 版` : '') + (s.job_id ? `（任务 ${s.job_id}）` : '') + (s.step === 'submit' ? `，复核人 ${s.reviewer}${s.self_review ? '（自审）' : ''}` : ''),
        s.pool_version ? ['，成为 ', vref(s.pool_version)] : null),
      el('td', {}, s.comment, s.images.length ? el('ul', { class: 'mono' }, s.images.map((i) => el('li', {},
        el('a', { href: hrefOf(['img', b.name, String(s.revision), ...i.split('/')]) }, i)))) : null)));
    show(crumbs('batches', b, b.name),
      el('h1', {}, b.name, ' ', el('span', { class: `tag ${BATCH_CLASS[b.status]}`, id: 'batch-status' }, b.status_text)),
      el('p', {}, '入库后是 ', el('a', { href: hrefOf(['d', b.pool]) }, b.pool), ` 的 ${b.dir}/`,
        b.imported ? ['；已入库为 ', vref(b.imported)] : null,
        `。上传账号 ${b.owner}，复核人 ${b.reviewer || '还没有指定'}。`),
      msg, forms,
      el('h2', {}, '各版'),
      el('table', {}, el('tr', {}, ['版次', '大小', '标注人', '上传账号', '时间', '说明', ''].map((h) => el('th', {}, h))), revisions),
      pre.prelabels.length ? [el('h2', {}, '预标注'),
        el('table', { id: 'prelabels' }, el('tr', {}, ['版次', '模型版本', '状态', '图片', '框', '提交', '结果'].map((h) => el('th', {}, h))),
          pre.prelabels.map((r) => el('tr', {},
            el('td', {}, `第 ${r.revision} 版`), el('td', {}, can('models.read') ? mref(r.model) : r.model),
            el('td', {}, r.status_text + (r.status === 'queued' ? `（前面还有 ${r.queued_ahead} 个）` : ''),
              r.error ? el('div', { class: 'bad' }, r.error) : null),
            el('td', {}, String(r.images)), el('td', {}, r.boxes == null ? '' : String(r.boxes)),
            el('td', {}, `${r.submitted_by}，${when(r.created_at)}`),
            el('td', {}, r.prediction_set ? [el('a', { href: hrefOf(['p', String(r.prediction_set)]) }, `预测集 #${r.prediction_set}`),
              r.revision === b.revision ? el('div', { class: 'mono muted' }, `fw batch get ${b.name} --prelabel`) : null] : ''))))] : null,
      compare ? [el('h2', {}, '这一版改了什么'), compareTable(compare)] : null,
      el('h2', {}, '经过'),
      el('table', {}, el('tr', {}, ['时间', '账号', '做了什么', '意见'].map((h) => el('th', {}, h))), steps));
  };
  await draw();
}

function tokenTable(tokens, onRevoke) {
  if (!tokens.length) return el('p', { class: 'muted' }, '还没有凭据');
  return el('table', {}, el('tr', {}, ['编号', '说明', '建立', '最近使用', '状态', ''].map((h) => el('th', {}, h))),
    tokens.map((t) => el('tr', {},
      el('td', {}, String(t.id)), el('td', {}, t.label || '-'), el('td', {}, when(t.created_at)),
      el('td', {}, t.last_used_at ? when(t.last_used_at) : '没用过'),
      el('td', { class: t.revoked_at ? 'muted' : '' }, t.revoked_at ? `已吊销 ${when(t.revoked_at)}` : '有效'),
      el('td', {}, t.revoked_at ? null : el('button', { onclick: () => onRevoke(t) }, '吊销')))));
}

async function usersPage() {
  const show = live();
  const msg = el('div');
  const say = (e) => msg.replaceChildren(errorBox(e));
  const draw = async () => {
    const { users, roles } = await api.users();
    const people = Object.entries(roles).filter(([k]) => k !== 'worker');
    const name = el('input', { type: 'text', placeholder: '账号名：字母、数字、. _ -，例如 zhangsan' });
    const role = el('select', {}, Object.entries(roles).map(([k, v]) => el('option', { value: k }, v)));
    role.value = 'field';
    const detail = el('div');
    const openTokens = async (u) => {
      const label = el('input', { type: 'text', placeholder: '给谁或哪台机器用，例如 训练机-01' });
      const fresh = el('div');
      const list = async () => tokenTable(await api.userTokens(u.name), async (t) => {
        if (!window.confirm(`吊销 ${u.name} 的凭据 ${t.id}（${t.label || '无说明'}）？用它登录的机器会立刻连不上。`)) return;
        try { await api.revokeToken(t.id); await openTokens(u); } catch (e) { say(e); }
      });
      const make = async () => {
        try {
          const made = await api.createToken(u.name, label.value.trim());
          const table = await list();
          detail.replaceChildren(el('h2', {}, `${u.name} 的凭据`), el('div', { class: 'panel' },
            el('p', {}, `凭据 ${made.id} 只显示这一次，现在就复制给使用的人：`), el('pre', { class: 'mono', id: 'new-token' }, made.token),
            el('p', { class: 'muted' }, `用法：fw login --server ${location.origin} --token <凭据>；网页登录时粘贴同一串。`)), table);
        } catch (e) { say(e); }
      };
      detail.replaceChildren(el('h2', {}, `${u.name} 的凭据`), await list(), fresh,
        u.disabled_at ? el('p', { class: 'muted' }, '账号已停用，先启用才能发凭据')
          : el('div', { class: 'row' }, label, el('button', { class: 'primary', onclick: make }, '发新凭据')));
    };
    const rows = users.map((u) => {
      const pick = el('select', {}, (u.role === 'worker' ? [['worker', roles.worker]] : people).map(([k, v]) => el('option', { value: k }, v)));
      pick.value = u.role;
      pick.disabled = u.role === 'worker' || !!u.disabled_at;
      pick.addEventListener('change', async () => {
        if (!window.confirm(`把 ${u.name} 改成${roles[pick.value]}？下一次请求起生效，凭据不用重发。`)) { pick.value = u.role; return; }
        try { await api.setRole(u.name, pick.value); await draw(); } catch (e) { pick.value = u.role; say(e); }
      });
      const toggle = el('button', { onclick: async () => {
        if (!u.disabled_at && !window.confirm(`停用 ${u.name}？它的全部凭据立刻失效；以后启用也要重新发凭据。`)) return;
        try { await api.setDisabled(u.name, !u.disabled_at); await draw(); } catch (e) { say(e); }
      } }, u.disabled_at ? '启用' : '停用');
      return el('tr', { class: u.disabled_at ? 'muted' : '' },
        el('td', {}, u.name), el('td', {}, pick), el('td', {}, when(u.created_at)),
        el('td', {}, u.disabled_at ? `已停用 ${when(u.disabled_at)}` : '在用'), el('td', {}, String(u.active_tokens)),
        el('td', {}, el('div', { class: 'row' }, el('button', { onclick: () => openTokens(u).catch(say) }, '凭据'), toggle)));
    });
    const add = async (ev) => {
      ev.preventDefault();
      try { await api.createUser(name.value.trim(), role.value); await draw(); } catch (e) { say(e); }
    };
    msg.replaceChildren();
    show(crumbs('users'), el('h1', {}, '账号'),
      el('p', { class: 'muted' }, '一人一个账号。管理员管账号；算法工程师能新建和修改数据集、模型、评估和任务；现场实施能看数据集和模型、下载文件、登记发布；GPU worker 是 GPU 机用的账号。'
        + '每一次改动都记在「操作记录」里。'),
      msg,
      el('table', {}, el('tr', {}, ['账号', '角色', '建立', '状态', '有效凭据', ''].map((h) => el('th', {}, h))), rows),
      el('h2', {}, '新建账号'),
      el('form', { class: 'panel form', onsubmit: add }, el('label', {}, '账号名', name), el('label', {}, '角色', role),
        el('button', { class: 'primary', type: 'submit' }, '新建账号')),
      detail);
  };
  await draw();
}

async function mePage() {
  const show = live();
  const msg = el('div');
  const draw = async () => {
    const tokens = await api.myTokens();
    show(crumbs('me'), el('h1', {}, '我的凭据'),
      el('p', { class: 'muted' }, `${me.name}（${me.role_text || me.role}）。现在登录用的是凭据 ${me.token_id}${me.token_label ? `（${me.token_label}）` : ''}。`
        + '不再用的机器可以在这里吊销它的凭据；要新凭据找管理员。'),
      msg,
      tokenTable(tokens, async (t) => {
        if (!window.confirm(t.id === me.token_id ? '这是现在登录用的凭据，吊销后要重新登录。继续？' : `吊销凭据 ${t.id}（${t.label || '无说明'}）？`)) return;
        try { await api.revokeToken(t.id); if (t.id === me.token_id) { location.hash = '#/login'; return; } await draw(); } catch (e) { msg.replaceChildren(errorBox(e)); }
      }));
  };
  await draw();
}

// The particulars of an event (its detail object) as one line of text.
export function detailText(detail) {
  return Object.entries(detail || {}).filter(([, v]) => v !== null && v !== '' && v !== undefined)
    .map(([k, v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : v}`).join('  ');
}

async function eventsPage(query) {
  const show = live();
  const asked = { limit: '100' };
  for (const k of ['before', 'actor', 'action', 'q']) if (query.get(k)) asked[k] = query.get(k);
  const r = await api.events(asked);
  const go = (changes) => {
    const next = { actor: asked.actor, action: asked.action, q: asked.q, ...changes };
    location.hash = hrefOf(['events'], next);
  };
  const groups = [...new Set(Object.keys(r.actions).map((a) => a.split('.')[0]))];
  const groupNames = { dataset: '数据集', version: '版本', model: '模型', model_version: '模型版本', eval: '评估', release: '发布',
    runner: '推理环境', job: '任务', prediction_set: '预测集', user: '账号', token: '凭据' };
  const action = el('select', {}, el('option', { value: '' }, '全部操作'),
    groups.map((g) => el('option', { value: g }, groupNames[g] || g)));
  action.value = (asked.action || '').split('.')[0];
  const actor = el('input', { type: 'text', placeholder: '账号', value: asked.actor || '' });
  const q = el('input', { type: 'text', placeholder: '对象里的字，例如 SES-TX', value: asked.q || '' });
  const filter = (ev) => { ev.preventDefault(); go({ action: action.value, actor: actor.value.trim(), q: q.value.trim() }); };
  show(crumbs('events'), el('h1', {}, '操作记录'),
    el('p', { class: 'muted' }, '谁在什么时候改了什么：建版本、登记模型和发布、提交和取消任务、账号和凭据的变动。和改动本身一起写入，只增不改。'),
    el('form', { class: 'row', onsubmit: filter }, action, actor, q, el('button', { type: 'submit' }, '筛选')),
    r.events.length ? el('table', {}, el('tr', {}, ['时间', '账号', '操作', '对象', '细节'].map((h) => el('th', {}, h))),
      r.events.map((e) => el('tr', {},
        el('td', {}, when(e.at)), el('td', {}, e.by_server ? '服务器命令行' : e.actor), el('td', {}, e.action_text),
        el('td', {}, e.object), el('td', { class: 'muted mono' }, detailText(e.detail)))))
      : el('p', { class: 'muted' }, '没有符合的记录'),
    el('div', { class: 'row', style: 'margin-top:12px' },
      asked.before ? el('a', { class: 'button', href: hrefOf(['events'], { actor: asked.actor, action: asked.action, q: asked.q }) }, '回到最新') : null,
      r.next_before ? el('a', { class: 'button', href: hrefOf(['events'], { actor: asked.actor, action: asked.action, q: asked.q, before: r.next_before }) }, '更早的') : null));
}

// A site's or line's code with the name shown next to it.
function placeText(p) {
  return p.name ? `${p.code}（${p.name}）` : p.code;
}

// Two selects, the registered sites and the lines of the one chosen: a pool or model only takes those.
function placePicker(tree, at = { site: '', line: '' }) {
  const site = el('select', { id: 'site' }, tree.sites.map((x) => el('option', { value: x.code }, placeText(x))));
  const line = el('select', { id: 'line' });
  const fill = () => {
    const at = tree.sites.find((x) => x.code === site.value);
    line.replaceChildren(...(at ? at.lines : []).map((x) => el('option', { value: x.code }, placeText(x))));
  };
  site.addEventListener('change', fill);
  const chosen = tree.sites.find((x) => x.code.toLowerCase() === at.site.toLowerCase());  // the one being browsed
  if (chosen) site.value = chosen.code;
  fill();
  const picked = chosen && chosen.lines.find((x) => x.code.toLowerCase() === at.line.toLowerCase());
  if (picked) line.value = picked.code;
  return { site, line };
}

// Registering a site or a line of it (codes are for good; the shown name can change later).
function newPlaceForm(done) {
  const ref = el('input', { id: 'place-ref', type: 'text', placeholder: '现场/产线，例如 SES/TX' });
  const name = el('input', { id: 'place-name', type: 'text', placeholder: '产线的显示名（可不写），例如 贴片线' });
  const msg = el('div');
  const go = async (ev) => {
    ev.preventDefault();
    try { await api.addSite(ref.value.trim(), name.value.trim()); await done(); } catch (e) { msg.replaceChildren(errorBox(e)); }
  };
  return el('details', { class: 'panel' }, el('summary', {}, '登记现场 / 产线'),
    el('form', { class: 'form', onsubmit: go }, msg,
      el('p', { class: 'muted' }, '数据池和模型只能建在登记过的现场和产线下。代号只能用汉字、字母、数字、_ 和 .，登记后不能改（它在训练集快照的目录和发布编号里）；显示名以后能改。只写现场（SES）就只登记现场。'),
      el('label', {}, '现场 / 产线', ref), el('label', {}, '显示名', name),
      el('button', { id: 'place-go', class: 'primary', type: 'submit' }, '登记')));
}

function newDatasetForm(tree, at = { site: '', line: '' }) {
  const name = el('input', { id: 'dataset-name', type: 'text', placeholder: '名称，例如 SES-TX' });
  const { site, line } = placePicker(tree, at);
  const note = el('input', { type: 'text', placeholder: '备注' });
  const msg = el('div');
  const go = async (ev) => {
    ev.preventDefault();
    try {
      const d = await api.createDataset({ name: name.value.trim(), site: site.value, line: line.value, note: note.value.trim() });
      location.hash = hrefOf(['upload', d.name]);
    } catch (e) { msg.replaceChildren(errorBox(e)); }
  };
  return el('details', { class: 'panel' }, el('summary', {}, '新建数据池'),
    el('form', { class: 'form', onsubmit: go }, msg,
      tree.sites.some((x) => x.lines.length) ? null : el('p', { class: 'muted' }, '还没有登记产线：先在下面「登记现场 / 产线」里登记。'),
      el('label', {}, '名称', name), el('label', {}, '现场', site),
      el('label', {}, '产线', line), el('label', {}, '备注', note), el('button', { id: 'dataset-go', class: 'primary', type: 'submit' }, '建好后上传')));
}

// --- the overview of a line --------------------------------------------------------------------------------
// One line at a glance: what runs there now (the release), its models with their last evaluation, its training
// snapshots, its pools, and the batches still under way. Without a line: every line and how much it holds.
const KEY_METRICS = ['mAP50', 'mAP50-95', 'P', 'R'];

// The few numbers of an evaluation shown in a list: [[name, text], ...] for those the report has.
export function keyMetrics(metrics) {
  return KEY_METRICS.filter((k) => typeof (metrics || {})[k] === 'number').map((k) => [k, numberText(metrics[k])]);
}

// Batches still under way: not imported, not closed.
export function batchesUnderWay(batches) {
  return batches.filter((b) => b.status !== 'imported' && b.status !== 'closed');
}

// The newest evaluation of each model version name: {model name: report}; reports come oldest first.
export function lastEvalByModel(evals) {
  const out = {};
  for (const e of evals) out[e.model_version.ref.split('@')[0]] = e;
  return out;
}

function lineTable(tree, at) {
  const shown = shownPlaces(tree, placePrefs().hidden, at);  // the lines chosen in the tree
  const sites = shown.sites.filter((s) => !at.site || s.code.toLowerCase() === at.site.toLowerCase());
  const rows = sites.flatMap((s) => s.lines.map((x) => el('tr', {},
    el('td', {}, el('a', { href: hrefOf([], { site: s.code, line: x.code }) }, `${placeText(s)} / ${placeText(x)}`)),
    ...['datasets', 'snapshots', 'models', 'batches', 'releases'].map((k) => el('td', { class: 'num' }, String(x[k] || 0))))));
  const all = tree.sites.filter((s) => !at.site || s.code.toLowerCase() === at.site.toLowerCase());
  const lines = (list) => list.reduce((n, s) => n + s.lines.length, 0);
  const missing = at.site ? lines(all) - lines(sites) : shown.hidden;  // of what this page would list
  const left = missing ? el('p', { class: 'muted small' }, `有 ${missing} 个${at.site ? '产线' : '现场 / 产线'}没显示：在左边点「选择」勾上。`) : null;
  if (!rows.length) {
    return [el('p', { class: 'muted' }, at.site ? '这个现场没有要显示的产线。' : tree.sites.length ? '没有要显示的产线。' : '还没有登记现场和产线：在「数据集」页最下面登记。'), left];
  }
  return [el('table', { id: 'lines' }, el('tr', {}, ['现场 / 产线', '数据池和测试集', '训练集快照', '模型', '在办的批次', '发布次数'].map((h, i) => el('th', { class: i ? 'num' : '' }, h))), rows), left];
}

// A day and time without the year when it is this year: 11月13日 17:25.
export function shortWhen(stamp, now = new Date()) {
  if (!stamp) return '';
  const d = new Date(stamp);
  const p = (x) => String(x).padStart(2, '0');
  const day = `${d.getMonth() + 1}月${d.getDate()}日 ${p(d.getHours())}:${p(d.getMinutes())}`;
  return d.getFullYear() === now.getFullYear() ? day : `${d.getFullYear()}年${day}`;
}

// What a row of the overview's model table shows: the version its numbers belong to (that of its last
// evaluation, or the newest version when it has none) and the menu behind its ⋯.
export function modelRow(m, e) {
  const ref = e ? e.model_version.ref : m.latest || '';
  const [name, number] = ref ? ref.split('@') : [m.name, ''];
  const items = [];
  if (ref) items.push({ icon: 'open', text: '查看版本', href: hrefOf(['mv', name, number]) });
  if (e) items.push({ icon: 'chart', text: '查看评估', href: hrefOf(['eval', String(e.id)]) });
  if (ref) items.push({ icon: 'copy', text: '复制版本号', copy: ref });
  return { ref, number, items };
}

// The one menu open now (a ⋯ menu): Esc or a click elsewhere closes it.
let openMenu = null;
function closeMenu(refocus = false) {
  if (openMenu) openMenu.close(refocus);
}

// A ⋯ button and its menu. items: {icon, text, href} or {icon, text, copy}. Opened, the first item has the
// focus; arrows move it; Esc, Tab, a click elsewhere or a choice closes it (by keys the focus goes back to ⋯).
function moreMenu(label, items) {
  const wrap = el('div', { class: 'more' });
  const button = el('button', { class: 'tool dots', type: 'button', title: '更多', 'aria-label': `${label} 的更多操作`,
    'aria-haspopup': 'menu', 'aria-expanded': 'false' }, icon('more'));
  const open = () => {
    closeMenu();
    const menu = el('div', { class: 'menu', role: 'menu', 'aria-label': label });
    const close = (refocus) => {
      menu.remove();
      wrap.classList.remove('open');
      button.setAttribute('aria-expanded', 'false');
      if (openMenu && openMenu.wrap === wrap) openMenu = null;
      if (refocus) button.focus();
    };
    menu.append(...items.map((it) => (it.href
      ? el('a', { role: 'menuitem', href: it.href, tabindex: '-1', onclick: () => close(false) }, icon(it.icon), el('span', {}, it.text))
      : el('button', { role: 'menuitem', type: 'button', tabindex: '-1', onclick: () => copyRef(it.copy, menu, close) }, icon(it.icon), el('span', {}, it.text)))));
    menu.addEventListener('keydown', (ev) => {
      const list = [...menu.querySelectorAll('[role=menuitem]')];
      const at = list.indexOf(document.activeElement);
      const go = (i) => { ev.preventDefault(); if (list.length) list[(i + list.length) % list.length].focus(); };
      if (ev.key === 'Escape') { ev.preventDefault(); ev.stopPropagation(); close(true); }
      else if (ev.key === 'Tab') close(false);
      else if (ev.key === 'ArrowDown') go(at + 1);
      else if (ev.key === 'ArrowUp') go(at - 1);
      else if (ev.key === 'Home') go(0);
      else if (ev.key === 'End') go(-1);
    });
    wrap.append(menu);
    wrap.classList.add('open');
    button.setAttribute('aria-expanded', 'true');
    openMenu = { wrap, close };
    menu.querySelector('[role=menuitem]').focus();
  };
  button.addEventListener('click', () => { if (openMenu && openMenu.wrap === wrap) closeMenu(); else open(); });
  button.addEventListener('keydown', (ev) => { if (ev.key === 'ArrowDown') { ev.preventDefault(); open(); } });
  wrap.append(button);
  return wrap;
}

// Copy a version's full reference. Where the browser has no clipboard for the page (an http address on the
// intranet), the menu shows the reference selected and tries the older copy command on it; if that fails
// too, it says to press Ctrl+C.
async function copyRef(ref, menu, close) {
  try {
    if (!navigator.clipboard || !navigator.clipboard.writeText) throw new Error('no clipboard');
    await navigator.clipboard.writeText(ref);
    close(true);
    toast(`已复制 ${ref}`);
  } catch {
    const field = el('input', { class: 'copy-field mono', type: 'text', readonly: true, value: ref, 'aria-label': '版本号' });
    menu.replaceChildren(el('p', { class: 'small' }, '浏览器不让这个页面写剪贴板，按 Ctrl+C 复制：'), field);
    field.focus();
    field.select();
    let copied = false;
    try { copied = document.execCommand('copy'); } catch { /* not there either */ }
    if (copied) {
      close(true);
      toast(`已复制 ${ref}`);
    }
  }
}

// A short note at the bottom of the window that goes away by itself.
let toastTimer = null;
function toast(text) {
  let box = document.getElementById('toast');
  if (!box) {
    box = el('div', { id: 'toast', role: 'status', 'aria-live': 'polite' });
    document.body.append(box);
  }
  box.replaceChildren(icon('copy'), el('span', {}, text));
  box.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => box.classList.remove('show'), 1800);
}

const BATCH_DOT = { open: 'blue', review: 'orange', rejected: 'red', approved: 'green', imported: 'gray', closed: 'gray' };
const DATA_ORDER = ['pool', 'trainset', 'test', 'regression', 'other'];

async function overviewPage(query) {
  const show = live();
  const at = { ...scopeOf(query), q: '' };
  scope = at;
  renderNav();
  const tree = await api.sites();
  const side = placeTree(tree, at, null, (site, line) => hrefOf([], { site, line }));
  const site = tree.sites.find((s) => s.code.toLowerCase() === at.site.toLowerCase());
  if (!at.line) {
    show(at.site ? crumbs(null, at) : el('nav', { class: 'crumbs' }, sideToggle()), el('h1', {}, at.site ? (site ? placeText(site) : at.site) : '概览'), side,
      el('div', { class: 'browse' }, el('div', { class: 'list' },
        el('p', { class: 'muted' }, '点一条产线，看它现在发布的模型、最近的评估、训练集快照、数据池和在办的批次。'), lineTable(tree, at))));
    return;
  }
  const where = { site: at.site, line: at.line };
  const none = (e) => { if (e.status === 404) return null; throw e; };
  const [data, models, release, batches] = await Promise.all([
    api.datasets(where), api.models(where), api.currentRelease(at.site, at.line).catch(none),
    can('batches.read') ? api.batches(where).then((r) => batchesUnderWay(r.batches)) : null]);
  const evals = lastEvalByModel((await Promise.all(models.map((m) => api.evals(m.name)))).flat());
  const head = (id, text, parts, link) => el('div', { class: 'section-head' }, el('h2', { id }, text),
    el('a', { class: 'small', href: hrefOf(parts, { ...where, ...(parts[0] === 'batches' ? { view: 'all' } : {}) }) }, link));
  const line = site && site.lines.find((x) => x.code.toLowerCase() === at.line.toLowerCase());
  const mvHref = (ref) => { const [n, k] = ref.split('@'); return hrefOf(['mv', n, k]); };

  const card = release
    ? el('section', { id: 'release', class: 'release', 'aria-label': '当前发布' },
      el('div', { class: 'chip' }, icon('chip')),
      el('div', { class: 'what' }, el('div', { class: 'label' }, '当前发布'), el('div', { class: 'ref' }, release.ref),
        el('div', { class: 'sub', title: `登记：${release.created_by}，${when(release.created_at)}${release.note ? `；备注：${release.note}` : ''}` },
          el('span', { class: 'mv' }, release.model_version.ref), el('span', { class: 'bar' }), el('span', {}, `部署于 ${shortWhen(release.deployed_at)}`))),
      el('div', { class: 'state' }, el('span', { class: 'dot green' }), '已部署'),
      el('a', { class: 'button primary go', href: mvHref(release.model_version.ref) }, '查看模型版本', icon('arrow')))
    : el('section', { id: 'release', class: 'release none', 'aria-label': '当前发布' },
      el('div', { class: 'chip' }, icon('chip')),
      el('div', { class: 'what' }, el('div', { class: 'label' }, '当前发布'),
        el('p', { class: 'muted' }, '这条产线还没有登记过发布；部署成功后用 fw release register 登记。')));

  const modelTable = models.length ? el('table', { class: 'models' },
    el('tr', {}, ['模型', '版本', ...KEY_METRICS].map((h, i) => el('th', { class: i > 1 ? 'num' : '' }, h)), el('th', { class: 'act' })),
    models.map((m) => {
      const e = evals[m.name];
      const row = modelRow(m, e);
      const shown = e ? Object.fromEntries(keyMetrics(e.metrics)) : {};
      return el('tr', { title: e ? `评估 #${e.id}：${e.model_version.ref} 在 ${e.testset.ref} 上，${when(e.created_at)}` : '这个模型还没有评估' },
        el('td', {}, el('a', { href: hrefOf(['m', m.name]) }, m.name)),
        el('td', {}, row.ref ? el('a', { href: mvHref(row.ref) }, `@${row.number}`) : '-'),
        ...KEY_METRICS.map((k) => el('td', { class: `num${shown[k] ? '' : ' muted'}` }, shown[k] || '—')),
        el('td', { class: 'act' }, row.items.length ? moreMenu(row.ref, row.items) : null));
    })) : el('p', { class: 'muted' }, '这条产线还没有模型。');

  const sorted = data.slice().sort((a, b) => DATA_ORDER.indexOf(a.kind) - DATA_ORDER.indexOf(b.kind));
  const dataTable = sorted.length ? el('table', { class: 'data' },
    el('tr', {}, el('th', {}, '名称'), el('th', {}, '类型'), el('th', { class: 'num' }, '大小'), el('th', { class: 'act' })),
    sorted.map((d) => el('tr', { title: d.latest ? `${d.latest.file_count} 个文件，最新版本 ${when(d.latest.created_at)}` : '还没有版本' },
      el('td', {}, el('span', { class: 'with-icon' }, icon('folder'),
        d.latest ? el('a', { href: hrefOf(['v', d.name, String(d.latest.number)]) }, d.latest.ref) : el('a', { href: hrefOf(['d', d.name]) }, d.name), labelChips(d.latest || {}))),
      el('td', {}, d.kind === 'trainset' ? '训练集快照' : KIND_NAMES[d.kind] || d.kind),
      el('td', { class: 'num' }, d.latest ? sizeText(d.latest.total_bytes) : '—'),
      el('td', { class: 'act' }, el('a', { class: 'tool go', href: hrefOf(['d', d.name]), title: `打开 ${d.name}`, 'aria-label': `打开 ${d.name}` }, icon('right'))))))
    : el('p', { class: 'muted' }, '这条产线还没有数据池和训练集快照。');

  const batchList = batches === null ? null : batches.length ? el('table', { class: 'batches' },
    batches.map((b) => el('tr', {},
      el('td', {}, bref(b.name), el('div', { class: 'small muted sub', title: `入库到 ${b.pool} / ${b.dir}/，上传账号 ${b.owner}` }, `${b.dir}/ · ${b.owner}`)),
      el('td', { class: 'state' }, el('span', { class: `dot ${BATCH_DOT[b.status] || 'gray'}` }), b.status_text))))
    : el('p', { class: 'muted' }, '没有在办的批次（没入库、没关闭的）。');

  show(crumbs(null, at),
    el('h1', {}, line ? [line.code, line.name].filter(Boolean).join(' ') : at.line), side,
    el('div', { class: 'browse' }, el('div', { class: 'list' },
      line ? null : el('p', { class: 'error' }, `${at.site} / ${at.line} 不是登记过的产线，下面是名字对得上的东西。`),
      card,
      el('div', { class: `cols${batchList ? '' : ' one'}` },
        el('div', { class: 'col' }, head('models', '模型与最近评估', ['models'], '全部模型'), modelTable,
          head('data', '数据池与快照', ['datasets'], '全部数据集'), dataTable),
        batchList ? el('div', { class: 'col right' }, head('batches', '在办的批次', ['batches'], '全部批次'), batchList) : null))));
}

// --- frame and routing ------------------------------------------------------------------------------------
// [link, text, the capability that shows it, the pages it stands for]
const NAV = [
  ['#/', '概览', 'sites.read', /^#\/?($|\?|overview)/, 'overview'],  // the home page
  ['#/datasets', '数据集', 'datasets.read', /^#\/(datasets|d\/|v\/|img\/|diff\/|upload\/|add\/|remove\/)/, 'datasets'],
  ['#/batches', '标注批次', 'batches.read', /^#\/(batches|b\/|batch-new|batch-up\/)/, 'batches'],
  ['#/models', '模型', 'models.read', /^#\/(models|m\/|mv\/|eval)/, 'models'],
  ['#/jobs', '任务', 'jobs.read', /^#\/(jobs|job\/)/, 'jobs'],
  ['#/predictions', '预测集', 'datasets.read', /^#\/(predictions|p\/)/, 'predictions'],
  ['#/events', '操作记录', 'events.read', /^#\/events/, 'events'],
  ['#/users', '账号', 'users.manage', /^#\/users/, 'users'],
];

const SCOPED = new Set(['#/', '#/datasets', '#/batches', '#/models']);

function renderNav() {
  const here = location.hash || '#/';
  document.getElementById('nav').replaceChildren(...NAV.filter(([, , cap]) => !me || can(cap))
    .map(([href, text, , on, name]) => el('a', {  // the lists keep the site and line being browsed
      href: SCOPED.has(href) && scope.site ? hrefOf(href.slice(2).split('/').filter(Boolean), { site: scope.site, line: scope.line }) : href,
      class: on.test(here) ? 'on' : '', 'aria-current': on.test(here) ? 'page' : null,
    }, icon(name), text)));
}

function renderWho(account) {
  me = account;
  renderNav();
  const who = document.getElementById('who');
  if (!me) { who.replaceChildren(); return; }
  who.replaceChildren(icon('user', 'avatar'), el('a', { href: '#/me', title: '我的凭据' }, `${me.name}（${me.role_text || me.role}）`), el('button', {
    class: 'tool', type: 'button', title: '退出', 'aria-label': '退出',
    onclick: () => { try { localStorage.removeItem(TOKEN_KEY); } catch { /* ignore */ } renderWho(null); location.hash = '#/login'; },
  }, icon('logout')));
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
  closeMenu();
  const show = live();
  const { parts, query } = route(location.hash);
  renderNav();
  if (parts[0] === 'login' || !token()) { visits = []; loginPage(); return; }
  visits = visited(visits, location.hash || '#/');
  if (parts.length === 0 && me && !can('sites.read')) {  // their home: what they may see
    visits = [];
    location.replace(can('datasets.read') ? '#/datasets' : can('jobs.read') ? '#/jobs' : '#/models');
    return;
  }
  try {
    if (parts.length === 0 || parts[0] === 'overview') await overviewPage(query);  // #/overview: the address it had before it became the home page
    else if (parts[0] === 'datasets') await datasetsPage(query);
    else if (parts[0] === 'd' && parts[1]) await datasetPage(parts[1]);
    else if (parts[0] === 'v' && parts[2]) await versionPage(parts[1], parts[2], query);
    else if (parts[0] === 'img' && parts[3]) await imagePage(parts[1], parts[2], parts.slice(3).join('/'), query);
    else if (parts[0] === 'diff' && parts[1]) await diffPage(parts[1], query);
    else if (parts[0] === 'models') await modelsPage(query);
    else if (parts[0] === 'm' && parts[1]) await modelPage(parts[1]);
    else if (parts[0] === 'mv' && parts[2]) await modelVersionPage(parts[1], parts[2]);
    else if (parts[0] === 'eval' && parts[1]) await evalPage(parts[1]);
    else if (parts[0] === 'evals') await evalComparePage(query);
    else if (parts[0] === 'jobs') await jobsPage(query);
    else if (parts[0] === 'job' && parts[1]) await jobPage(parts[1], query);
    else if (parts[0] === 'predictions') await predictionsPage();
    else if (parts[0] === 'p' && parts[1]) await predictionPage(parts[1], query);
    else if (parts[0] === 'upload' && parts[1]) await uploadPage(parts[1]);
    else if (parts[0] === 'add' && parts[1]) await addPage(parts[1]);
    else if (parts[0] === 'batches') await batchesPage(query);
    else if (parts[0] === 'batch-new') await batchUploadPage(null, query);
    else if (parts[0] === 'batch-up' && parts[1]) await batchUploadPage(parts[1], query);
    else if (parts[0] === 'b' && parts[1]) await batchPage(parts[1], query);
    else if (parts[0] === 'users') await usersPage();
    else if (parts[0] === 'events') await eventsPage(query);
    else if (parts[0] === 'me') await mePage();
    else if (parts[0] === 'remove' && parts[1]) await removePage(parts[1]);
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
  try { if (localStorage.getItem(SIDE_KEY) === 'off') document.body.classList.add('side-off'); } catch { /* private window */ }
  window.addEventListener('hashchange', render);
  for (const kind of ['dragover', 'drop']) {  // a folder dropped beside the box must not open in the tab instead
    window.addEventListener(kind, (ev) => { if (ev.dataTransfer && [...ev.dataTransfer.types].includes('Files')) ev.preventDefault(); });
  }
  document.addEventListener('keydown', (ev) => {  // one listener; the page shown now sets keyNav
    if (ev.key === 'Escape' && openMenu) { closeMenu(true); return; }
    if (keyNav && !ev.target.closest('input, select, textarea')) keyNav(ev.key);
  });
  document.addEventListener('pointerdown', (ev) => {  // a click outside the open menu closes it
    if (openMenu && !openMenu.wrap.contains(ev.target)) closeMenu();
  });
  await render();
}

if (main) start();
