# 验收记录

## 2026-09-12 本轮：淘宝、京东、Apple 浏览器扩展

基线 `9962ebddc75dbbdef3227668ae953b79ca489bf6`，开工 `git status --short` 为空。本轮沿用现有仓库，不重建架构；扩展绑定日常浏览器当前标签页，Web → Runtime → Engine → 现有 Adapter 继续负责购买。只提供淘宝、京东、Apple 三个平台入口，旧天猫数据与保护判断保留。

### 本轮修改

| 文件 | 修改原因与行为 |
|---|---|
| `extension/manifest.json`, `worker.mjs`, `bridge.mjs`, `popup.html`, `popup.mjs` | Manifest V3，本机配对，只暴露主动绑定的一张购物标签页。通过浏览器公开 debugger API 复用现有 Playwright 页面流程，不复制 profile/Cookie。拒绝新建/关闭标签、关闭浏览器、Cookie 导出和 HTTP 重放命令；断线不自动重连或重放。后台监听器同步注册，修复真实临时 Chromium 中顶层 await 导致后台无法启动的问题；原生调试会话共享请求拦截，所有检查同意后才放行一次 |
| `src/browser/extension.py`, `src/web/app.py` | 本机 WebSocket 中继，配对码仅本机传递；严格 Host/Origin/loopback/令牌检查，只容纳一个扩展和一个控制器。复用原 ProfileLock；关闭只回收连接，不关闭用户页面。已发出的动作结果仍须由业务核验，不把断线解释成未执行 |
| `src/core/config.py`, `config/config.example.yaml`, `src/runtime.py`, `src/main.py`, `src/diagnostics.py` | 增加 extension 模式，限制一次运行当前绑定平台；检查登录不导航离开当前页。结束本机任务断开扩展但保留 Adapter 加购/提交尝试和 SQLite guard。独立 CLI 购买进程不能假装复用 Web 配对；提示从普通 Web 入口继续 |
| `src/core/engine.py`, `src/platforms/marketplace.py` | 修复非 Apple 加购暂停后直接当作成功的旧逻辑。统一 NOT_ATTEMPTED / ATTEMPTED_UNKNOWN / CART_VERIFIED；点击前暂停恢复可执行第一次加购，点击后未知只读核验；不核验通过不通知加购成功、不进入结算 |
| `src/web/index.html`, `README.md`, `FILES.md`, `extension/README.md` | 三平台控制台、明确连接反馈、安装与实际适配边界。没有把“配对成功”显示为“已登录” |
| `pyproject.toml`, `requirements.lock.txt` | 添加本机 WebSocket 中继所需 websockets（实际17.1），Playwright最低1.62以支持 no_defaults，连接时不覆盖日常浏览器的下载、焦点或媒体默认设置。没有新增商城私有鉴权模块 |
| `tests/test_config.py`, `test_session_control.py` | 示例默认方式更新为扩展；旧专用 profile 生命周期夹具明确指定 chrome，保留原断言。扩展连接与留页由真实临时 Chromium 测试覆盖，不能把两种模式混用 |
| `tests/test_extension_bridge.py`, `test_extension_runtime.py`, `test_marketplace_cart_resume.py`, `extension/bridge.test.mjs` | 分别检查本机配对/清理、正常扩展入口、三态加购恢复、原生调试消息协调与隐私过滤；不使用真实账户或原数据库 |

### 真实浏览器与商城边界

- 用户提供了淘宝购物车链接，并说明商品详情不支持电脑浏览、商品已在购物车。本轮调整方向为未来从已有购物车条目开始，不再要求重复加购；**这个购物车入口尚未实现**，不能写成购物车流程通过。
- Chrome 页签清单实际找到用户原有“淘宝网 - 我的购物车”；随后读取该页被工具明确拒绝：`Browser use is not permitted on https://cart.taobao.com/cart.htm`。这是站点安全策略拒绝，工具注明没有发起用户权限提示或自动审批；没有改用扩展、原始 CDP、其他浏览器、接口或间接执行绕过。仅页签元数据已读取，购物车内容、勾选、金额、结算均 **BLOCKED / NOT RUN**。
- 本轮真实 Apple 商品/新品启用 Continue/购物袋/结算 **NOT RUN**。现有启用 Continue 分支仍明确暂停，未根据假想页面编写选择器。
- 本轮真实京东商品/购物车/结算 **NOT RUN**。已有购物车字段尚未补齐；新版确认订单与立即支付仍人工处理，不绕开付款分支。
- 没有安装到用户日常 Chrome，没有运行原账户购买任务，没有真实加购、提交、付款或取消订单。日常浏览器登录、程序独立 profile 历史与本轮临时浏览器测试不混用。
- 原 `8766` Web 服务（本轮只读确认 PID17288）未重启，原 Edge 页面保留；原启动版本见前轮记录。源码修改不表示该进程已加载新后端。

### 本地验证

扩展后台已在临时 Chromium 空白页实际加载，修复了启动失败；Node 的模拟 Chrome/CDP 检查只证明路由规则。`tests/test_extension_runtime.py` 使用真正扩展、普通 Web 控制入口、Runtime/Engine/Apple Adapter 和临时数据库，所有商城 HTTP 请求都以本地 HTML 响应，其他 HTTP 请求中止。测试副本仅为模拟工具栏 activeTab 授权而额外允许合成 Apple 域；生产 manifest 未扩大权限。测试中的 iPhone17/微信是现有本地夹具，不是本次真实购买计划。

| 环节 | 实际方法/入口与本地页面结果 | 人工与真实网站边界 |
|---|---|---|
| 连接、首次登录检查 | `/extension/pair` → 普通扩展弹窗 → `/check-login` → `Runtime.check_login` → `AppleCNAdapter.login_status`，先 REQUIRED | 登录控件为本地 HTML；不证明真实登录 |
| 开始前确认、暂停恢复 | 未批准 `/start` 返回409；`/approve-plan` → `/start` → `Runtime.start` → `Engine.run`，登录暂停；模拟登录后 `/check-login` 返回 AUTHENTICATED，再 `/resume` | 通过普通 Web 控制 API 确认本地计划，没有直接注入 Adapter 确认状态；真实用户凭据未读取 |
| 商品、规格、加购 | 原 Apple Adapter 的 `open_product/check_stock/select_sku/add_to_cart/verify_cart`，加购事件一次，购物袋核验为 CART_VERIFIED | iPhone17本地夹具；新品 Continue 分支未覆盖、未实现 |
| 结算核验 | `checkout/verify_order` 后状态 READY_TO_SUBMIT；记录动作严格为 add、view_bag、checkout 各一次 | 本地地址/金额/微信选项；真实地址、24期免息、真实结算 NOT RUN |
| 结束 | `/finish-task` → `Runtime.finish_task`，原页面和另一张无关标签页仍打开；临时 CLAIMED guard保持 | 不提交、不付款；原成功订单保护另行只读核对 |

首次全量运行真实结果为 **6 failed、285 passed、3 warnings，96.45秒**（`outputs/extension-full.txt/.xml`）。六项均来自旧专用 profile 夹具隐含使用示例浏览器，示例改为 extension 后走入扩展分支；修复为明确指定 chrome，未删除或放宽断言。首次修改直接赋值被冻结配置模型拒绝（该定向运行9个setup errors）；改用项目已有 model_copy 后定向9项通过。独立复查另发现断开重连竞态和重定向响应头过滤遗漏，修复后重新验收，最终结果如下。

完整 pytest **PASS：291 passed，0 failed、0 skipped，3 warnings，99.56秒**，退出0；证据 `outputs/extension-final.txt/.xml`。这一次运行覆盖本轮最终 Python 源码和原生扩展集成，不是将定向通过数相加。三条警告为现有 Starlette/httpx/AnyIO 弃用与 Pydantic Decimal 夹具序列化提示。

完整运行后仅对扩展 JS 的旧异步回调做最后窄修复：旧页面读取、旧 WebSocket 或旧调试事件的异常不能断开新绑定。该修改后的 Node **11/11 PASS、0 skipped**；原生扩展集成复查 **1 passed、4.39秒**，证据 `outputs/extension-recheck.txt/.xml`，仍从普通弹窗/Web/Runtime入口到 READY_TO_SUBMIT，加购一次、无提交。JS语法复查通过。

