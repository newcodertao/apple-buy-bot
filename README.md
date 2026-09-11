# apple-buy-bot

面向 Apple 中国大陆官网、京东、天猫和淘宝的本机购买辅助工程。使用正式版 Chrome，保留程序专用登录会话，提供商品检查、规格优先级、Apple 本机购买计划、结算核对、24 期零费用分期核验、单次提交和人工接管。**当前未完成任何平台在程序独立 profile 中的真实自动下单验收。** 本轮补充登录诊断和购物袋恢复；本机正常运行入口仍受既有 SUCCESS 订单锁保护，不清锁验证，也不宣称已在实站到达 READY_TO_SUBMIT。新品 Continue 分支仍为 NOT RUN。普通 Chrome 的历史待付款单和本地合成页面演练均不能代替实站验收，详细边界见 [验收记录](VALIDATION.md)。

所有最终支付由用户手动完成。没有验证码识别、滑块破解、短信/人脸/设备验证绕过、隐身插件、代理轮换、批量账号或私有下单接口。

## 快速运行（Windows / PowerShell 7）

本目录已建立 `.venv` 并安装依赖。运行：

```powershell
Set-Location 'D:\Apple\apple-buy-bot'
& .\.venv\Scripts\python.exe -m src.main doctor
& .\.venv\Scripts\python.exe -m src.main web
```

打开 <http://127.0.0.1:8765>，选择平台后点击“打开登录”，登录后点击“检查登录”。在“商品配置与检查”中保存商品链接，检查后核对卖家和配送地区标识。Apple 还需核对“本次购买计划”中的商品、备选规格、数量、预算和付款条件，再点击批准。可以单平台演练，也可以启动所有已配置平台。默认 `dry_run: true`、`auto_submit: false`；批准计划不会改变这些开关，网页/API 无法关闭演练保护或开启自动提交。

本机 <http://127.0.0.1:8766/> 已加载本轮代码，HTTP/API 核验可见购买计划和登录诊断；原 SUCCESS 锁仍保留。旧 8765 服务保留未关闭。需要另行启动时使用 `python -m src.main web --port 8766`；不要在同一端口重复启动。

京东检查结果返回公开店铺 ID；天猫官方店使用其公开店铺域名作为标识，配送地区使用本机摘要，不显示收货地址。商品未选规格或报价为补贴、领券条件价时，检查结果明确说明阻塞原因。“检查当前结算”可读取京东已打开的新版结算窗口，报告实际金额、24 期费用和最终按钮类型；它不点击付款。“读取当前订单”仅核对已记录订单的回执，不重复提交。

新环境安装：

```powershell
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
& .\.venv\Scripts\python.exe -m src.main init
& .\.venv\Scripts\python.exe -m pytest -v
& .\.venv\Scripts\python.exe -m ruff check .
```

要求 Python 3.12+ 和已安装的正式版 Google Chrome；程序使用 Playwright 的 chrome 通道，CLI、网页登录和购买流程统一使用正式版，不回退到 Chrome for Testing。程序使用专用 profile，不读取或继承日常 Chrome 的默认资料目录；日常 Chrome 已登录不代表程序已登录。首次在程序中登录后保存，过期或验证时人工处理。

本次实测 Python 3.14.5、Playwright 1.62.0。`requirements.lock.txt` 保存本次通过检查的直接/间接依赖版本，可先安装它，再执行 `pip install -e . --no-deps` 复现。Python 3.12 本身未单独运行兼容性测试。

## 当前状态与证据（2026-09-11）

