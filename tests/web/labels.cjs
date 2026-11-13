// Giving a version its label (v0.1.0) in the browser (run by tests/server/test_web.py):
// node labels.cjs <url> <token> <dataset with two versions> <out dir>.
// Takes the proposed minor label for version 1, is refused the same label on version 2, and opens the version
// by its label. Prints one JSON line of what it saw.
const { chromium } = require('playwright');

(async () => {
  const [url, token, name, out] = process.argv.slice(2);
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  page.on('console', (m) => { if (m.type() === 'error' && !/409/.test(m.text())) errors.push(m.text()); });
  await page.goto(url);
  await page.evaluate((t) => localStorage.setItem('fw-token', t), token);
  await page.goto(`${url}#/d/${name}`);
  await page.reload();
  const seen = {};
  await page.waitForSelector('#label-go');
  seen.proposed = await page.textContent('#label-minor');
  await page.selectOption('#label-version', '1');
  await page.click('#label-minor');
  await page.fill('#label-note', '冷启动');
  await page.click('#label-go');
  await page.waitForSelector('table .tag.ver');
  seen.row = await page.textContent('table tr:last-child');
  seen.next = await page.textContent('#label-minor');
  await page.selectOption('#label-version', '2');
  await page.fill('#label-text', 'v0.1.0');
  await page.click('#label-go');
  await page.waitForSelector('main .error');
  seen.taken = await page.textContent('main .error');
  await page.screenshot({ path: `${out}/labels.png`, fullPage: true });
  await page.goto(`${url}#/v/${name}/v0.1.0`);
  await page.waitForSelector('h1 .tag.ver');
  seen.title = await page.textContent('h1');
  seen.errors = errors;
  console.log(JSON.stringify(seen));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
