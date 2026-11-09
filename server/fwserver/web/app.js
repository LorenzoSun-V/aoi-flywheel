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
  for (const c of children.flat()) {
    if (c === undefined || c === null || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

function present(nodes) {
  return nodes.flat().filter((n) => n !== null && n !== undefined && n !== false);
}

function show(...nodes) {
  main.replaceChildren(...present(nodes));
  window.scrollTo(0, 0);
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

async function call(path, { method = 'GET', body, raw = false, stay = false } = {}) {
  const headers = { Authorization: `Bearer ${token()}` };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const r = await fetch(`/api/v1${path}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
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
};

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

const blobs = new Map();

async function blobUrl(digest) {
  if (!blobs.has(digest)) {
    blobs.set(digest, api.object(digest).then((r) => r.blob()).then((b) => URL.createObjectURL(b)));
  }
  return blobs.get(digest);
}

// --- pages ------------------------------------------------------------------------------------------------
function errorBox(e) {
  return el('div', { class: 'error' }, e instanceof Error ? e.message : String(e));
}

function loginPage() {
  const input = el('input', { type: 'password', placeholder: '凭据（fw-server token create 给的那一串）', autocomplete: 'off' });
  const msg = el('div');
  const go = async (ev) => {
    ev.preventDefault();
    try { localStorage.setItem(TOKEN_KEY, input.value.trim()); } catch { /* private window */ }
    try {
      const me = await api.me(true);
      renderWho(me);
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
      .map((h) => el('th', {}, h))), body) : el('p', { class: 'muted' }, '还没有数据集：用 fw dataset create 建一个，再 fw dataset import 导入。'));
}

async function datasetPage(name) {
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

function lazyThumb(img, digest) {
  const io = new IntersectionObserver((seen) => {
    if (seen.some((s) => s.isIntersecting)) {
      io.disconnect();
      blobUrl(digest).then((u) => { img.src = u; }).catch(() => { img.alt = '读不了'; });
    }
  });
  io.observe(img);
}

async function versionPage(name, number, query) {
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
      lazyThumb(img, f.sha256);
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

async function imagePage(name, number, path) {
  const version = await api.version(name, number);
  const files = await filesOf(version);
  const image = files.find((f) => f.path === path);
  if (!image) throw new Error(`${version.ref} 里没有 ${path}`);
  const label = labelIndex(files)(image);
  const dir = path.includes('/') ? path.slice(0, path.lastIndexOf('/')) : '';
  const siblings = listDir(files, dir).images;
  const at = siblings.findIndex((f) => f.path === path);
  const go = (f) => { location.hash = hrefOf(['img', version.dataset, String(version.number), f.path]); };
  const canvas = el('canvas');
  const side = el('div', { class: 'panel' }, el('p', { class: 'muted' }, '加载中…'));
  show(crumbsFor(version, dir),
    el('div', { class: 'row' }, el('h1', { style: 'flex:1' }, path.slice(path.lastIndexOf('/') + 1)),
      el('button', { disabled: at <= 0, onclick: () => go(siblings[at - 1]) }, '上一张'),
      el('span', { class: 'muted' }, `${at + 1} / ${siblings.length}`),
      el('button', { disabled: at + 1 >= siblings.length, onclick: () => go(siblings[at + 1]) }, '下一张')),
    el('div', { class: 'viewer' }, el('div', {}, canvas), side));
  const keys = (ev) => {
    if (!location.hash.startsWith('#/img/')) { document.removeEventListener('keydown', keys); return; }
    if (ev.key === 'ArrowLeft' && at > 0) { document.removeEventListener('keydown', keys); go(siblings[at - 1]); }
    if (ev.key === 'ArrowRight' && at + 1 < siblings.length) { document.removeEventListener('keydown', keys); go(siblings[at + 1]); }
  };
  document.addEventListener('keydown', keys);

  const img = new Image();
  img.src = await blobUrl(image.sha256);
  await img.decode();
  let doc = null;
  let problem = '';
  if (label) {
    try { doc = JSON.parse(await (await api.object(label.sha256)).text()); } catch (e) { problem = `标注读不了：${e.message}`; }
  }
  canvas.width = img.naturalWidth;
  canvas.height = img.naturalHeight;
  const g = canvas.getContext('2d');
  g.drawImage(img, 0, 0);
  const names = [];
  const lw = Math.max(1, Math.round(Math.max(canvas.width, canvas.height) / 500));
  const shapes = doc && Array.isArray(doc.shapes) ? doc.shapes : [];
  for (const s of shapes) {
    const c = colorOf(String(s.label), names);
    const o = outline(s);
    g.strokeStyle = c; g.fillStyle = c; g.lineWidth = lw * 2;
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
  const counts = new Map();
  for (const s of shapes) counts.set(String(s.label), (counts.get(String(s.label)) || 0) + 1);
  side.replaceChildren(...present([
    el('dl', { class: 'kv' },
      el('dt', {}, '尺寸'), el('dd', {}, `${img.naturalWidth} × ${img.naturalHeight}`),
      el('dt', {}, '大小'), el('dd', {}, sizeText(image.size)),
      el('dt', {}, 'sha256'), el('dd', { class: 'mono' }, image.sha256),
      el('dt', {}, '标注'), el('dd', {}, label ? label.path.slice(label.path.lastIndexOf('/') + 1) : '没有（正常图）'),
      doc ? [el('dt', {}, '确认'), el('dd', {}, doc.checked === true ? '已确认' : '未确认')] : null),
    problem ? errorBox(problem) : null,
    shapes.length ? el('ul', { class: 'shapes' }, [...counts.entries()].map(([n, k]) =>
      el('li', {}, el('span', { class: 'dot on', style: `background:${colorOf(n, names)}` }), `${n} × ${k}`))) : null]));
}

async function diffPage(name, query) {
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

// --- frame and routing ------------------------------------------------------------------------------------
const NAV = [['#/', '数据集']];

function renderNav() {
  const here = location.hash || '#/';
  document.getElementById('nav').replaceChildren(...NAV.map(([href, text]) =>
    el('a', { href, class: (href === '#/' ? !/^#\/(models|jobs)/.test(here) : here.startsWith(href)) ? 'on' : '' }, text)));
}

function renderWho(me) {
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
  const { parts, query } = route(location.hash);
  renderNav();
  if (parts[0] === 'login' || !token()) { loginPage(); return; }
  try {
    if (parts.length === 0) await datasetsPage();
    else if (parts[0] === 'd' && parts[1]) await datasetPage(parts[1]);
    else if (parts[0] === 'v' && parts[2]) await versionPage(parts[1], parts[2], query);
    else if (parts[0] === 'img' && parts[3]) await imagePage(parts[1], parts[2], parts.slice(3).join('/'));
    else if (parts[0] === 'diff' && parts[1]) await diffPage(parts[1], query);
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
  await render();
}

if (main) start();
