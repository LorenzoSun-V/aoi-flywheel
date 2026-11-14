// node --test tests/web/helpers.test.mjs: the parts of the web pages that do not need a browser.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash, randomBytes } from 'node:crypto';
import { createRequire } from 'node:module';
import {
  shortWhen, modelRow, downloadText, readDropped,
  trailOf, visited, readPlaces, toggled, shownPlaces, placeKey,
  scopeOf, scopeQuery, countsBy, datasetCounts, keyMetrics, batchesUnderWay, lastEvalByModel,
  diffManifests, flattenMetrics, hrefOf, manifestText, relPaths, labelIndex, listDir, numberText, outline, parseManifest, predictedShapes, route, shownByBrowsers, sizeText, withLayout,
} from '../../server/fwserver/web/app.js';

const files = parseManifest('fw-manifest 1\nA/a.png\taa\t10\nA/a.JSON\tbb\t5\nA/x/b.jpg\tcc\t7\nA/x/y/c.png\tdd\t1\nr.bmp\tee\t2\nnote.txt\tff\t1\n');

test('manifest', () => {
  assert.equal(files.length, 6);
  assert.deepEqual(files[0], { path: 'A/a.png', sha256: 'aa', size: 10 });
  assert.throws(() => parseManifest('nope\n'));
});

test('folders and images of a folder', () => {
  assert.deepEqual(listDir(files, ''), { folders: [['A', 3]], images: [files[4]] });
  const a = listDir(files, 'A');
  assert.deepEqual(a.folders, [['x', 2]]);
  assert.deepEqual(a.images.map((f) => f.path), ['A/a.png']);
  assert.deepEqual(listDir(files, 'A/x').folders, [['y', 1]]);
});

test('a snapshot is listed in the folders fw get makes', () => {
  const stored = parseManifest('fw-manifest 1\nclasses.txt\tc\t1\nimages/train/SES/TX/train/images/1.bmp\ti1\t9\n'
    + 'images/train/SES/TX/neg_train/images/2.bmp\ti2\t9\nimages/val/SES/TX/a/b/3.bmp\ti3\t9\n'
    + 'json/train/SES/TX/train/images/1.json\tj1\t5\nlabels/train/SES/TX/train/images/1.txt\tl1\t40\n');
  const rows = [['classes.txt', 'c', 1, 'classes.txt'], ['train/images/1.bmp', 'i1', 9, 'images/train/SES/TX/train/images/1.bmp'],
    ['train/labels/1.txt', 'l1', 40, 'labels/train/SES/TX/train/images/1.txt'], ['neg_train/2.bmp', 'i2', 9, 'images/train/SES/TX/neg_train/images/2.bmp'],
    ['val/images/3.bmp', 'i3', 9, 'images/val/SES/TX/a/b/3.bmp'], ['val/labels/3.txt', 'e3', 0, null]];
  const shown = withLayout(stored, rows);
  assert.deepEqual(listDir(shown, '').folders, [['neg_train', 1], ['train', 1], ['val', 1]]);
  assert.deepEqual(listDir(shown, 'train').folders, [['images', 1]]);
  const [neg] = listDir(shown, 'neg_train').images;
  assert.equal(neg.path, 'images/train/SES/TX/neg_train/images/2.bmp');  // links keep the stored path
  assert.equal(neg.show, 'neg_train/2.bmp');
  const [one] = listDir(shown, 'train/images').images;
  assert.equal(labelIndex(shown, true)(one).path, 'json/train/SES/TX/train/images/1.json');
  assert.equal(labelIndex(shown, true)(neg), undefined);
  assert.equal(labelIndex(shown)(one), undefined);  // a pool pairs by the same folder only
  assert.equal(withLayout(stored, []), stored);  // names collide when flat: the stored folders are shown
  assert.deepEqual(listDir(stored, '').folders, [['images', 3]]);
});

test('labels pair with images in any extension case', () => {
  const labelOf = labelIndex(files);
  assert.equal(labelOf(files[0]).path, 'A/a.JSON');
  assert.equal(labelOf(files[2]), undefined);
});

test('routes keep slashes inside an image path', () => {
  const h = hrefOf(['img', 'P', '3', 'A/x/b c.jpg']);
  const r = route(h);
  assert.deepEqual(r.parts.slice(0, 3), ['img', 'P', '3']);
  assert.equal(r.parts.slice(3).join('/'), 'A/x/b c.jpg');
  assert.equal(route(hrefOf(['v', 'P', '1'], { dir: 'A/x', page: 0 })).query.get('dir'), 'A/x');
});

