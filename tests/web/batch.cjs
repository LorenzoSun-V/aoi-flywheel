// A labelling batch from upload to the pool, in the browser (run by tests/server/test_web.py):
// node batch.cjs <url> <field token> <algo token> <algo name> <pool> <batch name> <out dir> <folder> <fixed folder>.
// The field account uploads the folder as a batch and sends it to the algo account, which rejects it; the
// field account uploads the fixed folder; the algo account approves and imports it. Prints one JSON line.
const { chromium } = require('playwright');

(async () => {
  const [url, fieldToken, algoToken, algoName, pool, batch, out, folder, fixed] = process.argv.slice(2);
  const browser = await chromium.launch();
  const errors = [];
  const open = async (token) => {
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const page = await context.newPage();
    page.on('pageerror', (e) => errors.push(String(e)));
    page.on('console', (m) => { if (m.type() === 'error' && !/40[09]/.test(m.text())) errors.push(m.text()); });
    await page.goto(url);
    await page.evaluate((t) => localStorage.setItem('fw-token', t), token);
    await page.reload();
    return page;
  };
  const field = await open(fieldToken);
  const algo = await open(algoToken);
  const seen = {};

  await field.goto(`${url}#/batches`);
  await field.waitForSelector('a:has-text("上传新批次")');
  seen.fieldNav = await field.textContent('#nav');
  await field.click('a:has-text("上传新批次")');
  await field.waitForSelector('#pool');
  await field.selectOption('#pool', pool);
  await field.setInputFiles('input[type=file]', folder);
  await field.waitForSelector('#send-folder');
  seen.dir = await field.inputValue('#batch-dir');
  await field.fill('#batch-name', batch);
  await field.click('#send-folder');                       // no labeler yet: refused before anything is made
  await field.waitForSelector('main .error');
  seen.noLabeler = await field.textContent('main .error');
  await field.setInputFiles('input[type=file]', folder);
  await field.waitForSelector('#send-folder');
  await field.fill('#labeler', '张三');
  await field.selectOption('#reviewer', algoName);
  await field.click('#send-folder');
  await field.waitForSelector('#batch-status');
  seen.uploaded = await field.textContent('#batch-status');
  await field.screenshot({ path: `${out}/batch-review.png`, fullPage: true });

  await algo.goto(`${url}#/batches`);
  await algo.waitForSelector(`a:has-text("${batch}")`);
  seen.todo = await algo.textContent('table');
  await algo.click(`a:has-text("${batch}")`);
  await algo.waitForSelector('#reject');
  await algo.click('#reject');                             // no comment: refused
  await algo.waitForSelector('main .error');
  seen.noComment = await algo.textContent('main .error');
  await algo.fill('#review-comment', 'b.png 漏标');
  await algo.fill('#review-images', 'b.png');
  await algo.click('#reject');
  await algo.waitForFunction(() => document.querySelector('#batch-status')?.textContent === '已退回');

  await field.reload();
  await field.waitForSelector('a:has-text("上传新的一版")');
  seen.rejected = await field.textContent('main');
  seen.prelabel = await field.locator('#prelabel-go').count();  // the owner may have a rejected batch pre-labelled
  await field.click('a:has-text("上传新的一版")');
  await field.waitForSelector('#labeler');
  await field.setInputFiles('input[type=file]', fixed);
  await field.waitForSelector('#send-folder');
  await field.fill('#labeler', '李四');
  seen.sameReviewer = await field.inputValue('#reviewer');
  await field.click('#send-folder');
  await field.waitForSelector('#box-changes');
  seen.changes = await field.textContent('#box-changes');
  seen.resubmitted = await field.textContent('#batch-status');
  await field.screenshot({ path: `${out}/batch-revision.png`, fullPage: true });

  await algo.reload();
  await algo.waitForSelector('#approve');
  await algo.click('#approve');
  await algo.waitForSelector('#import-preview');
  await algo.click('#import-preview');
  await algo.waitForSelector('#import-go');
  seen.plan = await algo.textContent('main');
  await algo.screenshot({ path: `${out}/batch-import.png`, fullPage: true });
  await algo.click('#import-go');
  await algo.waitForFunction(() => document.querySelector('#batch-status')?.textContent === '已入库');
  seen.done = await algo.textContent('main');
  seen.buttons = await algo.locator('main button').count();
  seen.errors = errors;
  console.log(JSON.stringify(seen));
  await browser.close();
})().catch(async (e) => { console.error(e); process.exit(1); });
