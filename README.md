# apple-buy-bot

当前开发范围为 **淘宝、京东、Apple 中国大陆**。浏览器扩展连接你主动选择的日常 Chrome/Edge 标签页，复用该页登录；本机 Python 程序继续负责开售时间、候选优先级、预算、购物车核验和订单锁。不是三个平台均已完成实站抢购，实际缺口见下表与 [VALIDATION.md](VALIDATION.md)。

## 使用浏览器扩展

1. 安装本项目依赖：`.venv\Scripts\python.exe -m pip install -r requirements.txt`。
2. 本机 `config/config.yaml` 设置 `app.browser: extension`。开发保持 `app.dry_run: true`、`order.auto_submit: false`。示例只提供三个平台，商品链接和开售时间须根据实际页面填写。
3. 启动普通 Web 入口：`.venv\Scripts\python.exe -m src.main web --port 8766`。不要与旧服务重复占用端口；旧任务有待核对页面时先保留它。
4. 在日常 Chrome 的扩展管理页（Edge 使用自己的扩展管理页）加载已解压的 `D:\Apple\apple-buy-bot\extension` 文件夹。需要 Chrome/Edge 125+，不降级、不复制默认用户资料。完整说明见 [扩展说明](extension/README.md)。
5. 控制台点击“生成本机连接码”。到已登录的购物标签页打开扩展，填写控制台地址和连接码，点击连接。连接码仅在本机使用，不发聊天、不提交仓库。
6. 回到本机控制台选择对应平台，点击“检查登录”；检查页面真实登录状态后再按本次条件开始。验证码、短信和设备确认由你完成，随后点击“人工处理后继续”。

扩展每次只绑定一个平台的一张标签页，连接本身不启动购买。浏览器正常显示调试连接提示。扩展不能自动接管新弹窗或不在范围内的页面，遇到未适配分支会停下。普通浏览器中的其他标签页不会暴露给程序。

“结束本机任务”停止引擎并断开扩展，保留你的页面、历史订单和现有加购/未知提交保护。断线不等于没有执行，程序不会重新发送旧动作。已有 SUCCESS/UNKNOWN/SUBMITTING guard 仍阻止新订单，也不会因切换平台、重新连接、批准购买条件而清除。

扩展当前使用 **Web → Runtime → Engine → 原 Adapter** 入口。直接执行 `run`、`login` 等独立 CLI 进程不能复用 Web 配对，会提示改用 Web；`init/status/doctor` 仍可用。旧 `chrome`/`msedge` 专用 profile 模式为兼容已有使用和历史核对保留。

## 实际适配边界

| 平台/功能 | 当前范围与限制 |
|---|---|
| 扩展连接 | 本机配对、选定标签、独立导航保护、断线不重放；本轮本地与实站证据分别记录 |
| Apple | 沿用已有商品、规格、购物袋、结算核验；新品启用后的 Continue 仍缺真实页面证据，没有宣称已适配 |
| 京东 | 已有商品/购物车字段不完整；新版确认订单包含立即付款，继续保持人工处理；不能宣称自动交易已通过 |
| 淘宝 | 用户希望从已有购物车商品开始，避开无法在电脑打开的详情页；该入口尚未实现。读取真实购物车被浏览器工具站点安全策略拒绝，未绕行采集或猜测生产选择器 |
| 天猫历史记录 | 不作为本轮扩展平台；原数据、适配代码和跨平台订单保护保留 |

开发默认不提交订单。正式提交必须同时在本机明确配置关闭 dry-run、开启 auto_submit，并通过原有订单核验与单次提交保护；网页/API不能打开提交。最终支付仍由你完成。无验证码破解、代理轮换、私有下单接口或 Cookie 导出。

数量、预算仍按本机配置执行；空容量/颜色优先列表表示接受实际读取到的任何规格，不能接受未知规格。当前授权为 iPhone 18 系列、1 件、总额不高于 ¥15,000，具体型号入口只能来自实际页面。

