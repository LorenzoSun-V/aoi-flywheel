// Finding one's way: the home page is the overview, the tree folds up and shows only the chosen sites and
// lines (remembered by the browser), every page says where it is and leads back, table headers stay on a line.
const { chromium } = require('playwright');

(async () => {
  const [url, token, site, other, pool, hiddenPool, out] = process.argv.slice(2);
  const browser = await chromium.launch();
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  page.on('console', (m) => { if (m.type() === 'error' && !/404/.test(m.text())) errors.push(m.text()); });  // 404: a line without a release
  await page.goto(url);
  await page.evaluate((t) => localStorage.setItem('fw-token', t), token);
  await page.reload();
  const seen = {};
  const h1 = (text) => page.waitForFunction((t) => document.querySelector('main h1')?.textContent === t, text);
  const tree = () => page.$$eval('#places li a .name', (items) => items.map((x) => x.textContent));
  const mine = (names) => names.filter((n) => n.startsWith(site) || n.startsWith(other) || n === 'TX' || n === 'TY');
  const key = (s, line) => (line ? `${s}/${line}` : s).toLowerCase();

  // the home page is the overview
  await page.waitForSelector('#lines');
  seen.home = { h1: await page.textContent('main h1'), on: await page.textContent('#nav a.on'), crumbs: await page.$$eval('.crumbs', (c) => c.length) };
  seen.brand = await page.getAttribute('.brand', 'href');

  // a site folds up, and stays so
  const row = `#places li.site:has(a[href$="site=${site}"])`;
  seen.open = (await tree()).filter((n) => n === 'TX' || n === 'TY').length;
  seen.linesBefore = await page.$$eval(`#places li.line a[href*="site=${site}&"]`, (a) => a.length);
  await page.click(`${row} button.fold`);
  await page.waitForFunction((r) => document.querySelector(r).classList.contains('closed'), row);
  seen.linesFolded = await page.$$eval(`#places li.line a[href*="site=${site}&"]`, (a) => a.length);
  seen.foldSays = await page.getAttribute(`${row} button.fold`, 'aria-expanded');
  await page.reload();
  await page.waitForSelector('#lines');
  seen.linesFoldedAfterReload = await page.$$eval(`#places li.line a[href*="site=${site}&"]`, (a) => a.length);
  await page.screenshot({ path: `${out}/nav-folded.png`, fullPage: true });
  await page.click(`${row} button.fold`);
  await page.waitForFunction((r) => !document.querySelector(r).classList.contains('closed'), row);
  seen.linesOpened = await page.$$eval(`#places li.line a[href*="site=${site}&"]`, (a) => a.length);

  // only the ticked sites and lines are shown, in the tree and in the table of lines
  await page.click('#places-choose');
  await page.waitForSelector('#places input.pick');
  await page.screenshot({ path: `${out}/nav-choosing.png`, fullPage: true });
  await page.uncheck(`#places input[data-place="${key(site, 'TY')}"]`);
  await page.uncheck(`#places input[data-place="${key(other)}"]`);
  seen.lineOfHiddenSiteOff = await page.isDisabled(`#places input[data-place="${key(other, 'TX')}"]`);
  await page.click('#places-choose');
  await page.waitForFunction(() => !document.querySelector('#places input.pick') && document.querySelector('#lines'));
  const shownNow = async () => ({
    tree: await page.$$eval('#places li a', (as) => as.map((a) => a.getAttribute('href'))),
    table: await page.$$eval('#lines tr td:first-child', (c) => c.map((x) => x.textContent)),
  });
  seen.chosen = await shownNow();
  seen.hiddenNote = await page.textContent('#places-hidden');
  await page.screenshot({ path: `${out}/nav-chosen.png`, fullPage: true });
  await page.reload();
  await page.waitForSelector('#lines');
  seen.chosenAfterReload = await shownNow();
  // the lists themselves hide nothing: only the tree is tidied
  await page.click('#nav a:has-text("数据集")');
  await page.waitForSelector('#pools');
  seen.listTree = await page.$$eval('#places li a', (as) => as.map((a) => a.getAttribute('href')));
  seen.listPools = await page.$$eval('#pools + table tr td:first-child', (c) => c.map((x) => x.textContent));
  seen.listCrumbs = (await page.textContent('.crumbs .path')).trim();
  seen.headers = await page.$$eval('main th', (ths) => ths.map((t) => [getComputedStyle(t).whiteSpace, t.getClientRects()[0].height < 45]));
  await page.screenshot({ path: `${out}/nav-datasets.png`, fullPage: true });
  // a line that is not shown is still there when one goes to it
  await page.goto(`${url}#/?site=${site}&line=TY`);
  await h1(`${site} / TY`);
  seen.browsedHidden = await page.$$eval('#places li.line a.on', (a) => a.map((x) => x.getAttribute('href')));

  // from the overview of a line into a dataset: the page says where it is, and back leads back
  await page.goto(`${url}#/?site=${site}&line=TX`);
  await h1(`${site} / TX`);
  seen.lineCrumbs = await page.$$eval('.crumbs .path > *', (c) => c.map((x) => x.textContent));
  await page.click(`#pools + table a:text-is("${pool}")`);
  await h1(pool);
  seen.crumbs = await page.$$eval('.crumbs .path a, .crumbs .path .here', (c) => c.map((x) => [x.textContent, x.getAttribute('href')]));
  seen.onInDataset = await page.textContent('#nav a.on');
  await page.screenshot({ path: `${out}/nav-dataset.png`, fullPage: true });
  await page.click(`a:text-is("${pool}@1")`);
  await page.waitForSelector('.folders');
  seen.versionCrumbs = await page.$$eval('.crumbs .path a, .crumbs .path .here', (c) => c.map((x) => x.textContent));
  await page.click('.crumbs button.back');
  await h1(pool);
  await page.click('.crumbs button.back');
  await h1(`${site} / TX`);
  seen.backTo = page.url().split('#')[1];
  // the name on the top left is the way home
  await page.click(`#pools + table a:text-is("${pool}")`);
  await h1(pool);
  await page.click('.brand');
  await h1('概览');
  seen.homeHash = page.url().split('#')[1] || '';

  // a page opened directly (a bookmark): back is the step above, not out of the platform
  const tab = await context.newPage();
  tab.on('pageerror', (e) => errors.push(String(e)));
  await tab.goto(`${url}#/d/${pool}`);
  await tab.waitForFunction((t) => document.querySelector('main h1')?.textContent === t, pool);
  await tab.click('.crumbs button.back');
  await tab.waitForSelector('#pools');
  seen.upTo = tab.url().split('#')[1];
  seen.upPools = await tab.$$eval('#pools + table tr td:first-child', (c) => c.map((x) => x.textContent));
  // the address the datasets had before still shows something sensible: the home page
  seen.hiddenPoolListed = seen.listPools.includes(hiddenPool);
  seen.errors = errors;
  console.log(JSON.stringify(seen));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
