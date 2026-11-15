// A line's benchmark test set and the numbers measured on it (run by tests/server/test_web.py):
// node benchmark.cjs <url> <algo token> <field token> <site> <model> <first test set> <second test set> <out dir>.
// Sets the benchmark of <site>/TX on its overview, changes it, reads the settings kept; the model's page with
// its versions on the benchmark and the evaluation tables with their numbers, at 1180 wide; a field account
// reads the benchmark but has nothing to set it with; the new-batch page offers data pools only, by line.
// Prints one JSON line.
const { chromium } = require('playwright');

(async () => {
  const [url, token, field, site, model, first, second, out] = process.argv.slice(2);
  const browser = await chromium.launch();
  const errors = [];
  const open = async (t) => {
    const p = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    p.on('pageerror', (e) => errors.push(String(e)));
    p.on('console', (m) => { if (m.type() === 'error' && !/40[09]/.test(m.text())) errors.push(m.text()); });  // the refusals asked for below
    await p.goto(url);
    await p.evaluate((x) => localStorage.setItem('fw-token', x), t);
    await p.reload();
    return p;
  };
  let page = await open(token);
  const seen = {};
  const overview = async () => {
    await page.goto(`${url}#/?site=${site}&line=TX`);
    await page.waitForFunction((s) => location.hash.includes(`site=${s}&line=TX`) && document.querySelector('#benchmark'), site);
  };
  const toast = async (text) => {
    await page.waitForFunction((t) => document.querySelector('#toast.show')?.textContent.includes(t), text);
    return page.textContent('#toast');
  };
  try {

  // a line without a benchmark: nothing to compare on yet
  await overview();
  seen.none = { bar: await page.textContent('#benchmark'), models: await page.$$eval('table.models tr:nth-child(2) td', (c) => c.slice(1, 4).map((x) => x.textContent)) };
  await page.click('#benchmark-change');
  await page.waitForSelector('#benchmark-set');
  seen.offered = await page.$$eval('#benchmark-set option', (o) => o.map((x) => x.value));
  await page.click('#benchmark-go');
  await page.waitForSelector('#benchmark-form .error');
  seen.unchosen = await page.textContent('#benchmark-form .error');
  await page.selectOption('#benchmark-set', first);
  await page.fill('#benchmark-note', '首个基准');
  await page.click('#benchmark-go');
  seen.setToast = await toast('基准测试集');
  seen.first = { bar: await page.textContent('#benchmark'), models: await page.$$eval('table.models tr:nth-child(2) td', (c) => c.slice(1, 4).map((x) => x.textContent)) };

  // changed to the other test set: its numbers, and the settings kept
  await page.click('#benchmark-change');
  await page.waitForSelector('#benchmark-set');
  seen.offeredNow = await page.$$eval('#benchmark-set option', (o) => o.map((x) => x.value));  // not the one set now
  await page.selectOption('#benchmark-set', second);
  await page.click('#benchmark-go');
  await toast(second);
  seen.second = { bar: await page.textContent('#benchmark'), models: await page.$$eval('table.models tr:nth-child(2) td', (c) => c.slice(1, 6).map((x) => x.textContent)) };
  await page.click('#benchmark-change');
  await page.waitForSelector('#benchmark-history');
  seen.history = await page.$$eval('#benchmark-history tr', (rows) => rows.slice(1).map((r) => [...r.cells].slice(1).map((c) => c.textContent)));
  await page.screenshot({ path: `${out}/benchmark-overview.png`, fullPage: true });

  // the model's page at 1180: its versions on the benchmark, the release marked; evaluations with their numbers
  await page.setViewportSize({ width: 1180, height: 900 });
  await page.goto(`${url}#/m/${encodeURIComponent(model)}`);
  await page.waitForSelector('#bench-table');
  seen.bench = await page.$$eval('#bench-table tr', (rows) => rows.slice(1).map((r) => [...r.cells].slice(0, 5).map((c) => c.textContent)));
  seen.benchRef = await page.textContent('#model-bench + .bench-line');
  seen.evalHead = await page.$$eval('table.evals th', (t) => t.map((x) => x.textContent));
  seen.evalRows = await page.$$eval('table.evals tr', (rows) => rows.slice(1).map((r) => [...r.cells].slice(1, 9).map((c) => c.textContent)));
  seen.evalWho = await page.getAttribute('table.evals tr:nth-child(2) td.time', 'title');
  const fits = () => page.evaluate(() => [...document.querySelectorAll('main table')].filter((t) => {
    const box = t.parentElement;
    const style = getComputedStyle(box);
    return t.getBoundingClientRect().width > box.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight) + 1;
  }).map((t) => t.id || t.className));
  seen.modelWide = await fits();
  await page.screenshot({ path: `${out}/benchmark-model.png`, fullPage: true });
  await page.goto(`${url}#/mv/${encodeURIComponent(model)}/3`);
  await page.waitForFunction(() => location.hash.startsWith('#/mv/') && document.querySelector('main h1')?.textContent.includes('@3')
    && document.querySelector('table.evals'));  // the version's page, not the model's table still shown
  seen.versionEvalHead = await page.$$eval('table.evals th', (t) => t.map((x) => x.textContent));
  seen.versionWide = await fits();
  await page.setViewportSize({ width: 1440, height: 900 });

  // a new batch goes into a data pool: the pools by line, none chosen; from a line with one pool, that one
  await page.goto(`${url}#/batch-new`);
  await page.waitForSelector('#pool optgroup', { state: 'attached' });
  seen.pools = await page.$$eval('#pool optgroup', (gs) => gs.map((g) => [g.label, [...g.children].map((o) => o.value)]));
  seen.poolFirst = await page.$eval('#pool', (s) => [s.value, s.options[0].textContent]);
  await page.goto(`${url}#/batch-new?site=${site}&line=TX`);
  await page.reload();
  await page.waitForSelector('#pool optgroup', { state: 'attached' });
  seen.poolOfLine = await page.inputValue('#pool');
  await page.goto(`${url}#/batch-new?site=${site}&line=TY`);
  await page.reload();
  await page.waitForSelector('#pool optgroup', { state: 'attached' });
  seen.poolOfTwo = await page.inputValue('#pool');

  // a field account reads the line's benchmark; nothing sets it
  page = await open(field);
  await overview();
  seen.field = { bar: await page.textContent('#benchmark'), change: await page.$$eval('#benchmark-change', (b) => b.length) };

  } catch (e) {
    await page.screenshot({ path: `${out}/benchmark-failed.png`, fullPage: true }).catch(() => {});
    throw new Error(`${e.message}\npage: ${await page.textContent('main').catch(() => '')}`);
  }
  seen.errors = errors;
  console.log(JSON.stringify(seen));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
