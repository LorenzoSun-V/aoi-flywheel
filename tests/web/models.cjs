// A browser walk through the model, job and prediction pages (run by tests/server/test_web.py):
// node models.cjs <url> <token> <phase> <model version ref> <out dir> [<data ref> <runner> | <prediction set id>].
// submit: on the model version page, submit a prediction job of <data ref> through the form;
// view: the evaluations (compare), the jobs, the prediction set and an image with its predicted boxes.
// Prints one JSON line of what it saw.
const { chromium } = require('playwright');

(async () => {
  const [url, token, phase, ref, out, arg, runner] = process.argv.slice(2);
  const [model, number] = ref.split('@');
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
  await page.goto(url);
  await page.evaluate((t) => localStorage.setItem('fw-token', t), token);
  await page.reload();  // signed in from the start, as after a login
  const seen = {};
  const h1 = (text) => page.waitForFunction((t) => document.querySelector('h1') && document.querySelector('h1').textContent.includes(t), text);
  if (phase === 'submit') {
    await page.goto(`${url}#/mv/${model}/${number}`);
    await h1(ref);
    seen.nav = await page.$$eval('#nav a', (as) => as.map((a) => a.textContent));
    seen.info = await page.textContent('dl.kv');
    seen.evals = await page.$$eval('a[href^="#/eval/"]', (as) => as.map((a) => a.textContent));
    await page.screenshot({ path: `${out}/model-version.png`, fullPage: true });
    const form = page.locator('form', { hasText: '预标注' });
    await form.locator('input[placeholder^="数据池版本"]').fill('nope@1');
    await form.locator('button[type=submit]').click();
    await form.locator('.error').waitFor();
    seen.refused = await form.locator('.error').textContent();
    errors.length = 0;  // the 404 of the refused submit
    await form.locator('input[placeholder^="数据池版本"]').fill(arg);
    await form.locator('input[placeholder^="目录"]').fill('A');
    await form.locator('select').selectOption(runner);
    await form.locator('button[type=submit]').click();
    await h1('推理（预标注）');
    seen.job = page.url().split('#')[1];
    seen.jobText = await page.textContent('dl.kv');
    seen.cancel = await page.isVisible('button:has-text("取消任务")');
    await page.screenshot({ path: `${out}/job-queued.png` });
  } else {
    await page.goto(`${url}#/m/${model}`);
    await h1(model);
    await page.check('table tr:nth-child(2) input[type=checkbox]');
    await page.check('table tr:nth-child(3) input[type=checkbox]');
    await page.click('button:has-text("比较所选的两份报告")');
    await h1('比较评估');
    seen.compare = await page.$$eval('table tr td:first-child', (tds) => tds.map((t) => t.textContent));
    await page.screenshot({ path: `${out}/eval-compare.png` });
    await page.goto(`${url}#/jobs`);
    await h1('任务');
    seen.jobs = await page.$$eval('table tr td:nth-child(5)', (tds) => tds.map((t) => t.textContent));
    await page.click('table tr:nth-child(2) td:first-child a');
    await page.waitForFunction(() => document.querySelector('pre.log') && document.querySelector('pre.log').textContent.length > 0);
    seen.result = await page.textContent('dl.kv');
    seen.log = await page.textContent('pre.log');
    await page.screenshot({ path: `${out}/job-done.png` });
    await page.goto(`${url}#/p/${arg}`);
    await h1(`预测集 #${arg}`);
    await page.waitForSelector('.tile img[src^="blob:"]');
    seen.tiles = await page.$$eval('.tile .name', (ns) => ns.map((n) => n.textContent));
    seen.summary = await page.textContent('.row.panel');
    await page.screenshot({ path: `${out}/prediction-set.png` });
    await page.click('.tile >> nth=0');
    await page.waitForFunction(() => document.querySelector('.viewer dl') !== null);
    seen.image = await page.textContent('.viewer .panel');
    seen.imageHash = page.url().split('#')[1];
    await page.keyboard.press('ArrowRight');
    await page.waitForFunction(() => /^2 \//.test([...document.querySelectorAll('.row .muted')].map((m) => m.textContent).join('')));
    seen.next = page.url().split('#')[1];
    await page.screenshot({ path: `${out}/image-predictions.png` });
  }
  seen.errors = errors;
  console.log(JSON.stringify(seen));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
