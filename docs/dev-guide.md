# 开发指南：常见任务怎么做

这份指南给第一次改这份代码的人（或 Claude）：每种常见任务改哪几个文件、照着哪段现成的代码写、怎么测、容易错在哪里。代码怎么分层和绝不能破的规则在 [维护和开发说明](development.md)，为什么这样设计在 [架构文档](architecture.md)，这里不重复。

每一节都拿仓库里已经有的一个功能当样板。最完整的一个是**现场和产线的登记**（`fw site …`）：它有迁移、服务端模块、权限、操作记录、接口、客户端方法、命令、网页和四种测试，新功能大多数时候是它的缩小版。要找某一步怎么写，先看它在这个功能里是怎么写的：

| 一步 | 现场和产线登记里对应的地方 |
|---|---|
| 表 | `server/fwserver/migrations/0013_sites.sql` |
| 服务端逻辑 | `server/fwserver/sites.py`（最底层，只管自己的表）、`datasets.py` 里的 `place`、`add_place`、`rename_place`、`list_places`、`where_place` |
| 权限 | `auth.CAPS` 里的 `sites.read`、`sites.write` |
| 操作记录 | `events.ACTIONS` 里的 `site.add`、`line.add`、`site.rename` |
| 接口 | `app.py` 里的 `/api/v1/sites` 三个路由 |
| 客户端 | `client/fw/api.py` 的 `sites`、`add_site`、`rename_site`；`cli.py` 的 `cmd_site_*` 和 `site` 子命令 |
| 网页 | `server/fwserver/web/app.js` 的 `browse`、`scopeOf`、`scopeQuery`、`placePicker` |
| 测试 | `tests/server/test_sites.py`、`test_migrations.py` 的 `test_what_existed_becomes_the_register_of_sites`、`tests/web/browse.cjs`、`tests/web/helpers.test.mjs` |

## 0. 开始之前

1. 装好环境，把全部测试跑通一遍（命令在仓库根目录的 `CLAUDE.md`）。本机没有 PostgreSQL 16 就装一个，建一个能建库的账号，把地址写进 `FW_TEST_ADMIN_DSN`。
2. 读 `docs/commands.md` 里和任务相关的那一节，知道现在的行为是什么。
3. 判断任务属于下面哪一类。**简单的可以直接做，其余的先写出打算怎么改，等人确认。**

| 可以直接做 | 先问 |
|---|---|
| 给一个列表加筛选条件或加一列 | 加表、改表 |
| 给一条命令加选项，改输出或报错的措辞 | 新的一类权限，或改已有权限给哪些角色 |
| 网页上加一列、加一个链接、改一处文字或样式 | 改冻结的数据的任何东西（版本、模型版本、评估报告、发布） |
| 给已有接口的回答加字段 | 改清单、快照、`fw-objects` 的格式，改已有字段的含义，改路径规则 |
| 补测试、补文档、修一个有复现步骤的小问题 | 动 `store.py` 的落盘、`jobs.py` 的租约、`deploy/` 的脚本 |

## 1. 加一条 `fw` 命令或一个选项

样板：`fw site add`。

1. `client/fw/api.py`：加一个方法，只发请求。查询参数里空的不发（看 `datasets()` 怎么写）。
2. 逻辑不止「调接口再打印」时（读本机文件、多步、训练脚本也要用），写在 `client/fw/ops.py`，并让它抛 `ops.FwError`，话写成用户能照着做的。
3. `client/fw/cli.py`：写 `cmd_<组>_<动作>(args, api)`，只解析参数和打印；在 `main` 里用 `add_parser` 挂上，`help` 写中文。表格用文件里现成的打印函数，和别的命令对齐。
4. 新选项的默认值必须保持原来的行为。
5. 测试：只动客户端的，在 `tests/client/` 用 `tests/fakeapi.py` 里的假服务测，假服务里没有这个接口就加上；经过真实服务的，在 `tests/server/` 里调 `fw_cli.main([...])`（`test_sites.py` 里有例子）。
6. `docs/commands.md` 加用途、例子、常见问题；`CHANGELOG.md` 记一段。

容易错的地方：

- `client/fw` 要在 **Python 3.8** 上跑：不能用 `match`、`X | Y` 类型写法、`list[str]`（用 `typing.List`）、`str.removeprefix`。CI 里有 Windows 和 Ubuntu 的 3.8。
- 客户端不能 import `fwserver`。两边都要用的东西（引用的写法、校验规则）放 `client/fw/contract.py`，它不能依赖 `fw` 里别的模块。
- 路径用 `pathlib`，不要假设分隔符是 `/`，也不要依赖文件修改时间的精度。

## 2. 加一个接口

样板：`POST /api/v1/sites`。

