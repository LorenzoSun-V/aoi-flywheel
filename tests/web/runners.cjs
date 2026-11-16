// Registering a runner from the web (run by tests/server/test_web.py):
// node runners.cjs <url> <algo token> <worker token> <runner> <machine> <new name>.
// The runners page lists each runner with its machine and newest check; the form asks the machine's worker to
// register a new one; a runner's page asks it to register that one again. A worker account sees no buttons.
// Prints one JSON line.
const { chromium } = require('playwright');

(async () => {
  const [url, algo, workerToken, runner, machine, made] = process.argv.slice(2);
  const browser = await chromium.launch();
  const errors = [];
  const open = async (token, hash) => {
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    page.on('pageerror', (e) => errors.push(String(e)));
    await page.goto(url);
    await page.evaluate((t) => localStorage.setItem('fw-token', t), token);
    await page.goto(`${url}${hash}`);
    await page.reload();
    return page;
  };
  const seen = { errors };
  const page = await open(algo, '#/runners');
  await page.waitForSelector('#runners');
  seen.nav = await page.$$eval('#nav a', (a) => a.map((x) => x.textContent.trim()));
  seen.row = await page.$eval(`#runners tr[data-runner="${runner}"]`, (tr) => [...tr.cells].slice(0, 8).map((c) => c.textContent.trim()));
  await page.click(`#runners tr[data-runner="${runner}"] button.dots`);  // the row's ⋯: 检查 opens the new check job
  seen.menu = await page.$$eval('[role=menu] [role=menuitem]', (m) => m.map((x) => x.textContent.trim()));
  await page.click(`[id="check-${runner}"]`);
  await page.waitForFunction(() => location.hash.startsWith('#/job/') && document.querySelector('main h1')?.textContent.includes('检查推理环境'));
  seen.checkHash = await page.evaluate(() => location.hash);
  await page.goto(`${url}#/runners`);
  await page.waitForSelector('#runners');
  await page.click('#runner-new');
  await page.waitForSelector('#runner-form');
  await page.click('#runner-go');
  seen.empty = await page.textContent('#runner-form .error');
  await page.fill('#runner-name', made);
  await page.selectOption('#runner-machine', machine);
  await page.fill('#runner-env', made);
  seen.toolShown = await page.isVisible('#runner-onnx2engine');
  await page.selectOption('#runner-framework', '');
  seen.toolHidden = !(await page.isVisible('#runner-onnx2engine'));
  await page.click('#runner-go');
  await page.waitForFunction(() => document.querySelector('main h1')?.textContent.includes('登记推理环境') && location.hash.startsWith('#/job/'));
  seen.madeHash = await page.evaluate(() => location.hash);
  seen.madeWhat = await page.textContent('main dl.kv dd');
  seen.madeWaits = await page.textContent('#job-waits');
  await page.goto(`${url}#/runner/${runner}`);
  await page.waitForSelector('#runner-versions');
  seen.check = await page.textContent('#runner-last-check');
  seen.versions = await page.$$eval('#runner-versions tr', (r) => r.length - 1);
  await page.click('#runner-again');
  await page.waitForFunction(() => location.hash.startsWith('#/job/') && document.querySelector('main h1')?.textContent.includes('登记推理环境'));
  seen.againHash = await page.evaluate(() => location.hash);
  const viewer = await open(workerToken, `#/runner/${runner}`);
  await viewer.waitForSelector('#runner-versions');
  seen.workerButtons = await viewer.$$eval('#runner-check, #runner-again, #runner-elsewhere', (b) => b.length);
  console.log(JSON.stringify(seen));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
