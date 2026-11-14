// Browsing the lists by site and line (run by tests/server/test_web.py):
// node browse.cjs <url> <token> <site> <pool on TX> <pool on TY> <snapshot from TX> <model on TX> <out dir>.
// Picks the line TY in the tree, then TX, searches, and follows the scope to the models. Prints one JSON line.
const { chromium } = require('playwright');

(async () => {
  const [url, token, site, a, b, snapshot, model, out] = process.argv.slice(2);
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
  await page.goto(url);
  await page.evaluate((t) => localStorage.setItem('fw-token', t), token);
  await page.goto(`${url}#/datasets`);
  await page.reload();
  const seen = {};
  const lists = async () => ({
    pools: await page.$$eval('#pools + table tr td:first-child', (c) => c.map((x) => x.textContent)),
    snapshots: await page.$$eval('#snapshots + table tr td:first-child', (c) => c.map((x) => x.textContent)),
  });
  await page.waitForSelector('#pools');
  seen.all = await lists();
  seen.tree = await page.$$eval('#places li', (items) => items.map((x) => x.textContent));
  await page.click(`#places li.site a:has-text("${site}")`);
  await page.waitForFunction(() => location.hash.includes('site=') && document.querySelector('#places li.site a.on'));
  seen.site = await lists();
  await page.click(`#places li.line a[href*="site=${site}&line=TY"]`);  // other sites have a line of this name too
  await page.waitForFunction(() => location.hash.includes('line=TY') && document.querySelector('#places li.line a.on'));
  seen.ty = await lists();
  seen.tyHash = page.url().split('#')[1];
  await page.click(`#places li.line a[href*="site=${site}&line=TX"]`);  // other sites have a line of this name too
  await page.waitForFunction(() => location.hash.includes('line=TX') && (document.querySelector('#places li.line a.on')?.textContent || '').startsWith('TX'));
  seen.tx = await lists();
  seen.formSite = await page.inputValue('#site');
  seen.formLine = await page.inputValue('#line');
  await page.screenshot({ path: `${out}/browse.png`, fullPage: true });
  await page.fill('#search', 'no-such-name');
  await page.press('#search', 'Enter');
  await page.waitForFunction(() => location.hash.includes('q=no-such-name') && !document.querySelector('#pools + table'));
  seen.none = await page.textContent('.browse .list');
  await page.click('.browse a:has-text("清除")');
  await page.waitForSelector('#pools + table');
  // the links at the top keep the line
  seen.navModels = await page.getAttribute('#nav a:has-text("模型")', 'href');
  await page.click('#nav a:has-text("模型")');
  await page.waitForFunction(() => document.querySelector('main h1')?.textContent === '模型' && document.querySelector('#places li.line a.on'));
  seen.models = await page.$$eval('.browse table tr td:first-child', (c) => c.map((x) => x.textContent));
  seen.navOn = await page.textContent('#nav a.on');
  await page.click('#places li a:has-text("全部")');
  await page.waitForFunction(() => !location.hash.includes('site=') && !document.querySelector('#places li.line a.on'));
  seen.navAfter = await page.getAttribute('#nav a:has-text("数据集")', 'href');
  await page.goto(`${url}#/batches?view=all&site=${site}`);
  await page.waitForFunction(() => document.querySelector('main h1')?.textContent === '标注批次' && document.querySelector('#places li.site a.on'));
  seen.batches = await page.textContent('.browse .list');
  seen.errors = errors;
  console.log(JSON.stringify(seen));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
