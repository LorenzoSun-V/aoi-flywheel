// Upload folders from the browser (run by tests/server/test_web.py):
// node upload.cjs <url> <token> <new dataset name> <out dir> <folder 1> <folder 2>.
// Creates the dataset from the datasets page (新建数据集), uploads folder 1 (first refused for its other file, then allowed
// with the check box), then folder 2 as the next version. Prints one JSON line of what it saw.
const { chromium } = require('playwright');

(async () => {
  const [url, token, name, out, first, second] = process.argv.slice(2);
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
  await page.goto(url);
  await page.evaluate((t) => localStorage.setItem('fw-token', t), token);
  await page.goto(`${url}#/datasets`);
  await page.reload();
  const seen = {};
  await page.waitForSelector('#dataset-new');
  await page.click('#dataset-new');
  await page.waitForSelector('#dataset-kind');
  await page.fill('#dataset-name', name);
  await page.selectOption('#site', 'SES');
  await page.selectOption('#line', 'TX');
  await page.click('button:has-text("创建并上传")');
  await page.waitForFunction((n) => document.querySelector('h1') && document.querySelector('h1').textContent === `上传到 ${n}`, name);

  await page.setInputFiles('input[type=file]', first);
  await page.waitForSelector('main .error');
  seen.refused = await page.textContent('main .error');
  seen.report = await page.textContent('main > div:last-child');
  await page.check('input[type=checkbox] >> nth=0');
  await page.waitForSelector('button:has-text("算哈希")', { timeout: 5000 }).catch(async (e) => {
    await page.screenshot({ path: `${out}/upload-failed.png`, fullPage: true });
    throw e;
  });
  await page.fill('input[placeholder^="数据说明"]', '第一批');
  await page.click('button:has-text("算哈希")');
  await page.waitForSelector('button:has-text("上传并建")');
  seen.plan = await page.textContent('main > div:last-child');
  await page.screenshot({ path: `${out}/upload-plan.png`, fullPage: true });
  await page.click('button:has-text("上传并建")');
  await page.waitForSelector('.folders');
  seen.first = page.url().split('#')[1];

  await page.goto(`${url}#/upload/${name}`);
  await page.waitForSelector('input[type=file]');
  await page.check('input[type=checkbox] >> nth=0');  // the other file again
  await page.setInputFiles('input[type=file]', second);
  await page.click('button:has-text("算哈希")');
  await page.waitForSelector('button:has-text("上传并建")');
  seen.plan2 = await page.textContent('main > div:last-child');
  await page.click('button:has-text("上传并建")');
  await page.waitForSelector('.folders');
  seen.second = page.url().split('#')[1];
  seen.errors = errors;
  console.log(JSON.stringify(seen));
  await browser.close();
})().catch(async (e) => { console.error(e); process.exit(1); });