| 项目 | 本轮结果 | 证据边界 |
|---|---|---|
| 全量 pytest / Ruff / compileall | PASS | 262 passed，3 warnings；本地夹具和单元测试不代表实站交易通过，详见 VALIDATION |
| 开售许可、任务清理、未知订单保锁 | PASS（本地故障注入） | 未来时间使用可注入时钟；取消/审计失败、停止重入和重启保锁纳入完整回归 |
| Chrome 持久会话 | PASS（本地站） | Cookie/localStorage 关闭后重开仍存在；天猫与淘宝共用阿里会话；不代表真实账户已认证 |
| Apple 受保护演练 | PASS（本轮合成页面） | 正常 Runtime 入口，首次登录/计划/商品/地址确认及恢复后到 READY_TO_SUBMIT；提交与付款点击 0，非实站验收 |
| 本机原 SUCCESS 订单锁 | PASS（保护生效） | 真实配置 dry-run 入口退出 2，原 guard 全部字段摘要未变 |
| 24 期零费用方案 | PASS（本地解析/页面测试） | 核对银行、本金、总额、利息、手续费和完整每期金额；当前 review 重读，缺失为 UNKNOWN；完整实页披露未验证 |
| 地址/国行确认与诊断隐私 | PASS（本地夹具） | 显式确认绑定当前摘要，变更重新确认；诊断最小采集，不保证绝对脱敏 |
| 本机购买计划、登录状态导入、购物袋恢复 | 本地回归，最终结果见 VALIDATION | 计划绑定配置与确认摘要；导入不证明登录，购物袋未知结果不重复加购 |
| 本机状态页 | PASS（HTTP/API） | 8766 已加载本轮代码；未通过浏览器点击 UI 验收 |
| Apple 新品 Continue / 程序实站完整交易 | NOT RUN | Continue 无新实页证据；正常入口受既有 SUCCESS 锁保护，不清锁，不猜流程 |
| JD / Tmall / Taobao 真实交易 | NOT RUN | 本轮不增加平台；京东“立即支付”仍人工处理 |