test('shapes', () => {
  assert.deepEqual(outline({ shape_type: 'rectangle', points: [[1, 2], [3, 4]] }).pts, [[1, 2], [3, 2], [3, 4], [1, 4]]);
  assert.equal(outline({ shape_type: 'linestrip', points: [[0, 0], [1, 1]] }).closed, false);
  assert.equal(outline({ points: [[0, 0], [1, 1], [2, 0]] }).closed, true);  // no shape_type: polygon
  assert.ok(outline({ shape_type: 'circle', points: [[0, 0], [3, 4]] }).circle);
});

test('sizes', () => {
  assert.equal(sizeText(512), '512 B');
  assert.equal(sizeText(1536), '1.5 KB');
});

test('metrics are flattened like fw does', () => {
  const rows = flattenMetrics({ map: 0.5, per_class: { a: { ap: 0.25 }, b: { ap: 1 } }, ok: true, list: [1], note: 'x' });
  assert.deepEqual(rows, [['map', 0.5], ['per_class.a.ap', 0.25], ['per_class.b.ap', 1]]);
  assert.equal(numberText(0.123456), '0.1235');
  assert.equal(numberText(0.5), '0.5');
  assert.equal(numberText(3), '3');
  assert.equal(numberText(undefined), '-');
});

test('predicted boxes of an image at a threshold', () => {
  const doc = { classes: ['scratch', 'dent'], images: { 'A/a.png': { size: [10, 10], boxes: [[1, 2, 3, 4, 0.9, 1], [0, 0, 1, 1, 0.2, 0]] } } };
  assert.deepEqual(predictedShapes(doc, 'A/a.png', 0.3),
    [{ label: 'dent', score: 0.9, shape_type: 'rectangle', points: [[1, 2], [3, 4]] }]);
  assert.equal(predictedShapes(doc, 'A/a.png', 0.1).length, 2);
  assert.deepEqual(predictedShapes(doc, 'B/none.png', 0.1), []);
});

test('TIFF is not shown by browsers', () => {
  assert.equal(shownByBrowsers('A/a.TIF'), false);
  assert.equal(shownByBrowsers('A/a.tiff'), false);
  assert.equal(shownByBrowsers('A/a.bmp'), true);
});

test('the manifest is sorted by UTF-8 bytes like fw', () => {
  const e = (path) => ({ path, sha256: 'ab', size: 1 });
  // UTF-16 order would put the emoji (a surrogate pair) before U+FF5E; UTF-8 bytes put it after
  const text = manifestText(['b', '\u{1F600}.png', 'a/x', '\uFF5E.png', 'a.png', 'B'].map(e));
  assert.equal(text, 'fw-manifest 1\nB\tab\t1\na.png\tab\t1\na/x\tab\t1\nb\tab\t1\n\uFF5E.png\tab\t1\n\u{1F600}.png\tab\t1\n');
});

test('paths inside the picked folder and changes against the parent', () => {
  assert.deepEqual(relPaths([{ webkitRelativePath: 'top/A/a.png' }, { webkitRelativePath: 'top/b.json' }]), ['A/a.png', 'b.json']);
  const old = [{ path: 'a', sha256: '1' }, { path: 'b', sha256: '2' }, { path: 'c', sha256: '3' }];
  const now = [{ path: 'a', sha256: '1' }, { path: 'b', sha256: '9' }, { path: 'd', sha256: '4' }];
  assert.deepEqual(diffManifests(old, now), { added: ['d'], deleted: ['c'], modified: ['b'] });
});

test('sha256 in plain JS', async () => {
  const { Sha256, hashFile } = createRequire(import.meta.url)('../../server/fwserver/web/sha256.js');
  for (const n of [0, 1, 55, 56, 63, 64, 65, 119, 120, 4096, 100003]) {
    const data = randomBytes(n);
    const s = new Sha256();
    for (let i = 0; i < n; i += 37) s.update(data.subarray(i, i + 37));
    assert.equal(s.hex(), createHash('sha256').update(data).digest('hex'), `${n} bytes`);
  }
  const big = randomBytes((4 << 20) + 5);  // more than one chunk of a file
  assert.equal(await hashFile(new Blob([big])), createHash('sha256').update(big).digest('hex'));
});

test('the site and line being browsed, from the address', () => {
  assert.deepEqual(scopeOf(new URLSearchParams('site=SES&line=TX&q=det')), { site: 'SES', line: 'TX', q: 'det' });
  assert.deepEqual(scopeOf(new URLSearchParams('line=TX')), { site: '', line: '', q: '' });  // a line belongs to a site
  assert.deepEqual(scopeQuery({ site: 'SES', line: '', q: '' }, { todo: true, mine: false }), { todo: true, site: 'SES' });
  assert.deepEqual(scopeQuery({ site: '', line: '', q: '' }), {});
});

