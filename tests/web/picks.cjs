// Data and model versions are picked from lists, not typed (run by tests/server/test_web.py):
// node picks.cjs <url> <algo token> <field token> <model version ref> <benchmark> <runner> <batches> <out dir>.
// The evaluation form of the model version starts on the line's benchmark test set and lists its folders when
// asked; one is ticked and the evaluation submitted. The pre-labelling form of each batch (comma-separated, one
// page each) offers the release of its line first and the model versions of every line. Prints one JSON line.
const { chromium } = require('playwright');

(async () => {
  const [url, algo, field, ref, bench, runner, batches, out] = process.argv.slice(2);
  const [model, number] = ref.split('@');
  const browser = await chromium.launch();
  const errors = [];
  const open = async (token) => {
    const p = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    p.on('pageerror', (e) => errors.push(String(e)));
    p.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
    await p.goto(url);
    await p.evaluate((t) => localStorage.setItem('fw-token', t), token);
    await p.reload();
    return p;
  };
  const seen = {};

  let page = await open(algo);
  let manifests = 0;  // file lists read: a large pool's is many MB, read only when its folders are asked for
  page.on('request', (r) => { if (r.url().includes('/api/v1/objects/')) manifests += 1; });
  await page.goto(`${url}#/mv/${model}/${number}`);
  await page.waitForFunction(() => document.querySelector('#eval-version')?.options.length > 0);
  await page.waitForTimeout(300);
  seen.before = { manifests, dirs: await page.$$eval('#eval-dirs input', (i) => i.length), said: await page.textContent('#eval-dirs-box summary') };
  await page.click('#eval-dirs-box summary');
  await page.waitForFunction(() => document.querySelectorAll('#eval-dirs input[type=checkbox]').length > 0);
  seen.eval = await page.evaluate(() => ({
    data: document.querySelector('#eval-data').value,
    versions: [...document.querySelector('#eval-version').options].map((o) => o.textContent),
    dirs: [...document.querySelectorAll('#eval-dirs label')].map((l) => l.textContent),
    typed: [...document.querySelectorAll('.forms input[type=text]')].map((i) => i.className),  // only the conf
  }));
  await page.check('#eval-dirs input[value="D2"]');
  seen.said = await page.textContent('#eval-dirs-box summary');
  const form = page.locator('form', { hasText: '测试数据' });
  await form.locator('select').first().selectOption(runner);
  await page.screenshot({ path: `${out}/eval-form.png`, fullPage: true });
  await form.locator('button[type=submit]').click();
  await page.waitForFunction(() => /^#\/job\/\d+/.test(location.hash));
  seen.job = Number(/^#\/job\/(\d+)/.exec(await page.evaluate(() => location.hash))[1]);
  // a split of the snapshot: no version, no folders
  await page.goto(`${url}#/mv/${model}/${number}`);
  await page.waitForSelector('#eval-data');
  const split = await page.$eval('#eval-data', (s) => s.querySelector('optgroup[label="训练集快照"] option').value);
  await page.selectOption('#eval-data', split);
  seen.split = await page.evaluate(() => [document.querySelector('#eval-version').closest('label').hidden,
    document.querySelector('#eval-dirs').closest('.label').hidden]);

  seen.prelabel = [];
  for (const batch of batches.split(',')) {
    page = await open(field);
    await page.goto(`${url}#/b/${batch}`);
    await page.waitForSelector('#prelabel-model');
    const form = await page.$eval('#prelabel-form', (f) => {
      const s = f.querySelector('#prelabel-model');
      return { value: s.value, disabled: s.disabled, first: s.options[0].textContent, chosen: s.selectedOptions[0]?.textContent || null,
        groups: [...s.querySelectorAll('optgroup')].map((g) => [g.label, [...g.querySelectorAll('option')].map((o) => o.value)]),
        hint: f.querySelector('p.muted')?.textContent || null, go: f.querySelector('#prelabel-go').disabled };
    });
    if (!form.value && !form.go) {
      await page.click('#prelabel-go');
      await page.waitForSelector('div.error');
      form.refused = (await page.textContent('div.error')).trim();
    }
    seen.prelabel.push(form);
    await page.screenshot({ path: `${out}/prelabel-${seen.prelabel.length}.png`, fullPage: true });
  }

  seen.errors = errors;
  console.log(JSON.stringify(seen));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
