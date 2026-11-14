// The overview of a line (run by tests/server/test_web.py):
// node overview.cjs <url> <token> <worker token> <site> <out dir>.
// Opens the overview, picks the line TX of the site, then TY, a line that is not registered, and the page as
// an account that may not see it. Prints one JSON line.
const { chromium } = require('playwright');

(async () => {
  const [url, token, workerToken, site, out] = process.argv.slice(2);
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  await page.goto(url);
  await page.evaluate((t) => localStorage.setItem('fw-token', t), token);
  await page.reload();
  const seen = {};
  const firstCells = (id) => page.$$eval(`#${id} + table tr td:first-child`, (c) => c.map((x) => x.textContent));
  await page.waitForSelector('#nav a');
  seen.nav = await page.$$eval('#nav a', (as) => as.map((a) => a.textContent));
  await page.click('#nav a:has-text("概览")');
  await page.waitForSelector('#lines');
  seen.lineRow = await page.$$eval(`#lines tr:has(a[href*="site=${site}&line=TX"]) td`, (c) => c.map((x) => x.textContent));
  await page.screenshot({ path: `${out}/overview-lines.png`, fullPage: true });
  await page.click(`#lines a[href*="site=${site}&line=TX"]`);
  await page.waitForFunction((s) => document.querySelector('main h1')?.textContent === `${s} / TX` && document.querySelector('#release'), site);
  seen.release = await page.textContent('#release + *');
  seen.models = await page.$$eval('#models + table tr:nth-child(2) td', (c) => c.map((x) => x.textContent));
  seen.snapshots = await firstCells('snapshots');
  seen.pools = await firstCells('pools');
  seen.batches = await page.$$eval('#batches + table tr:nth-child(2) td', (c) => c.map((x) => x.textContent));
  seen.treeOn = await page.textContent('#places li.line a.on');
  seen.navModels = await page.getAttribute('#nav a:has-text("模型")', 'href');
  seen.moreBatches = await page.getAttribute('#batches a', 'href');
  await page.screenshot({ path: `${out}/overview-line.png`, fullPage: true });
  await page.click(`#places li.line a[href*="site=${site}&line=TY"]`);
  await page.waitForFunction((s) => document.querySelector('main h1')?.textContent === `${s} / TY` && document.querySelector('#release'), site);
  seen.ty = await page.textContent('.browse .list');
  seen.tyPools = await firstCells('pools');
  await page.goto(`${url}#/overview?site=${site}&line=NOPE`);
  await page.waitForFunction((s) => document.querySelector('main h1')?.textContent === `${s} / NOPE` && document.querySelector('#release'), site);
  seen.unknown = await page.textContent('.browse .list');
  await page.goto(`${url}#/overview?site=${site}`);
  await page.waitForFunction(() => document.querySelector('#lines') && document.querySelector('#places li.site a.on'));
  seen.siteLines = await page.$$eval('#lines tr td:first-child', (c) => c.map((x) => x.textContent));
  // an account without the capability: no link, and the page says why
  await page.evaluate((t) => localStorage.setItem('fw-token', t), workerToken);
  await page.goto(`${url}#/jobs`);
  await page.reload();
  await page.waitForFunction(() => document.querySelector('main h1')?.textContent === '任务');
  seen.workerNav = await page.$$eval('#nav a', (as) => as.map((a) => a.textContent));
  await page.goto(`${url}#/overview`);
  await page.waitForSelector('main .error');
  seen.refused = await page.textContent('main .error');
  seen.errors = errors;
  console.log(JSON.stringify(seen));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