Ruff `src tests`、compileall `src tests`、扩展与控制台 JS 语法检查通过。环境为 Python3.14.5、Node24.19.0、Playwright1.62.0、websockets17.1、pytest9.1.1、Ruff0.16.6。

原真实数据库开工与全部测试完成后只读核验一致：1条 SUCCESS guard，tuple JSON SHA256均为 `3c00c641d5215b0e570ab8aad8f424ae3c6d135d190aeb4f8b0ed710dc55af08`。没有删除、释放或替换数据库。

### 本轮实际命令

```powershell
git status --short
git rev-parse HEAD
.venv\Scripts\python.exe -m pip install 'websockets>=15,<18'
.venv\Scripts\python.exe -m pytest tests/test_marketplace_cart_resume.py tests/test_marketplace_flow.py tests/test_marketplace_engine.py tests/test_cart_runtime_engine.py tests/test_engine.py -q
.venv\Scripts\python.exe -m pytest tests/test_extension_bridge.py -q
.venv\Scripts\python.exe -m pytest tests/test_config.py tests/test_cli_web.py -q
.venv\Scripts\python.exe -m pytest tests/test_extension_runtime.py -x -q
.venv\Scripts\python.exe -m pytest tests/test_session_control.py -q --tb=short
.venv\Scripts\python.exe -m pytest -q --tb=short --junitxml=outputs/extension-full.xml
.venv\Scripts\python.exe -m pytest -q --tb=short --junitxml=outputs/extension-final.xml
.venv\Scripts\python.exe -m pytest -q tests/test_extension_runtime.py --tb=short --junitxml=outputs/extension-recheck.xml
node --test extension/bridge.test.mjs
.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\python.exe -m compileall -q src tests
node --check outputs/extension-web.js
node --check extension/worker.mjs
node --check extension/popup.mjs
node --check extension/bridge.mjs
git diff --check
git ls-remote origin refs/heads/main
```

测试进程仅移除三个可选凭据环境变量，没有修改用户的持久环境或登录文件。独立复查已修正断开期间重连、旧异步回调影响新连接，以及 `redirectResponse`/原始头文本中的敏感信息遗漏。控制台仅通过本机明示配对连接；调试权限本身仍有较高能力，只用于信任的本机程序。

本轮还查阅 Chrome 官方 content scripts / messaging / debugger 文档和 websockets 发布页用于公开扩展 API 与依赖核对，不是实站交易执行结果。以下全部为历史轮次，不能作为本轮验收。

---

# Apple 实页适配验收

## 2026-09-12 追加：商城登录尝试与不限规格

本轮基线 `606ffbda14fbd2b1d969f63e2b5767143658252f`，`git status --short` 为空。用户要求在 Apple 商店更新期间尝试现有京东/天猫/淘宝入口，随后明确 iPhone 18 系列容量、颜色不限，再转为搜索可行抢购策略。没有继续增加平台或凭据框架。

### 当前实测，不等同于下方历史 Chrome 记录

| 环节 | 本轮实际入口、结果与人工参与 |
|---|---|
| 程序 Edge 京东首次登录 | 正常 Web `/login` → `Runtime.open_login` → `BrowserManager`，使用 `data/profiles/msedge/jd`；返回200并打开京东登录。登录前 `/check-login` 返回 REQUIRED。用户在本机手动完成登录后提供截图，页面显示“当前页面异常”；再检查返回 UNKNOWN，没有将用户口述或 profile 存在当作认证成功 |
| 京东公开目标 | 搜索工具从 Apple 产品京东自营旗舰店导航找到 Pro Max `https://item.jd.com/100317829587.html`、Pro `https://item.jd.com/100414360094.html`；只证明公开入口关联。搜索工具打开详情页仅得空壳，规格/价格/库存 NOT VERIFIED |
| 程序 Edge 商品读取 | Web `/save-target` 为 `iphone18promax` 保存上述 Pro Max URL（200），卖家和配送依据留空，不猜测；随后 `/check-product` → `Runtime.check_product` → `JDAdapter.open_product` 返回409，原因 `Could not save minimized browser diagnostics; browser needs attention`。后续 `/check-login` 仍 UNKNOWN。仅只读窗口清单观察到标题“PC频控页 -京东商城 - 个人 - Microsoft Edge”，据此停止网站请求；没有足够证据判定具体账号/IP/浏览器特征原因。诊断保存失败的底层浏览器异常尚未定位，不能将其当作已修复 |
| 天猫、淘宝 | 本轮真实程序登录、商品与交易 NOT RUN。Edge 未连接浏览器控制工具，创建其工具标签返回 `Browser is not available: edge`，此错误不是本轮淘宝域拒绝。历史淘宝工具访问拒绝保持历史标注，没有换程序/API绕过它 |
| 规格选择、加购、购物车、结算、提交、付款 | 程序真实页面全部 NOT RUN。没有改真实购物车、点击付款/定金、创建或取消订单；没有以普通 Chrome 或历史待付款订单代替程序验收 |

本轮网站操作使用仍在运行的基线 Web 服务 `python -u -m src.main web --port 8766`。完成以下源码更新后没有重启服务，保留当前浏览器页面。**本机配置已保存容量/颜色空列表、1件、总额15000；新配置在服务下次启动时生效。** 模型仍为已有且查到官方入口的 iPhone 18 Pro Max / Pro，没有凭空新增其他型号入口。开发仍 `dry_run=true`、`auto_submit=false`。

### 本轮源码修改及本地验证

- `src/main.py`、`tests/test_cli_safety.py`：CLI 没有商品 URL 也调用真实 Runtime 登录检查，去掉直接 UNKNOWN 的短路。新增普通 CLI 分发回归先失败1项（Runtime未调用），修复后与命令解析检查14项通过；该回归使用替身，不是实站登录证据。
- `src/core/config.py`、`src/order/priority.py`、`checkout.py`、`plan.py`：容量/颜色空列表表示不限，型号仍必须非空；具体规格必须读取到，实际商品/数量/预算/结算身份仍严格核对。
- `src/platforms/apple_cn/adapter.py`：不限时从当前可见规格选项寻找首个合格候选。已指定优先列表的原路径保持；未知加购/提交不能切换或重放。新品 Continue 没有获得新页面证据，仍未补齐。
- `src/platforms/marketplace.py`：不限时接受页面当前明确选中规格，不把空白规格当合格，不扩展未验证交易选择器。
- `src/web/index.html`、`README.md`：开始前计划明确显示“容量不限”“颜色不限”，解释空列表含义。
- `tests/test_config.py`、`tests/test_unrestricted_variants.py`：先复现空列表被拒绝，再验证不限、严格型号/预算/结算、可见候选及首个匹配返回。相关本地64项通过，最终限缩到不限分支后Apple2项复查通过。页面为临时profile合成HTML，不是官网。

完整 pytest **PASS：279 passed，0 failed、0 skipped，3 warnings，138.06秒**，退出0；证据 `outputs/market-sep12-full.txt/.xml`。Ruff `src tests`、compileall `src tests`、Web脚本语法与 `git diff --check` 均通过。之后仅更新文档。定向通过数不相加作为全量。完整运行命令及证据：

```powershell
git status --short
git rev-parse HEAD
.\.venv\Scripts\python.exe -m pytest -q --tb=short --junitxml=outputs/market-sep12-full.xml
.\.venv\Scripts\python.exe -m ruff check src tests
.\.venv\Scripts\python.exe -m compileall -q src tests
node --check outputs/market-sep12-web.js
git diff --check
```

测试进程仅移除三个可选凭据环境变量；没有修改用户环境或真实登录文件。实际 Web 请求均为本机 `http://127.0.0.1:8766`，控制头 `X-Apple-Bot-Control: local`，平台 `jd`：`POST /login`、`POST /check-login`、`POST /save-target`、`POST /check-product`。未调用 `/start`、提交或付款方法。原数据库只读全字段核对：仍1条SUCCESS guard，tuple JSON SHA256 `3c00c641d5215b0e570ab8aad8f424ae3c6d135d190aeb4f8b0ed710dc55af08`，没有删除或释放保护。

