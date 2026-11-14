// Pools from folders the way they were collected (run by tests/server/test_web.py):
// node folders.cjs <url> <algo token> <empty pool> <out dir> <pool folder with 20261101/, 20261102/>
//   <folder 20261103> <folder 20261104> <folder with a loose file next to its subfolder> <field token>.
// Where a new pool starts from a line; adding two folders dragged in at once; adding the folders inside a
// "pool" folder without that level; a loose file refused in that case; what the whole-version upload says it
// will make of a "pool" folder and of folders dragged in; a field account sees no way to start a pool.
// Prints one JSON line.
const { chromium } = require('playwright');
const { dropOn } = require('./drop.cjs');

(async () => {
  const [url, token, pool, out, parent, day3, day4, loose, field] = process.argv.slice(2);
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  page.on('console', (m) => { if (m.type() === 'error' && !/40[49]/.test(m.text())) errors.push(m.text()); });  // 404: no release on the line yet
  await page.goto(url);
  await page.evaluate((t) => localStorage.setItem('fw-token', t), token);
  await page.reload();
  const seen = {};
  const failed = async (e) => {  // what the page said when a step did not come
    await page.screenshot({ path: `${out}/folders-failed.png`, fullPage: true }).catch(() => {});
    throw new Error(`${e.message}\npage: ${await page.textContent('main').catch(() => '')}`);
  };
  try {

  // a new pool starts from the line: on its overview and on the sites and lines page
  await page.goto(`${url}#/?site=SES&line=TX`);
  await page.waitForSelector('#data-new');
  seen.overviewNew = await page.getAttribute('#data-new', 'href');
  await page.screenshot({ path: `${out}/folders-overview.png` });
  await page.goto(`${url}#/places`);
  await page.waitForSelector('#places-table a.new-pool');
  seen.placesNew = await page.$$eval('#places-table a.new-pool', (as) => as.map((a) => a.getAttribute('href')));

  const plan = async () => {
    await page.click('button:has-text("算哈希")');
    await page.waitForSelector('button:has-text("上传并建")');
    const text = await page.textContent('main > div:last-child');
    await page.click('button:has-text("上传并建")');
    await page.waitForFunction(() => location.hash.startsWith('#/v/') && !document.querySelector('.drop'));  // the version page is up
    return { text, at: page.url().split('#')[1] };
  };

  // two folders dragged in at once: each under its own name
  await page.goto(`${url}#/add/${pool}`);
  await page.waitForFunction((n) => document.querySelector('main h1')?.textContent === `往 ${n} 里加目录`, pool);
  await dropOn(page, [day3, day4]);
  await page.waitForSelector('#add-dirs');
  seen.two = { dirs: await page.textContent('#add-dirs'), box: await page.textContent('.drop > span'),
    target: await page.$eval('input[placeholder^="选了目录"]', (i) => [i.value, i.disabled]), inside: await page.$eval('#add-inside', (i) => i.disabled) };
  seen.two.plan = await plan();

  // the folders inside a "pool" folder, without that level
  await page.goto(`${url}#/add/${pool}`);
  await page.waitForFunction((n) => document.querySelector('main h1')?.textContent === `往 ${n} 里加目录`, pool);
  await page.setInputFiles('input[type=file]', parent);
  await page.waitForSelector('button:has-text("算哈希")');
  seen.inside = { target: await page.inputValue('input[placeholder^="选了目录"]'), label: await page.textContent('label:has(#add-inside)') };
  await page.check('#add-inside');
  await page.waitForSelector('#add-dirs');
  seen.inside.dirs = await page.textContent('#add-dirs');
  seen.inside.targetAfter = await page.$eval('input[placeholder^="选了目录"]', (i) => [i.value, i.disabled]);
  await page.screenshot({ path: `${out}/folders-inside.png`, fullPage: true });
  seen.inside.plan = await plan();

  // a file right in the "pool" folder has no folder to go to
  await page.goto(`${url}#/add/${pool}`);
  await page.waitForFunction((n) => document.querySelector('main h1')?.textContent === `往 ${n} 里加目录`, pool);
  await page.setInputFiles('input[type=file]', loose);
  await page.waitForSelector('button:has-text("算哈希")');
  await page.check('#add-inside');
  await page.waitForSelector('main .error');
  seen.loose = await page.textContent('main .error');

  // the whole-version upload says what it makes of a "pool" folder, and of folders dragged in
  await page.goto(`${url}#/upload/${pool}`);
  await page.waitForFunction((n) => document.querySelector('main h1')?.textContent === `上传到 ${n}`, pool);
  await page.setInputFiles('input[type=file]', parent);
  await page.waitForSelector('#version-shape');
  seen.wholeParent = await page.textContent('#version-shape');
  await dropOn(page, [day3, day4]);
  await page.waitForFunction(() => document.querySelector('#version-shape')?.textContent.startsWith('这 2 个目录'));
  seen.wholeTwo = await page.textContent('#version-shape');
  await page.screenshot({ path: `${out}/folders-whole.png`, fullPage: true });

  // a field account sees the line and the sites, but nothing that starts a pool
  await page.evaluate((t) => localStorage.setItem('fw-token', t), field);
  await page.goto(`${url}#/?site=SES&line=TX`);
  await page.reload();
  await page.waitForSelector('main h2#data');
  seen.fieldOverview = { newData: await page.$$eval('#data-new', (x) => x.length), links: await page.$$eval('main a[href^="#/dataset-new"]', (x) => x.length) };
  await page.goto(`${url}#/places`);
  await page.waitForSelector('#places-table');
  seen.fieldPlaces = await page.$$eval('#places-table a.new-pool', (x) => x.length);

  } catch (e) { await failed(e); }
  seen.errors = errors;
  console.log(JSON.stringify(seen));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