历史：2026-09-10 曾通过程序读取 [Apple iPhone 18 Pro / Pro Max 商品页](https://www.apple.com.cn/shop/buy-iphone/iphone-18-pro)；普通 Chrome 曾在用户授权下创建一笔微信待付款单，未付款。程序独立 profile 的购物袋/配送存在 404/541 记录。以上均不是本轮实测，旧诊断产物也不满足本轮最小采集规则。

页面当时标注 9 月 12 日晚 8 点接受预购；这只是历史观察，使用前应重新查看官方页面和本机配置。`config.example.yaml` 留空商品 URL 和时间。历史选择器证据见 [Apple 实页记录](docs/apple-live-evidence.md)，没有将历史速度作为抢购性能指标。

## 目录与职责

```text
apple-buy-bot/
  pyproject.toml / requirements.txt / requirements.lock.txt
  README.md / VALIDATION.md / FILES.md
  config/config.example.yaml / config.yaml
  data/profiles/{apple,jd,tmall}/  # 淘宝与天猫共用旧 tmall 目录，仅本机
  data/database.db
  data/purchase-plan.json        # 已批准 Apple 计划与确认摘要，仅本机
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
  src/order/                     # 购买计划、排序、匹配、订单核验、持久化订单锁
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

`login` 由你在显示出的正式版 Chrome 中完成；按 Enter 或关闭窗口后保存 profile。也可在网页选择平台并打开登录。程序不收集密码/验证码、不导出 Cookie；`check-login` 通过各网站已经观察到的账户控件核实认证，打开页面或存在 profile 本身不代表登录成功。Apple 登录诊断区分表单已就绪、组件仍在加载和组件异常；只读这些状态，不读取输入值或私密错误正文。首次登录后可沿用会话，过期或安全验证时再人工处理。

已有本机 Apple 登录状态文件时，可在启动程序前设置可选环境变量 `APPLE_BUY_BOT_STORAGE_STATE_FILE`，指向本机 UTF-8 JSON 文件。支持标准 Playwright `cookies` / `origins`（其中含 `localStorage`）格式，也支持标准 Cookie 数组；仅接收允许的 Apple 来源。导入只补充专用 profile 中缺失的 Cookie 和 localStorage 项，不清空或覆盖已有项。导入成功不等于登录成功，每次运行仍检查真实账户状态。IndexedDB、通行密钥和裸 Token 不支持；状态文件含敏感登录信息，只留本机，不放入配置或仓库，也不要粘贴到对话。

`APPLE_BUY_BOT_USERNAME` / `APPLE_BUY_BOT_PASSWORD` 自动填充在本轮为 **NOT RUN**：缺少已验证的密码和提交控件，当前实现不读取这两个环境变量，也不会尝试填写或猜测登录步骤。验证码、短信和设备验证仍由用户在官网完成。

`run` 使用本机系统时间等待 T−10 分钟准备、T−3 分钟打开商品、T−30 秒复查、T−5 秒就绪、T=0 监测。`dry-run` 与 `run --now` 跳过等待；只有 `dry-run` 强制禁止最终提交。每次运行在准备阶段必须实际检查登录；尚未认证、登录组件加载或异常时暂停，正常登录后才能继续监测。已进入公开监测不反复打开登录页，结算阶段仍重新检查认证和安全验证。

运行控制台输入 `plan` 查看 Apple 购买计划，再输入 `approve-plan <digest>` 批准刚展示的完整摘要版本；这两个是运行中的控制命令，不是独立 CLI 子命令。也可在网页直接核对并批准计划。批准内容包括商品链接、型号、备选容量和颜色、数量、单价及总预算、付款方式和分期银行，保存在本机 `data/purchase-plan.json`；这些购买条件或商品链接变化后重新批准，不会自动打开提交开关。

运行控制台还可输入 `resume [apple|jd|tmall|taobao]`、`status` 或 `stop`。人工处理、待提交或成功待付款期间浏览器保持打开，直到你停止或关窗。提交结果 UNKNOWN 不可通过 resume 再次提交：先人工核查订单历史。出现验证的 inspect 同样等待人工，并在 resume 时只检查现有页面。

首次需要确认时先在官网核对当前收货地址与国行版本，再输入 `confirm-address apple`、`confirm-market apple`（或网页上的同名确认按钮），最后 `resume apple`。Apple 需先批准购买计划；地址与国行确认保存到同一计划，只记录摘要，不保存地址正文。计划和当前页面摘要不变时沿用已有确认，换地址、地址补充字段变化或商品摘要变化后才重新确认。不会把配送地区、店铺名称或中国商店网址自动当作国行版本证明。

`status` 读取 SQLite 历史记录，实时运行状态使用 Web `/status`。`doctor` 用临时无窗口会话验证正式版 Chrome 能否启动并报告版本，不使用账号 profile、不执行真实登录或购买；默认网络是 NOT RUN，`--online` 额外执行一次公开 HEAD 请求。时钟诊断显示本地时间、UTC、目标时间、时区偏移；外部时钟误差未测量，不会声称完成时间同步。

## SKU 与提交保护

全局 `product` 给出型号、容量、颜色的优先顺序及数量、单价上限和可选总预算 `max_total`。每个 `products.<id>` 可覆盖这些限制，且型号须存在于全局型号优先级中。平台 URL 在每个商品下配置，`platforms.<platform>.enabled` 控制启用。京东/天猫/淘宝目标还需 `seller_ids`、`region`、运费上限 `max_shipping` 和其他费用上限 `max_fees`。缺失时暂停，不自动接受任意店铺。

排序为型号 → 容量 → 颜色的字典序。示例中 512GB 黑色优于 512GB 银色，后者优于 256GB 黑色。未列出的选项、未知/不可用库存、非 CNY、超价 SKU 均排除。一个平台的多个型号按优先级轮流检查，单个页面不会被多个协程同时操作。

Apple 逐个选择配置并读取最终商品摘要，不推算未观察组合的报价。`available` 表示已适配的添加购物袋入口可用，不保证最终下单成功；新品 Continue 分支仍为 NOT RUN，按钮启用也不会声明可购。每轮最多核对 64 个组合，保留实际可用备选；仅在加购前明确失效的候选允许冷却和回退。页面结构整体变化或加购/提交结果未知时转人工，不切换候选制造第二单。

Apple 购物袋记录三种状态：`NOT_ATTEMPTED` 表示尚未点击加购，确认计划或国行版本后可恢复首次点击；`ATTEMPTED_UNKNOWN` 表示已尝试但结果未核实，恢复只进入或读取现有购物袋，不再加购、不改数量；`CART_VERIFIED` 仅在实际购物袋的商品、数量、价格和总额完全匹配后成立。重新核验失败会回到未知，不能把“点击完成”当作加购成功。

付款默认 `order.payment_method: installments`、`order.installment_bank: 中国建设银行`，按本次用户偏好设置。Apple 必须读取同一选中方案的银行、24 期、本金、总还款、零利息、零手续费及实际每期还款额。完整计划或前 23 期加明确末期金额须分币相加一致，不能用 0% 年化广告或约月供代替缺失证据。最终 review 重读当前方案；当前实页缺少这些字段时暂停。也可显式配置 `wechat`，创建订单后人工扫码；银行/微信付款操作均不自动执行。

最终提交需要全部成立：

- 配置 `app.dry_run=false`、`order.auto_submit=true`，且不是 dry-run 命令。
- Apple 当前商品、数量、预算和付款条件符合已批准的本机购买计划。
- 单一持久化订单锁属于本流程；平台、商品 ID、SKU ID、型号、容量、颜色全部匹配。
- 币种 CNY、单价与已选 SKU 一致、单价不超过上限，数量正确，实付总额不超过总预算。Apple 总价等于单价 × 数量；市场渠道还核对优惠、运费和其他费用。
- 只有一行目标商品，没有未确认的额外配件/费用；当前地址与本轮本机确认摘要一致，国行版本已独立核验，结算有效、没有安全验证。
- 在点击前再次执行完整 `verify_order()` 和验证检测。

四个渠道使用三个物理会话：Apple、京东、淘宝/天猫。淘宝与天猫串行轮转，天猫优先；开始选择商品后由该渠道保留页面，人工处理期间不能被另一个渠道跳走。race 和 parallel 都在有效核验后取得唯一订单锁，其他平台在下一次业务动作前停止，最终只允许一个提交。

预热成功不等于开售许可。监测、选择、加购、验单及提交均独立检查开售时间，失败重试和人工恢复也不能绕过。只有显式 `--now` 或网页立即演练跳过等待，实际模式写入状态和事件；单独 `dry_run=true` 不取消计划等待。停止或工作者出错时取消并等待所有工作者结束，再释放任务引用；审计失败不会让挂起提交失去管理，取消提交仍保留不确定锁。

市场渠道的结算要求商品、店铺、地区、现货状态与当前报价一致；商品小计−优惠＋运费＋其他费用必须等于实付总额。报价有效期 30 秒，人工处理后读取新的结算报价，不重放加购。24 期方案必须同时有 0 利息、0 服务费和一致的本金/还款总额；广告文案不能作为证明。`allow_post_order_financing_check` 默认关闭，仅在明确配置后允许未知分期留到已创建订单检查；已知有息方案始终阻断。立即支付/开通授信入口不属于自动提交订单。

SQLite 中的单一订单锁可跨进程/重启保留。只有明确 `REJECTED` 才自动释放；超时、取消或缺少确定订单号记为 UNKNOWN，不能自动重试。SUCCESS 也保留。演练 READY_TO_SUBMIT 会保留预约，避免重复运行误购。

重新演练或处置遗留 UNKNOWN 前，先停止程序并关闭使用该 profile 的浏览器，亲自检查平台订单，确认没有已创建或待支付订单后，使用 `status` 中的完整 owner：

```powershell
python -m src.main reconcile "从 status 复制的完整 owner" --confirmed-no-order
```

该命令会检查三个 profile 是否仍被占用；它不查询平台、不取消任何订单，也不会清除 SUCCESS 锁。不要在提交仍可能进行时声称订单不存在。

## 频率、故障与本地数据

Apple 沿用原阶段频率；京东、淘宝、天猫的检查间隔至少 10 秒，可通过平台的 `refresh_interval` 调长。淘宝/天猫按共用会话合计节流。最大检测次数默认 120、最大自动重试 5 次、指数退避上限 30 秒；服务端 `Retry-After` 优先，不能因本地上限提前重试。验证码/风控/未知页面转人工，不进入自动重试。加购结果不明后不会重放点击，恢复时先精确核验现有购物袋。

每次状态变化持久化 timestamp、platform、product、sku、old_state、new_state、message。数据库包括 runs、events、stock_checks、orders 和 order_guard。日志有毫秒时间、动作、耗时、结果及异常类型，配置 URL 去掉查询串；诊断 metadata 仅保留已知公开商品路由，未知/敏感地址省略。stock_check_ms、sku_select_ms、cart_ms、checkout_ms、submit_ms 可从当前状态及日志查看。

profile 包含本机敏感登录状态；配置严禁密码、短信码、支付信息。日志不输出原始网络/浏览器异常正文。登录、账号、安全验证、结算、回执及未知页面只采控件数量；商品页按固定字段、属性和值语法白名单采公开信息，不再保存整页正文或原始 HTML。伴随 HTML 是最小数据生成的报告。截图继续请求文字/媒体遮罩，metadata 明确不是绝对隐私保证；分享前仍需查看本地文件。历史诊断文件未自动清洗。文件名使用 UTC 时间且包含微秒。

data、logs、screenshots、outputs、config.yaml 已在 `.gitignore` 排除，不应分享真实 profile 或未经检查的本机诊断目录。通知使用 Notifier 接口，后续 Webhook/ServerChan/PushPlus 实现可替换 ConsoleNotifier，当前没有外发通知。

## Web API

GET：`/status`、`/platforms`、`/products`、`/purchase-plan`、`/events?limit=100`、`/orders?limit=100`、`/health`。POST：`/start`、`/stop`、`/resume`、`/login`、`/check-login`、`/save-target`、`/check-product`、`/check-checkout`、`/read-order`、`/approve-plan`、`/confirm-address`、`/confirm-market`。计划批准接口必须传当前 `digest` 和 `confirmed:true`；地址和国行确认接口也必须显式传 `confirmed:true`，这些接口均不能启用提交。`/start` 可传 `dry_run:true` 强制演练。接口说明在 `/docs`；拒绝密码或 Token 等额外字段。订单列表只返回订单号后四位，完整引用和提交前摘要留在本机 SQLite。

控制 POST 必须带 `X-Apple-Bot-Control: local`；拒绝跨站 Origin 和非本机 Host，默认只监听 `127.0.0.1:8765`。网页自动刷新只是读取本机状态，每 2 秒一次；可暂停，不会产生商品请求。不得把当前本机控制接口直接转发到公网。

## 下一阶段

淘宝、天猫、京东的接入方案见 [多平台接入设计](docs/marketplace-integration-design.md)。该文档是设计，实际渠道能力仍以逐阶段实页验收为准。

剩余工作是程序自己的持久化浏览器全流程验收、实际支持免息的结算与回执，以及当前受限的淘宝交易页面；未见过的结构不填猜测选择器。已创建的待付款测试单和不明结果都计入订单保护，不自动重试或清锁。数据库升级前自动生成含 WAL 数据的本地备份，迁移保留旧订单锁。

技术参考：[Playwright persistent context](https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context)、[Pydantic models](https://docs.pydantic.dev/latest/concepts/models/)、[Chromium Fetch 导航检查](https://chromedevtools.github.io/devtools-protocol/tot/Fetch/)。
