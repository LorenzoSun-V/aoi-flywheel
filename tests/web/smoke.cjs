// A browser walk through the pages (run by tests/server/test_web.py when Playwright for node is installed):
// node smoke.cjs <url> <token> <dataset> <out dir>. Prints one JSON line of what it saw.
const { chromium } = require('playwright');

(async () => {
  const [url, token, dataset, out] = process.argv.slice(2);
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
  const fetched = [];  // the files the pages fetch: thumbnails or whole objects
  page.on('request', (r) => { const m = r.url().match(/\/api\/v1\/objects\/[0-9a-f]{64}(\/thumb\?size=\d+)?$/); if (m) fetched.push(m[1] ? 'thumb' : 'object'); });
  const seen = {};
  await page.goto(url);
  await page.waitForSelector('form.login');
  await page.fill('input[type=password]', 'wrong');
  await page.click('button[type=submit]');
  await page.waitForSelector('.error');
  seen.wrong = await page.textContent('.error');
  errors.length = 0;  // the 401 of the wrong token
  await page.fill('input[type=password]', token);
  await page.click('button[type=submit]');
  await page.waitForFunction(() => document.querySelector('main h1')?.textContent === '概览');  // signing in leads home
  seen.home = await page.textContent('#nav a.on');
  await page.click('#nav a:has-text("数据集")');
  await page.waitForSelector('#pools + table');
  seen.datasets = await page.$$eval('table tr td:first-child', (tds) => tds.map((t) => t.textContent));
  seen.who = await page.textContent('#who');
  await page.screenshot({ path: `${out}/datasets.png` });
  await page.click(`a:text-is("${dataset}")`);
  await page.waitForSelector('#versions');  // the dataset's page
  seen.versions = await page.$$eval('#versions tr td:first-child', (tds) => tds.map((t) => t.textContent));
  await page.click(`#versions a[title="${dataset}@2"]`);
  await page.waitForSelector('.folders');
  seen.folders = await page.$$eval('.folders a', (as) => as.map((a) => a.textContent));
  await page.click('.folders a >> nth=0');
  await page.waitForFunction(() => { const is = [...document.querySelectorAll('.tile img')]; return is.length && is.every((i) => i.src.startsWith('blob:') && i.complete); });
  seen.tiles = await page.$$eval('.tile', (ts) => ts.length);
  seen.tileSizes = await page.$$eval('.tile img', (is) => is.map((i) => [i.naturalWidth, i.naturalHeight]));
  seen.tileFetches = fetched.slice();
  seen.labeled = await page.$$eval('.tile .dot.on', (ts) => ts.length);
  await page.screenshot({ path: `${out}/version.png` });
  await page.click('.tile >> nth=0');
  await page.waitForFunction(() => document.querySelector('.viewer dl') !== null);
  seen.image = await page.textContent('.viewer .panel');
  seen.canvas = await page.$eval('canvas', (c) => [c.width, c.height]);
  const at = (n) => page.waitForFunction((k) => document.querySelector('.row .muted') && document.querySelector('.row .muted').textContent.startsWith(`${k} /`), n);
  for (const [button, n] of [['下一张', 2], ['上一张', 1], ['下一张', 2], ['上一张', 1]]) {  // pages seen before must not keep their keys
    await page.click(`button:has-text("${button}")`);
    await at(n);
  }
  await page.evaluate(() => { window.hashchanges = 0; window.addEventListener('hashchange', () => { window.hashchanges += 1; }); });
  await page.keyboard.press('ArrowRight');
  await at(2);
  await page.waitForTimeout(300);
  seen.hashchanges = await page.evaluate(() => window.hashchanges);
  await page.screenshot({ path: `${out}/image.png` });
  await page.goto(`${url}#/d/${encodeURIComponent(dataset)}`);
  await page.waitForSelector('#versions');
  await page.click('#versions tr[data-number="2"] .more .dots');  // a version against its parent, from its ⋯
  await page.click('.menu [role=menuitem]:has-text("与父版本比较")');
  await page.waitForSelector('#diff-counts');
  seen.diff = await page.textContent('#diff-counts');
  seen.diffTitle = await page.textContent('main h1');
  await page.selectOption('#old', '2');  // any two versions, chosen on the page
  await page.selectOption('#new', '1');
  await page.click('#diff-pick button');
  await page.waitForFunction(() => document.querySelector('main h1').textContent.includes('@2 →'));
  seen.diffBack = await page.textContent('#diff-counts');
  await page.screenshot({ path: `${out}/diff.png` });
  await page.click('#who button');
  await page.waitForSelector('form.login');
  seen.errors = errors;
  console.log(JSON.stringify(seen));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
