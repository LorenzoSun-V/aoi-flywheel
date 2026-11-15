// Giving a version its label (v0.1.0) in the browser (run by tests/server/test_web.py):
// node labels.cjs <url> <token> <dataset with two versions> <out dir>.
// From the ⋯ menu of version 1 takes the proposed minor label (the page stays where it was and says so), is
// refused the same label on version 2, and opens the version by its label. Prints one JSON line of what it saw.
const { chromium } = require('playwright');

(async () => {
  const [url, token, name, out] = process.argv.slice(2);
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1280, height: 520 } });
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  page.on('console', (m) => { if (m.type() === 'error' && !/409/.test(m.text())) errors.push(m.text()); });
  await page.goto(url);
  await page.evaluate((t) => localStorage.setItem('fw-token', t), token);
  await page.goto(`${url}#/d/${name}`);
  await page.reload();
  const seen = {};
  // from a version's ⋯ menu: the form opens under the table for that version
  const open = async (number) => {
    await page.click(`#versions tr[data-number="${number}"] .more .dots`);
    await page.click('.menu [role=menuitem]:has-text("设定版本号")');
    await page.waitForSelector('#label-go');
    return page.textContent('#label-form h3');
  };
  await page.waitForSelector('#versions');
  seen.menu = await (async () => {
    await page.click('#versions tr[data-number="2"] .more .dots');
    const items = await page.$$eval('.menu [role=menuitem]', (xs) => xs.map((x) => x.textContent));
    await page.keyboard.press('Escape');
    return items;
  })();
  seen.closed = await page.$eval('#label-form', (f) => f.hidden);
  seen.form = await open(1);
  seen.proposed = await page.textContent('#label-minor');
  await page.click('#label-minor');
  await page.fill('#label-note', '冷启动');
  await page.evaluate(() => window.scrollTo(0, 120));
  const before = await page.evaluate(() => window.scrollY);
  await page.click('#label-go');
  await page.waitForSelector('#labels .tag.ver');
  seen.row = await page.textContent('#labels tr:last-child');
  seen.toast = await page.textContent('#toast');
  seen.stayed = [before, await page.evaluate(() => window.scrollY)];  // the page is painted again where it was
  await open(2);
  seen.next = await page.textContent('#label-minor');
  await page.fill('#label-text', 'v0.1.0');
  await page.click('#label-go');
  await page.waitForSelector('#label-form .error');
  seen.taken = await page.textContent('#label-form .error');
  await page.screenshot({ path: `${out}/labels.png`, fullPage: true });
  await page.goto(`${url}#/v/${name}/v0.1.0`);
  await page.waitForSelector('h1 .tag.ver');
  seen.title = await page.textContent('h1');
  seen.errors = errors;
  console.log(JSON.stringify(seen));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
