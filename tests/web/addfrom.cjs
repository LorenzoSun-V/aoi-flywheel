// Where folders are added from (run by tests/server/test_web.py):
// node addfrom.cjs <url> <algo token> <field token> <pool> <test set> <site> <line>.
// The newest and an older version page of the pool, the ⋯ of each data row on the line's overview, and the
// same pages as a field account. Prints one JSON line.
const { chromium } = require('playwright');

(async () => {
  const [url, token, fieldToken, pool, test, site, line] = process.argv.slice(2);
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  await page.goto(url);
  const as = async (t) => { await page.evaluate((x) => localStorage.setItem('fw-token', x), t); await page.reload(); };
  const versionAt = async (name, n) => {
    await page.goto(`${url}#/v/${name}/${n}`);
    await page.waitForFunction((r) => document.querySelector('main h1')?.textContent.startsWith(r) && document.querySelector('dl.kv'), `${name}@${n}`);
    return page.$$eval('.page-head .actions > a', (a) => a.map((x) => [x.id, x.textContent]));
  };
  const overview = async () => {
    await page.goto(`${url}#/?site=${site}&line=${line}`);
    await page.waitForFunction(() => document.querySelector('table.data'));
    return page.$$eval('table.data tr', (rows) => rows.slice(1).map((r) => [r.cells[0].textContent.split('@')[0], r.querySelectorAll('button.dots').length]));
  };
  const seen = { errors };
  await as(token);
  seen.newest = await versionAt(pool, 2);
  seen.older = await versionAt(pool, 1);
  seen.testNewest = await versionAt(test, 1);
  seen.rows = await overview();
  const menuOf = async (name) => {
    await page.click(`table.data tr:has(a[href*="/${name}/"]) button.dots`);
    const items = await page.$$eval('.menu [role=menuitem]', (x) => x.map((i) => [i.textContent, i.getAttribute('href')]));
    await page.keyboard.press('Escape');
    return items;
  };
  seen.poolMenu = await menuOf(pool);
  seen.testMenu = await menuOf(test);
  await page.click(`table.data tr:has(a[href*="/${pool}/"]) button.dots`);
  await page.click('.menu [role=menuitem]:has-text("上传新增目录")');
  await page.waitForFunction(() => document.querySelector('main h1')?.textContent.startsWith('向 '));
  seen.opened = await page.evaluate(() => location.hash);
  await page.goto(url);
  await as(fieldToken);
  seen.fieldNewest = await versionAt(pool, 2);
  seen.fieldRows = await overview();
  console.log(JSON.stringify(seen));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
