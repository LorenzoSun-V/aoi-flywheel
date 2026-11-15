// Accounts and the record of who did what, in the browser (run by tests/server/test_web.py):
// node accounts.cjs <url> <admin token> <new account name> <a dataset with a version> <out dir>.
// As the administrator: makes the account, changes its role (asked on the page, not in a browser box; 取消 puts the
// role back, and so does another question opening), gives it a token, reads the record. Then signs in with that token (a field account) and looks at what
// it is shown. Prints one JSON line of what it saw.
const { chromium } = require('playwright');

(async () => {
  const [url, token, name, dataset, out] = process.argv.slice(2);
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  page.on('console', (m) => { if (m.type() === 'error' && !/40[034]/.test(m.text())) errors.push(m.text()); });
  const dialogs = [];
  page.on('dialog', (d) => { dialogs.push(d.message()); d.dismiss(); });
  await page.goto(url);
  await page.evaluate((t) => localStorage.setItem('fw-token', t), token);
  await page.goto(`${url}#/users`);
  await page.reload();
  const seen = {};
  await page.waitForSelector('h1:has-text("账号")');
  seen.nav = await page.$$eval('#nav a', (as) => as.map((a) => a.textContent));
  await page.fill('input[placeholder^="账号名"]', name);
  await page.selectOption('form select', 'algo');
  await page.click('button:has-text("新建账号")');
  const row = page.locator('tr', { hasText: name });
  await row.waitFor();
  seen.made = await page.textContent('#toast');
  // a change of role is asked first, under the row; 取消 puts the role back
  await row.locator('select').selectOption('field');
  await page.waitForSelector('tr.asking .ask-go');
  seen.asked = await page.textContent('tr.asking .ask-text');
  await page.click('tr.asking .ask-cancel');
  seen.cancelled = [await page.locator('tr.asking').count(), await row.locator('select').inputValue()];
  // another role picked while asked: that one is asked; another question opening answers 取消, the role goes back
  await row.locator('select').selectOption('field');
  await page.waitForSelector('tr.asking .ask-go');
  await row.locator('select').selectOption('admin');
  await page.waitForFunction(() => document.querySelector('tr.asking .ask-text')?.textContent.includes('改为管理员'));
  seen.repicked = [await page.locator('tr.asking').count(), await row.locator('select').inputValue()];
  await page.locator('main tr').filter({ hasNotText: name }).locator('button.toggle:has-text("停用")').first().click();
  await page.waitForFunction(() => document.querySelector('tr.asking .ask-text')?.textContent.includes('停用'));
  seen.replaced = [await page.locator('tr.asking').count(), await row.locator('select').inputValue()];
  await page.click('tr.asking .ask-cancel');
  await row.locator('select').selectOption('field');
  await page.click('tr.asking .ask-go');
  await page.waitForFunction(() => !document.querySelector('tr.asking') && document.getElementById('toast').textContent.includes('角色'));
  seen.role = await row.locator('select').inputValue();  // the page painted again from the server
  seen.changed = await page.textContent('#toast');
  await page.locator('tr', { hasText: name }).locator('button:has-text("凭据")').click();
  await page.fill('input[placeholder^="用途说明"]', '现场笔记本');
  await page.click('button:has-text("发放新凭据")');
  await page.waitForSelector('#new-token');
  seen.token = await page.textContent('#new-token');
  seen.tokens = await page.textContent('main');
  await page.screenshot({ path: `${out}/users.png`, fullPage: true });
  // a name that will not do is refused where it was typed
  await page.fill('input[placeholder^="账号名"]', 'bad name');
  await page.click('button:has-text("新建账号")');
  await page.waitForSelector('main .error');
  seen.refused = await page.textContent('main .error');

  await page.goto(`${url}#/events?q=${name}`);
  await page.waitForFunction(() => document.querySelector('main h1')?.textContent === '操作记录' && document.querySelector('table'));  // not the accounts page's table
  seen.events = await page.$$eval('table tr', (rows) => rows.slice(1).map((r) => [...r.children].map((c) => c.textContent)));
  seen.eventLinks = await page.$$eval('table tr td:nth-child(4)', (tds) => tds.map((td) => td.querySelector('a')?.getAttribute('href') || null));
  await page.screenshot({ path: `${out}/events.png`, fullPage: true });

  // the new field account: reads, is offered nothing it may not do
  await page.evaluate((t) => localStorage.setItem('fw-token', t), seen.token);
  await page.goto(`${url}#/d/${dataset}`);
  await page.reload();
  await page.waitForSelector('#versions');
  seen.fieldNav = await page.$$eval('#nav a', (as) => as.map((a) => a.textContent));
  seen.fieldWho = [await page.textContent('#who .who-name'), await page.textContent('#who .who-role')];
  seen.fieldButtons = await page.$$eval('main a.button', (as) => as.map((a) => a.textContent));
  await page.goto(`${url}#/users`);
  await page.waitForSelector('main .error');
  seen.fieldUsers = await page.textContent('main .error');
  await page.click('#who a');
  await page.waitForSelector('h1:has-text("我的凭据")');
  seen.mine = await page.textContent('main table');
  seen.dialogs = dialogs;
  seen.errors = errors;
  console.log(JSON.stringify(seen));
  await browser.close();
})().catch(async (e) => { console.error(e); process.exit(1); });
