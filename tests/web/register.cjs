// Registering a model version in the browser (run by tests/server/test_web.py):
// node register.cjs <url> <algo token> <field token> <snapshot> <model name> <files dir> <out dir>.
// The files dir holds best.onnx, best.pt, metrics.json and external.onnx (an ONNX with external data).
// From the snapshot's version page: a new model on the snapshot's line, a weights file refused as the ONNX, an ONNX
// with external data refused by the server, then the version with its metrics and weights; from the model's page:
// the same ONNX again gives the version there is; a field account is offered nothing. Prints one JSON line.
const { chromium } = require('playwright');

(async () => {
  const [url, token, field, snapshot, name, dir, out] = process.argv.slice(2);
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  page.on('console', (m) => { if (m.type() === 'error' && !/40[049]/.test(m.text())) errors.push(m.text()); });  // the refusals asked for below
  await page.goto(url);
  await page.evaluate((t) => localStorage.setItem('fw-token', t), token);
  await page.reload();
  const seen = {};
  const form = async () => {
    await page.waitForSelector('#mv-go');
    await page.waitForFunction(() => document.querySelector('#mv-trainset-version option'));
    return {
      trainset: `${await page.inputValue('#mv-trainset')}@${await page.inputValue('#mv-trainset-version')}`,
      model: await page.inputValue('#mv-model'),
      newShown: await page.$eval('#mv-new-name', (i) => !i.closest('.label').hidden),
      place: `${await page.inputValue('#site')}/${await page.inputValue('#line')}`,
    };
  };
  const send = async () => {
    await page.click('#mv-go');
    await page.waitForFunction(() => location.hash.startsWith('#/mv/') || document.querySelector('#mv-result .error'));
    return page.evaluate(() => (location.hash.startsWith('#/mv/') ? null : document.querySelector('#mv-result .error').textContent));
  };
  try {

  await page.goto(`${url}#/v/${encodeURIComponent(snapshot)}/1`);
  await page.waitForSelector('#model-from');
  await page.click('#model-from');
  seen.first = await form();
  await page.fill('#mv-new-name', name);
  await page.setInputFiles('#mv-onnx', `${dir}/best.pt`);
  seen.notOnnx = await send();
  await page.setInputFiles('#mv-onnx', `${dir}/external.onnx`);
  seen.external = await send();
  seen.afterRefusal = await page.inputValue('#mv-model');  // the model made on the way is now the one chosen
  await page.setInputFiles('#mv-onnx', `${dir}/best.onnx`);
  await page.setInputFiles('#mv-metrics', `${dir}/metrics.json`);
  await page.setInputFiles('#mv-weights', `${dir}/best.pt`);
  await page.fill('#mv-repo', 'https://git.example.com/aoi/rf-detr-pro.git');
  await page.fill('#mv-commit', 'a'.repeat(40));
  await page.fill('#mv-note', '网页登记');
  await page.screenshot({ path: `${out}/register-form.png`, fullPage: true });
  seen.sent = await send();
  await page.waitForFunction(() => document.querySelector('main h1') && document.querySelector('main h1').textContent.includes('@'));
  seen.page = { hash: await page.evaluate(() => location.hash), title: await page.textContent('main h1'), info: await page.textContent('main .kv'),
    files: await page.$$eval('main table tr td:first-child', (c) => c.map((x) => x.textContent)) };
  await page.screenshot({ path: `${out}/register-done.png`, fullPage: true });

  // from the model's page: the model and its snapshot chosen; the same ONNX gives the version there is
  await page.goto(`${url}#/m/${encodeURIComponent(name)}`);
  await page.waitForSelector('#model-register');
  await page.click('#model-register');
  seen.again = await form();
  await page.setInputFiles('#mv-onnx', `${dir}/best.onnx`);
  await page.setInputFiles('#mv-metrics', `${dir}/metrics.json`);
  await page.setInputFiles('#mv-weights', `${dir}/best.pt`);
  await page.fill('#mv-repo', 'https://git.example.com/aoi/rf-detr-pro.git');
  await page.fill('#mv-commit', 'a'.repeat(40));
  seen.againSent = await send();
  await page.waitForSelector('#toast.show');
  seen.toast = await page.textContent('#toast');
  seen.againHash = await page.evaluate(() => location.hash);

  // the overview and the models page lead here too
  await page.goto(`${url}#/models`);
  await page.waitForSelector('#model-register');
  seen.models = await page.getAttribute('#model-register', 'href');

  // a field account: nothing to register with
  await page.evaluate((t) => localStorage.setItem('fw-token', t), field);
  await page.goto(`${url}#/v/${encodeURIComponent(snapshot)}/1`);
  await page.reload();
  await page.waitForSelector('#snapshot-info');
  seen.fieldButtons = await page.$$eval('#model-from, #model-register', (x) => x.length);
  await page.goto(`${url}#/model-register?trainset=${encodeURIComponent(`${snapshot}@1`)}`);
  await page.waitForSelector('main .error');
  seen.fieldPage = await page.textContent('main .error');

  } catch (e) {
    await page.screenshot({ path: `${out}/register-failed.png`, fullPage: true }).catch(() => {});
    throw new Error(`${e.message}\npage: ${await page.textContent('main').catch(() => '')}`);
  }
  seen.errors = errors;
  console.log(JSON.stringify(seen));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