test('the overview of a line shows the numbers a report has, the batches under way and each model\'s last report', () => {
  assert.deepEqual(keyMetrics({ mAP50: 0.8125, 'mAP50-95': 0.5, P: '0.9', images: 3, nested: { R: 1 } }), [['mAP50', '0.8125'], ['mAP50-95', '0.5']]);
  assert.deepEqual(keyMetrics(null), []);
  const b = (status) => ({ status });
  assert.deepEqual(batchesUnderWay([b('open'), b('imported'), b('review'), b('closed'), b('rejected'), b('approved')]).map((x) => x.status),
    ['open', 'review', 'rejected', 'approved']);
  const e = (id, ref) => ({ id, model_version: { ref } });
  const last = lastEvalByModel([e(1, 'A@1'), e(2, 'B@1'), e(3, 'A@2')]);
  assert.deepEqual([last.A.id, last.B.id], [3, 2]);
});

test('a number in the tree of sites and lines is the number of rows choosing it lists', () => {
  const line = (code, n) => ({ code, name: '', datasets: n.d, trainsets: n.own, snapshots: n.s, models: n.m, batches: 0, releases: 0 });
  const tree = {
    sites: [{ code: 'S', name: '', lines: [line('TX', { d: 1, own: 1, s: 2, m: 3 }), line('TY', { d: 1, own: 0, s: 1, m: 0 })], listed: { datasets: 3, trainsets: 2 } }],
    other: { datasets: 2, trainsets: 1, models: 1, batches: 0, releases: 0 },
    listed: { datasets: 4, trainsets: 2 }, unplaced: { datasets: 1, trainsets: 0 },
  };
  const d = datasetCounts(tree);
  // a snapshot from both lines: once on each line, once on the site; a dataset of the site without a line: on the site
  assert.deepEqual([d.line(tree.sites[0].lines[0]), d.line(tree.sites[0].lines[1]), d.site(tree.sites[0]), d.all, d.loose], [3, 2, 5, 6, 1]);
  const m = countsBy(tree, ['models']);
  assert.deepEqual([m.line(tree.sites[0].lines[0]), m.site(tree.sites[0]), m.all, m.loose], [3, 3, 4, 1]);
});

test('the path of a page starts at the home page and goes through the place the thing belongs to', () => {
  assert.deepEqual(trailOf('datasets', { site: 'SES', line: 'TX' }, [['P', '#/d/P'], '@2']), [
    ['概览', '#/'], ['SES', '#/?site=SES'], ['TX', '#/?site=SES&line=TX'], ['数据集', '#/datasets?site=SES&line=TX'], ['P', '#/d/P'], ['@2', null]]);
  // a list is the page itself; a thing of no place (a snapshot from several lines) hangs under the whole list
  assert.deepEqual(trailOf('models', { site: 'SES', line: '' }), [['概览', '#/'], ['SES', '#/?site=SES'], ['模型', null]]);
  assert.deepEqual(trailOf('datasets', { site: '', line: '' }, ['T']), [['概览', '#/'], ['数据集', '#/datasets'], ['T', null]]);
  assert.deepEqual(trailOf('jobs', { site: 'SES', line: 'TX' }, ['任务 3']).map((x) => x[1]), ['#/', '#/?site=SES', '#/?site=SES&line=TX', '#/jobs', null]);
  assert.deepEqual(trailOf(null, { site: 'SES', line: 'TX' }), [['概览', '#/'], ['SES', '#/?site=SES'], ['TX', null]]);  // the overview of a line
});

test('back never leaves the platform: only the pages gone through here count', () => {
  let stack = [];
  for (const h of ['#/', '#/d/P', '#/v/P/1']) stack = visited(stack, h);
  assert.deepEqual(stack, ['#/', '#/d/P', '#/v/P/1']);
  stack = visited(stack, '#/d/P');  // a step back
  assert.deepEqual(stack, ['#/', '#/d/P']);
  assert.deepEqual(visited(stack, '#/d/P'), stack);  // drawn again: no step
  assert.deepEqual(visited(visited(stack, '#/'), '#/jobs'), ['#/', '#/jobs']);
  assert.equal(visited([], '#/d/P').length, 1);  // opened directly: nothing to go back to
});

