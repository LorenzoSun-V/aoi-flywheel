// The layout every page shares (run by tests/server/test_web.py):
// node layout.cjs <url> <algo token> <pool with a long note and source> <batch that was sent back> <out dir>.
// Tables keep each cell on one line and fit their panels, a text cut short keeping a few words; a long note is
// cut short with its whole text on pointing; a version is its number in its dataset's table; times read
// 今天 HH:MM; states are a dot and words; version labels are not green; forms keep one width with check boxes
// under the fields; files are chosen in a box like the folders; the sign-in page has no side bar, the account's
// name and role are on two lines. Prints one JSON line of what it saw.
const { chromium } = require('playwright');

(async () => {
  const [url, token, pool, batch, out] = process.argv.slice(2);
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1180, height: 900 } });
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
  const seen = {};

  // signed out: the sign-in form alone, with the platform's name; no sections offered
  await page.goto(`${url}#/login`);
  await page.waitForSelector('form.login');
  seen.loginSide = await page.$eval('#side', (s) => getComputedStyle(s).display);
  seen.loginNav = await page.$$eval('#nav a', (as) => as.length);
  seen.loginBrand = await page.textContent('form.login .brand');
  await page.screenshot({ path: `${out}/layout-login.png` });
  await page.fill('form.login input', token);
  await page.click('form.login button[type=submit]');
  await page.waitForSelector('#who .who-role');
  seen.side = await page.$eval('#side', (s) => getComputedStyle(s).display);
  const line = (sel) => page.$eval(sel, (e) => Math.round(e.getBoundingClientRect().top));
  seen.whoTwoLines = (await line('#who .who-role')) > (await line('#who .who-name'));

  // a pool's versions at a narrow window: one line per row, nothing wider than the window
  await page.goto(`${url}#/d/${encodeURIComponent(pool)}`);
  await page.waitForSelector('#versions');
  const oneLine = (sel) => page.$$eval(sel, (cells) => cells.every((c) => c.getBoundingClientRect().height < 45));
  // no table runs past the panels around it, and a cut text still shows a few words
  const tables = () => page.evaluate(() => {
    const wide = [...document.querySelectorAll('main table')].filter((t) => {
      const box = t.parentElement;
      const style = getComputedStyle(box);
      const room = box.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
      return t.getBoundingClientRect().width > room + 1;
    }).map((t) => `${t.id || t.rows[0].textContent.slice(0, 12)} ${Math.round(t.getBoundingClientRect().width)}: ${
      [...t.rows[t.rows.length - 1].cells].map((c) => Math.round(c.getBoundingClientRect().width)).join(' ')}`);
    const narrow = [...document.querySelectorAll('main td.text')].filter((c) => c.getBoundingClientRect().width < 80)
      .map((c) => `${c.closest('table').id}: ${c.textContent.slice(0, 10)}`);
    return { wide, narrow };
  });
  seen.tables = {};
  seen.versionsOneLine = await oneLine('#versions td');
  seen.fits = await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);
  seen.note = await page.$eval('#versions tr:nth-child(2) td:last-child', (td) => ({
    title: td.title, cut: td.scrollWidth > td.clientWidth, overflow: getComputedStyle(td).textOverflow }));
  seen.time = await page.$eval('#versions tr:nth-child(2) td.time', (td) => [td.textContent, td.title]);
  seen.source = await page.$eval('#versions tr:nth-child(2) td.text.muted', (td) => [td.textContent, td.title]);
  seen.numbersRight = await page.$$eval('#versions th.num', (ths) => ths.map((t) => getComputedStyle(t).textAlign));
  seen.labelChip = await page.$eval('#versions .tag.ver', (t) => getComputedStyle(t).backgroundColor);
  seen.removeButton = await page.locator('a.button', { hasText: '移除目录' }).count();
  seen.versionRefs = await page.$$eval('#versions tr td:nth-child(3) a', (as) => as.map((a) => [a.textContent, a.title]));
  seen.tables.pool = await tables();
  await page.screenshot({ path: `${out}/layout-pool.png`, fullPage: true });

  // the record of who did what: one line per row
  await page.goto(`${url}#/events`);
  await page.waitForSelector('main table');
  seen.eventsOneLine = await oneLine('main table td');
  seen.eventsFit = await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);
  seen.tables.events = await tables();
  // the lists, narrowed to this test's own rows (those of other tests have names of any length)
  for (const [name, route, sel] of [['datasets', `#/datasets?q=${encodeURIComponent(pool)}`, 'main table td.text'],
    ['batches', `#/batches?view=all&q=${encodeURIComponent(batch)}`, 'main table td.text']]) {
    await page.goto(`${url}${route}`);
    await page.waitForSelector(sel);
    seen.tables[name] = await tables();
  }

  // a batch sent back: its state as a dot and words, its steps under 流转记录 with 操作
  await page.goto(`${url}#/b/${encodeURIComponent(batch)}`);
  await page.waitForSelector('#batch-steps');
  seen.batchState = await page.$eval('#batch-status', (s) => [s.textContent, !!s.querySelector('.dot.red')]);
  seen.steps = await page.$$eval('#batch-steps th', (ths) => ths.map((t) => t.textContent));
  seen.stepsTitle = await page.$eval('#batch-steps', (t) => t.previousElementSibling.textContent);
  seen.tables.batch = await tables();

  // a form at a wide window keeps its width; check boxes sit under the fields
  await page.setViewportSize({ width: 1920, height: 1000 });
  await page.goto(`${url}#/add/${encodeURIComponent(pool)}`);
  await page.waitForSelector('.panel.form label.check');
  seen.formWidth = await page.$eval('.panel.form', (f) => Math.round(f.getBoundingClientRect().width));
  seen.checkUnderFields = await page.evaluate(() => {
    const field = document.querySelector('.panel.form .drop').getBoundingClientRect().left;
    const box = document.querySelector('.panel.form label.check input').getBoundingClientRect().left;
    return Math.abs(field - box) < 6;
  });
  // a file is chosen in a box like the folder's, not the browser's own control
  await page.goto(`${url}#/model-register`);
  await page.waitForSelector('#mv-form');
  seen.fileBoxes = await page.$$eval('#mv-form .drop', (d) => d.length);
  seen.nativeHidden = await page.$eval('#mv-onnx', (i) => getComputedStyle(i).opacity);
  await page.setInputFiles('#mv-onnx', { name: 'best.onnx', mimeType: 'application/octet-stream', buffer: Buffer.from([8, 7]) });
  seen.fileChosen = await page.textContent('#mv-form .drop .got');
  await page.screenshot({ path: `${out}/layout-register.png`, fullPage: true });

  seen.errors = errors;
  console.log(JSON.stringify(seen));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
