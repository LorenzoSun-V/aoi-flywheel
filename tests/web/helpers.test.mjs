// node --test tests/web/helpers.test.mjs: the parts of the web pages that do not need a browser.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash, randomBytes } from 'node:crypto';
import { createRequire } from 'node:module';
import {
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
