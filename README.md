# apple-buy-bot

面向 Apple 中国大陆官网、京东、天猫和淘宝的本机购买辅助工程。使用正式版 Chrome，保留登录会话，提供商品检查、规格优先级、结算核对、24 期零费用分期核验、单次提交和人工接管。Apple 已有一笔 Chrome 真实待付款测试订单；京东商品、购物车及新版结算窗口、天猫官方商品页已读取真实结构。**目前还不能把四个平台都认定为可自动下单**：京东当前结算直接连接付款，淘宝访问受工具策略限制，未验证的交易步骤会暂停。详细边界见 [验收记录](VALIDATION.md)。

所有最终支付由用户手动完成。没有验证码识别、滑块破解、短信/人脸/设备验证绕过、隐身插件、代理轮换、批量账号或私有下单接口。

## 快速运行（Windows / PowerShell 7）

本目录已建立 `.venv` 并安装依赖。运行：

```powershell
Set-Location 'D:\Apple\apple-buy-bot'
& .\.venv\Scripts\python.exe -m src.main doctor
& .\.venv\Scripts\python.exe -m src.main web
```

打开 <http://127.0.0.1:8765>，选择平台后点击“打开登录”，登录后点击“检查登录”。在“商品配置与检查”中保存商品链接，检查后核对卖家和配送地区标识。可以单平台演练，也可以启动所有已配置平台。默认 `dry_run: true`、`auto_submit: false`；网页/API 无法关闭演练保护或开启自动提交。

**本机本轮的新版本服务在 <http://127.0.0.1:8766/>**，旧 8765 服务保留未关闭。需要另行启动时使用 `python -m src.main web --port 8766`；不要在同一端口重复启动。

京东检查结果返回公开店铺 ID；天猫官方店使用其公开店铺域名作为标识，配送地区使用本机摘要，不显示收货地址。商品未选规格或报价为补贴、领券条件价时，检查结果明确说明阻塞原因。“检查当前结算”可读取京东已打开的新版结算窗口，报告实际金额、24 期费用和最终按钮类型；它不点击付款。“读取当前订单”仅核对已记录订单的回执，不重复提交。

新环境安装：

```powershell
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
& .\.venv\Scripts\python.exe -m src.main init
& .\.venv\Scripts\python.exe -m pytest -v
& .\.venv\Scripts\python.exe -m ruff check .
```

要求 Python 3.12+ 和已安装的正式版 Google Chrome；程序使用 Playwright 的 chrome 通道，CLI、网页登录和购买流程统一使用正式版，不回退到 Chrome for Testing。现有独立 profile 路径保持不变，不读取个人 Chrome 的默认资料目录。首次在程序中登录后保存，过期或验证时人工处理。

本次实测 Python 3.14.5、Playwright 1.62.0。`requirements.lock.txt` 保存本次通过检查的直接/间接依赖版本，可先安装它，再执行 `pip install -e . --no-deps` 复现。Python 3.12 本身未单独运行兼容性测试。

## 当前状态与证据

| 项目 | 本轮结果 | 证据边界 |
|---|---|---|
| 配置、状态机、SKU 排序、SQLite、调度 | PASS | 本机单元/回归测试 |
| Chrome 持久会话 | PASS | 本地测试站 Cookie/localStorage 关闭后重开仍存在；Apple、京东、阿里会话隔离，天猫与淘宝共用阿里会话 |
| 手动登录入口 | PASS（机制） | CLI 和网页共用程序 profile；Chrome 的个人 profile 与程序 profile 分开 |
| Apple 官方商品页 inspect | PASS（实页） | `outputs/apple-public-inspect/` 内 JSON、脱敏 HTML、布局 PNG、metadata |
| Apple 商品配置与价格 | PASS（程序实页） | Pro Max 黑色 512GB，页面价格与购买按钮状态均读取实际 DOM |
| Apple 登录、购物袋、结算、微信待付款回执 | PASS（Chrome 实页） | 当前在售机型完成一次正常流程；程序独立路径的结果见 VALIDATION |
| 24 期免息 | 已实现，回执待验收 | 验证银行、24 期、0% 年化利率及总额；不自动批准银行付款 |
| JD / Tmall / Taobao | 分阶段接入 | 京东、天猫商品字段已接真实 DOM；通用交易链有本地页面测试。真实订单提交未验收，淘宝实页被工具策略阻断 |
| race / parallel 执行 | PASS（模拟） | FakeAdapter 并发测试，不代表平台购买成功 |
| FastAPI / 本机状态页 | PASS | API 测试与真实 Chromium 桌面/手机视口检查 |
| 真实待付款订单 | PASS（Chrome） | 用户授权后创建一笔测试单；付款、到货均 NOT RUN |

