// node --test tests/web/helpers.test.mjs: the parts of the web pages that do not need a browser.
import test from 'node:test';
import assert from 'node:assert/strict';
import { hrefOf, labelIndex, listDir, outline, parseManifest, route, sizeText } from '../../server/fwserver/web/app.js';

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
