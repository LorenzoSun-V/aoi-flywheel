// A labelling batch from upload to the pool, in the browser (run by tests/server/test_web.py):
// node batch.cjs <url> <field token> <algo token> <algo name> <pool> <batch name> <out dir> <folder> <fixed folder>.
// The field account uploads the folder as a batch and sends it to the algo account, which rejects it; the
// field account uploads the fixed folder; the algo account approves and imports it. Prints one JSON line.
const { chromium } = require('playwright');

const { dropOn } = require('./drop.cjs');

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
  // while the browser reads the chosen folder the box says so, then what it got
  const [chooser] = await Promise.all([field.waitForEvent('filechooser'), field.click('.drop .choose')]);
  seen.reading = await field.textContent('.drop.reading > span');
  await field.screenshot({ path: `${out}/batch-reading.png` });
  await chooser.setFiles(folder);
  await field.waitForSelector('#send-folder');
  await field.screenshot({ path: `${out}/batch-picked.png` });
  seen.picked = { text: await field.textContent('.drop > span'), reading: await field.locator('.drop.reading').count() };
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
  seen.reviewStart = await algo.textContent('#review-start');   // or marked by path, after looking in LiteAnno
  await algo.fill('#mark-path', 'zz.png');
  await algo.click('#mark-add');
  seen.wrongPath = await algo.textContent('.mark-add .bad');
  await algo.fill('#mark-path', 'b.png');
  await algo.press('#mark-path', 'Enter');
  await algo.fill('#review-marks input', '缺少划痕框');
  seen.marks = await algo.textContent('#review-marks-name');
  await algo.screenshot({ path: `${out}/batch-marks.png`, fullPage: true });
  await algo.click('#reject');
  await algo.waitForFunction(() => document.querySelector('#batch-status')?.textContent === '已退回');

  await field.reload();
  await field.waitForSelector('#rejected-card');
  seen.rejected = await field.textContent('main');
  seen.rejectedCard = await field.textContent('#rejected-card');
  seen.flowRejected = await field.$$eval('#batch-flow li', (l) => l.map((x) => [x.className, x.textContent]));
  seen.revise = await field.textContent('#revise');
  await field.screenshot({ path: `${out}/batch-rejected.png`, fullPage: true });
  await field.click('.batch-head .more-button');
  seen.fieldMore = await field.$$eval('.menu [role=menuitem]', (l) => l.map((x) => x.textContent));
  await field.keyboard.press('Escape');
  await field.click('#revise');
  await field.waitForSelector('#labeler');
  await dropOn(field, [fixed], { asFiles: true });  // a file, not a folder, is refused in the box itself
  seen.droppedFile = await field.textContent('.drop > span.bad');
  await dropOn(field, [fixed, folder]);  // a batch is one folder
  seen.droppedTwo = await field.textContent('.drop > span.bad');
  await dropOn(field, [folder], { slow: 400 });  // a slow first drag, then the fixed folder: the first one, done last, is not used
  await field.$eval('.drop input', (i) => { i.dispatchEvent(new Event('click')); i.dispatchEvent(new Event('cancel')); });
  seen.cancelled = { text: await field.textContent('.drop > span'), reading: await field.locator('.drop.reading').count() };
  await dropOn(field, [fixed]);
  await field.waitForSelector('#send-folder');
  await field.waitForTimeout(1200);
  seen.dropped = await field.textContent('.drop > span');
  await field.fill('#labeler', '李四');
  seen.sameReviewer = await field.inputValue('#reviewer');
  await field.click('#send-folder');
  await field.waitForSelector('#box-changes');
  seen.changes = await field.textContent('#box-changes');
  seen.resubmitted = await field.textContent('#batch-status');
  await field.screenshot({ path: `${out}/batch-revision.png`, fullPage: true });

  await algo.reload();
  await algo.waitForSelector('#since-card');
  seen.since = { fixed: await algo.textContent('#since-fixed .value'), rows: await algo.textContent('#since-rejected'),
    changed: await algo.textContent('#review-changed'), start: await algo.textContent('#review-start') };
  await algo.screenshot({ path: `${out}/batch-since.png`, fullPage: true });
  await algo.click('#approve');
  await algo.waitForSelector('#import-preview');
  await algo.click('#import-preview');
  await algo.waitForSelector('#import-go');
  seen.plan = await algo.textContent('main');
  await algo.screenshot({ path: `${out}/batch-import.png`, fullPage: true });
  await algo.click('#import-go');                           // asked once more: a pool version cannot be taken back
  seen.importAsk = await algo.textContent('.ask .ask-text');
  await algo.click('.ask .ask-go');
  await algo.waitForFunction(() => document.querySelector('#batch-status')?.textContent === '已入库');
  seen.done = await algo.textContent('main');
  seen.imported = await algo.textContent('#imported-card');
  seen.flowDone = await algo.$$eval('#batch-flow li', (l) => l.map((x) => x.className));
  await algo.screenshot({ path: `${out}/batch-imported.png`, fullPage: true });
  // nothing left to do: the side bar switch, back and 更多 (copying the fetch command) are not steps of the batch
  seen.buttons = await algo.locator('main button:not(.tool):not(.more-button)').count();
  seen.errors = errors;
  console.log(JSON.stringify(seen));
  await browser.close();
})().catch(async (e) => { console.error(e); process.exit(1); });
