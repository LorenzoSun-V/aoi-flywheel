// node --test tests/web/helpers.test.mjs: the parts of the web pages that do not need a browser.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash, randomBytes } from 'node:crypto';
import { createRequire } from 'node:module';
import {
  shortWhen, listWhen, sourceText, errorTrail, modelRow, downloadText, readDropped, diffListText, topDirs, namesText,
  parseClasses, dirsOf, splitRef, nextRef, ruleText, onnxName,
  trailOf, visited, readPlaces, toggled, shownPlaces, placeKey, placeGroups,
  scopeOf, scopeQuery, countsBy, datasetCounts, keyMetrics, batchesUnderWay, onBenchmark, benchmarkByModel, benchmarkByVersion, metricDelta,
  newerCandidates, currentReleases, releaseDone, localTime, zonedTime, todoItems, todoText, compareCells, eventText, eventHref, dataChoices, prelabelChoices,
  batchFlow, rejectionChecks, fitView, zoomAt, readReview, reviewText, reviewKey, changedImages, reviewMatch, stepTo, stripWindow, rejectBody,
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

test('the overview of a line shows the numbers a report has and the batches under way', () => {
  assert.deepEqual(keyMetrics({ mAP50: 0.8125, 'mAP50-95': 0.5, P: '0.9', images: 3, nested: { R: 1 } }), [['mAP50', '0.8125'], ['mAP50-95', '0.5']]);
  assert.deepEqual(keyMetrics(null), []);
  const b = (status) => ({ status });
  assert.deepEqual(batchesUnderWay([b('open'), b('imported'), b('review'), b('closed'), b('rejected'), b('approved')]).map((x) => x.status),
    ['open', 'review', 'rejected', 'approved']);
});

test('models are compared on the whole of the benchmark version only, each by its newest version evaluated there', () => {
  const e = (id, ref, testset = 9, more = {}) => ({ id, model_version: { ref }, testset: { id: testset }, split: '', dirs: [], ...more });
  const reports = [e(1, 'A@1'), e(2, 'B@1'), e(3, 'A@3'), e(4, 'A@2'), e(5, 'B@2', 8),  // B@2: on another version
    e(6, 'B@3', 9, { dirs: ['20261101'] }), e(7, 'C@1', 9, { split: 'test' }), e(8, 'A@3')];  // part of it; a split
  assert.deepEqual(onBenchmark(reports, 9).map((x) => x.id), [1, 2, 3, 4, 8]);
  const by = benchmarkByModel(reports, 9);
  assert.deepEqual(Object.fromEntries(Object.entries(by).map(([k, v]) => [k, v.id])), { A: 8, B: 2 });  // A@3, its later report
  assert.deepEqual(benchmarkByVersion(reports.filter((x) => x.model_version.ref.startsWith('A@')), 9).map((x) => x.id), [8, 4, 1]);
  assert.deepEqual(benchmarkByModel(reports, 7), {});
  assert.equal(metricDelta(0.912, 0.9), '+0.012');
  assert.equal(metricDelta(0.8, 0.8125), '−0.0125');
  assert.equal(metricDelta(0.5, 0.5), '±0');
  assert.equal(metricDelta(0.5, undefined), null);
  assert.equal(metricDelta('0.5', 0.4), null);
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
  // the jobs of a line are a list of their own: a job of a model of TX goes back to the jobs of TX
  assert.deepEqual(trailOf('jobs', { site: 'SES', line: 'TX' }, ['任务 3']).map((x) => x[1]), ['#/', '#/?site=SES', '#/?site=SES&line=TX', '#/jobs?site=SES&line=TX', null]);
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

test('a list says today, yesterday, a day of this year, or the day of another year', () => {
  const now = new Date(2026, 10, 14, 9, 30);
  assert.equal(listWhen(new Date(2026, 10, 14, 0, 5).toISOString(), now), '今天 00:05');
  assert.equal(listWhen(new Date(2026, 10, 13, 22, 9).toISOString(), now), '昨天 22:09');
  assert.equal(listWhen(new Date(2026, 10, 12, 17, 25).toISOString(), now), '11月12日 17:25');
  assert.equal(listWhen(new Date(2026, 0, 1, 8, 0).toISOString(), new Date(2026, 0, 1, 23, 59)), '今天 08:00');
  assert.equal(listWhen(new Date(2025, 11, 31, 23, 0).toISOString(), new Date(2026, 0, 1, 1, 0)), '昨天 23:00');
  assert.equal(listWhen(new Date(2025, 5, 2, 8, 0).toISOString(), now), '2025-06-02');
  assert.equal(listWhen(null, now), '');
});

test('a source is shown by how the version was made and the names of its folders', () => {
  assert.equal(sourceText('fw dataset import trainer-01:/data/aoi/pool'), 'fw dataset import · pool');
  assert.equal(sourceText('fw dataset add pc7:/data/a/20261103、/data/a/20261104/'), 'fw dataset add · 20261103、20261104');
  assert.equal(sourceText('fw batch upload LAB-PC:C:\\标注\\SES-TX-1104'), 'fw batch upload · SES-TX-1104');
  assert.equal(sourceText('fw dataset remove trainer-01'), 'fw dataset remove');
  assert.equal(sourceText('fw trainset create'), 'fw trainset create');
  assert.equal(sourceText('网页上传新增目录 20261103'), '网页上传新增目录 20261103');
  assert.equal(sourceText(''), '');
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

test('the list of changes saved from a comparison is the one fw dataset diff --list prints', () => {
  const d = { added: ['a/1.png', 'a/2.png'], modified: ['b.json'], deleted: ['c.png'], renamed: [['old.png', 'new.png']] };
  assert.equal(diffListText(d), '新增\ta/1.png\n新增\ta/2.png\n修改\tb.json\n删除\tc.png\n改名\told.png\tnew.png\n');
  assert.equal(diffListText({ added: [], modified: [], deleted: [], renamed: [] }), '');
});

test('the first-level folders of a folder, and how a few names are said', () => {
  assert.deepEqual(topDirs(['20261102/b.png', '20261101/a.png', '20261101/x/a.json', 'readme.txt']),
    { dirs: ['20261101', '20261102'], loose: ['readme.txt'] });
  assert.deepEqual(topDirs([]), { dirs: [], loose: [] });
  assert.equal(namesText(['a', 'b']), 'a/、b/');
  assert.equal(namesText(['a', 'b', 'c', 'd']), 'a/、b/、c/ 等 4 个');
  assert.equal(namesText(['a', 'b', 'c'], 2, ''), 'a、b 等 3 个');
});

test('a class table is read as typed or from a classes.txt, and a snapshot is told by its rule', () => {
  assert.deepEqual(parseClasses('\uFEFFscratch\r\ndent\r\n\r\n  \nshort \n'), ['scratch', 'dent', 'short ']);  // the server refuses 'short '
  assert.deepEqual(parseClasses(''), []);
  assert.deepEqual(dirsOf('2026-10, SES/TX/2026-11、x\n\n'), ['2026-10', 'SES/TX/2026-11', 'x']);
  assert.deepEqual(splitRef('SES-TX@v0.1.0'), { name: 'SES-TX', number: 'v0.1.0' });
  assert.equal(splitRef('SES-TX'), null);
  assert.equal(splitRef('SES-TX@'), null);
  assert.equal(nextRef('ses-tx-det', null), 'ses-tx-det@1');
  assert.equal(nextRef('ses-tx-det', 'SES-TX-det@3'), 'SES-TX-det@4');
  assert.equal(ruleText({ mode: 'fresh', val: 0.2, test: 0.1, seed: 0, test_dirs: [] }), '按比例划分（val 20%，test 10%，随机种子 0）');
  assert.equal(ruleText({ mode: 'continue', val: 0.2, seed: 3, new_to: 'test', test_dirs: ['2026-10'], merge_test: false }),
    '接着上一版的划分；新样本全部进入 test；2026-10 中的新样本进入 test');
  assert.equal(ruleText({ mode: 'continue', val: 0.25, test: 0, seed: 0, new_to: 'trainval', merge_test: true, class_map: { DL1: 'DL', others: '' } }),
    '接着上一版的划分；新样本按比例划分（val 25%，test 0%，随机种子 0）；上一版 test 中的样本重新划分到 train 和 val；类别映射 DL1→DL、others→移除');
  assert.equal(ruleText({ mode: 'split-file', split_count: 12 }), '按划分清单划分（12 个样本）');
});

test('a model version\'s ONNX is saved under its reference and highest 版本号', () => {
  assert.equal(onnxName({ model: 'SES-TX-detr', number: 3, labels: [] }), 'SES-TX-detr@3.onnx');
  assert.equal(onnxName({ model: 'SES-TX-detr', number: 3 }), 'SES-TX-detr@3.onnx');
  assert.equal(onnxName({ model: 'SES-TX-detr', number: 12, labels: ['v0.9.0', 'v1.0.0'] }), 'SES-TX-detr@12_v1.0.0.onnx');  // in use, lowest first
});

test('a page that cannot be shown keeps the path of its section and the name asked for', () => {
  assert.deepEqual(errorTrail(['d', 'SES-TX']), { section: 'datasets', steps: ['SES-TX'] });
  assert.deepEqual(errorTrail(['v', 'SES-TX', '3']), { section: 'datasets', steps: ['SES-TX'] });
  assert.deepEqual(errorTrail(['mv', 'SES-TX-detr', '2']), { section: 'models', steps: ['SES-TX-detr'] });
  assert.deepEqual(errorTrail(['b', 'SES-TX-20261104']), { section: 'batches', steps: ['SES-TX-20261104'] });
  assert.deepEqual(errorTrail(['users']), { section: 'users', steps: [] });
  assert.deepEqual(errorTrail(['nothing-here']), { section: null, steps: [] });
});

test('a release: the current one of each line, the newer candidates, the deployment time with its zone', () => {
  const r = (site, line, n) => ({ site, line, ref: `${site}-${line}-r${n}` });
  // listed oldest deployed first: the last of each line runs there now (codes of a line in any case)
  assert.deepEqual([...currentReleases([r('SES', 'TX', 2), r('SES', 'TY', 1), r('ses', 'tx', 1)])].sort(), ['SES-TY-r1', 'ses-tx-r1']);
  assert.deepEqual([...currentReleases([])], []);
  const v = (id, tags) => ({ id, ref: `M@${id}`, tags });
  const versions = [v(1, []), v(2, ['candidate']), v(3, ['candidate', 'deprecated']), v(5, ['candidate']), v(4, ['candidate'])];
  assert.deepEqual(newerCandidates(versions, versions[1]).map((x) => x.id), [5, 4]);       // registered after it, not 弃用
  assert.deepEqual(newerCandidates(versions, null).map((x) => x.id), [5, 4, 2]);           // nothing released: every candidate
  assert.deepEqual(newerCandidates(versions, versions[3]), []);
  assert.equal(releaseDone({ ref: 'SES-TX-r3', existing: false, current: { ref: 'SES-TX-r3' } }), '已登记发布 SES-TX-r3');
  assert.equal(releaseDone({ ref: 'SES-TX-r3', existing: false, current: { ref: 'SES-TX-r2' } }),
    '已登记发布 SES-TX-r3；该产线当前发布仍为 SES-TX-r2（部署时间更晚）');
  assert.equal(releaseDone({ ref: 'SES-TX-r3', existing: true, current: { ref: 'SES-TX-r4' } }), '该次部署已登记为 SES-TX-r3');
  assert.equal(localTime(new Date(2026, 0, 5, 7, 3, 59)), '2026-01-05T07:03');
  assert.equal(zonedTime('2026-11-20T10:30', 480), '2026-11-20T10:30+08:00');
  assert.equal(zonedTime('2026-11-20T10:30', 0), '2026-11-20T10:30+00:00');
  assert.equal(zonedTime('2026-11-20T10:30', -570), '2026-11-20T10:30-09:30');
  assert.equal(zonedTime('2026-11-20T10:30', 345), '2026-11-20T10:30+05:45');
});

test('what waits for an account: its groups in order, newest first, each batch by its state, only its own recent failures', () => {
  const b = (name, status, at, more = {}) => ({ name, status, updated_at: at, revision: 2, owner: 'li', reviewer: 'zhang', pool: 'SES-TX', dir: '20261113', ...more });
  const j = (id, status, by, at, more = {}) => ({ id, status, submitted_by: by, created_at: at, finished_at: at, kind_text: '评估', ...more });
  const now = new Date('2026-11-15T12:00:00Z');
  const rows = todoItems({
    toReview: [b('A', 'review', '2026-11-14T10:00:00Z'), b('B', 'review', '2026-11-15T10:00:00Z'), b('X', 'approved', '2026-11-15T10:00:00Z')],
    approved: [b('C', 'approved', '2026-11-13T10:00:00Z')],
    mine: [b('D', 'open', '2026-11-15T09:00:00Z'), b('E', 'rejected', '2026-11-12T09:00:00Z'), b('A', 'review', '2026-11-14T10:00:00Z'),
      b('F', 'imported', '2026-11-15T11:00:00Z')],
    failed: [j(7, 'failed', 'zhang', '2026-11-14T08:00:00Z', { error: '读不了 conda 环境\n详细信息' }), j(8, 'failed', 'wang', '2026-11-15T08:00:00Z'),
      j(9, 'failed', 'zhang', '2026-11-01T08:00:00Z'), j(10, 'done', 'zhang', '2026-11-15T08:00:00Z')],
  }, 'zhang', now);
  assert.deepEqual(rows.map((r) => [r.kind, r.batch ? r.batch.name : r.job.id]),
    [['review', 'B'], ['review', 'A'], ['import', 'C'], ['rejected', 'E'], ['unsent', 'D'], ['failed', 7]]);
  assert.deepEqual(rows.map(todoText), [
    '第 2 版，上传账号 li，入库到 SES-TX / 20261113/', '第 2 版，上传账号 li，入库到 SES-TX / 20261113/',
    '第 2 版已由 zhang 复核通过，入库到 SES-TX / 20261113/', '第 2 版已被 zhang 退回，请修改后上传新版次', '第 2 版尚未提交复核',
    '评估：读不了 conda 环境']);
  assert.deepEqual(todoItems({}, 'zhang', now), []);
  assert.equal(todoText({ kind: 'failed', job: j(11, 'failed', 'zhang', '2026-11-15T08:00:00Z', { error: null }) }), '评估');
});

test('a column sorts numbers by value, text by its words and the numbers in it, and blank cells last both ways', () => {
  const sort = (keys, numeric, desc) => keys.slice().sort((a, b) => compareCells(a, b, numeric, desc));
  assert.deepEqual(sort(['10', '', '9', '1024', '-'], true, false), ['9', '10', '1024', '', '-']);
  assert.deepEqual(sort(['10', '', '9', '1024'], true, true), ['1024', '10', '9', '']);
  assert.deepEqual(sort(['任务 12', '任务 3', '任务 101'], false, false), ['任务 3', '任务 12', '任务 101']);
  assert.deepEqual(sort(['SES / TX', 'ses / ax', 'ABC / L2'], false, false), ['ABC / L2', 'ses / ax', 'SES / TX']);
  assert.deepEqual(sort(['2026-11-15T08:00:00Z', '', '2026-11-14T09:30:00Z'], false, true),
    ['2026-11-15T08:00:00Z', '2026-11-14T09:30:00Z', '']);
});

test('the record of operations says each one in words and leads to what it was done to', () => {
  const said = [
    ['dataset.create', { kind: 'pool', site: 'SES', line: 'TX', classes: ['scratch', 'dent'] }, '数据池；SES / TX；类别：scratch、dent'],
    ['dataset.classes', { added: ['pit'], count: 3 }, '添加：pit；共 3 个类别'],
    ['version.create', { kind: 'pool', file_count: 45, total_bytes: 2048, parent_id: 7, source: 'fw dataset import host:/data/pool' }, '45 个文件，2.0 KB；来源：fw dataset import · pool'],
    ['version.label', { label: 'v0.1.0', note: '' }, '版本号 v0.1.0'],
    ['model_version.unlabel', { label: 'v0.2.0', note: '设错了' }, '版本号 v0.2.0；原因：设错了'],
    ['batch.upload', { pool: 'SES-TX', dir: '20261113', labeler: '张三' }, '入库到 SES-TX / 20261113/；标注人：张三'],
    ['batch.upload', { labeler: '张三', by_reviewer: true }, '标注人：张三；由复核人修改后上传'],
    ['batch.submit', { reviewer: 'li', self_review: false }, '复核人 li'],
    ['batch.reject', { comment: '划痕漏标', images: 3 }, '意见：划痕漏标；问题图片 3 张'],
    ['batch.approve', { changed_by_reviewer: false }, ''],
    ['batch.import', { pool_version: 'SES-TX@5', dir: '20261113', replace: true }, '入库为 SES-TX@5 的 20261113/；替换原有目录'],
    ['batch.close', { comment: '重复上传' }, '原因：重复上传'],
    ['site.rename', { name: '' }, '清除显示名'],
    ['line.benchmark', { dataset: 'SES-TX-test', note: '' }, '基准测试集 SES-TX-test'],
    ['model.create', { site: 'SES', line: 'TX', task: 'det' }, 'SES / TX；检测'],
    ['model_version.register', { trainset_id: 3, onnx_sha256: 'ab'.repeat(32) }, 'ONNX 哈希：abababababab'],
    ['model_version.tag', { tag: 'candidate', added: true, note: '' }, '标记为「候选」'],
    ['model_version.tag', { tag: 'deprecated', added: false, note: '误标' }, '取消标记「弃用」；备注：误标'],
    ['eval.register', { model_version: 'M@2', testset_id: 9, split: 'test', job_id: 12 }, '模型版本 M@2；快照划分：test；任务 12'],
    ['prediction_set.register', { model: 'M@2', data: 'SES-TX@5', job_id: 13, images: 120, boxes: 340 }, 'M@2 在 SES-TX@5 上；120 张图片，340 个框；任务 13'],
    ['release.register', { model_version: 'M@2', site: 'SES', line: 'TX', deploy_commit: '0123456789abcdef' }, 'SES / TX 部署 M@2；部署提交号：01234567'],
    ['job.submit', { kind: 'check', model: null, data: null, runner: 'rfdetr@2' }, '检查推理环境；推理环境：rfdetr@2'],
    ['job.cancel', { was: 'running' }, '运行中，已请求停止'],
    ['user.role', { from: 'algo', to: 'field' }, '角色由算法工程师改为现场实施'],
    ['user.disable', { tokens_revoked: 2 }, '同时吊销 2 个凭据'],
    ['token.revoke', { token_id: 4, label: '现场笔记本' }, '凭据 4（现场笔记本）'],
    ['something.new', { a: 1, b: null }, 'a=1'],
  ];
  for (const [action, detail, text] of said) assert.equal(eventText(action, detail), text, action);
  const links = [
    ['dataset.create', 'SES-TX', '#/d/SES-TX'], ['version.label', 'SES-TX@3', '#/v/SES-TX/3'], ['batch.reject', 'SES-TX-20261113@2', '#/b/SES-TX-20261113'],
    ['site.add', 'SES', '#/?site=SES'], ['line.benchmark', 'SES/TX', '#/?site=SES&line=TX'], ['model.create', 'SES-TX-detr', '#/m/SES-TX-detr'],
    ['model_version.tag', 'SES-TX-detr@3', '#/mv/SES-TX-detr/3'], ['eval.register', '评估 12', '#/eval/12'], ['prediction_set.register', '预测集 5', '#/p/5'],
    ['job.cancel', '任务 17', '#/job/17'], ['release.register', 'SES-TX-r3', '#/releases?ref=SES-TX-r3'], ['runner.register', 'rfdetr@2', null],
    ['user.create', 'zhang', null], ['token.create', 'zhang', null],
  ];
  for (const [action, object, href] of links) assert.equal(eventHref(action, object), href, action);
  assert.equal(eventHref('user.role', 'zhang', true), '#/users');
});

test('the data to evaluate or predict on is offered by kind, the line of the model first', () => {
  const v = { ref: 'x@1' };
  const datasets = [
    { name: 'B-test', kind: 'test', site: 'B', line: 'L', latest: v }, { name: 'A-test', kind: 'test', site: 'A', line: 'L', latest: v },
    { name: 'A-empty', kind: 'test', site: 'A', line: 'L', latest: null }, { name: 'A-pool', kind: 'pool', site: 'A', line: 'L', latest: v },
    { name: 'A-snap', kind: 'trainset', site: 'A', line: 'L', latest: v }, { name: 'A-pool-20261101', kind: 'batch', site: 'A', line: 'L', latest: v },
    { name: 'shared', kind: 'regression', site: '', line: '', latest: v }, { name: 'C-pool', kind: 'pool', site: 'C', line: 'L', latest: v },
  ];
  const names = (groups) => groups.map(([label, list]) => [label, list.map((d) => d.name)]);
  assert.deepEqual(names(dataChoices(datasets, { site: 'b', line: 'l' })),
    [['测试集', ['B-test', 'A-test']], ['回归集', ['shared']], ['数据池', ['A-pool', 'C-pool']]]);
  assert.deepEqual(names(dataChoices(datasets)), [['测试集', ['A-test', 'B-test']], ['回归集', ['shared']], ['数据池', ['A-pool', 'C-pool']]]);
  assert.deepEqual(dataChoices([]), []);
});

test('a batch is pre-labelled with its line\'s release first, then the model versions of every line', () => {
  const v = (id, ref, site, line, tags = []) => ({ id, ref, site, line, tags });
  const versions = [v(1, 'A-det@1', 'A', 'L1', ['deprecated']), v(2, 'A-det@2', 'A', 'L1'), v(3, 'A2-det@1', 'A', 'L2'),
    v(4, 'B-det@1', 'B', 'L1'), v(5, 'A-det@3', 'A', 'L1', ['candidate'])];
  const refs = (out) => [out.released, out.groups.map(([label, list]) => [label, list.map((x) => x.ref)])];
  assert.deepEqual(refs(prelabelChoices(versions, { site: 'a', line: 'l1' })),
    ['', [['本产线', ['A-det@3', 'A-det@2']], ['同一现场的其他产线', ['A2-det@1']], ['其他现场', ['B-det@1']]]]);
  // the release of another line's model, even a deprecated one, is offered first and only there
  assert.deepEqual(refs(prelabelChoices(versions, { site: 'A', line: 'L2', model: { ref: 'A-det@1' } })),
    ['A-det@1', [['本产线', ['A2-det@1']], ['同一现场的其他产线', ['A-det@3', 'A-det@2']], ['其他现场', ['B-det@1']]]]);
  assert.deepEqual(refs(prelabelChoices([], { site: 'A', line: 'L1' })), ['', []]);
});

test('an image fits its stage and zooms around the pointer, within limits', () => {
  assert.deepEqual(fitView(200, 100, 400, 400), { scale: 2, x: 0, y: 100 });
  const v = zoomAt({ scale: 1, x: 0, y: 0 }, 2, 100, 50, 0.5, 4);
  assert.deepEqual(v, { scale: 2, x: -100, y: -50 });   // the image point under (100, 50) stays there
  assert.equal((100 - v.x) / v.scale, 100);
  assert.equal(zoomAt(v, 10, 0, 0, 0.5, 4).scale, 4);
  assert.equal(zoomAt(v, 0.01, 0, 0, 0.5, 4).scale, 0.5);
});

test('what a reviewer has seen and marked is read back as written, and anything else starts afresh', () => {
  const r = readReview('');
  assert.deepEqual([r.seen.size, r.marks.size], [0, 0]);
  r.seen.add('a.png');
  r.marks.set('b.png', '漏标');
  const back = readReview(reviewText(r));
  assert.deepEqual([[...back.seen], [...back.marks]], [['a.png'], [['b.png', '漏标']]]);
  for (const text of ['{', 'null', '[]', '"x"', '{"seen": "a", "marks": []}']) {
    const x = readReview(text);
    assert.deepEqual([x.seen.size, x.marks.size], [0, 0], text);
  }
  const mixed = readReview('{"seen": ["a", 1], "marks": {"a": "x", "b": 2}}');
  assert.deepEqual([[...mixed.seen], [...mixed.marks]], [['a'], [['a', 'x']]]);
  assert.equal(reviewKey('alice', 'B-1', 2), 'fw-review:alice:B-1@2');
});

test('the images under review are filtered and paged through, and a rejection sends the marks with their notes', () => {
  const before = parseManifest('fw-manifest 1\na.json\tl1\t1\na.png\ti1\t1\nb.png\ti2\t1\nc.png\ti3\t1\nc.json\tl3\t1\n');
  const after = parseManifest('fw-manifest 1\na.json\tl1\t1\na.png\ti1\t1\nb.json\tl2\t1\nb.png\ti2\t1\nc.png\ti3\t1\nd.png\ti4\t1\n');
  assert.deepEqual([...changedImages(before, after)].sort(), ['b.png', 'c.png', 'd.png']);  // labelled, unlabelled, added
  const state = { seen: new Set(['a.png', 'b.png']), marks: new Map([['b.png', ' 漏标 '], ['d.png', '']]) };
  const changed = changedImages(before, after);
  const paths = ['a.png', 'b.png', 'c.png', 'd.png'];
  const only = (f) => paths.filter((p) => reviewMatch(f, p, state, changed));
  assert.deepEqual([only('all'), only('changed'), only('marked'), only('unseen')],
    [paths, ['b.png', 'c.png', 'd.png'], ['b.png', 'd.png'], ['c.png', 'd.png']]);
  const marked = (p) => reviewMatch('marked', p, state, changed);
  assert.deepEqual([stepTo(paths, 0, 1, marked), stepTo(paths, 1, 1, marked), stepTo(paths, 3, 1, marked), stepTo(paths, 3, -1, marked)], [1, 3, -1, 1]);
  assert.deepEqual(stepTo(paths, -1, 1, () => true), 0);   // the image shown is not in the list: from the first
  assert.deepEqual(stripWindow(paths, 0, marked), [0, 1, 3]);   // the image shown stays in the strip
  const many = Array.from({ length: 500 }, (_, i) => `${i}`);
  assert.deepEqual(stripWindow(many, 250, () => true, 2), [248, 249, 250, 251, 252]);
  assert.deepEqual(rejectBody(' 请补标 ', state.marks, 2), { comment: '请补标', images: ['b.png', 'd.png'], notes: { 'b.png': '漏标' }, revision: 2 });
});

test('a batch shows its four steps by where it stands, and what became of the images sent back', () => {
  const now = new Date(2026, 10, 15, 18, 0);
  const at = new Date(2026, 10, 15, 17, 12).toISOString();
  const step = (s, revision, actor = 'li') => ({ step: s, revision, actor, at });
  const b = (status, steps, more = {}) => ({ status, revision: 2, owner: 'li', reviewer: 'zhang', imported: null, steps, ...more });
  const states = (x) => batchFlow(x, now).map((s) => s.state);
  const texts = (x) => batchFlow(x, now).map((s) => s.text);
  const up = [step('upload', 1), step('submit', 1), step('reject', 1, 'zhang'), step('upload', 2)];
  assert.deepEqual(states(b('open', up)), ['done', 'on', 'todo', 'todo']);
  assert.equal(texts(b('open', up))[1], '待 li 提交第 2 版');
  const sent = [...up, step('submit', 2)];
  assert.deepEqual(states(b('review', sent)), ['done', 'done', 'on', 'todo']);
  assert.deepEqual(texts(b('review', sent)).slice(0, 3), ['第 2 版 · 今天 17:12', 'li · 今天 17:12', '待 zhang 复核第 2 版']);
  const back = [...sent, step('reject', 2, 'zhang')];
  assert.deepEqual(states(b('rejected', back)), ['on', 'todo', 'bad', 'todo']);
  assert.deepEqual(texts(b('rejected', back)).slice(0, 3), ['待 li 上传第 3 版', '上传后提交给 zhang', '第 2 版已退回']);
  const ok = [...sent, step('approve', 2, 'zhang')];
  assert.deepEqual(states(b('approved', ok)), ['done', 'done', 'done', 'on']);
  assert.deepEqual(states(b('imported', [...ok, step('import', 2, 'zhang')], { imported: 'P@5' })), ['done', 'done', 'done', 'done']);
  assert.equal(texts(b('imported', ok, { imported: 'P@5' }))[3], '已入库，建立 P@5');
  // approved with the reviewer's own folder: a revision nobody submitted
  const own = [...sent, step('approve', 3, 'zhang')];
  assert.deepEqual(batchFlow(b('approved', own, { revision: 3 }), now).map((s) => [s.state, s.text]).slice(1, 3),
    [['done', 'zhang 修改后的一版'], ['done', 'zhang 通过 · 今天 17:12']]);
  // and then closed before it was imported: it stopped at 入库 (review of #54)
  const shut = batchFlow(b('closed', [...own, step('close', null, 'zhang')], { revision: 3 }), now);
  assert.deepEqual(shut.map((s) => s.state), ['done', 'done', 'done', 'bad']);
  assert.deepEqual(shut.map((s) => s.text).slice(1), ['zhang 修改后的一版', 'zhang 通过 · 今天 17:12', '批次已关闭']);
  assert.deepEqual(states(b('closed', [...sent, step('close', null)])), ['done', 'done', 'bad', 'off']);
  assert.equal(texts(b('closed', up))[1], '批次已关闭');

  const checks = rejectionChecks({ images: ['a.png', 'b.png', 'c.png'], notes: { 'a.png': '漏标' } }, new Set(['a.png']), ['a.png', 'b.png']);
  assert.deepEqual(checks, { done: 2, rows: [{ path: 'a.png', note: '漏标', change: 'changed' },
    { path: 'b.png', note: '', change: 'same' }, { path: 'c.png', note: '', change: 'gone' }] });
});

test('a list is grouped by site and line in the order of the tree, what has no line last', () => {
  const tree = { sites: [{ code: 'SES', name: '', lines: [{ code: 'TX', name: '贴片线' }, { code: 'ASM', name: '' }] },
    { code: 'HZ', name: '杭州', lines: [{ code: 'L1', name: '' }] }] };
  const rows = [['a', 'HZ', 'L1'], ['b', '', ''], ['c', 'ses', 'tx'], ['d', 'SES', ''], ['e', 'OLD', 'X'], ['f', 'SES', 'ASM'],
    ['g', 'SES', 'TX'], ['h', 'SES', 'ZZ'], ['i', 'OLD', '']].map(([name, site, line]) => ({ name, site, line }));
  const shown = (at) => placeGroups(tree, at, rows, (r) => r, '不属于任何产线').map((g) => [g.title, g.items.map((r) => r.name)]);
  const all = { site: '', line: '' };
  assert.deepEqual(shown(all), [
    ['SES / TX（贴片线）', ['c', 'g']],          // one line however its code is spelled; rows in the order given
    ['SES / ASM', ['f']],
    ['SES / ZZ', ['h']],                         // a line not registered: after the site's own
    ['SES（未指定产线）', ['d']],
    ['HZ（杭州） / L1', ['a']],
    ['OLD / X', ['e']], ['OLD（未指定产线）', ['i']],  // a site not registered: after the registered ones
    ['不属于任何产线', ['b']]]);
  assert.deepEqual(placeGroups(tree, all, rows, (r) => r, 'x').map((g) => g.key).slice(0, 2), ['ses/tx', 'ses/asm']);
  // one line chosen: a plain list, unless something on no line is listed there too (a snapshot of several lines)
  const one = [rows[2], rows[6]];
  assert.deepEqual(placeGroups(tree, { site: 'SES', line: 'TX' }, one, (r) => r, '跨产线').map((g) => [g.title, g.items.length]), [[null, 2]]);
  assert.deepEqual(placeGroups(tree, { site: 'SES', line: 'TX' }, [...one, rows[1]], (r) => r, '跨产线').map((g) => g.title),
    ['SES / TX（贴片线）', '跨产线']);
  assert.deepEqual(placeGroups(tree, { site: 'SES', line: '' }, one, (r) => r, '跨产线').map((g) => g.title), ['SES / TX（贴片线）']);
  assert.deepEqual(placeGroups(tree, all, [], (r) => r, 'x'), []);
  assert.deepEqual(placeGroups(tree, all, [{ name: 'j' }], () => null, '不属于任何产线').map((g) => g.title), ['不属于任何产线']);
});
