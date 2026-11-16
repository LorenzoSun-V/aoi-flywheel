// A failed job in the browser (run by tests/server/test_web.py):
// node failed.cjs <url> <algo token> <first failed job> <second failed job> <batch job> <out dir>.
// Both jobs are evaluations that failed and wait on the home page. The first is submitted again from its page
// (the runner offered: the newest version of its own), the second is marked handled with a note; after each,
// the home page no longer lists it. The batch job is a failed pre-label of a batch another account uploaded:
// what its page offers. Prints one JSON line.
const { chromium } = require('playwright');

(async () => {
  const [url, token, first, second, batchJob, out] = process.argv.slice(2);
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
  await page.goto(url);
  await page.evaluate((t) => localStorage.setItem('fw-token', t), token);
  await page.reload();
  const seen = {};
  const waiting = async () => {  // the failed jobs listed on the home page
    await page.goto('about:blank');
    await page.goto(`${url}#/`);
    await page.waitForSelector('#todo, #todo-none');
    return page.$$eval('#todo tr[data-kind="failed"] td:nth-child(2)', (tds) => tds.map((td) => td.textContent));
  };
  const open = async (id) => {
    await page.goto('about:blank');
    await page.goto(`${url}#/job/${id}`);
    await page.waitForSelector('#job-failed');
  };
  const text = async (sel) => (await page.textContent(sel)).trim();
  seen.before = await waiting();

  // submitted again, on the runner offered
  await open(first);
  seen.card = { error: await text('#job-error'), runner: await page.$eval('#retry-runner', (s) => s.selectedOptions[0].textContent),
    buttons: await page.$$eval('#job-failed-actions button', (b) => b.map((x) => x.textContent)) };
  await page.screenshot({ path: `${out}/job-failed.png`, fullPage: true });
  await page.click('#job-retry');
  await page.waitForFunction((id) => location.hash.startsWith('#/job/') && location.hash !== `#/job/${id}`
    && (document.querySelector('main h1')?.textContent || '').startsWith(`任务 ${location.hash.split('/').pop()}：`), first);  // the new job's page drawn
  seen.retried = { hash: page.url().split('#')[1], title: await text('main h1') };
  await open(first);
  seen.firstAfter = { retried: await text('#job-retried'), actions: await page.locator('#job-failed-actions').count() };
  seen.afterRetry = await waiting();

  // marked handled, with a note
  await open(second);
  await page.click('#job-handle');
  seen.ask = await text('.ask-text');
  await page.fill('.ask-reason', '模型版本选错，无需重跑');
  await page.click('.ask-go');
  await page.waitForSelector('#job-handled');
  seen.handled = { text: await text('#job-handled'), actions: await page.locator('#job-failed-actions').count() };
  await page.screenshot({ path: `${out}/job-handled.png`, fullPage: true });
  seen.afterHandled = await waiting();
  await page.goto(`${url}#/jobs?status=failed`);
  await page.waitForSelector('#jobs');
  seen.list = await page.$$eval('#jobs tr:not(.group-head)', (rows, ids) => rows.filter((r) => ids.includes(r.cells[0]?.textContent.trim()))
    .map((r) => [r.cells[0].textContent.trim(), r.cells[4].textContent.trim()]), [`任务 ${first}`, `任务 ${second}`]);

  // a pre-label of a batch another account uploaded: run again from the batch, by its uploader
  await open(batchJob);
  seen.batchJob = { owner: await text('#job-batch-owner'), link: await page.getAttribute('#job-batch-owner a', 'href'),
    retry: await page.locator('#job-retry').count(), runner: await page.locator('#retry-runner').count(),
    actions: await page.locator('#job-failed-actions').count() };
  seen.errors = errors;
  console.log(JSON.stringify(seen));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