1. 逻辑写在所属的服务端模块里，函数第一个参数是数据库连接 `conn`，出错抛 `DatasetError(状态码, 消息)`（已有一个统一的处理把它变成 `{"detail": …}`）。常用的状态码：400 写法不对，403 没权限，404 没有，409 和已有的冲突。
2. 改了东西就在**同一个事务里**写操作记录：`events.record(conn, actor_id, '<对象>.<动作>', '<对什么>', 细节=…)`。新的动作先加进 `events.ACTIONS`，带上中文名字。
3. `app.py` 加路由。它只做三件事：用 `Depends(need('<能力>'))` 定谁能调，`with conn() as c:` 开事务（正常离开时提交，抛异常时回滚），调模块里的函数。请求体用 pydantic 的 `…Body` 类。
4. 能力在 `auth.CAPS`：已有的能表达就用已有的；确实是新的一类操作才加一行，并且先问（见第 0 节）。加了要同时改 `tests/server/test_accounts.py` 的 `test_the_table_of_capabilities`，它逐个角色列出全部能力。没写 `need(...)` 的路由会被 `test_every_endpoint_asks_for_a_capability` 拦下。
5. `client/fw/contract.py` 的 `API_LEVEL` 加一。`fw` 的新命令离不开这个接口时，把 `MIN_SERVER_LEVEL` 提到同一个数，这样连到旧服务器时用户看到的是「请升级服务器」而不是 404。
6. 测试写在 `tests/server/`，至少三种：正常的；没权限的（`make_api('field')` 换一个角色，`status_of(lambda: …) == 403`）；出错的（写法不对、重名、对象不存在）。再用 `api.events(...)` 或已有测试里的写法核对操作记录写上了。
7. 文档：`docs/commands.md`（有命令时）、`CHANGELOG.md`。

容易错的地方：

- `/api/v1` **只加不改**：可以加接口、给回答加字段、加可选的查询参数；不能删字段、改字段的含义或类型。
- 列表的筛选在服务器上做（SQL 的 `WHERE`），不要让网页或命令行取回全部再筛。`datasets.where_place` 是现成的例子，注意它用参数绑定，不拼字符串。
- 一个请求里的多步改动要在一个事务里，不要在路由里开两次 `conn()`。
- 新模块要在 `.importlinter` 里排进某一层。要 import 的模块在你上面或和你同一层时，说明函数放错了地方：把它挪到下层，或者让上层把需要的东西传进来。**不要改分层规则来通过检查。**

## 3. 加表或改表

样板：`0013_sites.sql`。这一类先问，确认了再做。

1. 加 `server/fwserver/migrations/00NN_<名字>.sql`，编号是现有最大的加一。**已经合并的迁移文件一个字也不能改**（服务器记着每个文件的哈希，改了拒绝启动）。
2. 迁移要能在有数据的库上跑：新列给默认值或允许空；要从旧数据算出新数据的（0013 从已有的数据集、模型、发布里登记现场），在迁移里用 SQL 做完。
3. 不许变的东西用触发器守住，不要只靠 Python 里的检查（0013 的 `guard_place`：代号不能改，行不能删）。
4. 每张表只有一个模块写。别的模块要改它，调那个模块的函数。
5. 测试：
   - 新表的行为，在所属模块的测试文件里。
   - 守着不许变的触发器：用 `sql` 这个 fixture 直接 `UPDATE` / `DELETE`，断言抛 `psycopg.Error`（看 `test_frozen.py`）。
   - 迁移对旧数据做了转换的：在 `test_migrations.py` 里建一个停在上一版的库，放进旧数据，再迁移，核对结果（看 `test_what_existed_becomes_the_register_of_sites`）。
6. 在 `docs/development.md` 的规则表里加一行：规则、守它的地方、测试。`docs/architecture.md` 里说明这张表为什么这样设计。

## 4. 改网页

网页是三个文件：`server/fwserver/web/index.html`、`app.js`、`style.css`，没有框架，没有构建，改完刷新就能看。

1. 页面元素只用 `el(标签, 属性, ...子节点)` 拼。**不许用 `innerHTML`、`insertAdjacentHTML`**（有测试拦着）：数据集名、备注这些都是用户写的字，只能当文本放进去。
2. 请求走文件上面的 `api` 对象，加一个方法就行；链接用 `hrefOf(parts, query)` 拼，它管转义。
3. 加一页：写 `async function xxxPage(…)`，**第一行是 `const show = live();`**，取完数据后用 `show(...)` 画（这样用户已经点去别的页时，慢回来的这一页不会盖上去）；在 `render()` 里按地址分给它；要出现在顶上的导航里就在 `NAV` 加一行，写上看得到它要有的能力。
4. 按钮、表单、链接是否出现，用 `can('<能力>')` 判断，和服务器的权限表一致。这只是不让人看到点不了的东西，真正拦的是服务器。
5. 页面的状态（选中的现场产线、搜索的字、页签）放在地址的 `?…` 里，这样能收藏、能后退，刷新不丢。`scopeOf` / `scopeQuery` 是例子。
6. 样式写在 `style.css`，用文件上面 `:root` 里定义好的颜色变量。只支持桌面浏览器，不用管手机。风格是简洁、留白多、少用颜色，颜色只用来表达状态。
7. 测试：
   - 纯函数（拼地址、算文字、解析）加 `export`，在 `tests/web/helpers.test.mjs` 里测，不要浏览器。
   - 页面上的操作，写一个 `tests/web/<名字>.cjs`（Playwright），在 `tests/server/test_web.py` 里加一个测试：用 `api` 准备数据，跑这个脚本，脚本把看到的东西打印成一行 JSON，Python 里断言。照 `browse.cjs` 和 `test_the_lists_are_browsed_by_site_and_line` 写。
   - 加环境变量 `FW_WEB_SHOTS=<目录>` 跑，脚本里的截图会留在那个目录，**自己打开看一眼**页面是不是对的，再提交。
