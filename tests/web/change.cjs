// Add a folder to a pool and take one out, from the browser (run by tests/server/test_web.py):
// node change.cjs <url> <token> <dataset with a version> <out dir> <folder to add> <top folder to remove>.
// Adds the folder (its duplicate image is reported, not refused), is refused when adding it again, then
// removes the other folder. Prints one JSON line of what it saw.
const { chromium } = require('playwright');

(async () => {
  const [url, token, name, out, folder, gone] = process.argv.slice(2);
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  page.on('console', (m) => { if (m.type() === 'error' && !/409/.test(m.text())) errors.push(m.text()); });
  await page.goto(url);
  await page.evaluate((t) => localStorage.setItem('fw-token', t), token);
  await page.goto(`${url}#/d/${name}`);
  await page.reload();
  const seen = {};
  await page.click('a:has-text("上传新增目录")');
  await page.waitForSelector('input[type=file]');
  await page.setInputFiles('input[type=file]', folder);
  await page.waitForSelector('button:has-text("算哈希")');
  seen.target = await page.inputValue('input[placeholder^="选择目录后"]');
  await page.fill('input[placeholder^="数据说明"]', '第三天');
  await page.click('button:has-text("算哈希")');
  await page.waitForSelector('button:has-text("上传并建")');
  seen.plan = await page.textContent('main > div:last-child');
  await page.screenshot({ path: `${out}/add-plan.png`, fullPage: true });
  await page.click('button:has-text("上传并建")');
  await page.waitForSelector('.folders');
  seen.added = page.url().split('#')[1];

  await page.goto(`${url}#/add/${name}`);
  await page.waitForSelector('input[type=file]');
  await page.setInputFiles('input[type=file]', folder);
  await page.click('button:has-text("算哈希")');
  await page.waitForSelector('main .error');
  seen.again = await page.textContent('main .error');
  seen.offered = await page.locator('button:has-text("上传并建")').count();

  await page.goto(`${url}#/remove/${name}`);
  await page.waitForSelector(`input[type=checkbox][value="${gone}"]`);
  await page.check(`input[type=checkbox][value="${gone}"]`);
  await page.fill('input[placeholder^="去掉的原因"]', '拍糊了');
  await page.click('button:has-text("预览")');
  await page.waitForSelector('button:has-text("建立 ")');
  seen.removal = await page.textContent('main > div:last-child');
  await page.screenshot({ path: `${out}/remove-plan.png`, fullPage: true });
  await page.click('button:has-text("建立 ")');
  await page.waitForSelector('.folders');
  seen.removed = page.url().split('#')[1];

  await page.goto(`${url}#/d/${name}`);
  await page.waitForSelector('table');
  seen.versions = await page.textContent('table');
  seen.errors = errors;
  console.log(JSON.stringify(seen));
  await browser.close();
})().catch(async (e) => { console.error(e); process.exit(1); });
