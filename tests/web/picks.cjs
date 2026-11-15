// Data and model versions are picked from lists, not typed (run by tests/server/test_web.py):
// node picks.cjs <url> <algo token> <field token> <model version ref> <benchmark> <runner> <batch> <out dir>.
// The evaluation form of the model version starts on the line's benchmark test set and lists its folders; one is
// ticked and the evaluation submitted. The batch's pre-labelling form lists the model versions of its line.
// Prints one JSON line of what it saw.
const { chromium } = require('playwright');

(async () => {
  const [url, algo, field, ref, bench, runner, batch, out] = process.argv.slice(2);
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
  await page.goto(`${url}#/mv/${model}/${number}`);
  await page.waitForFunction(() => document.querySelectorAll('#eval-dirs input[type=checkbox]').length > 0);
  seen.eval = await page.evaluate(() => ({
    data: document.querySelector('#eval-data').value,
    versions: [...document.querySelector('#eval-version').options].map((o) => o.textContent),
    dirs: [...document.querySelectorAll('#eval-dirs label')].map((l) => l.textContent),
    typed: [...document.querySelectorAll('.forms input[type=text]')].map((i) => i.className),  // only the conf
  }));
  await page.check('#eval-dirs input[value="D2"]');
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

  page = await open(field);
  await page.goto(`${url}#/b/${batch}`);
  await page.waitForSelector('#prelabel-model');
  seen.prelabel = await page.$eval('#prelabel-model', (s) => ({ value: s.value, options: [...s.options].map((o) => o.textContent), tag: s.tagName }));
  await page.screenshot({ path: `${out}/prelabel-form.png`, fullPage: true });

  seen.errors = errors;
  console.log(JSON.stringify(seen));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
