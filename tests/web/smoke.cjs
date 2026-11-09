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
  await page.waitForSelector('table');
  seen.datasets = await page.$$eval('table tr td:first-child', (tds) => tds.map((t) => t.textContent));
  seen.who = await page.textContent('#who');
  await page.screenshot({ path: `${out}/datasets.png` });
  await page.click(`text=${dataset}`);
  await page.waitForSelector('h2');
  seen.versions = await page.$$eval('table tr td:first-child', (tds) => tds.map((t) => t.textContent));
  await page.click(`text=${dataset}@2`);
  await page.waitForSelector('.folders');
  seen.folders = await page.$$eval('.folders a', (as) => as.map((a) => a.textContent));
  await page.click('.folders a >> nth=0');
  await page.waitForSelector('.tile img[src^="blob:"]');
  seen.tiles = await page.$$eval('.tile', (ts) => ts.length);
  seen.labeled = await page.$$eval('.tile .dot.on', (ts) => ts.length);
  await page.screenshot({ path: `${out}/version.png` });
  await page.click('.tile >> nth=0');
  await page.waitForFunction(() => document.querySelector('.viewer dl') !== null);
  seen.image = await page.textContent('.viewer .panel');
  seen.canvas = await page.$eval('canvas', (c) => [c.width, c.height]);
  await page.keyboard.press('ArrowRight');
  await page.waitForFunction(() => document.querySelector('.row .muted') && document.querySelector('.row .muted').textContent.startsWith('2 /'));
  await page.screenshot({ path: `${out}/image.png` });
  await page.goto(`${url}#/d/${encodeURIComponent(dataset)}`);
  await page.waitForSelector('select#old');
  await page.click('button:has-text("比较")');
  await page.waitForSelector('h1');
  await page.waitForFunction(() => document.querySelector('h1').textContent.includes('→'));
  seen.diff = await page.textContent('main p');
  await page.screenshot({ path: `${out}/diff.png` });
  await page.click('#who button');
  await page.waitForSelector('form.login');
  seen.errors = errors;
  console.log(JSON.stringify(seen));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
