// Saving a model version's ONNX from its page (run by tests/server/test_web.py):
// node onnx.cjs <url> <field token> <algo token> <model> <out dir>; the model has versions 1 (no 版本号),
// 2 (v0.9.0 and v1.0.0), 3 (v1.1.0, retired), and the weights of version 1 are missing from the store.
// A field account (deployment) saves the ONNX from the head of the page and from the file list, under the name of
// the version; the other files keep their names; a file the server cannot give says so. Prints one JSON line.
const crypto = require('crypto');
const fs = require('fs');
const { chromium } = require('playwright');

(async () => {
  const [url, field, algo, model, out] = process.argv.slice(2);
  const browser = await chromium.launch();
  const errors = [];
  const signIn = async (t) => {  // a browser context of its own for each account
    const p = await (await browser.newContext({ viewport: { width: 1440, height: 900 }, acceptDownloads: true })).newPage();
    p.on('pageerror', (e) => errors.push(String(e)));
    p.on('console', (m) => { if (m.type() === 'error' && !/404/.test(m.text())) errors.push(m.text()); });  // the missing weights
    await p.goto(url);
    await p.evaluate((x) => localStorage.setItem('fw-token', x), t);
    await p.reload();
    return p;
  };
  let page = await signIn(field);
  const seen = {};
  const open = async (number) => {
    await page.goto(`${url}#/mv/${encodeURIComponent(model)}/${number}`);
    await page.reload();  // the page of the version before is not taken for this one
    await page.waitForSelector('#onnx-download');
  };
  const save = async (button) => {
    const [got] = await Promise.all([page.waitForEvent('download'), page.click(button)]);
    const bytes = fs.readFileSync(await got.path());
    return { name: got.suggestedFilename(), sha256: crypto.createHash('sha256').update(bytes).digest('hex') };
  };
  const row = (path) => `main table tr:has(td:text-is("${path}")) button`;
  try {

  await open(1);
  seen.one = await save('#onnx-download');
  seen.oneRow = await save(row('model.onnx'));
  seen.icon = await page.$$eval('#onnx-download svg', (x) => x.length);  // still there after a download
  seen.classes = (await save(row('classes.txt'))).name;
  seen.tags = await page.locator('#tag-candidate, #tag-deprecated').count();
  await page.click(row('files/best.pt'));
  await page.waitForSelector('main table .bad');
  seen.missing = await page.textContent('main table .bad');
  await page.screenshot({ path: `${out}/onnx-download.png`, fullPage: true });
  await open(2);
  seen.two = (await save('#onnx-download')).name;
  await open(3);
  seen.three = (await save('#onnx-download')).name;

  // an algorithm engineer has the download and the tags side by side
  page = await signIn(algo);
  await open(1);
  seen.algo = await page.$$eval('.page-head .actions button:not(.info)', (b) => b.map((x) => x.textContent));

  } catch (e) {
    await page.screenshot({ path: `${out}/onnx-failed.png`, fullPage: true }).catch(() => {});
    throw new Error(`${e.message}\npage: ${await page.textContent('main').catch(() => '')}`);
  }
  seen.errors = errors;
  console.log(JSON.stringify(seen));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
