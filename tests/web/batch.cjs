// A labelling batch from upload to the pool, in the browser (run by tests/server/test_web.py):
// node batch.cjs <url> <field token> <algo token> <algo name> <pool> <batch name> <out dir> <folder> <fixed folder>.
// The field account uploads the folder as a batch and sends it to the algo account, which rejects it; the
// field account uploads the fixed folder; the algo account approves and imports it. Prints one JSON line.
const { chromium } = require('playwright');

// Drops a folder of this machine on the page's folder box, the way a browser hands a dragged folder over
// (FileSystemDirectoryEntry), or (asFolder false) drops the folder's first file only.
async function dropOn(page, folder, asFolder, slow = 0) {
  const fs = require('fs');
  const path = require('path');
  const walk = (dir, rel) => fs.readdirSync(dir, { withFileTypes: true }).sort((a, b) => (a.name < b.name ? -1 : 1))
    .flatMap((d) => (d.isDirectory() ? walk(path.join(dir, d.name), `${rel}/${d.name}`)
      : [[`${rel}/${d.name}`, fs.readFileSync(path.join(dir, d.name)).toString('base64')]]));
  const top = `/${path.basename(folder)}`;
  await page.$eval('.drop', (box, [top, files, asFolder, slow]) => {
    const dirs = new Map();
    const dir = (fullPath) => {
      if (!dirs.has(fullPath)) {
        const kids = [];
        dirs.set(fullPath, { isDirectory: true, isFile: false, name: fullPath.slice(fullPath.lastIndexOf('/') + 1), fullPath, kids,
          createReader() { let given = false; return { readEntries(ok) { setTimeout(() => { ok(given ? [] : kids); given = true; }, slow); } }; } });
        const up = fullPath.slice(0, fullPath.lastIndexOf('/'));
        if (up) dir(up).kids.push(dirs.get(fullPath));
      }
      return dirs.get(fullPath);
    };
    const leaves = files.map(([fp, b64]) => {
      const name = fp.slice(fp.lastIndexOf('/') + 1);
      const file = new File([Uint8Array.from(atob(b64), (c) => c.charCodeAt(0))], name);
      const entry = { isDirectory: false, isFile: true, name, fullPath: fp, file: (ok) => ok(file) };
      dir(fp.slice(0, fp.lastIndexOf('/'))).kids.push(entry);
      return entry;
    });
    const ev = new Event('drop', { bubbles: true, cancelable: true });
    const entry = asFolder ? dir(top) : leaves[0];
    Object.defineProperty(ev, 'dataTransfer', { value: { types: ['Files'], items: [{ kind: 'file', webkitGetAsEntry: () => entry }] } });
    box.dispatchEvent(ev);
  }, [top, walk(folder, top), asFolder, slow]);
}

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
  await algo.fill('#review-images', 'b.png');
  await algo.click('#reject');
  await algo.waitForFunction(() => document.querySelector('#batch-status')?.textContent === '已退回');

  await field.reload();
  await field.waitForSelector('a:has-text("上传新的一版")');
  seen.rejected = await field.textContent('main');
  seen.prelabel = await field.locator('#prelabel-go').count();  // the owner may have a rejected batch pre-labelled
  await field.click('a:has-text("上传新的一版")');
  await field.waitForSelector('#labeler');
  await dropOn(field, fixed, false);  // a file, not a folder, is refused in the box itself
  seen.droppedFile = await field.textContent('.drop > span.bad');
  await dropOn(field, folder, true, 400);  // a slow first drag, then the fixed folder: the first one, done last, is not used
  await field.$eval('.drop input', (i) => { i.dispatchEvent(new Event('click')); i.dispatchEvent(new Event('cancel')); });
  seen.cancelled = { text: await field.textContent('.drop > span'), reading: await field.locator('.drop.reading').count() };
  await dropOn(field, fixed, true);
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
  seen.buttons = await algo.locator('main button:not(.tool)').count();  // nothing left to do (the side bar switch and back are not steps of the batch)
  seen.errors = errors;
  console.log(JSON.stringify(seen));
  await browser.close();
})().catch(async (e) => { console.error(e); process.exit(1); });