## 开发验证

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\python.exe -m compileall -q src tests
node --test extension/bridge.test.mjs
```

真实扩展集成测试仅使用临时 Chromium、临时数据库和本地 HTML，并未访问真实账户或创建订单。测试用弹出页不产生浏览器工具栏的 activeTab 手势，因此仅临时测试副本增加合成页面域权限；发布的 manifest 不包含购物站点常驻权限。程序专用 profile、日常 Chrome、扩展与合成页面的记录严格分开。

以下保留已有目录和兼容模式说明；本轮支持范围以上述三平台与验收记录为准。

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

以下为专用 profile 模式的兼容命令；扩展模式使用上方 Web 入口。已激活虚拟环境时可以写 `python`，否则使用 `& .\.venv\Scripts\python.exe`。

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

`login` 由你在显示出的所选浏览器中完成；按 Enter 或关闭窗口后保存 profile。也可在网页选择平台并打开登录。程序不收集密码/验证码、不导出 Cookie；`check-login` 通过各网站已经观察到的账户控件核实认证，打开页面或存在 profile 本身不代表登录成功。Apple 登录诊断区分表单已就绪、组件仍在加载和组件异常；只读这些状态，不读取输入值或私密错误正文。首次登录后可沿用会话，过期或安全验证时再人工处理。

已有本机 Apple 登录状态文件时，可在启动程序前设置可选环境变量 `APPLE_BUY_BOT_STORAGE_STATE_FILE`，指向本机 UTF-8 JSON 文件。支持标准 Playwright `cookies` / `origins`（其中含 `localStorage`）格式，也支持标准 Cookie 数组；仅接收允许的 Apple 来源。导入只补充专用 profile 中缺失的 Cookie 和 localStorage 项，不清空或覆盖已有项。导入成功不等于登录成功，每次运行仍检查真实账户状态。IndexedDB、通行密钥和裸 Token 不支持；状态文件含敏感登录信息，只留本机，不放入配置或仓库，也不要粘贴到对话。

`APPLE_BUY_BOT_USERNAME` / `APPLE_BUY_BOT_PASSWORD` 自动填充在本轮为 **NOT RUN**：缺少已验证的密码和提交控件，当前实现不读取这两个环境变量，也不会尝试填写或猜测登录步骤。验证码、短信和设备验证仍由用户在官网完成。

`run` 使用本机系统时间等待 T−10 分钟准备、T−3 分钟打开商品、T−30 秒复查、T−5 秒就绪、T=0 监测。`dry-run` 与 `run --now` 跳过等待；只有 `dry-run` 强制禁止最终提交。每次运行在准备阶段必须实际检查登录；尚未认证、登录组件加载或异常时暂停，正常登录后才能继续监测。已进入公开监测不反复打开登录页，结算阶段仍重新检查认证和安全验证。

`run` / `dry-run apple` 在启动工作者前展示 Apple 购买条件，首次需由你在本机输入 `confirm-start <digest>`，其中 digest 使用刚展示的完整版本；输入 `plan` 重看，`stop` 退出。这些是控制台交互命令，不是独立子命令。已有相同条件的确认时直接复用；既有订单锁或会话保护会在等待新确认前报出。网页将这次确认合并在“开始此平台”或“立即演练此平台”按钮中。

确认内容包括商品链接、型号、备选容量和颜色、数量、单价及总预算、付款方式和分期银行，以及“中国大陆 Apple 官网直售配置商品、使用当前账户结算选中的已保存地址”的依据，保存在本机 `data/purchase-plan.json`。首次实际完整读取地址后绑定本地摘要；确认开始并不表示程序已经读过地址。条件、商品链接或已绑定地址变化时才重新确认，不会自动打开提交开关。

运行中可输入 `resume [apple|jd|tmall|taobao]`、`status`、`finish` 或 `stop`。`finish` 正常结束本机自动任务，保留浏览器页面、订单记录、加购尝试与订单锁；它不取消订单、不解除待付款或未知订单保护，也不保证可以开始新购买。Web 的“结束本机任务”含义相同。CLI 的 `stop` 退出并关闭程序浏览器；Web 的“停止自动动作”只停止工作者。提交结果 UNKNOWN 不可通过 resume 再次提交，须先人工核查订单历史。

仅当页面提示内容变化或依据缺失时，核对后使用 `confirm-address apple`、`confirm-market apple`，再 `resume apple`；网页将兼容入口收在默认关闭的“实际内容变化后的处理”中。没有变化的正常登录或验证码处理直接继续。地址只保存摘要，不保存正文。每次实际读取商品仍需符合已批准的 Apple CN 官方直售配置；该批准依据不声称已经读取独立的硬件版本标识，也不把商城配送地区或卖家 ID 当作国行证明。

`status` 读取 SQLite 历史记录，实时运行状态使用 Web `/status`。`doctor` 用临时无窗口会话验证所选浏览器能否启动并报告版本，不使用账号 profile、不执行真实登录或购买；默认网络是 NOT RUN，`--online` 额外执行一次公开 HEAD 请求。时钟诊断显示本地时间、UTC、目标时间、时区偏移；外部时钟误差未测量，不会声称完成时间同步。

## SKU 与提交保护

全局 `product` 给出型号、容量、颜色的优先顺序及数量、单价上限和可选总预算 `max_total`。每个 `products.<id>` 可覆盖这些限制，且型号须存在于全局型号优先级中。平台 URL 在每个商品下配置，`platforms.<platform>.enabled` 控制启用。京东/天猫/淘宝目标还需 `seller_ids`、`region`、运费上限 `max_shipping` 和其他费用上限 `max_fees`。缺失时暂停，不自动接受任意店铺。

`capacity_priority: []`、`color_priority: []` 分别表示容量、颜色不限，仍须从当前页面读取明确规格，并符合型号、数量及总预算。Apple 只从当前可见选项寻找首个符合条件的组合；“不限”不会接受空白或无法识别的规格，也不会取消已有加购/订单保护。非空列表仍按原有严格优先级匹配。

排序为型号 → 容量 → 颜色的字典序。示例中 512GB 黑色优于 512GB 银色，后者优于 256GB 黑色。未列出的选项、未知/不可用库存、非 CNY、超价 SKU 均排除。一个平台的多个型号按优先级轮流检查，单个页面不会被多个协程同时操作。

Apple 按优先级逐个选择配置并读取实际摘要，找到首个库存、规格和预算均符合条件的 SKU 就立即返回并继续，不扫描完所有备选，也不推算未观察组合的报价。`available` 表示已适配的添加购物袋入口可用，不保证最终下单成功；新品 Continue 启用后的路径仍为 NOT RUN。只有加购前明确失效、且没有未知副作用的候选允许冷却并检查下一候选。页面结构整体变化或加购/提交结果未知时转人工，不切换候选制造第二单。

Apple 购物袋记录三种状态：`NOT_ATTEMPTED` 表示尚未点击加购，人工处理完成后可恢复首次点击；`ATTEMPTED_UNKNOWN` 表示已尝试但结果未核实，恢复只进入或读取现有购物袋，不再加购、不改数量；`CART_VERIFIED` 仅在实际购物袋的商品、数量、价格和总额完全匹配后成立。重新核验失败会回到未知，不能把“点击完成”当作加购成功。

付款默认 `order.payment_method: installments`、`order.installment_bank: 中国建设银行`，按本次用户偏好设置。Apple 必须读取同一选中方案的银行、24 期、本金、总还款、零利息、零手续费及实际每期还款额。完整计划或前 23 期加明确末期金额须分币相加一致，不能用 0% 年化广告或约月供代替缺失证据。最终 review 重读当前方案；当前实页缺少这些字段时暂停。也可显式配置 `wechat`，创建订单后人工扫码；银行/微信付款操作均不自动执行。

最终提交需要全部成立：

- 配置 `app.dry_run=false`、`order.auto_submit=true`，且不是 dry-run 命令。
- Apple 当前商品、数量、预算和付款条件符合已批准的本机购买计划。
- 单一持久化订单锁属于本流程；平台、商品 ID、SKU ID、型号、容量、颜色全部匹配。
- 币种 CNY、单价与已选 SKU 一致、单价不超过上限，数量正确，实付总额不超过总预算。Apple 总价等于单价 × 数量；市场渠道还核对优惠、运费和其他费用。
- 只有一行目标商品，没有未确认的额外配件/费用；当前地址与本轮绑定摘要一致，商品符合已批准的 Apple CN 官方直售依据或实际人工核验依据，结算有效、没有安全验证。
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

GET：`/status`、`/platforms`、`/products`、`/purchase-plan`、`/events?limit=100`、`/orders?limit=100`、`/health`。POST：`/start`、`/stop`、`/finish-task`、`/resume`、`/login`、`/check-login`、`/save-target`、`/check-product`、`/check-checkout`、`/read-order`、`/approve-plan`、`/confirm-address`、`/confirm-market`。网页开始前的单次确认沿用 `/approve-plan`，必须传当前 `digest` 和 `confirmed:true`；后两个确认接口保留给内容变化后的处理，不能启用提交。`/start` 可传 `dry_run:true` 强制演练；`/finish-task` 结束本机任务并保留页面、记录和保护。接口说明在 `/docs`；拒绝密码或 Token 等额外字段。订单列表只返回订单号后四位，完整引用和提交前摘要留在本机 SQLite。

控制 POST 必须带 `X-Apple-Bot-Control: local`；拒绝跨站 Origin 和非本机 Host，默认只监听 `127.0.0.1:8765`。网页自动刷新只是读取本机状态，每 2 秒一次；可暂停，不会产生商品请求。不得把当前本机控制接口直接转发到公网。

## 下一阶段

当前目标仅为 Apple：程序浏览器手动登录一次并复用会话，从正常 CLI/Web 入口到真实结算页。优先补齐新品 Continue 启用后的真实路径；未看到的结构不填猜测选择器。开发演练不提交，正式提交需用户本机显式配置，最终付款手动完成。

淘宝、天猫、京东的原接入设计保留在 [多平台接入设计](docs/marketplace-integration-design.md)，本轮不扩展其能力。已创建的待付款单和不明结果都计入订单保护，不自动重试或清锁。数据库升级前自动生成含 WAL 数据的本地备份，迁移保留旧订单锁。

技术参考：[Playwright persistent context](https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context)、[Pydantic models](https://docs.pydantic.dev/latest/concepts/models/)、[Chromium Fetch 导航检查](https://chromedevtools.github.io/devtools-protocol/tot/Fetch/)。
