// Reviewing a batch revision in the viewer (run by tests/server/test_web.py):
// node review.cjs <url> <algo token> <field token> <batch name> <out dir>.
// The batch has two revisions (b.png and c.png changed in the second); the algo account goes through it in the
// viewer: zooms, hides a class, marks a.png with a note, pages through the changed images, comes back to its
// marks after a reload, and rejects from the viewer. The field account then sees the note on a.png. Prints one
// JSON line.
const { chromium } = require('playwright');

(async () => {
  const [url, algoToken, fieldToken, batch, out] = process.argv.slice(2);
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
  const algo = await open(algoToken);
  const seen = {};
  const text = async (sel) => (await algo.textContent(sel)).trim();
  const transform = () => algo.$eval('#stage canvas', (c) => c.style.transform);

  await algo.goto(`${url}#/b/${batch}`);
  await algo.waitForSelector('#review-start');
  seen.start = { button: await text('#review-start'), seen: await text('#review-seen') };
  await algo.click('#review-start');
  await algo.waitForSelector('#review-panel');
  await algo.waitForFunction(() => document.querySelector('#zoom')?.textContent.endsWith('%'));
  seen.first = { title: await text('main h1'), progress: await text('#review-progress'), shots: await algo.locator('#review-strip .shot').count(),
    on: await algo.locator('#review-strip .shot.on').count(), filters: await algo.$$eval('.strip-tabs a', (a) => a.map((x) => x.textContent)) };
  // zoom and pan
  const fit = await text('#zoom');
  await algo.click('#zoom-in');
  const zoomedIn = await text('#zoom');
  const box = await algo.locator('#stage').boundingBox();
  await algo.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await algo.mouse.wheel(0, -300);
  const wheeled = await text('#zoom');
  const beforeDrag = await transform();
  await algo.mouse.down();
  await algo.mouse.move(box.x + box.width / 2 + 40, box.y + box.height / 2 + 20, { steps: 4 });
  await algo.mouse.up();
  const dragged = await transform();
  await algo.dblclick('#stage');
  seen.zoom = { fit, zoomedIn, wheeled, moved: beforeDrag !== dragged, back: await text('#zoom') };
  await algo.click('#zoom-one');
  seen.zoom.one = await text('#zoom');
  await algo.click('#zoom-fit');
  // a class hidden and shown again
  await algo.click('button.class-toggle');
  seen.hidden = await algo.getAttribute('button.class-toggle', 'aria-pressed');
  await algo.click('button.class-toggle');
  seen.shownAgain = await algo.getAttribute('button.class-toggle', 'aria-pressed');
  // a.png marked, with a note
  seen.noteBefore = await algo.isVisible('#mark-note');
  await algo.click('#mark');
  await algo.fill('#mark-note', '右下角漏标一个划痕');
  seen.marked = { button: await text('#mark'), progress: await text('#review-progress'), flagged: await algo.locator('#review-strip .shot.marked').count() };
  await algo.waitForFunction(() => [...document.querySelectorAll('#review-strip img')].every((i) => i.complete && i.naturalWidth));  // thumbnails
  await algo.screenshot({ path: `${out}/review-marked.png`, fullPage: true });
  // only the changed images: the next one is b.png, then c.png, then nothing
  await algo.click('.strip-tabs a[data-filter="changed"]');
  await algo.waitForSelector('.strip-tabs a.on[data-filter="changed"]');   // the page drawn again with the filter on
  seen.changed = { shots: await algo.locator('#review-strip .shot').count() };
  await algo.keyboard.press('ArrowRight');
  await algo.waitForFunction(() => document.querySelector('main h1')?.textContent === 'b.png');
  await algo.waitForSelector('#review-panel');
  await algo.keyboard.press('ArrowRight');
  await algo.waitForFunction(() => document.querySelector('main h1')?.textContent === 'c.png');
  await algo.waitForSelector('#review-panel');
  seen.changed.last = await algo.isDisabled('button:has-text("下一张")');
  seen.changed.progress = await text('#review-progress');
  // what was marked is kept across a reload of the page
  await algo.goto(`${url}#/img/${batch}/2/a.png`);
  await algo.reload();
  await algo.waitForSelector('#review-panel');
  seen.kept = { button: await text('#mark'), note: await algo.inputValue('#mark-note'), progress: await text('#review-progress') };
  // rejected from the viewer: asked on the page first, then the batch page with the notes in its record
  await algo.click('#review-reject');
  seen.ask = await text('.ask-text');
  await algo.click('.ask-go');                       // no comment: refused in place
  await algo.waitForFunction(() => document.querySelector('.ask .bad')?.textContent);
  seen.noComment = await text('.ask .bad');
  await algo.fill('.ask-reason', '请补标划痕');
  await algo.click('.ask-go');
  await algo.waitForFunction(() => document.querySelector('#batch-status')?.textContent === '已退回');
  seen.steps = await text('#batch-steps');
  seen.left = await algo.evaluate(() => Object.keys(localStorage).filter((k) => k.startsWith('fw-review')));
  await algo.screenshot({ path: `${out}/review-rejected.png`, fullPage: true });

  const field = await open(fieldToken);
  await field.goto(`${url}#/img/${batch}/2/a.png`);
  await field.waitForSelector('#rejected-note');
  seen.owner = { note: (await field.textContent('#rejected-note')).trim(), panel: await field.locator('#review-panel').count() };
  seen.errors = errors;
  console.log(JSON.stringify(seen));
  await browser.close();
})().catch(async (e) => { console.error(e); process.exit(1); });
