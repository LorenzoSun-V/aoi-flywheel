// Registering a model version in the browser (run by tests/server/test_web.py):
// node register.cjs <url> <algo token> <field token> <snapshot> <other snapshot> <model name> <files dir> <out dir>.
// Both snapshots are of one line with no model yet. The files dir holds best.onnx, best.pt, metrics.json and
// external.onnx (an ONNX with external data).
// From the snapshot's version page: a new model on the snapshot's line, a weights file refused as the ONNX, an ONNX
// with external data refused by the server, then the version with its metrics and weights. From the model's page
// and the models page no snapshot is chosen until one is picked; the model is proposed only when one was trained on
// that snapshot; the same ONNX again gives the version there is. A field account is offered nothing. One JSON line.
const { chromium } = require('playwright');

(async () => {
  const [url, token, field, snapshot, other, name, dir, out] = process.argv.slice(2);
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
  const form = async () => {
    await page.waitForSelector('#mv-go');
    await page.waitForFunction(() => document.querySelector('#mv-trainset-version option'));
    await page.waitForTimeout(300);  // the model proposed for the snapshot, if any
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
  await page.selectOption('#mv-trainset', other);          // and stays chosen when the snapshot changes
  seen.kept = await form();
  await page.selectOption('#mv-trainset', snapshot);
  await form();
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

  // from the model's page: the model chosen, the snapshot not (the model's next version may be trained on another)
  await page.goto(`${url}#/m/${encodeURIComponent(name)}`);
  await page.waitForSelector('#model-register');
  await page.click('#model-register');
  seen.again = await form();
  await page.setInputFiles('#mv-onnx', `${dir}/best.onnx`);
  seen.noSnapshot = await send();

  // from the models page: nothing chosen; a snapshot no model was trained on proposes none, not the line's only
  // model; the snapshot the model was trained on proposes it; the same ONNX gives the version there is
  await page.goto(`${url}#/models`);
  await page.waitForSelector('#model-register');
  seen.models = await page.getAttribute('#model-register', 'href');
  await page.click('#model-register');
  seen.bare = await form();
  await page.selectOption('#mv-trainset', other);
  seen.other = await form();
  await page.selectOption('#mv-trainset', snapshot);
  await page.waitForFunction((n) => document.querySelector('#mv-model').value === n, name);
  seen.picked = await form();
  await page.setInputFiles('#mv-onnx', `${dir}/best.onnx`);
  await page.setInputFiles('#mv-metrics', `${dir}/metrics.json`);
  await page.setInputFiles('#mv-weights', `${dir}/best.pt`);
  await page.fill('#mv-repo', 'https://git.example.com/aoi/rf-detr-pro.git');
  await page.fill('#mv-commit', 'a'.repeat(40));
  seen.againSent = await send();
  await page.waitForSelector('#toast.show');
  seen.toast = await page.textContent('#toast');
  seen.againHash = await page.evaluate(() => location.hash);

  // a field account: nothing to register with
  page = await open(field);
  await page.goto(`${url}#/v/${encodeURIComponent(snapshot)}/1`);
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
