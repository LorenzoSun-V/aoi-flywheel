// The small management steps that were only on the command line, in the browser (run by tests/server/test_web.py):
// node admin.cjs <url> <algo token> <admin token> <field token> <out dir> <dataset with label v0.1.0>
//   <model version ref> <dataset with 511 changes from @1 to @2> <new site code>.
// A new test set of no line (and one refused for its taken name), registering and renaming a line, retiring a
// label (refused without a reason first), marking a model version candidate and deprecated and back, listing and
// saving every change of a large comparison; what a field account does not get. Prints one JSON line.
const fs = require('fs');
const { chromium } = require('playwright');

(async () => {
  const [url, algoToken, adminToken, fieldToken, out, labelled, mvRef, big, site] = process.argv.slice(2);
  const browser = await chromium.launch();
  const errors = [];
  const open = async (token) => {
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, acceptDownloads: true });
    const page = await context.newPage();
    page.on('pageerror', (e) => errors.push(String(e)));
    page.on('console', (m) => { if (m.type() === 'error' && !/40[09]/.test(m.text())) errors.push(m.text()); });
    await page.goto(url);
    await page.evaluate((t) => localStorage.setItem('fw-token', t), token);
    await page.reload();
    return page;
  };
  const algo = await open(algoToken);
  const admin = await open(adminToken);
  const field = await open(fieldToken);
  const seen = {};
  const [model, number] = mvRef.split('@');

  // a new dataset starts from the head of the datasets page
  await algo.goto(`${url}#/datasets?site=SES&line=TX`);
  await algo.waitForSelector('.page-head #dataset-new');
  seen.newHref = await algo.getAttribute('#dataset-new', 'href');
  await algo.screenshot({ path: `${out}/admin-datasets.png` });
  await algo.click('#dataset-new');
  await algo.waitForSelector('#dataset-kind');
  seen.poolNoneOff = await algo.$eval('#site option[value=""]', (o) => o.disabled);
  seen.poolAt = [await algo.inputValue('#site'), await algo.inputValue('#line')];
  await algo.selectOption('#dataset-kind', 'test');
  await algo.selectOption('#site', '');
  seen.noLine = await algo.$eval('#line', (s) => s.disabled);
  const testName = `T-${site}`;
  await algo.fill('#dataset-name', testName);
  await algo.click('#dataset-go');
  await algo.waitForFunction((n) => document.querySelector('h1')?.textContent === `上传到 ${n}`, testName);
  await algo.screenshot({ path: `${out}/admin-after-new.png` });
  await algo.goto(`${url}#/dataset-new`);
  await algo.waitForSelector('#dataset-kind');
  await algo.selectOption('#dataset-kind', 'test');
  await algo.fill('#dataset-name', testName);
  await algo.click('#dataset-go');
  await algo.waitForSelector('main .error');
  seen.taken = await algo.textContent('main .error');

  // the sites and lines have their own page, reached from the tree
  await algo.goto(`${url}#/datasets`);
  await algo.waitForSelector('#places-manage');
  await algo.click('#places-manage');
  await algo.waitForSelector('#places-table');
  await algo.fill('#place-ref', 'a-b');
  await algo.click('#place-go');
  await algo.waitForSelector('main .error');
  seen.badCode = await algo.textContent('main .error');
  await algo.fill('#place-ref', `${site}/L1`);
  await algo.fill('#place-name', '一线');
  await algo.click('#place-go');
  await algo.waitForSelector(`button.rename[data-ref="${site}/L1"]`);
  await algo.click(`button.rename[data-ref="${site}/L1"]`);
  await algo.fill('.rename-text', '一号线');
  await algo.click('.rename-go');
  await algo.waitForFunction((s) => [...document.querySelectorAll('#places-table tr')].some((r) => r.textContent.includes(`${s} / L1`) && r.textContent.includes('一号线')), site);
  seen.tree = await algo.textContent('#side-places');
  await algo.screenshot({ path: `${out}/admin-places.png`, fullPage: true });

  // labels: listed for an algo account, retired by an admin with a reason
  await algo.goto(`${url}#/d/${labelled}`);
  await algo.waitForSelector('#labels');
  seen.algoRetire = await algo.locator('#labels button.retire').count();
  await admin.goto(`${url}#/d/${labelled}`);
  await admin.waitForSelector('#labels button.retire');
  await admin.click('#labels button.retire[data-label="v0.1.0"]');
  await admin.click('.retire-go');
  await admin.waitForFunction(() => /\S/.test(document.querySelector('tr.asking .bad')?.textContent || ''));
  seen.noReason = await admin.textContent('tr.asking .bad');
  await admin.fill('.retire-note', '起错版本了');
  await admin.screenshot({ path: `${out}/admin-retire.png`, fullPage: true });
  await admin.click('.retire-go');
  await admin.waitForFunction(() => !document.querySelector('#labels') && document.querySelector('main').textContent.includes('已撤销'));
  seen.retired = await admin.textContent('main');

  // candidate and deprecated, on the head of the model version page
  await algo.goto(`${url}#/mv/${model}/${number}`);
  await algo.waitForSelector('.page-head #tag-candidate');
  await algo.click('#tag-candidate');
  await algo.waitForFunction(() => document.querySelector('#tag-candidate')?.textContent === '取消「候选」');
  seen.candidate = await algo.textContent('.page-head h1');
  seen.metricsShown = !(await algo.textContent('main')).includes('[object');  // a list inside a list of parts is drawn too
  await algo.click('#tag-deprecated');
  await algo.fill('#tag-note', '漏检多');
  await algo.screenshot({ path: `${out}/admin-deprecate.png` });
  await algo.click('#tag-go');
  await algo.waitForFunction(() => document.querySelector('#tag-deprecated')?.textContent === '取消「弃用」');
  seen.both = await algo.textContent('.page-head h1');
  await algo.click('#tag-candidate');
  await algo.waitForFunction(() => document.querySelector('#tag-candidate')?.textContent === '标记为候选');
  seen.history = await algo.$$eval('#tag-history li', (li) => li.map((x) => x.textContent.split('，')[0] + (x.textContent.includes('：') ? x.textContent.slice(x.textContent.indexOf('：')) : '')));
  await algo.goto(`${url}#/m/${model}`);
  await algo.waitForFunction((m) => document.querySelector('main h1')?.textContent === m, model);
  seen.listTags = await algo.textContent('main table');

  // a comparison with more than 500 changes: all of them on request, and the list saved like fw dataset diff --list
  await algo.goto(`${url}#/diff/${big}?old=1&new=2`);
  await algo.waitForSelector('button.more-rows');
  seen.firstRows = await algo.$$eval('tr.change-added', (r) => r.length);
  seen.more = await algo.textContent('button.more-rows');
  await algo.click('button.more-rows');
  seen.allRows = await algo.$$eval('tr.change-added', (r) => r.length);
  const [download] = await Promise.all([algo.waitForEvent('download'), algo.click('#diff-download')]);
  seen.file = download.suggestedFilename();
  const text = fs.readFileSync(await download.path(), 'utf8');
  seen.lines = text.split('\n').filter(Boolean).map((l) => l.split('\t')[0]).reduce((n, k) => ({ ...n, [k]: (n[k] || 0) + 1 }), {});
  seen.firstLine = text.split('\n')[0];

  // a field account sees the places and the tags, and none of the buttons
  await field.goto(`${url}#/places`);
  await field.waitForSelector('#places-table');
  seen.field = { rename: await field.locator('button.rename').count(), register: await field.locator('#place-go').count(),
    manage: await field.locator('#places-manage').count() };
  await field.goto(`${url}#/mv/${model}/${number}`);
  await field.waitForSelector('.page-head h1');
  seen.field.tags = await field.locator('#tag-candidate, #tag-deprecated').count();
  await field.goto(`${url}#/datasets`);
  await field.waitForSelector('.page-head h1');
  seen.field.newDataset = await field.locator('#dataset-new').count();
  await field.goto(`${url}#/dataset-new`);
  await field.waitForSelector('main .error');
  seen.field.newPage = [await field.textContent('main .error'), await field.locator('#dataset-kind').count()];

  seen.errors = errors;
  console.log(JSON.stringify(seen));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
