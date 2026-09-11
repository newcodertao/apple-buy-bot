# Apple 实页适配验收

## 2026-09-11 审核修复：基线 65ee279（本轮）

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