8. `docs/commands.md` 里有一张网页每一页的表，改了页面要改它。

浏览器测试里容易错的地方（都真的出过问题）：

- **换页之后等的东西必须是新页才有的。** 页面是异步重画的，`waitForSelector('table')` 等到的可能是上一页的表。等新页特有的标题、面包屑，或者等地址和高亮同时到位：`waitForFunction(() => location.hash.includes('line=TY') && document.querySelector('#places li.line a.on'))`。
- **选择器要认准自己造的数据。** 所有测试共用一个服务，页面上有别的测试留下的数据集和现场。`a:has-text("TX")` 会点到别的现场的 TX；用带自己随机名字的 `a[href*="site=${site}&line=TX"]`，或者 `a:text-is("${name}")`。
- 断言里不要写「一共有几行」，只断言自己造的那几个在或不在。
- 单独跑通过不算，要把整个 `tests/server` 一起跑通过。

## 5. 写服务端测试

- 测试连的是真实的服务、真实的 PostgreSQL 和真实的文件库，整个测试过程共用一套。所以**每个测试的名字和文件内容都要独一无二**：名字带 `uuid.uuid4().hex[:8]`，图片用随机种子生成。两个测试导入了内容相同的文件，一个测试弄坏它，另一个就会莫名其妙地失败。
- 常用的 fixture（`tests/server/conftest.py`）：`api`（algo 角色的客户端）、`make_api('admin' | 'algo' | 'field' | 'deploy' | 'worker')`、`dataset`（一个新的空数据池，在 SES/TX 上）、`sql`（直接连数据库）、`service`（服务的地址）、`tokens`。
- 现场和产线要先登记才能用。`SES/TX` 已经登记好；测试自己编的现场，先调 `helpers.register_place(site, line)`。
- 造数据用别的测试文件里现成的函数：`test_trainsets.py` 的 `pool`、`make`、`sample_files`、`status_of`，`test_models.py` 的 `model`、`snapshot`、`register`。
- 故障用 `monkeypatch` 注入（换掉 `os.fsync`、`syncfs` 等），不要真的去删文件库里的文件：文件库是共用的，弄坏了后面所有测试的写入都会被拦住。
- 修问题时先写测试，确认它在没改的代码上失败，再改代码。

## 6. 提交之前

```
lint-imports
node --check server/fwserver/web/app.js && node --test tests/web/helpers.test.mjs     # 改了网页
FW_TEST_ADMIN_DSN=… pytest -q tests
```

逐条核对：

- [ ] 全部测试通过，没有跳过或删掉的；浏览器测试真的跑了（输出里没有因为缺 Playwright 而跳过）。
- [ ] 新功能有正常、没权限、出错三种测试。
- [ ] `CHANGELOG.md` 最上面「未发布」一节加了一段，从用的人的角度写。
- [ ] `docs/commands.md` 和实际行为一致（命令、选项、报错、网页）。
- [ ] 接口变了：`API_LEVEL` 加了一。
- [ ] 加了规则：`docs/development.md` 的规则表里有一行，写着守它的测试。
- [ ] 没有改已有的迁移文件、`.importlinter` 的规则、已有接口字段的含义。
- [ ] 提交里没有真实的现场数据，没有凭据。
- [ ] 给人看的字是中文，用词和别处一致（版本号、现场 / 产线、数据池、训练集快照、标注批次、凭据）。

PR 的说明写四段：改了什么（从用的人的角度）、为了守住哪些规则做了什么选择、怎么测的（跑了哪些、结果是多少个通过）、没做什么。PR 开到 `main`，**合并要等人明确说可以**。

## 7. 拿不准的时候

- 两种做法都说得通，而且做出来的数据或接口不一样：停下来，把两种做法和各自的后果写出来问人。
- 需求和「绝不能破的规则」冲突：不做，说明冲突在哪。
- 测试偶尔失败：不要重跑到通过为止。几乎都是上面说的两类原因（等错了东西、认错了数据），找到再改。
- 任务比看上去大（要动三个以上的模块，或者要加表）：先写出打算分几步、每步改什么，再动手。
