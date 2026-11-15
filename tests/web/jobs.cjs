// The lists of jobs and prediction sets by site and line, the older jobs loaded on request, and the lists sorted
// by a click on a column's header (run by tests/server/test_web.py):
// node jobs.cjs <url> <token> <site> <out dir>. Prints one JSON line of what it saw.
const { chromium } = require('playwright');

(async () => {
  const [url, token, site, out] = process.argv.slice(2);
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
  await page.goto(url);
  await page.evaluate((t) => localStorage.setItem('fw-token', t), token);
  await page.reload();
  const seen = {};
  const open = async (hash, ready) => {  // a fresh load: never the table of the page before
    await page.goto('about:blank');
    await page.goto(`${url}${hash}`);
    await page.waitForFunction(([h, s]) => location.hash === h && document.querySelector('h1') && document.querySelector(s), [hash, ready]);
  };
  const ids = (table) => page.$$eval(`${table} tr:not(:first-child) td:first-child`, (tds) => tds.map((td) => td.textContent.trim()));
  const sorted = (table) => page.$$eval(`${table} th`, (ths) => ths.map((th) => [th.textContent, th.getAttribute('aria-sort')])
    .filter(([, s]) => s && s !== 'none'));
  const list = () => page.evaluate(() => ({
    here: document.querySelector('#places a.on')?.textContent.trim() || null,
    scope: [...document.querySelectorAll('.browse .list .muted')].map((p) => p.textContent).find((t) => t.startsWith('范围')) || null,
    path: document.querySelector('.crumbs .path').textContent,
    search: !!document.querySelector('.browse form input'),
  }));

  // by line, by site
  await open(`#/jobs?site=${site}&line=L1`, '#jobs');
  seen.line = { ids: await ids('#jobs'), ...(await list()) };
  await open(`#/jobs?site=${site}`, '#jobs');
  seen.site = { ids: await ids('#jobs'), ...(await list()) };
  await page.screenshot({ path: `${out}/jobs-site.png` });
  await page.selectOption('#job-status', 'done');
  await page.waitForFunction(() => location.hash.includes('status=done') && document.querySelector('#job-status').value === 'done');
  await page.waitForSelector('#jobs');
  seen.done = { hash: await page.evaluate(() => location.hash), ids: await ids('#jobs') };
  await open(`#/predictions?site=${site}&line=L1`, '#prediction-list');
  seen.predictions = { ids: await ids('#prediction-list'), ...(await list()) };
  await open(`#/predictions?site=${site}&line=L2`, '.browse .list p.muted:last-child');
  seen.noPredictions = await page.textContent('.browse .list > p.muted:last-child');

  // older jobs on request, and sorting
  await open('#/jobs?status=cancelled', '#jobs');
  seen.first = { rows: (await ids('#jobs')).length, more: await page.isVisible('#jobs-more') };
  await page.click('#jobs th:nth-child(1)');
  seen.up = { ids: await ids('#jobs'), sorted: await sorted('#jobs') };
  await page.click('#jobs th:nth-child(1)');
  seen.down = { ids: await ids('#jobs'), sorted: await sorted('#jobs') };
  await page.click('#jobs th:nth-child(1)');  // ascending again, then the older ones come in their place
  await page.click('#jobs-more');
  await page.waitForFunction(() => document.querySelectorAll('#jobs tr').length > 101 && !document.querySelector('#jobs-more').disabled);
  seen.more = { ids: await ids('#jobs'), sorted: await sorted('#jobs'), button: await page.isVisible('#jobs-more') };
  await page.focus('#jobs th:nth-child(6)');
  await page.keyboard.press('Enter');
  seen.keyboard = await sorted('#jobs');
  await page.screenshot({ path: `${out}/jobs-sorted.png` });

  // the models of the site, sorted by name, the other way round
  await open(`#/models?site=${site}`, '#model-list');
  await page.click('#model-list th:nth-child(1)');
  await page.click('#model-list th:nth-child(1)');
  seen.models = { names: await ids('#model-list'), sorted: await sorted('#model-list') };
  // another page forgets the order; coming back shows the list as it comes
  const name = await page.textContent('#model-list tr:nth-child(2) td:first-child a');
  await page.click('#model-list tr:nth-child(2) td:first-child a');
  await page.waitForFunction((n) => document.querySelector('h1')?.textContent.includes(n) && !document.querySelector('#model-list'), name);
  await page.goBack();
  await page.waitForSelector('#model-list');
  seen.modelsBack = { names: await ids('#model-list'), sorted: await sorted('#model-list') };

  seen.errors = errors;
  console.log(JSON.stringify(seen));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
