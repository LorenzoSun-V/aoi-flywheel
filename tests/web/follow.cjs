// A test set's class table (run by tests/server/test_web.py):
// node follow.cjs <url> <algo token> <field token> <test set made before> <pool>.
// The test set from before follows no table: its page says so and offers no upload, the upload pages refuse it; the class tab names the
// pool once; then the page shows the table and the check of its newest version, and a field account reads it.
// Prints one JSON line.
const { chromium } = require('playwright');

(async () => {
  const [url, token, fieldToken, name, pool] = process.argv.slice(2);
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  const open = async (t) => {
    await page.goto(url);
    await page.evaluate((x) => localStorage.setItem('fw-token', x), t);
    await page.goto(`${url}#/d/${name}`);
    await page.reload();
    await page.waitForSelector('#tab-classes');
  };
  const seen = { errors };
  await open(token);
  seen.none = await page.textContent('#class-none');
  seen.upload = await page.locator('#add-folders').count();
  seen.tabOn = await page.getAttribute('#tab-classes', 'class');
  for (const to of ['add', 'upload']) {  // the upload pages refuse it before any folder is chosen
    await page.goto(`${url}#/${to}/${name}`);
    await page.waitForSelector('main .error');
    seen[`${to}Page`] = await page.textContent('main .error');
  }
  await page.goto(`${url}#/d/${name}`);
  await page.waitForSelector('#follow-pool');
  seen.choices = await page.$$eval('#follow-pool option', (o) => o.map((x) => x.value).filter(Boolean));
  await page.click('#follow-go');
  await page.waitForSelector('#follow-form .error');
  seen.unpicked = await page.textContent('#follow-form .error');
  await page.selectOption('#follow-pool', pool);
  await page.click('#follow-go');
  await page.waitForSelector('#class-table');
  seen.head = await page.textContent('main p.muted');
  seen.rows = await page.$$eval('#class-table tr td:nth-child(2)', (c) => c.map((x) => x.textContent));
  seen.check = await page.textContent('#class-check');
  seen.form = await page.locator('#follow-form').count();
  seen.uploadAfter = await page.locator('#add-folders').count();
  await open(fieldToken);
  await page.click('#tab-classes');
  await page.waitForSelector('#class-table');
  seen.field = { rows: await page.locator('#class-table tr').count() - 1, form: await page.locator('#follow-form').count() };
  console.log(JSON.stringify(seen));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
