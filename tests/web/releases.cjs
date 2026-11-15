// Registering releases on the web and reading them (run by tests/server/test_web.py):
// node releases.cjs <url> <field token> <algo token> <site> <model> <out dir>.
// As a field account on <site>/TX: the overview's release card (the ONNX of the current release to download, the
// newer candidate), registering that candidate's deployment from its page (a refusal shown where it was asked,
// the engine hashed in the browser and never uploaded), the line's record of releases; a deployment registered
// later but deployed earlier, and the same one again; the count of releases of the line leading to its record.
// As an algorithm engineer: the record and the card, with nothing to register. Prints one JSON line.
const { chromium } = require('playwright');

(async () => {
  const [url, field, algo, site, model, out] = process.argv.slice(2);
  const browser = await chromium.launch();
  const errors = [];
  const puts = [];  // files sent to the store
  const open = async (token) => {
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, timezoneId: 'Asia/Shanghai', locale: 'zh-CN', acceptDownloads: true });
    const p = await context.newPage();
    p.on('pageerror', (e) => errors.push(String(e)));
    p.on('console', (m) => { if (m.type() === 'error' && !/400/.test(m.text())) errors.push(m.text()); });  // the refusal asked for below
    p.on('request', (r) => { if (r.method() === 'PUT' && r.url().includes('/api/v1/objects/')) puts.push(r.url()); });
    await p.goto(url);
    await p.evaluate((t) => localStorage.setItem('fw-token', t), token);
    await p.reload();
    return p;
  };
  const engine = Buffer.from(`engine of ${site}`);
  let page = await open(field);
  const seen = {};
  const overview = async () => {
    await page.goto(`${url}#/?site=${site}&line=TX`);
    await page.waitForFunction((s) => location.hash.includes(`site=${s}&line=TX`) && document.querySelector('#release .ref'), site);
  };
  const toast = async (text) => {
    await page.waitForFunction((t) => document.querySelector('#toast.show')?.textContent.includes(t), text);
    return page.textContent('#toast');
  };
  const register = async (fill, said) => {  // the form filled, asked, confirmed: the toast it ends with (saying said)
    await page.waitForSelector('#release-go');
    await fill();
    await page.click('#release-go');
    await page.waitForSelector('.ask .ask-go');
    const asked = await page.textContent('.ask .ask-text');
    await page.click('.ask .ask-go');
    await page.waitForFunction(() => location.hash.startsWith('#/releases') && document.querySelector('#releases tr.picked'));
    return { asked, toast: await toast(said) };
  };
  try {

  // the card: what runs on the line now, its ONNX, and the newer candidate
  await overview();
  seen.card = { ref: await page.textContent('#release .ref'), acts: await page.textContent('#release .acts'),
    newer: await page.textContent('#release-newer') };
  const [download] = await Promise.all([page.waitForEvent('download'), page.click('#release-download')]);
  seen.downloaded = download.suggestedFilename();
  await page.screenshot({ path: `${out}/releases-card.png` });

  // the candidate's page: register its deployment
  await page.click('#release-newer a');
  await page.waitForFunction(() => location.hash.startsWith('#/mv/') && document.querySelector('#mv-release'));
  seen.versionActions = await page.$$eval('.page-head .actions > a, .page-head .actions > button', (b) => b.map((x) => x.textContent).filter(Boolean));  // not the ⓘ
  await page.click('#mv-release');
  await page.waitForSelector('#release-go');
  seen.form = { model: await page.textContent('#release-model'), repo: await page.inputValue('#release-repo'),
    site: await page.inputValue('#site'), line: await page.inputValue('#line') };
  await page.click('#release-go');
  seen.missing = await page.textContent('#release-result');
  await page.fill('#release-commit', 'xyz');
  await page.click('#release-go');
  await page.waitForSelector('.ask .ask-go');
  await page.click('.ask .ask-go');
  await page.waitForFunction(() => document.querySelector('.ask .bad')?.textContent);
  seen.refused = await page.textContent('.ask .bad');
  await page.click('.ask .ask-cancel');
  const sent = puts.length;
  const first = await register(async () => {
    await page.fill('#release-commit', '0123456789abcdef0123456789abcdef01234567');
    await page.fill('#release-gpu', 'RTX3070');
    await page.fill('#release-trt', '10.3');
    await page.selectOption('#release-precision', 'fp16');
    await page.setInputFiles('#release-engine', { name: 'model_fp16.engine', mimeType: 'application/octet-stream', buffer: engine });
    await page.fill('#release-note', '网页登记');
  }, '已登记发布');
  seen.first = { ...first, puts: puts.length - sent, picked: await page.getAttribute('#releases tr.picked', 'data-ref'),
    rows: await page.$$eval('#releases tr', (rows) => rows.slice(1).map((r) => [...r.cells].map((c) => c.textContent))) };
  await page.screenshot({ path: `${out}/releases-record.png`, fullPage: true });

  // an earlier deployment of a deprecated version, registered now: the current release stays; then the same again
  const earlier = async () => {
    await page.fill('#release-at', '2026-11-02T09:00');
    await page.fill('#release-commit', 'abcdef2');
  };
  await page.goto(`${url}#/release-new?model=${encodeURIComponent(`${model}@2`)}`);
  await page.waitForFunction(() => document.querySelector('#release-model .bad'));
  seen.deprecated = await page.textContent('#release-model .bad');
  seen.earlier = await register(earlier, '当前发布仍为');
  await page.goto(`${url}#/release-new?model=${encodeURIComponent(`${model}@2`)}`);
  seen.again = await register(earlier, '该次部署已登记为');
  seen.rows = await page.$$eval('#releases tr', (rows) => rows.slice(1).map((r) => [...r.cells].slice(0, 3).map((c) => c.textContent)));

  // the card now, and the count of releases leading to the record
  await overview();
  seen.after = { ref: await page.textContent('#release .ref'), newer: await page.$$eval('#release-newer', (n) => n.length) };
  await page.goto(`${url}#/`);
  await page.waitForSelector(`#lines a.releases[href*="site=${site}&line=TX"]`);
  seen.count = await page.$eval(`#lines a.releases[href*="site=${site}&line=TX"]`, (a) => [a.textContent, a.getAttribute('href')]);

  // the record fits at 1180
  await page.setViewportSize({ width: 1180, height: 900 });
  await page.goto(`${url}#/releases?site=${site}&line=TX`);
  await page.waitForFunction(() => location.hash.startsWith('#/releases') && !location.hash.includes('ref=') && document.querySelector('#releases'));
  seen.fits = await page.$eval('#releases', (t) => {
    const box = t.parentElement;
    const style = getComputedStyle(box);
    return [Math.round(t.getBoundingClientRect().width), box.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight),
      [...t.rows[1].cells].map((c) => Math.round(c.getBoundingClientRect().width))];
  });

  // an algorithm engineer reads; nothing registers
  page = await open(algo);
  await page.goto(`${url}#/releases?site=${site}&line=TX`);
  await page.waitForSelector('#releases');
  seen.algo = { rows: await page.$$eval('#releases tr', (r) => r.length - 1), add: await page.$$eval('#release-new', (b) => b.length) };
  await overview();
  seen.algo.acts = await page.textContent('#release .acts');
  seen.algo.download = await page.$$eval('#release-download', (b) => b.length);
  await page.goto(`${url}#/mv/${encodeURIComponent(model)}/3`);
  await page.waitForFunction(() => document.querySelector('main h1')?.textContent.includes('@3'));
  seen.algo.register = await page.$$eval('#mv-release', (b) => b.length);
  seen.algo.releases = await page.$eval('#mv-releases a', (a) => [a.textContent, a.getAttribute('href')]);
  await page.goto(`${url}#/release-new`);
  await page.waitForSelector('main .error');
  seen.algo.refused = await page.textContent('main .error');

  } catch (e) {
    await page.screenshot({ path: `${out}/releases-failed.png`, fullPage: true }).catch(() => {});
    throw new Error(`${e.message}\npage: ${await page.textContent('main').catch(() => '')}`);
  }
  seen.errors = errors;
  console.log(JSON.stringify(seen));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
