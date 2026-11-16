// Changing the reviewer of a batch in review (run by tests/server/test_web.py):
// node reassign.cjs <url> <owner token> <old reviewer token> <batch> <new reviewer>.
// The owner opens 更多 › 更换复核人…, picks another reviewer and confirms; the old reviewer's page then offers
// no review. Prints one JSON line.
const { chromium } = require('playwright');

(async () => {
  const [url, ownerToken, oldToken, name, to] = process.argv.slice(2);
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  const open = async (token) => {
    await page.goto(url);
    await page.evaluate((t) => localStorage.setItem('fw-token', t), token);
    await page.goto(`${url}#/b/${name}`);
    await page.reload();
    await page.waitForSelector('#batch-facts');
  };
  const seen = { errors };
  await open(ownerToken);
  await page.click('.batch-head .more-button');
  seen.more = await page.$$eval('.menu [role=menuitem]', (l) => l.map((x) => x.textContent));
  await page.click('#reassign-open');
  await page.waitForSelector('#reassign-form');
  seen.title = await page.textContent('#reassign-form h3');
  seen.choices = await page.$$eval('#reassign-form #reviewer option', (o) => o.map((x) => x.value).filter(Boolean));
  await page.click('#reassign-go');                         // nobody chosen yet: said on the page
  await page.waitForSelector('main .error');
  seen.unpicked = await page.textContent('main .error');
  await page.selectOption('#reassign-form #reviewer', to);
  await page.fill('#reassign-note', '原复核人出差');
  await page.click('#reassign-go');
  await page.waitForFunction(() => !document.querySelector('#reassign-form'));
  await page.waitForFunction(() => document.querySelector('#toast')?.textContent.includes('已更换'));
  seen.toast = await page.textContent('#toast');
  seen.facts = await page.textContent('#batch-facts');
  seen.trail = await page.$$eval('table tr', (rows) => rows.map((r) => r.textContent).filter((t) => t.includes('更换复核人')));
  await open(oldToken);
  seen.oldReview = await page.locator('#review-start').count();
  console.log(JSON.stringify(seen));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
