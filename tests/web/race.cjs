// An older folder-check answer arriving after a newer one. Pick folder A (clean), its check is
// held 2 s; then pick folder B (has readme.docx + Thumbs.db, must be refused). Prints what the page offers.
// node race.cjs <url> <token> <dataset> <folder A> <folder B>
const { chromium } = require('playwright');
(async () => {
  const [url, token, name, a, b] = process.argv.slice(2);
  const browser = await chromium.launch();
  const page = await browser.newPage();
  await page.goto(url);
  await page.evaluate((t) => localStorage.setItem('fw-token', t), token);
  await page.reload();
  let n = 0;
  await page.route('**/api/v1/folder-check', async (route) => {
    if (n++ === 0) await new Promise((r) => setTimeout(r, 2000));
    await route.continue();
  });
  const puts = [];
  page.on('request', (r) => { if (r.method() === 'PUT') puts.push(r.url()); });
  await page.goto(`${url}#/upload/${name}`);
  await page.waitForSelector('input[type=file]');
  await page.setInputFiles('input[type=file]', a);
  await page.waitForTimeout(300);
  await page.setInputFiles('input[type=file]', b);
  await page.waitForTimeout(3000);
  const seen = { shown: await page.textContent('main > div:last-child'),
    go: await page.isVisible('button:has-text("算哈希")') };
  if (seen.go) {
    await page.click('button:has-text("算哈希")');
    await page.waitForSelector('button:has-text("上传并建")');
    seen.plan = await page.textContent('main > div:last-child');
    await page.click('button:has-text("上传并建")');
    await page.waitForSelector('.folders');
    seen.url = page.url().split('#')[1];
  }
  seen.puts = puts.length;
  console.log(JSON.stringify(seen));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
