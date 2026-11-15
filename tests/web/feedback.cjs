// What the pages say back (run by tests/server/test_web.py):
// node feedback.cjs <url> <field token> <batch of that account, open> <a pool> <out dir>.
// A change that is hard to take back is asked on the page itself (closing a batch with its reason, revoking a
// token under its row; Esc and 取消 leave it), never in a browser box; a change made says so at the bottom; a page
// that cannot be shown keeps its path and the way back; a page slow to come shows the bar on top while it comes.
// Prints one JSON line of what it saw.
const { chromium } = require('playwright');

(async () => {
  const [url, token, batch, pool, out] = process.argv.slice(2);
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = [];
  const dialogs = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  page.on('console', (m) => { if (m.type() === 'error' && !/404/.test(m.text())) errors.push(m.text()); });
  page.on('dialog', (d) => { dialogs.push(d.message()); d.dismiss(); });
  await page.goto(url);
  await page.evaluate((t) => localStorage.setItem('fw-token', t), token);
  const seen = {};

  // closing a batch: asked under the button, with a field for the reason
  await page.goto(`${url}#/b/${encodeURIComponent(batch)}`);
  await page.reload();
  await page.waitForSelector('.batch-head .more-button');
  await page.click('.batch-head .more-button');             // 更多 › 关闭批次…
  await page.click('#close-batch');
  await page.waitForSelector('.ask .ask-reason');
  seen.closeAsk = await page.textContent('.ask .ask-text');
  await page.keyboard.press('Escape');
  seen.escaped = await page.locator('.ask').count();
  await page.click('.batch-head .more-button');
  await page.click('#close-batch');
  await page.fill('.ask .ask-reason', '重复上传');
  await page.screenshot({ path: `${out}/feedback-close.png`, fullPage: true });
  await page.click('.ask .ask-go');
  await page.waitForFunction(() => document.querySelector('#batch-status')?.textContent === '已关闭');
  seen.closeToast = await page.textContent('#toast');

  // revoking a token: asked in a row under it
  await page.goto(`${url}#/me`);
  await page.waitForSelector('main table');
  const row = page.locator('main tr', { hasText: '旧笔记本' });
  await row.locator('button.revoke').click();
  await page.waitForSelector('tr.asking .ask-go');
  seen.revokeAsk = await page.textContent('tr.asking .ask-text');
  await page.click('tr.asking .ask-cancel');
  seen.kept = [await page.locator('tr.asking').count(), await row.locator('button.revoke').count()];
  await row.locator('button.revoke').click();
  await page.click('tr.asking .ask-go');
  await page.waitForFunction(() => [...document.querySelectorAll('main tr')].some((r) => r.textContent.includes('旧笔记本') && r.textContent.includes('已吊销')));
  seen.revokeToast = await page.textContent('#toast');

  // a page that cannot be shown: its path and the way back stay
  await page.goto(`${url}#/d/no-such-pool`);
  await page.waitForSelector('main .error');
  seen.errorPath = await page.$$eval('.crumbs .path a, .crumbs .path .here', (xs) => xs.map((x) => x.textContent));
  seen.errorBack = await page.locator('.crumbs button.back').count();

  // a page slow to come: the bar on top while it comes, gone once it is there
  await page.route(`**/api/v1/datasets/${pool}`, async (r) => { await new Promise((f) => setTimeout(f, 900)); await r.continue(); });
  await page.goto(`${url}#/d/${encodeURIComponent(pool)}`);
  await page.waitForTimeout(450);
  seen.busy = await page.evaluate(() => document.body.classList.contains('busy'));
  await page.waitForSelector('#versions');
  seen.idle = await page.evaluate(() => !document.body.classList.contains('busy'));

  seen.dialogs = dialogs;
  seen.errors = errors;
  console.log(JSON.stringify(seen));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
