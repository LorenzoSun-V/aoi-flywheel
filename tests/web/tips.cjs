// The details of a page behind its ⓘ marks (run by tests/server/test_web.py):
// node tips.cjs <url> <algo token> <admin token> <pool with a version> <out dir>.
// Every page keeps only short lines in grey (the longer ones are behind an ⓘ); on the add-folders page the ⓘ of
// the title shows its text while pointed at or focused, keeps it after a click until Esc, a second click or a
// click elsewhere; the ⓘ of a check box does not tick it. Prints one JSON line.
const { chromium } = require('playwright');

const LONG = 60;  // a grey line longer than this on a page is a hint that belongs behind an ⓘ

(async () => {
  const [url, algo, admin, pool, out] = process.argv.slice(2);
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  page.on('console', (m) => { if (m.type() === 'error' && !/40[49]/.test(m.text())) errors.push(m.text()); });  // 404: no release on the line yet
  const as = async (token) => {
    await page.goto(url);
    await page.evaluate((t) => localStorage.setItem('fw-token', t), token);
    await page.reload();
  };
  const seen = { long: {}, marks: {} };
  const visit = async (hash) => {
    await page.evaluate(() => document.querySelector('main h1')?.setAttribute('data-left', ''));  // the page before
    await page.goto(`${url}${hash}`);
    await page.waitForSelector('main h1:not([data-left])');
    await page.waitForLoadState('networkidle');
    const long = await page.$$eval('main p.muted, main span.muted, main .muted.small', (xs, n) => xs.map((x) => x.textContent.trim())
      .filter((t) => t.length > n), LONG);
    if (long.length) seen.long[hash] = long;
    seen.marks[hash] = await page.$$eval('main button.info', (bs) => bs.length);
  };
  try {
    await as(algo);
    for (const hash of ['#/', '#/?site=SES&line=TX', '#/datasets', `#/d/${pool}`, `#/v/${pool}/1`, `#/add/${pool}`, `#/upload/${pool}`,
      `#/remove/${pool}`, '#/dataset-new', `#/trainset-new?from=${pool}@1`, '#/places', '#/models', '#/model-register', '#/batches', '#/batch-new', '#/jobs', '#/predictions',
      '#/events', '#/me']) await visit(hash);
    await as(admin);
    await visit('#/users');
    await as(algo);

    // the ⓘ of the add page's title
    await page.goto(`${url}#/add/${pool}`);
    await page.waitForFunction((n) => document.querySelector('main h1')?.textContent === `向 ${n} 添加目录`, pool);
    const mark = 'main h1 button.info';
    const tipText = async () => page.$eval('#tip', (t) => t.textContent).catch(() => null);
    await page.hover(mark);
    seen.hovered = await tipText();
    seen.label = await page.getAttribute(mark, 'aria-label');
    await page.mouse.move(5, 880);
    seen.left = await tipText();
    await page.click(mark);
    await page.mouse.move(5, 880);
    seen.clicked = { text: await tipText(), expanded: await page.getAttribute(mark, 'aria-expanded') };
    await page.screenshot({ path: `${out}/tips-open.png` });
    await page.keyboard.press('Escape');
    seen.escaped = await tipText();
    await page.click(mark);
    await page.click(mark);
    seen.secondClick = await tipText();
    await page.click(mark);
    await page.mouse.click(900, 860);
    seen.elsewhere = await tipText();
    await page.focus(mark);
    seen.focused = await tipText();
    await page.focus('#add-inside');
    seen.blurred = await tipText();

    // the ⓘ of a check box tells, it does not tick
    await page.click('label:has(#add-inside) button.info');
    seen.box = { ticked: await page.isChecked('#add-inside'), text: await tipText() };
    await page.screenshot({ path: `${out}/tips-check.png` });
    // a mark is not part of the name a screen reader gives its field or box
    seen.controls = (await page.locator('main').ariaSnapshot()).split('\n').map((x) => x.trim())
      .filter((x) => /^- (checkbox|textbox)/.test(x));
  } catch (e) {
    await page.screenshot({ path: `${out}/tips-failed.png`, fullPage: true }).catch(() => {});
    throw new Error(`${e.message}\npage: ${await page.textContent('main').catch(() => '')}`);
  }
  seen.errors = errors;
  console.log(JSON.stringify(seen));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
