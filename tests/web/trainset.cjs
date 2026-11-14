// Building a training-set snapshot in the browser (run by tests/server/test_web.py):
// node trainset.cjs <url> <algo token> <field token> <pool with versions 1 and 2> <out dir>.
// From the pool's first version: the name the page proposes, a class missing from the table refused, the preview,
// the snapshot and how to fetch it; from the second version: the next version of that snapshot, continuing its
// split; a snapshot's name typed and previewed at once; a line's newest pool as the source; a pool's name refused
// as a snapshot name; a field account offered nothing. Prints one JSON line.
const { chromium } = require('playwright');

(async () => {
  const [url, token, field, pool, out] = process.argv.slice(2);
  const browser = await chromium.launch();
  const errors = [];
  // a page (and browser context) of its own for each account: a request the page of the last account still makes
  // goes out as that account, not as the next one
  const open = async (t) => {
    const p = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    p.on('pageerror', (e) => errors.push(String(e)));
    p.on('console', (m) => { if (m.type() === 'error' && !/40[049]/.test(m.text())) errors.push(m.text()); });  // the refusals asked for below
    await p.goto(url);
    await p.evaluate((x) => localStorage.setItem('fw-token', x), t);
    await p.reload();
    return p;
  };
  let page = await open(token);
  const seen = {};
  const snapshot = `${pool}-det`;
  const form = async () => {
    await page.waitForSelector('#ts-name');
    await page.waitForFunction(() => document.querySelector('#ts-mode').textContent && document.querySelector('.ts-version option'));
    return {
      name: await page.inputValue('#ts-name'), mode: await page.textContent('#ts-mode'),
      sources: await page.$$eval('.ts-source', (rows) => rows.map((r) => `${r.querySelector('.ts-pool').value}@${r.querySelector('.ts-version').value}`)),
      classes: await page.inputValue('#ts-classes'), val: await page.inputValue('#ts-val'), test: await page.inputValue('#ts-test'),
      newTo: await page.$eval('#ts-new-to', (s) => (s.closest('.label').hidden ? null : s.value)),
    };
  };
  const preview = async () => {
    await page.click('#ts-preview');
    await page.waitForSelector('#ts-total, #ts-result .error');
  };
  try {

  // the line's overview leads here too
  await page.goto(`${url}#/?site=SES&line=TX`);
  await page.waitForSelector('#data-new');
  seen.overview = await page.getAttribute('#snapshot-new', 'href');

  // from the pool's first version: a new snapshot
  await page.goto(`${url}#/v/${encodeURIComponent(pool)}/1`);
  await page.waitForSelector('#trainset-from');
  await page.click('#trainset-from');
  seen.first = await form();
  await page.fill('#ts-classes', 'scratch');
  await preview();
  seen.missing = { error: await page.textContent('#ts-result .error'), create: await page.$$eval('#ts-create', (x) => x.length) };
  await page.fill('#ts-classes', 'scratch\ndent');
  seen.cleared = await page.$$eval('#ts-result > *', (x) => x.length);  // a change drops the preview shown
  await preview();
  seen.preview = {
    counts: await page.$$eval('#ts-counts tr', (rows) => rows.map((r) => [...r.children].map((c) => c.textContent.trim()))),
    total: await page.textContent('#ts-total'), create: await page.textContent('#ts-create'),
  };
  await page.screenshot({ path: `${out}/trainset-preview.png`, fullPage: true });
  await page.click('#ts-create');
  await page.waitForFunction((n) => location.hash === `#/v/${n}/1` && document.querySelector('#snapshot-info'), encodeURIComponent(snapshot));
  seen.made = {
    info: await page.textContent('#snapshot-info'), command: await page.inputValue('#fetch-command'),
    counts: await page.$$eval('#snapshot-counts tr', (rows) => rows.map((r) => [...r.children].map((c) => c.textContent.trim()))),
    next: await page.getAttribute('#trainset-next', 'href'),
  };
  await page.click('.command-row button');
  await page.waitForSelector('#toast.show');
  seen.copied = await page.textContent('#toast');
  await page.screenshot({ path: `${out}/trainset-made.png`, fullPage: true });

  // from the pool's second version: the next version of that snapshot, on its split
  await page.goto(`${url}#/v/${encodeURIComponent(pool)}/2`);
  await page.waitForSelector('#trainset-from');
  await page.click('#trainset-from');
  seen.second = await form();
  await preview();
  seen.secondPreview = { changes: await page.textContent('#ts-changes'), create: await page.textContent('#ts-create') };
  await page.click('#ts-create');
  await page.waitForFunction((n) => location.hash === `#/v/${n}/2` && document.querySelector('#snapshot-info'), encodeURIComponent(snapshot));
  seen.secondMade = await page.textContent('#snapshot-info');

  // the snapshot's own page offers its next version, with what it had
  await page.goto(`${url}#/trainset-new?name=${encodeURIComponent(snapshot)}`);
  seen.next = await form();

  // started from the line: its newest pool is the source, the snapshot that took data from it continued
  await page.goto(`${url}#/trainset-new?site=SES&line=TX`);
  await page.reload();  // the form of the page before (the same route) is not taken for this one
  seen.line = await form();

  // a pool's name is not a snapshot's
  await page.fill('#ts-name', pool);
  await page.dispatchEvent('#ts-name', 'change');
  await page.waitForFunction((p) => document.querySelector('#ts-mode').textContent === `将建立 ${p}@1`, pool);
  await page.fill('#ts-classes', 'scratch\ndent');
  await preview();
  seen.taken = await page.textContent('#ts-result .error');

  // a snapshot's name typed and 预览 pressed at once (the click blurs the name, which starts loading that
  // snapshot): the preview is of its next version, on its classes and sources
  await page.fill('#ts-classes', '');
  await page.fill('#ts-name', snapshot);
  await page.click('#ts-preview');
  await page.waitForSelector('#ts-total, #ts-result .error');
  await page.waitForTimeout(300);  // a load still running would wipe the preview
  seen.typed = { mode: await page.textContent('#ts-mode'), classes: await page.inputValue('#ts-classes'),
    error: await page.$$eval('#ts-result .error', (x) => x.map((e) => e.textContent).join('')),
    create: await page.$$eval('#ts-create', (x) => x.map((e) => e.textContent).join('')) };

  // a field account sees the snapshot but nothing that builds one
  page = await open(field);
  await page.goto(`${url}#/v/${encodeURIComponent(pool)}/1`);
  await page.waitForSelector('main .kv');
  seen.fieldButtons = await page.$$eval('#trainset-from, #trainset-next', (x) => x.length);
  await page.goto(`${url}#/trainset-new?from=${encodeURIComponent(`${pool}@1`)}`);
  await page.waitForSelector('main .error');
  seen.fieldPage = await page.textContent('main .error');

  } catch (e) {
    await page.screenshot({ path: `${out}/trainset-failed.png`, fullPage: true }).catch(() => {});
    throw new Error(`${e.message}\npage: ${await page.textContent('main').catch(() => '')}`);
  }
  seen.errors = errors;
  console.log(JSON.stringify(seen));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