### 官方资料研究，不是购买验收

本轮实际搜索/打开 [Apple 新品公告](https://www.apple.com.cn/newsroom/2026/09/apple-debuts-iphone-18-pro-and-iphone-18-pro-max/)、[京东违规订单规则](https://help.jd.com/user/notice/detail-657bf0c2e4b092fb71dedf3e.html)、[京东抢购成功说明](https://help.jd.com/user/issue/38-33.html)、[淘宝/天猫预售协议](https://terms.alicdn.com/legal-agreement/terms/suit_bu1_tmall/suit_bu1_tmall202203151048_70887.html)。Apple 公告明确9月12日20:00预购、18日发售。未获得京东/天猫本次活动完整细则，不能把Apple时间直接套用其他渠道。京东规则限制未经认可的机器人购货；淘宝/天猫定金预售另有定金/尾款流程，不能按普通加购处理。建议当前使用能正常登录的官方客户端手动交易，程序做准备、核验与提醒；这是基于目前失败边界的工程建议，不是官方保证客户端抢购概率更高。

---

## 2026-09-12 追加：按用户选择切换 Edge

本轮基线 `6f3702a0bb570ccdb19eedd9da494785fb422788`，开工工作区干净。本机原配置仅增加 `app.browser: msedge`；`dry_run=true`、`auto_submit=false` 和原 SUCCESS 订单锁保留。

- `src/core/config.py`、`config/config.example.yaml`：浏览器仅可选 `chrome` / `msedge`；示例默认 Chrome，本机配置选择 Edge。无新增凭据或购买接口。
- `src/browser/manager.py`：沿用 persistent context，Edge 实际资料目录为 `data/profiles/msedge/apple`，原 Chrome 资料不搬移、不复制、不删除。不同浏览器仍争用原 `data/profiles/apple` 中的 ProfileLock，不能并发操作同一平台；订单记录和 guard 仍在同一数据库。状态导入标记跟随各自资料目录，不能把 Chrome 的旧导入当作 Edge 已登录。
- `src/runtime.py`、`src/diagnostics.py`、`src/web/index.html`：普通 Runtime 传入所选渠道，doctor 检查所选浏览器，网页显示实际浏览器。批准计划、候选、提交保护均沿用，无更换 Adapter 的额外入口。
- `tests/test_browser.py`、`tests/test_config.py`：参数化既有测试，验证 Edge 本地 Cookie/localStorage 关闭重开仍存在、Chrome 原目录保留、Chrome↔Edge 共享平台锁、缺失浏览器不回退，以及拒绝未知配置。

真实入口：使用普通 `python -u -m src.main web --port 8766` 启动新服务，再经 `/login` → `Runtime.open_login` → `BrowserManager.open_visible` → Apple 正常账户入口。实际进程为 `C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`，版本 `153.0.4234.32`，沙盒启用。用户先关闭原程序 Chrome；没有关闭或修改日常 Chrome。

**真实结果 BLOCKED**：本次 Apple 登录主文档返回 HTTP **503**，`/login` 返回409（平台暂不可用），随后 `/check-login` 为 UNKNOWN / NOT_ON_LOGIN。请求错误分类仅包含已知主机、主文档、状态503，不保存查询参数或页面私密正文。没有到登录成功、购物袋或结算；没有加购、提交订单、付款、取消订单或清锁。不能把此503推广成所有浏览器都无法登录，也不能据此认定之前转圈的根因。

定向检查 **8 passed，18.42秒**，证据 `outputs/edge-targeted.xml`；只访问 localhost 和临时资料目录，不包含真实账户。最终完整 pytest **PASS：275 passed，0 failed、0 skipped，3 warnings，140.02秒**，退出0，证据 `outputs/edge-full.txt/.xml`。Ruff、compileall、网页脚本语法及 `git diff --check` 均通过；之后仅更新文档。原 guard 全字段 SHA256 仍为 `3c00c641d5215b0e570ab8aad8f424ae3c6d135d190aeb4f8b0ed710dc55af08`。实际检查命令：

```powershell
git status --short
git rev-parse HEAD
.\.venv\Scripts\python.exe -u -m src.main web --port 8766
.\.venv\Scripts\python.exe -m pytest -q --tb=short --junitxml=outputs/edge-full.xml
.\.venv\Scripts\python.exe -m ruff check src tests
.\.venv\Scripts\python.exe -m compileall -q src tests
node --check outputs/edge-web.js
```

下方为本日先前修复及真实 Chrome 观察，不能当作本次 Edge 登录成功证据。

---

## 2026-09-12 本轮：一次开始确认、候选继续和正常结束任务

本机开工 HEAD：`222d6bbaf0fc6b58e14d19efdbe7f8bba56dcecc`。工作区已有六个未提交文件（engine、main、Apple adapter、runtime、test_audit_apple、test_session_control），先记录并接续，未重置或覆盖。基线证据为本机 `outputs/sep12-baseline.json`。完成后的提交号和远端核对见交付回复。

**真实购买流程尚未通过。** 本轮正常入口的本地合成流程与真实网站分别列出；登录转圈、禁用 Continue 和原订单锁都是实际未完成项。没有新建真实订单、付款、取消订单、清空购物袋、删除 profile/数据库或释放原 guard。

### 文件与原因

| 文件 | 变更与边界 |
|---|---|
| `src/order/plan.py`、`src/runtime.py`、`src/platforms/apple_cn/adapter.py` | 原计划中明确 CN 官网直售目标、允许规格、数量、预算、付款和“当前账户选中的已保存地址首次完整读取后绑定”依据。开始前批准一次，同一内容沿用。真正看到保存地址选中且完整读取后才保存摘要；字段遮挡/地址变化暂停。商品实时核对批准入口与精确 SKU，可见海外版本仍拒绝；`APPROVED_APPLE_CN_DIRECT` 表示批准依据，不谎称读取到国行型号证明。配置/条款变化撤销旧批准，保存失败不启用新批准 |
| `src/main.py`、`src/web/api.py`、`src/web/index.html` | CLI `confirm-start <digest>` 与网页开始弹窗合并确认。CLI 先检查已有保护再等待输入。新增 CLI `finish`、Web“结束本机任务”及 `/finish-task`，等待任务停止、留页、保留记录与加购/订单保护；不等于取消订单。原 API 不能关闭 dry-run 或开启 auto_submit |
| `src/browser/manager.py` | 显式 `chromium_sandbox=True`，取消 Playwright 默认关闭沙盒造成的启动差异。本地浏览器回归通过，但原登录窗口仍待关闭，尚不能确认该差异就是 Apple 登录转圈的原因；没有改变身份、代理或跳过平台验证 |
| `src/platforms/apple_cn/adapter.py`、`src/core/engine.py`、`src/core/state_machine.py` | 返回首个符合优先级和总预算的实际候选。首次加购前明确价格/库存失效可回退；身份/全局页面结构变化仍暂停。冷却至少跨下一次候选枚举，避免等待后总选回失效首选。任何已尝试或未知加购/提交不换候选、不重放 |
| `tests/test_runtime_purchase_plan.py`、既有 CLI/Web、Apple、Engine、会话测试 | 从普通 Runtime、CLI/Web 控制入口核对一次确认、首次登录暂停恢复、单次加购、地址变化、冷却回退和正常结束任务；均使用本地页面或故障注入，不能替代官网 |

### 本轮逐步结果

| 步骤 | 实际方法/动作 | 本地页面或模拟 | 真实网站/程序专用 profile |
|---|---|---|---|
| 开始前确认 | CLI `run_console` / Web 开始 → `Runtime.approve_plan` → `Runtime.start` | 普通入口只确认一次；未批准不开始，同一批准复用 | 未代替用户批准真实计划；正常 Web `/start` 返回 HTTP 409，原 SUCCESS guard 阻止启动 |
| 登录 | CLI `src.main login apple` → `BrowserManager.manual_login`；运行中 `login_status` / `check_login` / `resume` | REQUIRED/UNKNOWN 暂停，实际合成账户控件认证后继续原流程 | **BLOCKED**：已实际打开原专用 profile，用户反馈登录持续转圈；未把打开页面或 profile 存在记为成功，等待关闭卡住窗口以复查现有登录状态 |
| 商品与规格 | `check_stock/get_skus` → `select_sku` | 首个符合候选即继续；明确失效才安全回退 | **PASS（普通 Chrome 公开商品观察）**：约13:35选择 Pro Max / 512GB / 黑色，RMB12,999；不是程序独立 profile 验收 |
| 新品 Continue | 官网页面配置完成后读取可见控件 | 启用却未知的 Continue 分支仍停止 | **NOT RUN（后续）**：配置完整后仍“暂未发售”，Continue disabled；页面写当日20:00接受预购，没有启用或点击禁用控件 |
| 加购一次/袋核验 | `add_to_cart` → `_bag_check`；未知时 `verify_cart` | 加购1次，精确商品/数量/总额成立后 CART_VERIFIED；已点击未知只读袋 | **NOT RUN**：未修改真实购物袋；普通 Chrome 原袋标记1件保留 |
| 真实结算 | `goto_checkout` → `verify_order` → `_read_review` | 合成 checkout 经真实 Adapter 到 READY_TO_SUBMIT，submit与付款调用0 | **NOT RUN**：未到真实 checkout，不能声称登录后购买已跑通 |
| 地址/付款 | 已保存地址、首次完整摘要绑定；当前方案复核 | 无第二次批准；变更/遮挡暂停，错误金额拒绝 | **NOT RUN**：本轮未读真实地址和分期完整计划，未点击银行授权/微信付款 |
| 结束旧任务 | Web 按钮 → `/finish-task` → `Runtime.finish_task` | 工作任务取消并等待，原记录、页面和副作用状态保留 | **PASS（本机真实服务）**：已实际点击结束按钮；页面仍显示 SUCCESS 与禁止提交，未清订单锁 |

### 统一检查与本轮命令

首轮完整 pytest：**267 passed、1 failed、3 warnings，100.22秒**。失败为 `test_address_and_market_confirmation_bind_the_current_item`：新增必需字段筛选漏掉 street2，导致补充地址改变未撤销确认。已修复为保留原完整表单字段摘要，地址遮挡仍停止，已保存遮挡联系方式不误判为缺地址；原失败断言不改。针对性地址/加购/正常 Runtime 复查 **6 passed，13.18秒**。

最终完整 pytest：**PASS，271 passed，0 failed、0 skipped，3 warnings，125.14秒**，证据 `outputs/sep12-final.txt/.xml`。Ruff `src tests`、compileall `src tests`、提取当前 Web 脚本后的 `node --check`、`git diff --check` 均退出0。最后一次检查包含恢复 Chrome 沙盒的代码；之后只改文档。3条 warning 为 Starlette/httpx/anyio 弃用及既有测试替身的 Decimal 序列化提示，不表示官网通过。

首轮证据 `outputs/sep12-full.txt/.xml` 保留失败记录。针对性 `outputs/sep12-address-cart.xml` 为6项通过；`outputs/entry-controls.xml` 为37项通过；`outputs/confirm-once-local.xml` 为4项通过。候选竞争失效的注入时钟复现修前3失败/3通过，修后6通过（1.27秒，`outputs/candidate-fallback-local.xml`）。这些通过数不相加充当全量。

实际命令（工作目录仓库根，PowerShell 7，`.venv`；无真实购买命令）：

```powershell
git status --short
git rev-parse HEAD
.\.venv\Scripts\python.exe -u -m src.main login apple
.\.venv\Scripts\python.exe -u -m src.main web --port 8766
.\.venv\Scripts\python.exe -m pytest -q --tb=short --junitxml=outputs/sep12-full.xml
.\.venv\Scripts\python.exe -m pytest -q --tb=short --junitxml=outputs/sep12-final.xml
.\.venv\Scripts\python.exe -m pytest tests/test_audit_apple.py::test_address_and_market_confirmation_bind_the_current_item tests/test_apple_cart_resume.py::test_confirmation_before_add_resumes_first_click_then_verifies tests/test_runtime_purchase_plan.py -q --tb=short --junitxml=outputs/sep12-address-cart.xml
.\.venv\Scripts\python.exe -m ruff check src tests
.\.venv\Scripts\python.exe -m compileall -q src tests
node --check outputs/sep12-web.js
git diff --check
git ls-remote origin refs/heads/main
.\.venv\Scripts\python.exe -m src.main dry-run apple
```

完整 pytest 进程仅移除三个可选凭据环境变量（STATE_FILE/USERNAME/PASSWORD），不修改用户环境或文件。真实 UI 使用 Chrome 扩展：公开商品选择及本机 Web 结束按钮；本机 `/start` 请求为 `platforms=[apple], immediate=true, dry_run=true`，结果409，没有工作者、没有提交。最终 CLI `dry-run apple` 在任何确认等待前报告 SUCCESS 保护，退出2；此前一次未设 UTF-8 的捕获输出乱码，重新以 `PYTHONIOENCODING=utf-8` 核对，未改变运行保护。

原数据库前后只读核对：SUCCESS guard 一条，全部字段按 tuple JSON 序列化 SHA256 始终为 `3c00c641d5215b0e570ab8aad8f424ae3c6d135d190aeb4f8b0ed710dc55af08`，后验记录为 `outputs/sep12-guard-after.json`。订单记录状态 SUCCESS，付款状态 UNKNOWN；不根据历史日期推断取消或已付款。本轮需要先明确现有订单情况才可能安排后续交易演练，正常结束任务不会解除这项保护。

### 未完成

- 程序专用 Chrome 登录持续转圈的原因与恢复、实际认证及重开复用：尚未验证。
- Continue 启用后的真实流程：尚未访问到，不猜选择器。
- 程序普通入口→真实购物袋→真实结算→READY_TO_SUBMIT：NOT RUN，不能用合成链替代。
- 既有 SUCCESS 记录没有本轮已付款/已取消/无订单的可靠证据，保护保留。
- 未新增任何真实订单；其他平台不在本轮推进范围。

---

## 历史：2026-09-11 购买计划与登录恢复（基线 1a330c8）

开工实际 HEAD：`1a330c80880aade9200549741e1226679ae63caa`，`git status --short` 为空。未覆盖用户未提交工作。本轮沿用现有工程，不扩平台。最终提交号及推送核对见本次交付回复；本节的结果与下方历史审核分开。

**当前不能宣称“真实购买流程可用”。** 新增验收从正常 Runtime 启动，首次登录和批准均发生在启动之后，使用真实 Engine 与 AppleCNAdapter，但页面仍为离线合成内容。程序专用 profile 的真实官网演练受本机既有 SUCCESS 订单锁阻断，不替换数据库或清锁来继续。

### 变更与原因

| 文件 | 本轮功能与边界 |
|---|---|
| `src/order/plan.py`、`src/runtime.py` | 本地批准计划包含入口链接及其摘要、允许型号/规格、数量、单价/总额上限、币种、付款方式、银行和地址依据。地址/版本仅存摘要。计划配置相同可复用，变化撤销批准。原子保存失败不发布新批准；商品配置先准备新对象再发布，防止新链接搭配旧批准。Runtime.start 复用现有 Adapter，已有加购/提交标志不能重置 |
| `src/core/models.py`、`src/core/engine.py`、`src/platforms/apple_cn/adapter.py` | 显式区分 NOT_ATTEMPTED、ATTEMPTED_UNKNOWN、CART_VERIFIED；点击前暂停可执行第一次加购；已点击后只读现有袋。只有单行目标商品、数量、行金额、总额和无错误全部匹配才进入结算。未知状态不重复加购。每次准备和暂停恢复重查实际登录；异常时停止动作，保留待核对页面 |
| `src/browser/manager.py`、`session.py`、Apple `selectors.py` | 优先复用程序 persistent profile。可选本机 `APPLE_BUY_BOT_STORAGE_STATE_FILE`，仅接受受支持的 Cookie 和 Playwright cookies/origins/localStorage JSON，合并缺项，不清空或覆盖已有项。待导入 context 离线启动并阻止 Service Worker 接管，合并成功后才联网；普通 profile 保持原启动策略。一次导入标记不代表登录通过。登录页区分表单未就绪/就绪/错误；网络诊断只留主文档/iframe 的域类别、状态码及故障类别，不留 URL 查询、正文或账户值 |
| `src/main.py`、`src/web/api.py`、`src/web/index.html` | CLI 展示计划，支持 plan/approve-plan；Web 显式本机批准绑定展示版本。API 不能开启提交，不能用批准重启已结束的购买。首次地址/国行确认后保存本地摘要，实际变化才重新确认 |

### 从正常 Runtime 入口逐步验收

`tests/test_runtime_purchase_plan.py::test_runtime_first_login_plan_product_address_and_resume` 使用临时数据库、临时正式 Chrome profile、浏览器 offline 和全路由合成 HTML。被记录的方法均调用真正 Adapter 实现；只替换网页传输，不提前调用 Adapter 或注入批准。

| 步骤 | 实际入口/方法 | 本地合成页面结果与人工操作 | 本轮真实官网 |
|---|---|---|---|
| 启动 | `Runtime.start` → `Engine.run/_prepare` → `open_product` | 启动时无页面、无批准 | **BLOCKED**：CLI 正常调用 Runtime.start，由原 SUCCESS guard 在工作者/浏览器启动前拦截，退出 2 |
| 首次登录 | `login_status`、`Runtime.check_login`、`Runtime.resume` | REQUIRED/UNKNOWN 暂停；合成用户点击登录完成后实际账户控件为 AUTHENTICATED，恢复同一任务 | NOT RUN |
| 计划与商品 | `Runtime.approve_plan`、`check_stock/get_skus`、`select_sku`、`Runtime.confirm_checkout(market)` | 首次计划暂停与首次国行确认均经 Runtime 显式批准；测试商品 iPhone 17 256GB 黑色 1 件，CNY 6799 | NOT RUN |
| 加购与购物袋 | `add_to_cart`、`_bag_check`；未知分支 `verify_cart` | 主流程加购 1 次；正确商品/数量/金额后 CART_VERIFIED。另例先确认再首次点击，或已点击结果未知仅查袋，错误数量/金额仍暂停 | NOT RUN |
| 结算 | `goto_checkout` → `verify_order` | 实际合成购物袋进入合成 checkout；点击结算 1 次 | NOT RUN |
| 首次地址与金额/付款 | `Runtime.confirm_checkout(address)` → `verify_order/_read_review` | 首次地址暂停后由本地确认入口批准；仅摘要保存。合成流程使用微信人工付款方式；完整 24 期金额仍由原解析/页面用例验证，不冒充实页分期验收 | NOT RUN |
| 就绪 | Engine `_purchase` → READY_TO_SUBMIT | dry_run=true，submit_order 调用 0、付款点击 0；页面保留，临时 CLAIMED 锁仍在。stop 后也不能重建 Adapter 重放购物车 | NOT RUN |

### 统一测试与实机入口

运行环境：本仓库 `.venv`，Python 3.14.5、pytest 9.1.1，浏览器测试使用正式 Chrome 与独立临时目录。全量命令进程内清除三个凭据相关环境变量，不修改用户全局环境；测试只用合成凭据。以下是最终同一版代码的完整结果，之后仅更新文档：

| 检查 | 结果 | 本机证据 |
|---|---|---|
| 最终完整 pytest | **PASS：262 passed，0 failed、0 skipped，3 warnings，75.11 秒** | `outputs/runtime-purchase-verified.txt/.xml` |
| Ruff | **PASS**：src tests，退出 0 | `outputs/runtime-ruff.txt` |
| compileall | **PASS**：src tests，退出 0 | `outputs/runtime-compile.txt` |
| 网页脚本语法 | **PASS**：提取当前 HTML 的 script 后 node --check，退出 0 | `outputs/runtime-web-script.js`；非浏览器 UI 验收 |
| 本机真实配置 CLI | **BLOCKED**：正常 `dry-run apple`，退出 2，原 SUCCESS 锁阻止启动购买工作者 | `outputs/runtime-live-entry.txt`，未导航或改变购物袋 |
| 原 guard 全字段比对 | **PASS**：1 条 SUCCESS，摘要与开工基线一致 | `outputs/runtime-guard-after.json`、`runtime-purchase-baseline.json` |
| 本机 8766 服务 | **PASS（HTTP/API）**：空闲服务安全刷新，计划/批准按钮/登录诊断已加载，dry_run=true、auto_submit=false、计划尚未批准，guard=SUCCESS | `outputs/runtime-service.json`；本轮没有用浏览器点击网页控件 |

3 条警告为旧有的 Starlette/AnyIO 弃用提示及一个市场平台假对象的 Decimal 序列化提示，未改无关依赖。旧 8766 的 PID 18196 确认 running=false 且无子进程后才停止；新服务 PID 45196。旧 8765 及其页面未关闭。

真实入口使用本机配置：iPhone 18 Pro Max 优先、Pro 备选，512GB/256GB/1TB，黑色/银色/冰川蓝色/勃艮第酒红色，1 件，单价和总额上限均 CNY 15000，建设银行 24 期零利息零手续费。已在执行前说明预期动作；本轮没有批准该实际计划，也没有越过原锁。原 guard 的完整行摘要为 `3c00c641d5215b0e570ab8aad8f424ae3c6d135d190aeb4f8b0ed710dc55af08`。首个比对脚本误用字典行序列化，改用与基线一致的 `json.dumps(fetchall_tuple_rows, sort_keys=True)` 后匹配；两种表示不同不代表数据库变化，证据文件记录了这一差异。

此前未完成的全量及红测保留如下，不与最终通过数拼接：

- 初次 260 项全量在旧 ReviewAdapter 假对象缺少购物袋状态处无限等待，核对 PID 后仅终止该测试进程；`outputs/runtime-purchase-full.txt` 为 **INCOMPLETE**，没有全量 PASS。加入超时复现后 6 FAIL / 2 PASS；补齐假对象真实语义后 8 PASS / 0.77 秒，未放宽 Engine 保护。
- 第二次完整 261 项：**3 FAIL / 258 PASS，72.75 秒**。`outputs/runtime-purchase-final.txt/.xml` 保留原始结果。失败分别为全局 100ms 超时误伤真实加购点击、旧假页面缺少 url、暂停提示文字不兼容。将故障注入限定到加购后的袋响应、补齐假对象 URL、保留“运行中”提示；对应 8 项通过（4.61 秒）。
- 计划集成定向：4 PASS / 6.37 秒，`outputs/runtime-plan-final.xml`；包含首次正常 Runtime 流程、重建复用/配置变化失效、API 精确批准、保存失败原状态不变。
- 登录导入隔离定向：5 PASS / 10.34 秒，`outputs/local-login-state-isolation.txt/.xml`，纳入最终全量。检查生产启动参数及本机 TCP 连接：导入完成前被离线阻止、完成后才可连接。新增夹具首轮因 Chrome 错误页 navigator.onLine 判断不可靠而 1 FAIL / 4 PASS，改为实际连接证明。既有 Apple 四例和 Engine/CLI 41 例定向通过均仅为调试结果，不代替全量。

### 本轮实际命令

下列命令在 `D:\Apple\apple-buy-bot` 的 pwsh 捕获通道运行；`python` 指 `.venv\Scripts\python.exe`。登录状态测试和完整 pytest 进程中移除 `APPLE_BUY_BOT_STORAGE_STATE_FILE`、`APPLE_BUY_BOT_USERNAME`、`APPLE_BUY_BOT_PASSWORD`，不读取或覆盖用户凭据。

```text
git status --short
git rev-parse HEAD
python -m pytest tests/test_runtime_purchase_plan.py -q --tb=short --junitxml=outputs/runtime-plan-final.xml
python -m pytest --collect-only -q
python -m pytest tests/test_regression_review.py -vv --tb=short
python -m pytest tests/test_apple_cart_resume.py tests/test_session_control.py -q --tb=short --junitxml=outputs/runtime-fixture-fixes.xml
python -m pytest -q tests/test_local_login_state.py --junitxml=outputs/local-login-state-isolation.xml
python -m pytest -v --tb=short -o faulthandler_timeout=45 --junitxml=outputs/runtime-purchase-verified.xml
python -m ruff check src tests
python -m compileall -q src tests
node --check outputs/runtime-web-script.js
python -m src.main dry-run apple
python -u -m src.main web --port 8766
Invoke-RestMethod -Uri 'http://127.0.0.1:8766/status'
Invoke-RestMethod -Uri 'http://127.0.0.1:8766/purchase-plan'
Invoke-RestMethod -Uri 'http://127.0.0.1:8766/health'
Invoke-WebRequest -Uri 'http://127.0.0.1:8766/'
git diff --check
```

另外通过 sqlite3 `mode=ro` 读取 guard 并比较摘要；通过 Get-NetTCPConnection/Get-CimInstance 核对服务归属及子进程，Stop-Process 仅停止已确认的本轮挂起测试 PID 48488 和空闲旧 8766 服务 PID 18196。没有清理用户 Chrome/profile/购物袋。所有本轮详细输出只保留在 Git 忽略的 outputs/。

### 本轮未实现或未执行

- **NOT RUN / 未实现**：账号密码自动填表。真实密码与登录提交控件没有新页面证据，`try_login_from_env` 明确返回 NOT_RUN，不读取用户名/密码环境变量，不写猜测选择器。可用入口为人工登录程序 profile 或导入受支持的本机状态文件，仍需实际认证。
- **NOT RUN / 未实现**：Apple 新品 Continue 后续分支；没有新的真实页面证据，保持暂停。普通 Chrome 已登录不意味着程序 profile 已登录，也不是程序链路验收。
- **不支持**：裸 Token、私有鉴权接口、任意域登录状态和含 IndexedDB/其他未适配字段的“完整”状态文件；拒绝并提示格式不支持，不把部分导入说成完整登录。
- **NOT RUN**：真实账号登录成功、真实商品规格/购物袋/结算/完整分期方案及 READY_TO_SUBMIT。本轮没有新订单、付款、取消订单或银行授权，未清现有购物袋/profile/数据库/订单锁。

## 2026-09-11 审核修复：基线 65ee279（历史轮次）

本轮仅修复既有流程、隐私和验证缺陷，不增加平台。开始时实际 `HEAD` 为 `65ee279dffab5ac50cd305cf7d5bd55d53c38f44`，`git status --short` 为空，无用户未提交工作。以下结果均来自本轮执行；后面的历史记录不能替代本轮或程序独立 profile 的验收。交付提交号以本轮完成后的 `git log -1` 和交付回复为准；未推送远端。

用户提及的 `test_harness.py` 和 extracts 在本机附件目录及仓库中未找到，已询问路径但未收到补充。因此没有声称运行过这两份附件；依据 R01—R07 场景在现有 pytest 夹具中添加故障注入，导入真正的 `src` 模块，先观察失败再修复。旧正向夹具补充了合成地址/国行确认字段和明确的本地 HTTP 测试入口，没有放宽生产断言或通过改摘录替代修复。

### 逐项修复及证据

| 项目 | 文件与修复原因 | 本轮验证及边界 |
|---|---|---|
| R01 开售许可 | `src/core/engine.py`、`scheduler.py`：准备成功后才置 prepared；库存读取入口（内部含规格选择）、选择、加购、结算、复核、提交独立检查开售时间。可注入时钟/等待器，快进测试时间；快照和首次审计记录 scheduled/immediate 与 live/dry_run 的实际模式 | `tests/test_audit_engine.py`：未来 5 分钟首导航失败、登录中断恢复、429 退避、人工暂停恢复；许可前没有选择/加购/提交。仅显式立即执行入口跳过等待，dry_run 本身不跳过计划等待 |
| R02 工作者与关闭 | `src/core/engine.py`、`src/runtime.py`、`src/browser/manager.py`：取消并等待全部工作者后才能释放引用；停止可重入；审计/日志异常不能丢失任务，提交中取消保留不确定锁；关闭浏览器也必须完成回收，失败保留资源供重试 | 原故障注入首轮 4 FAIL（其中含 R01 两例）；修复后审计及旧回归 63 PASS。另补 Runtime/BrowserManager 关闭阶段取消与失败：同一组测试 4 FAIL（0.56 秒）→ 4 PASS（0.62 秒），真实 ProfileLock 位于临时目录；最终全量结果见下表 |
| R03 最小诊断 | `src/browser/inspection.py`：登录、账号、验证、结算、回执及未知页面只记录结构数量；不读取整页正文/HTML。公开商品路线仅记录固定字段、值语法和已验证的 data-autom 控件。任意 id/class/aria/data-testid 等私密属性不导出；HTML 为最小 JSON 生成的报告 | `tests/test_audit_privacy.py` 红测 3 FAIL；修复后与诊断旧例 8 PASS。覆盖无标签姓名、中文地址、6 位验证码、aria、拆分节点、邮箱手机号及账号 URL。截图请求全字/媒体遮罩；元数据为 `privacy_guarantee=not_certified`，不保证绝对脱敏；历史诊断未清空 |
| R04 分期一致性 | `src/platforms/apple_cn/parser.py`、`adapter.py`：同一已选方案须明确银行、24 期、本金、总还款、零利息、零手续费和完整每期金额（或前 23 期及末期尾差），分币求和一致；最终 review 重读当前完整方案并比较原方案，不沿用旧时间戳许可 | `tests/test_audit_apple.py` 原误接受示例 1 FAIL，修复后的 Apple 定向套件曾 40 PASS；追加备选等用例后审计文件 9 PASS。含矛盾月供、小额收费、正常舍入/末期尾差、无尾差、缺失字段和银行切换。既有 `test_marketplace_contracts.py` 明确断言 31 秒过期证据为 UNKNOWN；Apple 旧时间戳字段已移除，当前 DOM 必须重核。真实完整分期披露 NOT RUN，不点击银行授权或付款 |
| R05 地址/版本许可 | `src/order/checkout.py`、`src/core/models.py`、Apple/marketplace 适配器、`src/core/config.py`、`src/platforms/base.py`、`src/runtime.py`、`src/web/api.py`、`src/main.py`、`src/web/index.html`：明确本机确认后绑定当前地址摘要；Apple 必需字段完整且包含当前可见补充地址字段，变化即失效。完整个人信息不进入通用诊断或订单模型。商品大陆商店校验与精确登录域白名单分离；seller/region 不能代替国行版本证明，缺证据时请求人工核验 | `tests/test_audit_checkout.py` 初始 3 FAIL，修复后 3 PASS；与受影响配置/商城/结算用例 45 PASS。实际 Apple 本地页面覆盖地址与补充地址变化、未确认版本、非 CH/A 版本拒绝。Web/CLI 新增显式地址/国行确认入口，不能开启 auto_submit 或关闭 dry_run |
| R06 安全候选回退 | `src/core/exceptions.py` 新增明确的 CandidateUnavailable；Engine 仅在选品阶段冷却并尝试剩余候选。Apple 枚举实际读到的最多 64 个组合；缺失/无货可跳过，无候选返回空。价格变化或库存竞争失效仅在未加购且完整身份核对后允许换候选；已有加购/提交尝试禁止回退 | Apple 实际适配器本地页面覆盖首选不存在/备选存在、全无、竞争失效、价格变化。Engine 注入覆盖冷却、全局选择器变化暂停、未知加购/提交不重放；既有测试覆盖明确拒绝与未知结果。UNKNOWN 不释放、不跨平台再提交 |
| R07 实际适配边界 | Apple parser/adapter/selectors、`src/web/api.py`、页面和 README：新品 Continue 启用仍明确报 NOT RUN 并暂停，不宣称可抢购；健康接口明确自动真实订单 NOT_VERIFIED | 本轮 Chrome 只读官网连接尝试返回 `User unavailable`，没有获得新页面证据；这不是 Apple 网站拒绝，也不是安全审核拒绝。Continue 未适配。京东“确认订单＋立即支付”仍保留人工付款分支；本轮没有京东实页测试 |

### 本轮统一检查

运行环境：本仓库 `.venv`，Python 3.14.5。pytest 中的浏览器测试使用程序正式 Chrome 通道和独立临时目录，内容来自本地 HTTP 夹具或路由合成页面。

| 检查 | 结果 | 证据 |
|---|---|---|
| 第一轮全量 pytest | PASS：235 passed，3 warnings，55.60 秒 | `outputs/audit-65ee279/pytest-full.txt`、`.xml`；随后交叉检查补出浏览器关闭阶段的缺口，不能作为最终代码的唯一结果 |
| 最终全量 pytest | **PASS：239 passed，3 warnings，57.19 秒** | `outputs/audit-65ee279/pytest-final.txt`、`.xml`；完整重跑，0 failed、0 skipped，未拼接定向通过数 |
| 最终 Ruff | **PASS** | `outputs/audit-65ee279/ruff-final.txt`，`src tests`，退出码 0 |
| 最终 compileall | **PASS** | `outputs/audit-65ee279/compile-final.txt`，`src tests`，退出码 0 |

两次全量的 3 条警告相同：2 条上游 Starlette/AnyIO 弃用提示，以及一个合成测试替身把 Decimal 字段赋为 int 的序列化提示；不是官网异常。未为了消除提示修改无关依赖。最终检查后只补充验收文档，没有再修改受测代码。

本机 8766 旧服务确认未运行任务、无子进程后仅重启该服务；旧 8765 未动。对新版 `/health`、`/status` 和 `/` 做只读 HTTP 检查，确认 `running=false`、`dry_run=true`、`auto_submit=false`、原 SUCCESS 锁、两个本机确认按钮，以及 `live_automatic_order=NOT_VERIFIED`、`apple_new_product_continue=NOT_RUN`。证据：`outputs/audit-65ee279/local-service.json`。本輪没有通过浏览器点击控制台按钮，不将 HTTP 检查写成 UI 实测。

### Apple 单平台受保护演练

`tests/test_audit_protected_run.py` 本轮 **4 PASS，3.73 秒**，证据为 `outputs/audit-protected-run.txt/.xml`，且纳入完整 pytest：

- 使用真实 `AppleCNAdapter` 与 `Engine`，临时数据库、临时程序独立 profile、离线浏览器和全路由本地合成响应；合成商品为 iPhone 17 / 256GB / 黑色 / CNY 6799。没有真实账号数据。
- 走过规格选择 → 加购一次 → 购物袋核对 → 结算 → 地址/国行人工确认暂停 → 显式确认并恢复 → `READY_TO_SUBMIT`。这是微信方式的本地结算夹具，不是分期端到端验收。
- 故意将测试配置 auto_submit 和 adapter allow_submit 置 true，同时保持 `dry_run=true`，断言最终提交、付款点击均 **0**；临时库保留 CLAIMED，页面保留至测试结束。
- 另分别为临时库预置 SUCCESS、UNKNOWN、SUBMITTING，断言完整 guard 原样保留，实际适配器所有业务方法调用均 **0**，无库存/订单记录，无页面打开。
- 本机真实配置执行 `python -m src.main dry-run apple`：**BLOCKED（预期保护生效）**，退出码 2，提示 `apple 正保留结算或订单页面：SUCCESS；请先核对结果`；在 Runtime 启动工作者之前拒绝，没有打开购买流程。首次终端中文编码不正确，设置进程内 UTF-8 后重执行相同入口并保存可读输出；两次均退出 2。
- 真实 SQLite 只读比对：原 guard 共 1 条，状态 SUCCESS；全部字段 SHA-256 为 `3c00c641d5215b0e570ab8aad8f424ae3c6d135d190aeb4f8b0ed710dc55af08`，与开工基线完全一致。证据：`outputs/audit-65ee279/guard-after.json`、`local-guarded-dry-run.txt`。没有清购物袋、profile、数据库或释放订单锁。

### 实际执行的命令

以下均在 `D:\Apple\apple-buy-bot` 的 pwsh 捕获通道执行，`python` 指 `.venv\Scripts\python.exe`。定向命令用于先复现和定位，不等同全量；部分早期红绿结果仅保留在本轮控制台，不能捏造文件证据。

```text
git status --short
git rev-parse HEAD
python --version
python -m pytest tests/test_audit_engine.py -q --tb=short
python -m pytest tests/test_audit_engine.py tests/test_engine.py tests/test_scheduler.py tests/test_regression_review.py tests/test_marketplace_engine.py -q --tb=short
python -m pytest tests/test_audit_engine.py -k close_fault -q --tb=short
python -m pytest tests/test_audit_apple.py -q
python -m pytest tests/test_audit_apple.py tests/test_apple_live_flow.py -q
python -m pytest tests/test_audit_apple.py -q --junitxml=outputs/apple-audit-final.xml
python -m pytest -q tests/test_audit_privacy.py --junitxml=outputs/r03-audit-before.xml
python -m pytest -q tests/test_audit_privacy.py tests/test_browser.py::test_redacted_inspection_inventory_html_and_screenshot tests/test_browser.py::test_inspect_challenge_records_waiting_human tests/test_inspect_cli.py --junitxml=outputs/r03-audit-after.xml
python -m pytest -q tests/test_audit_checkout.py tests/test_marketplace_flow.py tests/test_checkout.py tests/test_marketplace_contracts.py tests/test_config.py --tb=short --junitxml=outputs/audit-65ee279/r05-integration.xml
python -m pytest -q tests/test_audit_protected_run.py --junitxml=outputs/audit-protected-run.xml
python -m pytest -q --tb=short --junitxml=outputs/audit-65ee279/pytest-full.xml
python -m pytest -q --tb=short --junitxml=outputs/audit-65ee279/pytest-final.xml
python -m ruff check src tests
python -m compileall -q src tests
python -m src.main dry-run apple
python -u -m src.main web --port 8766
Invoke-RestMethod -Uri 'http://127.0.0.1:8766/health'
Invoke-RestMethod -Uri 'http://127.0.0.1:8766/status'
Invoke-WebRequest -Uri 'http://127.0.0.1:8766/'
git diff --check
```

隐私红绿、地址红绿和离线受保护演练还分别输出了 `outputs/r03-audit-before.*`、`outputs/r03-audit-after.*`、`outputs/audit-65ee279/r05-before.xml`、`r05-after.xml`、`outputs/audit-protected-run.*`。本机基线记录为 `outputs/audit-65ee279/baseline.json`，只含原 guard 数量、状态和摘要，不含完整订单/个人信息。输出、真实配置、数据库、日志、截图、profile 均不进入 Git。

另记录测试夹具自身的修正：隐私红测先修正 UTF-8 再复现；受保护演练首次因夹具路由回调错误为 1 failed、3 passed，修正后相同命令 4 passed；旧引擎夹具在加入显式确认要求后曾因缺合成确认字段挂起，停止该测试进程并补齐夹具。隐私/演练文件曾各有一个 Ruff 格式或导入错误，修正后检查通过。这些调试结果没有与最终全量通过数混合。

### 未解决及未执行

- **NOT RUN**：当前真实新品 Continue 后续购买分支；缺少可读取页面证据，不猜选择器。
- **NOT RUN**：本轮程序独立 profile 的真实网站完整购物袋/结算/分期流程、真实账户登录复用及新地址确认。既有锁不释放，普通 Chrome 的历史待付款单不充当验收。
- **NOT RUN**：任何真实订单创建、付款、取消订单，以及银行/微信授权。用户本轮禁止这些操作。
- **BLOCKED**：本轮 Chrome 只读页面工具连接不可用（`User unavailable`），未将其误记为网站反自动化结论。
- **未取得**：用户提及的故障注入附件原文件；已按明确文字场景迁移到真正模块测试，但不能声称逐行复用附件。
- **保留限制**：真实分期披露若缺银行、本金、完整还款计划、利息或手续费，一律 UNKNOWN；诊断为最小采集和遮罩而非脱敏认证，历史本机产物未清理。

---

## 历史：2026-09-11 多平台功能增量（65ee279 及之前，不是本轮结果）

已接四渠道配置/网页控制、淘宝与天猫共享会话、每会话串行工作、严格卖家/地区/费用/24期分期核验、订单摘要持久化、只读查单和独立付款状态。京东、天猫已使用真实商品组件；当前各阶段实页边界见 [多平台实页记录](docs/marketplace-live-evidence.md)。**仍未完成四平台真实自动下单验收。**

统一测试首轮：193 项，其中 **188 passed、5 failed，45.92 秒**。发现的问题是新会话检查与旧 inspect 测试替身未对齐，以及 Runtime 启动覆盖外部注入的适配器；已修复。仅复查受影响的 inspect、Runtime 和会话控制，**14 passed，2.33 秒**，覆盖原 5 个失败项，没有再次重跑全量。证据为 `outputs/pytest-marketplace.txt/.xml` 和 `outputs/pytest-marketplace-fixes.txt/.xml`。

新增测试集中在共享会话串行、完整本地交易链、防重复点击、当前金额/分期证据和旧数据库迁移保锁。无真实平台提交测试。首轮有 2 条上游弃用警告及 1 条测试替身 Decimal 序列化警告，不代表官网异常。

收尾修复 Pro/Pro Max 型号混淆、“暂无现货”误判以及小额利息漏判，仅执行 3 项对应判断回归及 2 项本地交易场景，**5 passed，5.59 秒**。`/health` 改为逐渠道报告实页阶段及自动下单未验证，相关 CLI/Web 检查 **17 passed，2.07 秒**（`outputs/pytest-marketplace-api.xml`）。这些是修改后的定向复查，未另跑第二轮全量。

Ruff PASS。doctor 实际启动正式版 Chrome 152.0.7977.83，Python 3.14.5、Playwright 1.62.0；数据库迁移成功并保留原 Apple SUCCESS guard 的 owner、创建时间与更新时间。迁移前 SQLite 备份留在本机 data 目录。

Chrome 验证新版本机页面 PASS：四渠道选择、商品配置表单、缺失链接提示、原订单保护显示及无整页横向溢出。新版服务在本轮使用 `http://127.0.0.1:8766/`，未终止旧 8765 服务。

京东实页现为嵌入式“确认订单＋立即支付”，所见 24 期有息且白条不可用；已加入只读检查和人工付款边界。天猫官方商品页已核对选中规格，但结算未验证。淘宝被浏览器工具策略阻断。上述平台尚未达到完整自动创建待付款订单的可用状态；共享代码本地测试通过不替代实页验收。

以下为 2026-09-10 历史记录，保留其证据边界。

日期：2026-09-10（Asia/Shanghai）。本次为第二轮开发，以下区分代码测试、Chrome 会话及程序自己的会话。

## 正式版 Chrome 切换

同日按用户要求，所有程序浏览器入口统一使用已安装的正式版 Chrome（chrome 通道）。实际启动版本 152.0.7977.83，doctor PASS；沿用程序独立 profile 路径，未复制个人 Chrome 的资料，未清除现有订单锁。未安装或启动失败时明确报错，不自动回退到 Chrome for Testing。

正式版遇到空内容 HTTP 429 时会触发浏览器错误页；现已保留此前收到的主文档响应，继续遵守 Retry-After。测试分别验证秒数、长等待及 HTTP-date，并覆盖浏览器启动失败后释放 profile 锁。正式版全量回归 **179 passed，2 条上游弃用警告，34.99 秒**；Ruff PASS。包含实际本地站 Cookie/localStorage 跨关闭重开及平台隔离测试。最新回归输出为 outputs/pytest-stable-chrome.txt 和 .xml。

浏览器切换与本地会话持久化测试不代表官网 541/404 已解决；下表的官网失败记录仍有效，完整购买流程须另行验证。

## 购买流程验收

**Apple 适配已实现；程序完整购买验收 BLOCKED，尚不能宣布可用于正式抢购。**

| 项目 | 结果 | 实际证据与限制 |
|---|---|---|
| Ruff | PASS | outputs/ruff-phase2.txt |
| 离线与本地 Chromium 回归 | PASS：176 项，44.69 秒 | outputs/pytest-phase2.txt 和 .xml；2 条上游弃用警告，不等于官网成功 |
| 新版 Web 状态页 | PASS（Chrome） | 正常展示 24 期免息偏好、演练保护及 SUCCESS 订单锁；桌面无整页横向溢出 |
| 程序读取目标新品配置 | PASS | 实际读取 Pro Max / 512GB / 黑色、RMB 12,999、购买按钮不可用；outputs/apple-live-product-result.json |
| Chrome 在售机型完整流程 | PASS | 用户手动登录、确认保存地址；iPhone 17 / 256GB / 黑色 / 1 件 / RMB 6,799；创建一笔微信待付款订单，未付款 |
| 程序自己的在售机型加购 | BLOCKED | 显示窗口和无窗口的独立 profile 均未通过；加购跳到 Page Not Found。只读复查购物袋为空 |
| 程序配送组件 | BLOCKED | 页面自身请求报 HTTP 541，配送控件没有加载。未判断其服务端原因；未绕过或伪造响应 |
| 最新配送缺失保护 | 已实现 | 配送控件没有有效内容时，在任何加购动作之前暂停；本地浏览器回归断言没有加购点击 |
| 24 期免息选择 | PASS（本地回归） | 按真实 DOM 建立银行、24 期、0% 年化利率、分期总额核验；Chrome 实页查看过该方案 |
| 分期待付款回执 | NOT RUN | 此轮已有一笔测试单，不追加订单；未自动执行银行授权/支付 |
| 真实账号在程序 profile 中登录及复用 | NOT RUN | 已实现登录/检查登录入口；个人 Chrome 已登录不能作为程序 profile 已认证的证据 |
| 新品“继续”启用后的预购流程 | NOT RUN | 当前按钮禁用；未猜测下一步 |
| 京东、天猫购买 | NOT RUN | 仍为诊断和适配接口骨架 |
| 拥堵环境、下单速度、到货、付款 | NOT RUN | 未将历史或配置耗时作为抢购成功指标 |

既有测试单计入本机 SQLite 的 SUCCESS 订单锁。SUCCESS 表示获得订单回执，**不表示已经付款**。锁保留并阻止再下单；没有因等待时间经过而假定订单已取消。outputs 中的测试工具、profile、订单锁记录和所有现场均不进入 Git。

## 本轮测试范围

- 商品摘要与选择的型号、容量、颜色必须一致；金额须为单一 CNY 全价，不能把月付金额视为全价。
- 只选页面真实存在的组合；购买按钮、配送信息、购物袋数量/金额、订单总额逐步核对。
- 分期仅接受配置银行的 24 期、0% 年化利率，总额须等于目标商品金额；订单回顾再次匹配银行及 24 个月，方案证据有时效。
- 已保存的脱敏地址/联系方式由官网正常验证，不把空白输入框当作未填写；程序不收集联系方式明文。
- 默认禁止提交；API 只能强制演练，不能关闭演练或开启提交。提交前再核验，点击后结果不明保留锁，禁止重放。
- 登录入口复用同一个程序 profile；运行中的登录控制不会跳走当前购物流程；打开页面不等于认证成功。
- 原有配置、优先级、状态机、SQLite 并发锁、跨重启不确定结果、人工恢复、节流、域名导航边界和脱敏检查仍纳入全量回归。

## 当前阻塞的复查方法

从本机网页打开“登录 Apple”，在程序自己的浏览器完成正常登录和官网验证，再点“检查登录”。登录仅用于保存本机会话；不能据此宣称 HTTP 541 已解决。后续必须重新进行不提交的程序路径验证，确认实际配送、购物袋和结算可用。现有订单锁不得为通过测试而清除。

选择器证据及仍未知的分支见 [实页记录](docs/apple-live-evidence.md)。第一轮 141 项工程检查属于历史基线；最新结果以上表及本轮测试文件为准。
