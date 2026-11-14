// The line overview as it is meant to be read: SES / TX 贴片线 at 1440 and 1920 wide, the ⋯ menu of a model row
// by mouse and by keys, copying a version reference (and what happens where the browser has no clipboard),
// switching lines in the side bar, a line with nothing on it, and less motion when the system asks for it.
const { chromium } = require('playwright');

(async () => {
  const [url, token, out, empty] = process.argv.slice(2);
  const browser = await chromium.launch();
  const errors = [];
  const open = async (options = {}, init = null) => {
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2, timezoneId: 'Asia/Shanghai', locale: 'zh-CN', ...options });
    if (init) await context.addInitScript(init);
    const page = await context.newPage();
    page.on('pageerror', (e) => errors.push(String(e)));
    page.on('console', (m) => { if (m.type() === 'error' && !/404/.test(m.text())) errors.push(m.text()); });  // 404: a line without a release
    await page.goto(url);
    await page.evaluate((t) => localStorage.setItem('fw-token', t), token);
    await page.reload();
    return { context, page };
  };
  const seen = {};
  const { context, page } = await open();
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
  const h1 = (p, text) => p.waitForFunction((t) => document.querySelector('main h1')?.textContent === t, text);
  const box = (sel) => page.$eval(sel, (e) => { const r = e.getBoundingClientRect(); return { x: r.x, y: r.y, w: r.width, h: r.height, bottom: r.bottom, right: r.right }; });
  const row = (name) => `table.models tr:has(a:text-is("${name}"))`;
  const opacity = (sel) => page.$eval(sel, (e) => getComputedStyle(e).opacity);

  // 概览 → SES → TX 贴片线, by the side bar
  await page.waitForSelector('#lines');
  await page.click('#places li.site a[href="#/?site=SES"]');
  await page.waitForFunction(() => document.querySelector('#lines') && document.querySelector('#places li.site a.on'));
  await page.click('#places li.line a[href="#/?site=SES&line=TX"]');
  await h1(page, 'TX 贴片线');
  await page.waitForSelector('#release .ref');
  seen.crumbs = await page.$$eval('.crumbs .path a, .crumbs .path .here', (c) => c.map((x) => x.textContent));
  seen.navOn = await page.textContent('#nav a.on');
  seen.lineOn = await page.textContent('#places li.line a.on');
  seen.release = { ref: await page.textContent('#release .ref'), sub: await page.textContent('#release .sub'),
    state: await page.textContent('#release .state'), go: await page.getAttribute('#release a.go', 'href') };
  seen.models = await page.$$eval('table.models tr', (rows) => rows.slice(1).map((r) => [...r.querySelectorAll('td')].slice(0, 4).map((c) => c.textContent)));
  seen.data = await page.$$eval('table.data tr', (rows) => rows.slice(1).map((r) => [...r.querySelectorAll('td')].slice(0, 2).map((c) => c.textContent)));
  seen.batches = await page.$$eval('table.batches tr', (rows) => rows.map((r) => r.querySelector('td.state').textContent));
  const layout = async () => {
    const [release, models, data, batches, mainBox] = await Promise.all(['#release', 'table.models', 'table.data', 'table.batches', 'main'].map(box));
    return { releaseFirst: release.bottom <= models.y && release.y < 300, dataUnderModels: data.y > models.bottom,
      sideBySide: batches.x > models.right && Math.abs(batches.y - models.y) < 5, stacked: batches.y > data.bottom,
      fills: Math.round(release.w), main: Math.round(mainBox.w) };
  };
  seen.at1440 = await layout();
  await page.mouse.move(5, 5);
  await page.screenshot({ path: `${out}/overview-tx-1440.png` });

  // pointing at a row brings its ⋯; the numbers stay where they are
  const dots = (name) => `${row(name)} button.dots`;
  const numX = () => page.$$eval('table.models tr', (rows) => rows.map((r) => Math.round(r.cells[2].getBoundingClientRect().right)));
  seen.dotsHidden = await opacity(dots('SES-TX-detr'));
  const before = await numX();
  await page.hover(`${row('SES-TX-detr')} td.num >> nth=0`);
  await page.waitForTimeout(300);
  seen.dotsHovered = [await opacity(dots('SES-TX-detr')), await opacity(dots('SES-TX-yolo'))];
  seen.numbersStay = JSON.stringify(before) === JSON.stringify(await numX());

  // the menu by mouse: three items for a model with an evaluation, two without
  await page.click(dots('SES-TX-detr'));
  await page.waitForSelector('.menu');
  seen.menu = await page.$$eval('.menu [role=menuitem]', (items) => items.map((i) => [i.textContent, i.getAttribute('href')]));
  seen.expanded = await page.getAttribute(dots('SES-TX-detr'), 'aria-expanded');
  await page.hover('.menu [role=menuitem] >> nth=0');
  await page.screenshot({ path: `${out}/overview-tx-menu-1440.png` });
  await page.mouse.click(700, 140);  // outside
  await page.waitForFunction(() => !document.querySelector('.menu'));
  seen.closedByOutside = true;
  await page.click(dots('SES-TX-seg'));
  seen.menuNoEval = await page.$$eval('.menu [role=menuitem]', (items) => items.map((i) => i.textContent));
  await page.click(dots('SES-TX-seg'));  // again: closes
  await page.waitForFunction(() => !document.querySelector('.menu'));

  // the menu by keys: Tab to ⋯, Enter, arrows, Esc back to ⋯
  await page.focus(`${row('SES-TX-detr')} td:nth-child(2) a`);
  await page.keyboard.press('Tab');
  seen.tabToDots = await page.evaluate(() => document.activeElement.classList.contains('dots'));
  await page.waitForTimeout(300);  // the fade
  seen.dotsFocused = await opacity(dots('SES-TX-detr'));
  await page.keyboard.press('Enter');
  await page.waitForSelector('.menu');
  seen.keyFirst = await page.evaluate(() => document.activeElement.textContent);
  await page.keyboard.press('ArrowDown');
  seen.keySecond = await page.evaluate(() => document.activeElement.textContent);
  await page.keyboard.press('Escape');
  await page.waitForFunction(() => !document.querySelector('.menu'));
  seen.focusBack = await page.evaluate(() => document.activeElement.getAttribute('aria-label'));

  // copying the reference the row shows
  await page.click(dots('SES-TX-detr'));
  await page.click('.menu [role=menuitem]:has-text("复制版本号")');
  await page.waitForSelector('#toast.show');
  seen.toast = await page.textContent('#toast');
  seen.clipboard = await page.evaluate(() => navigator.clipboard.readText());

  // the links of the menu go where the row says
  await page.click(dots('SES-TX-detr'));
  await page.click('.menu [role=menuitem]:has-text("查看评估")');
  await page.waitForFunction(() => location.hash.startsWith('#/eval/') && document.querySelector('main h1')?.textContent.startsWith('评估 #'));
  seen.evalPage = [page.url().split('#')[1], await page.textContent('main h1')];
  await page.goBack();
  await h1(page, 'TX 贴片线');
  await page.waitForSelector('table.models');
  await page.click(dots('SES-TX-detr'));
  await page.waitForSelector('.menu');
  await page.click('.menu [role=menuitem]:has-text("查看版本")');
  await page.waitForFunction(() => location.hash.startsWith('#/mv/') && document.querySelector('main h1')?.textContent.includes('@'));
  seen.versionPage = page.url().split('#')[1];
  await page.goBack();
  await h1(page, 'TX 贴片线');
  await page.waitForSelector('#release a.go');
  await page.click('#release a.go');
  await page.waitForFunction(() => location.hash.startsWith('#/mv/'));
  seen.releasePage = page.url().split('#')[1];

  // another line, with nothing on it, from the side bar; then back to TX
  await page.click(`#places li.line a[href="#/?site=SES&line=${empty}"]`);
  await h1(page, `${empty} 组装线`);
  seen.empty = { release: await page.textContent('#release'), models: await page.textContent('.cols .col'),
    lineOn: await page.textContent('#places li.line a.on') };
  await page.click('#places li.line a[href="#/?site=SES&line=TX"]');
  await h1(page, 'TX 贴片线');
  await page.waitForSelector('table.models');

  // wide and narrow windows
  await page.waitForFunction(() => !document.querySelector('#toast.show'));
  await page.setViewportSize({ width: 1920, height: 1080 });
  await page.waitForTimeout(200);
  seen.at1920 = await layout();
  await page.screenshot({ path: `${out}/overview-tx-1920.png` });
  await page.setViewportSize({ width: 1180, height: 900 });
  await page.waitForTimeout(200);
  seen.at1180 = await layout();
  await page.screenshot({ path: `${out}/overview-tx-1180.png`, fullPage: true });

  // a browser without a clipboard for this page (http on the intranet): the reference is shown, selected
  const bare = await open({}, () => { Object.defineProperty(Navigator.prototype, 'clipboard', { get: () => undefined }); });
  await bare.page.goto(`${url}#/?site=SES&line=TX`);
  await h1(bare.page, 'TX 贴片线');
  await bare.page.waitForSelector('table.models');
  await bare.page.click(dots('SES-TX-detr'));
  await bare.page.click('.menu [role=menuitem]:has-text("复制版本号")');
  await bare.page.waitForSelector('.menu .copy-field');
  seen.fallback = await bare.page.evaluate(() => {
    const f = document.activeElement;
    return { value: f.value, focused: f.classList.contains('copy-field'), selected: f.selectionStart === 0 && f.selectionEnd === f.value.length,
      text: document.querySelector('.menu p').textContent };
  });
  await bare.page.screenshot({ path: `${out}/overview-tx-copy-fallback.png` });
  await bare.page.keyboard.press('Escape');
  await bare.page.waitForFunction(() => !document.querySelector('.menu'));
  seen.fallbackFocusBack = await bare.page.evaluate(() => document.activeElement.classList.contains('dots'));

  // less motion when the system asks for it
  const calm = await open({ reducedMotion: 'reduce' });
  await calm.page.goto(`${url}#/?site=SES&line=TX`);
  await calm.page.waitForSelector('table.models button.dots');
  seen.calm = await calm.page.$eval('table.models button.dots', (e) => getComputedStyle(e).transitionDuration);
  seen.normal = await page.$eval('table.models button.dots', (e) => getComputedStyle(e).transitionDuration);

  seen.errors = errors;
  console.log(JSON.stringify(seen));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