test('the tree shows the ticked sites and lines, and always the one being browsed', () => {
  const tree = { sites: [{ code: 'SES', lines: [{ code: 'TX' }, { code: 'TY' }] }, { code: 'B2', lines: [{ code: 'L1' }] }] };
  const codes = (r) => r.sites.map((s) => [s.code, s.lines.map((x) => x.code)]);
  assert.deepEqual(codes(shownPlaces(tree, [])), [['SES', ['TX', 'TY']], ['B2', ['L1']]]);
  const hidden = [placeKey('SES', 'TY'), placeKey('B2')];
  assert.deepEqual(hidden, ['ses/ty', 'b2']);
  const r = shownPlaces(tree, hidden);
  assert.deepEqual([codes(r), r.hidden], [[['SES', ['TX']]], 2]);
  assert.deepEqual(codes(shownPlaces(tree, hidden, { site: 'ses', line: 'ty' })), [['SES', ['TX', 'TY']]]);
  assert.deepEqual(codes(shownPlaces(tree, hidden, { site: 'B2', line: '' })), [['SES', ['TX']], ['B2', ['L1']]]);
  assert.deepEqual(codes(shownPlaces(tree, [placeKey('B2'), placeKey('B2', 'L1')], { site: 'B2', line: '' })), [['SES', ['TX', 'TY']], ['B2', []]]);
  assert.deepEqual(toggled(toggled([], 'a'), 'b'), ['a', 'b']);
  assert.deepEqual(toggled(['a', 'b'], 'a'), ['b']);
});

test('what the browser kept about the tree is read with care', () => {
  assert.deepEqual(readPlaces('{"closed":["ses"],"hidden":["b2",3]}'), { closed: ['ses'], hidden: ['b2'] });
  for (const bad of [null, '', 'not json', '[]', '{"closed":"ses"}', 'null']) assert.deepEqual(readPlaces(bad), { closed: [], hidden: [] });
});

test('a release is dated without the year when it is this year', () => {
  const now = new Date(2026, 10, 14);
  assert.equal(shortWhen(new Date(2026, 10, 13, 17, 5).toISOString(), now), '11月13日 17:05');
  assert.equal(shortWhen(new Date(2025, 0, 2, 8, 0).toISOString(), now), '2025年1月2日 08:00');
  assert.equal(shortWhen(null, now), '');
});

test('a model row shows the version its numbers belong to, and its menu leads to that version and report', () => {
  const e = { id: 7, model_version: { ref: 'SES-TX-detr@12' } };
  const row = modelRow({ name: 'SES-TX-detr', latest: 'SES-TX-detr@13' }, e);
  assert.deepEqual([row.ref, row.number], ['SES-TX-detr@12', '12']);
  assert.deepEqual(row.items.map((i) => [i.text, i.href || i.copy]),
    [['查看版本', '#/mv/SES-TX-detr/12'], ['查看评估', '#/eval/7'], ['复制版本号', 'SES-TX-detr@12']]);
  const plain = modelRow({ name: 'M', latest: 'M@3' }, undefined);  // no evaluation: no report to show
  assert.deepEqual(plain.items.map((i) => i.text), ['查看版本', '复制版本号']);
  assert.equal(plain.number, '3');
  assert.deepEqual(modelRow({ name: 'N', latest: null }, undefined).items, []);  // no version yet: no menu
});

test('a download says how far it has come, in whole percent', () => {
  assert.equal(downloadText(0, 1000), '下载中 0%');
  assert.equal(downloadText(999, 1000), '下载中 99%');
  assert.equal(downloadText(1000, 1000), '下载中 100%');
  assert.equal(downloadText(5, 0), '下载中 0%');
});

test('a dragged folder is read through all its batches and subfolders, with paths inside the folder', async () => {
  const file = (fullPath) => ({ isFile: true, isDirectory: false, fullPath, file: (ok) => ok({ name: fullPath }) });
  const dir = (fullPath, kids, size = 2) => ({
    isFile: false, isDirectory: true, fullPath, name: fullPath.slice(fullPath.lastIndexOf('/') + 1),
    createReader() {  // hands the entries over a few at a time, like a browser does (100 at a time there)
      let at = 0;
      return { readEntries(ok) { const part = kids.slice(at, at + size); at += size; setTimeout(() => ok(part)); } };
    },
  });
  const top = dir('/20261104', [file('/20261104/a.png'), file('/20261104/a.json'), file('/20261104/b.png'),
    dir('/20261104/night', [file('/20261104/night/c.png')]), dir('/20261104/empty', [])]);
  const got = await readDropped(top);
  assert.equal(got.folder, '20261104');
  assert.deepEqual(got.paths, ['a.png', 'a.json', 'b.png', 'night/c.png']);
  assert.deepEqual(got.files.map((f) => f.name), ['/20261104/a.png', '/20261104/a.json', '/20261104/b.png', '/20261104/night/c.png']);
  const broken = dir('/x', []);
  broken.createReader = () => ({ readEntries: (ok, no) => no(new Error('读不了')) });
  await assert.rejects(readDropped(broken), /读不了/);
});
