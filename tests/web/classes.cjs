// The class table of a pool in the browser (run by tests/server/test_web.py):
// node classes.cjs <url> <algo token> <field token> <pool> <out dir>.
// The pool is one from before the class tables: a version, no table, one label naming Scratch. Its upload pages
// say it takes no upload; its page asks for the table; a duplicate is refused; the table is saved and the newest
// version's labels are checked against it; a class is added at its end. A field account sees the table only.
// Prints one JSON line of what it saw.
const { chromium } = require('playwright');

(async () => {
  const [url, token, field, pool, out] = process.argv.slice(2);
  const browser = await chromium.launch();
  const errors = [];
  // a page (and browser context) of its own for each account, as in register.cjs
  const open = async (t) => {
    const p = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    p.on('pageerror', (e) => errors.push(String(e)));
    p.on('console', (m) => { if (m.type() === 'error' && !/400/.test(m.text())) errors.push(m.text()); });  // the refusal asked for below
    await p.goto(url);
    await p.evaluate((x) => localStorage.setItem('fw-token', x), t);
    await p.reload();
    return p;
  };
  const seen = {};
  const page = await open(token);
  const rows = () => page.$$eval('#class-table tr td:last-child', (tds) => tds.map((t) => t.textContent));

  await page.goto(`${url}#/add/${encodeURIComponent(pool)}`);
  await page.waitForSelector('main .error');
  seen.add = await page.textContent('main .error');
  await page.goto(`${url}#/upload/${encodeURIComponent(pool)}`);
  await page.reload();
  await page.waitForSelector('main .error');
  seen.upload = await page.textContent('main .error');

  await page.goto(`${url}#/d/${encodeURIComponent(pool)}`);
  await page.waitForSelector('#class-go');
  seen.none = await page.textContent('#class-none');
  seen.uploadButtons = await page.$$eval('a.button', (as) => as.map((a) => a.textContent).filter((t) => t.startsWith('上传')));
  seen.formOpen = await page.$eval('#class-form', (d) => d.open);
  seen.button = await page.textContent('#class-go');
  await page.fill('#class-add', 'scratch\nSCRATCH\n');
  await page.click('#class-go');
  await page.waitForSelector('#class-form .error');
  seen.duplicate = await page.textContent('#class-form .error');
  await page.fill('#class-add', 'scratch\ndent\n');
  await page.click('#class-go');
  await page.waitForSelector('#class-table');
  seen.saved = await rows();
  seen.savedToast = await page.textContent('#toast');
  seen.check = await page.textContent('#class-check');
  seen.noneAfter = await page.$('#class-none') !== null;
  seen.formOpenAfter = await page.$eval('#class-form', (d) => d.open);
  await page.screenshot({ path: `${out}/classes.png`, fullPage: true });
  await page.click('#class-form summary');
  seen.buttonAfter = await page.textContent('#class-go');
  await page.fill('#class-add', 'crack');
  await page.click('#class-go');
  await page.waitForFunction(() => document.querySelectorAll('#class-table tr').length === 4);
  seen.added = await rows();
  seen.checkAfter = await page.$('#class-check') !== null;

  const other = await open(field);
  await other.goto(`${url}#/d/${encodeURIComponent(pool)}`);
  await other.reload();
  await other.waitForSelector('#versions');  // a pool with its class table opens on its versions; the table is a tab
  seen.fieldTabs = await other.$$eval('.page-tabs a', (as) => as.map((a) => a.textContent));
  await other.click('#tab-classes');
  await other.waitForSelector('#class-table');
  seen.fieldRows = await other.$$eval('#class-table tr td:last-child', (tds) => tds.map((t) => t.textContent));
  seen.fieldForm = await other.$('#class-form') !== null;
  seen.errors = errors;
  console.log(JSON.stringify(seen));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
