// A training-set snapshot in the browser (run by tests/server/test_web.py): its folders are the ones the default
// fw get makes. node snapshot.cjs <url> <token> <snapshot name> <out dir>. Prints one JSON line of what it saw.
const { chromium } = require('playwright');

(async () => {
  const [url, token, name, out] = process.argv.slice(2);
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
  const seen = {};
  const folders = () => page.$$eval('.folders a', (as) => as.map((a) => a.textContent.replace(/\s+/g, ' ').trim()));
  await page.goto(url);
  await page.waitForSelector('form.login');
  await page.fill('input[type=password]', token);
  await page.click('button[type=submit]');
  await page.waitForSelector('table');
  await page.goto(`${url}#/v/${encodeURIComponent(name)}/1`);
  await page.waitForSelector('.folders');
  seen.top = await folders();
  seen.note = await page.textContent('main p.muted');
  await page.screenshot({ path: `${out}/snapshot.png` });
  await page.click('.folders a:has-text("train") >> nth=-1');
  await page.waitForFunction(() => document.querySelector('h1').textContent.endsWith('· train'));
  seen.train = await folders();
  await page.click('.folders a:has-text("images")');
  await page.waitForSelector('.tile img[src^="blob:"]');
  seen.trainTiles = await page.$$eval('.tile .name', (ts) => ts.map((t) => t.textContent));
  seen.trainLabeled = await page.$$eval('.tile .dot.on', (ts) => ts.length);
  await page.click('.tile >> nth=0');
  await page.waitForFunction(() => document.querySelector('.viewer dl') !== null);
  seen.crumbs = (await page.textContent('.crumbs')).replace(/\s+/g, ' ');
  seen.image = await page.textContent('.viewer .panel');
  seen.count = await page.textContent('.row .muted');
  await page.screenshot({ path: `${out}/snapshot-image.png` });
  await page.goto(`${url}#/v/${encodeURIComponent(name)}/1?dir=neg_train`);
  await page.waitForFunction(() => document.querySelector('h1').textContent.endsWith('· neg_train') && document.querySelector('.tile'));
  seen.negTiles = await page.$$eval('.tile .name', (ts) => ts.map((t) => t.textContent));
  await page.goto(`${url}#/v/${encodeURIComponent(name)}/1?dir=val/images`);
  await page.waitForFunction(() => document.querySelector('h1').textContent.endsWith('· val/images') && document.querySelector('.tile'));
  seen.valTiles = await page.$$eval('.tile .name', (ts) => ts.map((t) => t.textContent));
  seen.errors = errors;
  console.log(JSON.stringify(seen));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