2026-09-10 实际通过本程序检查了 [Apple iPhone 18 Pro / Pro Max 商品页](https://www.apple.com.cn/shop/buy-iphone/iphone-18-pro)。本次快照中有 21 个按钮、77 个链接、111 个 role 元素和 225 个含 data 属性的元素。这是页面结构证据，不是控件可点击/交易成功的证明。

页面当时标注 9 月 12 日晚 8 点接受预购，因此**本地** `config/config.yaml` 已填入该时间和同一产品系列的 Apple URL；`config.example.yaml` 仍留空 URL 和时间。使用前应重新查看官方页面。京东、天猫 URL 仍为空。选择器证据见 [Apple 实页记录](docs/apple-live-evidence.md)，没有将历史速度作为抢购性能指标。

## 目录与职责

```text
apple-buy-bot/
  pyproject.toml / requirements.txt / requirements.lock.txt
  README.md / VALIDATION.md / FILES.md
  config/config.example.yaml / config.yaml
  data/profiles/{apple,jd,tmall}/  # 淘宝与天猫共用旧 tmall 目录，仅本机
  data/database.db
  logs/{engine,apple,jd,tmall,taobao}.log
  screenshots/                   # 脱敏现场
  outputs/                       # 本轮验收证据
  src/main.py                    # CLI
  src/runtime.py                 # 单一运行生命周期，供 CLI/Web 复用
  src/diagnostics.py             # doctor
  src/core/                      # 配置、模型、异常、状态机、引擎、时钟、调度、日志
  src/browser/                   # Chromium、profile 锁、脱敏页面检查
  src/platforms/base.py           # 统一 Adapter 接口
  src/platforms/{apple_cn,jd,tmall,taobao}/  # adapter / selectors / parser
  src/platforms/marketplace.py    # 市场渠道的通用交易与回执核对
  src/order/                     # 排序、匹配、订单核验、持久化订单锁
  src/monitor/                   # 有上限的轮询及健康信息
  src/notify/                    # Notifier / ConsoleNotifier
  src/storage/                   # SQLite schema 与存取
  src/web/                       # FastAPI 路由及本地状态页
  tests/                         # 单元、并发、安全回归及真实本地 Chromium 测试
```

完整文件清单见 `FILES.md`。当前工程用标准库 `logging`、`sqlite3`、`asyncio`，不引入消息队列或分布式服务。

## CLI

以下命令均可用；已激活虚拟环境时可以写 `python`，否则使用 `& .\.venv\Scripts\python.exe`。

```powershell
python -m src.main init
python -m src.main login apple
python -m src.main login jd
python -m src.main login tmall
python -m src.main login taobao
python -m src.main check-login
python -m src.main check-stock
python -m src.main dry-run apple
python -m src.main dry-run all
python -m src.main run
python -m src.main run --now
python -m src.main status
python -m src.main web
python -m src.main doctor
python -m src.main doctor --online
python -m src.main inspect apple https://www.apple.com.cn/shop/buy-iphone/iphone-18-pro
python -m src.main --headless inspect apple https://www.apple.com.cn/shop/buy-iphone/iphone-18-pro
```

`--config PATH` 与 `--headless` 是全局参数，放在子命令前。自定义配置按 `项目根/config/文件.yaml` 放置，运行目录始终锚定配置父目录的上一级，不随 shell 当前目录变化。`init` 不覆盖已有配置。

`login` 由你在显示出的正式版 Chrome 中完成；按 Enter 或关闭窗口后保存 profile。也可在网页选择平台并打开登录。程序不收集密码/验证码、不导出 Cookie；`check-login` 通过各网站已经观察到的账户控件核实认证，打开页面或存在 profile 本身不代表登录成功。首次登录一次，过期或安全验证时再人工处理。

`run` 使用本机系统时间等待 T−10 分钟准备、T−3 分钟打开商品、T−30 秒复查、T−5 秒就绪、T=0 监测。`dry-run` 与 `run --now` 跳过等待；只有 `dry-run` 强制禁止最终提交。Apple 公共商品配置和加购不强制预先登录；实际登录页和订单核验阶段必须确认认证，未知页面或验证会转人工。

运行控制台输入 `resume [apple|jd|tmall|taobao]`、`status` 或 `stop`。人工处理、待提交或成功待付款期间浏览器保持打开，直到你停止或关窗。提交结果 UNKNOWN 不可通过 resume 再次提交：先人工核查订单历史。出现验证的 inspect 同样等待人工，并在 resume 时只检查现有页面。

`status` 读取 SQLite 历史记录，实时运行状态使用 Web `/status`。`doctor` 用临时无窗口会话验证正式版 Chrome 能否启动并报告版本，不使用账号 profile、不执行真实登录或购买；默认网络是 NOT RUN，`--online` 额外执行一次公开 HEAD 请求。时钟诊断显示本地时间、UTC、目标时间、时区偏移；外部时钟误差未测量，不会声称完成时间同步。

## SKU 与提交保护

全局 `product` 给出型号、容量、颜色的优先顺序及数量、单价上限和可选总预算 `max_total`。每个 `products.<id>` 可覆盖这些限制，且型号须存在于全局型号优先级中。平台 URL 在每个商品下配置，`platforms.<platform>.enabled` 控制启用。京东/天猫/淘宝目标还需 `seller_ids`、`region`、运费上限 `max_shipping` 和其他费用上限 `max_fees`。缺失时暂停，不自动接受任意店铺。

排序为型号 → 容量 → 颜色的字典序。示例中 512GB 黑色优于 512GB 银色，后者优于 256GB 黑色。未列出的选项、未知/不可用库存、非 CNY、超价 SKU 均排除。一个平台的多个型号按优先级轮流检查，单个页面不会被多个协程同时操作。

Apple 逐个选择配置并读取最终商品摘要，不推算完整 SKU 笛卡尔积。`available` 表示当前页面购买按钮可用，不保证最终下单成功；配送详情未加载时会明确标注，并在加购前暂停，结算还须再次核对。候选搜索最多 64 个组合，找到首个符合价格和按钮条件的组合后停止本轮搜索。

付款默认 `order.payment_method: installments`、`order.installment_bank: 中国建设银行`，按本次用户偏好设置。仅接受明确显示 24 期、0% 年化利率且总额与商品金额一致的方案；缺失时暂停，不静默改用有息分期或一次性付款。也可显式配置 `wechat`，创建订单后人工扫码；银行/微信付款操作均不自动执行。

最终提交需要全部成立：

- 配置 `app.dry_run=false`、`order.auto_submit=true`，且不是 dry-run 命令。
- 单一持久化订单锁属于本流程；平台、商品 ID、SKU ID、型号、容量、颜色全部匹配。
- 币种 CNY、单价与已选 SKU 一致、单价不超过上限，数量正确，实付总额不超过总预算。Apple 总价等于单价 × 数量；市场渠道还核对优惠、运费和其他费用。
- 只有一行目标商品，没有未确认的额外配件/费用；地址存在、结算有效、没有安全验证。
- 在点击前再次执行完整 `verify_order()` 和验证检测。

四个渠道使用三个物理会话：Apple、京东、淘宝/天猫。淘宝与天猫串行轮转，天猫优先；开始选择商品后由该渠道保留页面，人工处理期间不能被另一个渠道跳走。race 和 parallel 都在有效核验后取得唯一订单锁，其他平台在下一次业务动作前停止，最终只允许一个提交。

市场渠道的结算要求商品、店铺、地区、现货状态与当前报价一致；商品小计−优惠＋运费＋其他费用必须等于实付总额。报价有效期 30 秒，人工处理后读取新的结算报价，不重放加购。24 期方案必须同时有 0 利息、0 服务费和一致的本金/还款总额；广告文案不能作为证明。`allow_post_order_financing_check` 默认关闭，仅在明确配置后允许未知分期留到已创建订单检查；已知有息方案始终阻断。立即支付/开通授信入口不属于自动提交订单。

SQLite 中的单一订单锁可跨进程/重启保留。只有明确 `REJECTED` 才自动释放；超时、取消或缺少确定订单号记为 UNKNOWN，不能自动重试。SUCCESS 也保留。演练 READY_TO_SUBMIT 会保留预约，避免重复运行误购。

重新演练或处置遗留 UNKNOWN 前，先停止程序并关闭使用该 profile 的浏览器，亲自检查平台订单，确认没有已创建或待支付订单后，使用 `status` 中的完整 owner：

```powershell
python -m src.main reconcile "从 status 复制的完整 owner" --confirmed-no-order
```

该命令会检查三个 profile 是否仍被占用；它不查询平台、不取消任何订单，也不会清除 SUCCESS 锁。不要在提交仍可能进行时声称订单不存在。

## 频率、故障与本地数据

Apple 沿用原阶段频率；京东、淘宝、天猫的检查间隔至少 10 秒，可通过平台的 `refresh_interval` 调长。淘宝/天猫按共用会话合计节流。最大检测次数默认 120、最大自动重试 5 次、指数退避上限 30 秒；服务端 `Retry-After` 优先，不能因本地上限提前重试。验证码/风控/未知页面转人工，不进入自动重试。加购结果不明后不会重放点击，恢复时进入结算核对数量。

每次状态变化持久化 timestamp、platform、product、sku、old_state、new_state、message。数据库包括 runs、events、stock_checks、orders 和 order_guard。日志有毫秒时间、动作、耗时、结果及异常类型，配置 URL 去掉查询串；快照 metadata 记录实际页面 URL。stock_check_ms、sku_select_ms、cart_ms、checkout_ms、submit_ms 可从当前状态及日志查看。

profile 包含本机敏感登录状态；配置严禁密码、短信码、支付信息。日志不输出原始网络/浏览器异常正文。诊断 HTML 去除脚本、隐藏内容、输入值和敏感属性，data 属性保留名称并隐藏值；截图只保留布局，文字及媒体全部屏蔽。故障复盘结合脱敏 DOM 与 metadata，不把黑色遮罩当作原始页面。文件名用 UTC 时间且包含微秒，metadata 也显式标注 UTC。

data、logs、screenshots、outputs、config.yaml 已在 `.gitignore` 排除，不应分享真实 profile 或未经检查的本机诊断目录。通知使用 Notifier 接口，后续 Webhook/ServerChan/PushPlus 实现可替换 ConsoleNotifier，当前没有外发通知。

## Web API

GET：`/status`、`/platforms`、`/products`、`/events?limit=100`、`/orders?limit=100`、`/health`。POST：`/start`、`/stop`、`/resume`、`/login`、`/check-login`、`/save-target`、`/check-product`、`/check-checkout`、`/read-order`。`/start` 可传 `dry_run:true` 强制演练。接口说明在 `/docs`；拒绝密码或 Token 等额外字段。订单列表只返回订单号后四位，完整引用和提交前摘要留在本机 SQLite。

控制 POST 必须带 `X-Apple-Bot-Control: local`；拒绝跨站 Origin 和非本机 Host，默认只监听 `127.0.0.1:8765`。网页自动刷新只是读取本机状态，每 2 秒一次；可暂停，不会产生商品请求。不得把当前本机控制接口直接转发到公网。

## 下一阶段

淘宝、天猫、京东的接入方案见 [多平台接入设计](docs/marketplace-integration-design.md)。该文档是设计，实际渠道能力仍以逐阶段实页验收为准。

剩余工作是程序自己的持久化浏览器全流程验收、实际支持免息的结算与回执，以及当前受限的淘宝交易页面；未见过的结构不填猜测选择器。已创建的待付款测试单和不明结果都计入订单保护，不自动重试或清锁。数据库升级前自动生成含 WAL 数据的本地备份，迁移保留旧订单锁。

技术参考：[Playwright persistent context](https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context)、[Pydantic models](https://docs.pydantic.dev/latest/concepts/models/)、[Chromium Fetch 导航检查](https://chromedevtools.github.io/devtools-protocol/tot/Fetch/)。
