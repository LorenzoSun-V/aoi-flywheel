// What waits for an account on the home page, and the counts on the batch list's tabs (run by tests/server/test_web.py):
// node todo.cjs <url> <algo token> <field token> <new field token> <site> <out dir>.
// For each account: the rows of 「待我处理」 (kind, object, words), the first row's link followed, the tabs of the
// batch list of <site> with their counts and rows, whether the table fits at 1180. Prints one JSON line.
const { chromium } = require('playwright');

(async () => {
  const [url, algo, field, fresh, site, out] = process.argv.slice(2);
  const browser = await chromium.launch();
  const errors = [];
  const open = async (token, width = 1440) => {
    const context = await browser.newContext({ viewport: { width, height: 900 }, timezoneId: 'Asia/Shanghai', locale: 'zh-CN' });
    const p = await context.newPage();
    p.on('pageerror', (e) => errors.push(String(e)));
    p.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
    await p.goto(url);
    await p.evaluate((t) => localStorage.setItem('fw-token', t), token);
    await p.reload();
    return p;
  };
  const home = async (page) => {
    await page.goto(`${url}#/`);
    await page.waitForSelector('#todo, #todo-none');
    return page.evaluate(() => ({
      head: document.querySelector('#todo-head').textContent,
      none: document.querySelector('#todo-none')?.textContent || null,
      rows: [...document.querySelectorAll('#todo tr[data-kind]')].map((tr) => [tr.dataset.kind, ...[...tr.cells].slice(0, 3).map((td) => td.textContent)]),
      lines: !!document.querySelector('#lines-head') && !!document.querySelector('#lines'),
    }));
  };
  const tabs = async (page, view = '', on = view) => {  // on: the tab the page opens on
    await page.goto(`${url}#/batches?site=${site}${view ? `&view=${view}` : ''}`);
    await page.waitForFunction((v) => document.querySelector('.tabs a.on')?.dataset.view === v, on);  // not the tab shown before
    return page.evaluate(() => ({
      tabs: [...document.querySelectorAll('.tabs a[data-view]')].map((a) => [a.dataset.view, a.querySelector('.count')?.textContent ?? null]),
      on: document.querySelector('.tabs a.on').dataset.view,
      rows: document.querySelectorAll('.browse table tr').length - (document.querySelector('.browse table') ? 1 : 0),
    }));
  };
  const seen = {};

  let page = await open(algo);
  seen.algo = await home(page);
  await page.screenshot({ path: `${out}/todo-algo.png`, fullPage: true });
  const link = await page.$('#todo tr[data-kind="review"] td:nth-child(2) a');
  seen.algoLink = await link.getAttribute('href');
  await link.click();
  await page.waitForFunction((h) => location.hash === h && document.querySelector('h1'), seen.algoLink);
  seen.algoTabs = { todo: await tabs(page, '', 'todo'), mine: await tabs(page, 'mine'), all: await tabs(page, 'all') };
  await page.screenshot({ path: `${out}/todo-algo-tabs.png` });

  page = await open(field);
  seen.field = await home(page);
  seen.fieldTabs = await tabs(page, '', 'mine');

  page = await open(fresh);
  seen.fresh = await home(page);

  page = await open(algo, 1180);
  await home(page);
  seen.fits = await page.evaluate(() => { const t = document.querySelector('#todo'); return [t.scrollWidth, t.parentElement.clientWidth]; });

  seen.errors = errors;
  console.log(JSON.stringify(seen));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
