// Whether a GPU machine is there (run by tests/server/test_web.py):
// node machines.cjs <url> <token> <job>.
// The jobs page lists the GPU machines; the page of a queued job whose machine is offline says so. Prints one
// JSON line.
const { chromium } = require('playwright');

(async () => {
  const [url, token, job] = process.argv.slice(2);
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  await page.goto(url);
  await page.evaluate((t) => localStorage.setItem('fw-token', t), token);
  await page.goto(`${url}#/jobs`);
  await page.reload();
  await page.waitForSelector('#machines');
  const seen = { errors };
  seen.machines = await page.$$eval('#machines .status', (s) => s.map((x) => [x.querySelector('.dot').className, x.textContent]));
  await page.goto(`${url}#/job/${job}`);
  await page.waitForFunction((id) => document.querySelector('main h1')?.textContent.startsWith(`任务 ${id}`), job);
  seen.waits = await page.textContent('#job-waits');
  console.log(JSON.stringify(seen));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
