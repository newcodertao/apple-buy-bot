# apple-buy-bot

面向 Apple 中国大陆官网、京东和天猫官方渠道的本机购买辅助工程。当前交付的是**第一轮工程与可测试的执行框架**：CLI、FastAPI、持久化浏览器、调度、状态机、SKU 排序和订单保护已实现；Apple 实页 inspect 已运行，真实登录判断与购买动作仍为 `UNKNOWN`，**不能将本版本作为已验收的抢购程序使用**。

所有最终支付由用户手动完成。没有验证码识别、滑块破解、短信/人脸/设备验证绕过、隐身插件、代理轮换、批量账号或私有下单接口。

## 快速运行（Windows / PowerShell 7）

本目录已建立 `.venv` 并安装依赖。运行：

```powershell
Set-Location 'D:\Apple\apple-buy-bot'
& .\.venv\Scripts\python.exe -m src.main doctor
& .\.venv\Scripts\python.exe -m src.main web
```

打开 <http://127.0.0.1:8765> 查看状态。Web 服务不自动开始购物操作，点击“启动”才按配置运行。默认配置是 `dry_run: true`、`auto_submit: false`；即使调用方传入 `dry_run=False`，也不能关闭配置中的演练保护。网页/API 不能修改这两个开关。

新环境安装：

```powershell
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
& .\.venv\Scripts\python.exe -m playwright install chromium
& .\.venv\Scripts\python.exe -m src.main init
& .\.venv\Scripts\python.exe -m pytest -v
& .\.venv\Scripts\python.exe -m ruff check .
```

要求 Python 3.12+；本次实测 Python 3.14.5、Playwright 1.62.0。`requirements.lock.txt` 保存本次通过检查的直接/间接依赖版本，可先安装它，再执行 `pip install -e . --no-deps` 复现。Python 3.12 本身未单独运行兼容性测试。

## 当前状态与证据

| 项目 | 本轮结果 | 证据边界 |
|---|---|---|
| 配置、状态机、SKU 排序、SQLite、调度 | PASS | 本机单元/回归测试 |
| Chromium persistent profile | PASS | 本地测试站 Cookie/localStorage 关闭后重开仍存在，平台相互隔离 |
| 手动登录入口 | PASS（机制） | Enter/关闭窗口保存路径均测试；未登录真实账号 |
| Apple 官方商品页 inspect | PASS（实页） | `outputs/apple-public-inspect/` 内 JSON、脱敏 HTML、布局 PNG、metadata |
| Apple 认证、SKU、库存、加购、结算、提交 | UNKNOWN / NOT RUN | 选择器没有实页交易验收；调用将停止并转人工 |
| JD / Tmall | 骨架 | 共用接口及诊断机制；购买选择器为 UNKNOWN，未实页验收 |
| race / parallel 执行 | PASS（模拟） | FakeAdapter 并发测试，不代表平台购买成功 |
| FastAPI / 本机状态页 | PASS | API 测试与真实 Chromium 桌面/手机视口检查 |
| 真实订单、支付、到货 | NOT RUN | 本轮没有加购、结算、创建真实订单或支付 |

2026-09-10 实际通过本程序检查了 [Apple iPhone 18 Pro / Pro Max 商品页](https://www.apple.com.cn/shop/buy-iphone/iphone-18-pro)。本次快照中有 21 个按钮、77 个链接、111 个 role 元素和 225 个含 data 属性的元素。这是页面结构证据，不是控件可点击/交易成功的证明。

页面当时标注 9 月 12 日晚 8 点接受预购，因此**本地** `config/config.yaml` 已填入该时间和同一产品系列的 Apple URL；`config.example.yaml` 仍留空 URL 和时间，避免把一次观察作为永久默认值。使用前应重新查看官方页面。京东、天猫 URL 仍为空。没有沿用历史 iPhone 17 的选择器或速度结论。

## 目录与职责

```text
apple-buy-bot/
  pyproject.toml / requirements.txt / requirements.lock.txt
  README.md / VALIDATION.md / FILES.md
  config/config.example.yaml / config.yaml
  data/profiles/{apple,jd,tmall}/  # 仅本机，不进入版本控制
  data/database.db
  logs/{engine,apple,jd,tmall}.log
  screenshots/                   # 脱敏现场
  outputs/                       # 本轮验收证据
  src/main.py                    # CLI
  src/runtime.py                 # 单一运行生命周期，供 CLI/Web 复用
  src/diagnostics.py             # doctor
  src/core/                      # 配置、模型、异常、状态机、引擎、时钟、调度、日志
  src/browser/                   # Chromium、profile 锁、脱敏页面检查
  src/platforms/base.py           # 统一 Adapter 接口
  src/platforms/{apple_cn,jd,tmall}/  # adapter / selectors / parser
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

`login` 必须由你在显示出的 Chromium 中完成；按 Enter 或关闭窗口后保存 profile。程序不收集密码/验证码，不导出 Cookie，也不因为 profile 存在就报告“登录成功”。本阶段 `check-login` 返回 UNKNOWN，需要 Phase 2 补上经过确认的认证判断。

`run` 使用本机系统时间等待 T−10 分钟准备、T−3 分钟打开商品、T−30 秒复查、T−5 秒就绪、T=0 监测。`dry-run` 与 `run --now` 跳过等待；只有 `dry-run` 强制禁止最终提交。UNKNOWN 认证或选择器会停在 `WAITING_HUMAN`，因此当前真实 Apple 演练不会到达购物袋/结算。

运行控制台输入 `resume [apple|jd|tmall]`、`status` 或 `stop`。验证期间浏览器保持打开，恢复后先重新检查登录/验证状态。提交结果 UNKNOWN 不可通过 resume 再次提交：先人工核查订单历史。出现验证的 inspect 同样等待人工，并在 resume 时只检查现有页面。

`status` 读取 SQLite 历史记录，实时运行状态使用 Web `/status`。`doctor` 不执行真实登录或购买；默认网络是 NOT RUN，`--online` 额外执行一次公开 HEAD 请求。时钟诊断显示本地时间、UTC、目标时间、时区偏移；外部时钟误差未测量，不会声称完成时间同步。

## SKU 与提交保护

全局 `product` 给出型号、容量、颜色的优先顺序及数量、单价上限。每个 `products.<id>` 可覆盖容量、颜色、数量和价格，且型号须存在于全局型号优先级中。平台 URL 在每个商品下配置，`platforms.<platform>.enabled` 控制启用。

排序为型号 → 容量 → 颜色的字典序。示例中 512GB 黑色优于 512GB 银色，后者优于 256GB 黑色。未列出的选项、未知/不可用库存、非 CNY、超价 SKU 均排除。一个平台的多个型号按优先级轮流检查，单个页面不会被多个协程同时操作。

最终提交需要全部成立：

- 配置 `app.dry_run=false`、`order.auto_submit=true`，且不是 dry-run 命令。
- 单一持久化订单锁属于本流程；平台、商品 ID、SKU ID、型号、容量、颜色全部匹配。
- 币种 CNY、单价与已选 SKU 一致、单价不超过上限，数量正确，总价等于单价 × 数量且不超过总预算。
- 只有一行目标商品，没有未确认的额外配件/费用；地址存在、结算有效、没有安全验证。
- 在点击前再次执行完整 `verify_order()` 和验证检测。

race 模式在有效订单核验后取得锁，其他平台暂停；parallel 允许同时准备结算，但最终仍只允许一个提交。锁不是禁限购机制的替代品，平台规则始终由正常购买流程遵守。

SQLite 中的单一订单锁可跨进程/重启保留。只有明确 `REJECTED` 才自动释放；超时、取消或缺少确定订单号记为 UNKNOWN，不能自动重试。SUCCESS 也保留。演练 READY_TO_SUBMIT 会保留预约，避免重复运行误购。

重新演练或处置遗留 UNKNOWN 前，先停止程序并关闭使用该 profile 的浏览器，亲自检查平台订单，确认没有已创建或待支付订单后，使用 `status` 中的完整 owner：

```powershell
python -m src.main reconcile "从 status 复制的完整 owner" --confirmed-no-order
```

该命令会检查三个 profile 是否仍被占用；它不查询平台、不取消任何订单，也不会清除 SUCCESS 锁。不要在提交仍可能进行时声称订单不存在。

## 频率、故障与本地数据

轮询下限 0.3 秒，正常默认 3 秒，开售默认 0.5 秒，带 jitter。最大检测次数默认 120、最大自动重试 5 次、指数退避上限 30 秒；服务端 `Retry-After`（秒数或 HTTP-date）优先，不能因本地上限提前重试。验证码/风控/未知页面转人工，不进入自动重试。加购结果不明后不会自动重放加购点击，恢复时进入结算核对数量。

每次状态变化持久化 timestamp、platform、product、sku、old_state、new_state、message。数据库包括 runs、events、stock_checks、orders 和 order_guard。日志有毫秒时间、动作、耗时、结果及异常类型，配置 URL 去掉查询串；快照 metadata 记录实际页面 URL。stock_check_ms、sku_select_ms、cart_ms、checkout_ms、submit_ms 可从当前状态及日志查看。

profile 包含本机敏感登录状态；配置严禁密码、短信码、支付信息。日志不输出原始网络/浏览器异常正文。诊断 HTML 去除脚本、隐藏内容、输入值和敏感属性，data 属性保留名称并隐藏值；截图只保留布局，文字及媒体全部屏蔽。故障复盘结合脱敏 DOM 与 metadata，不把黑色遮罩当作原始页面。文件名用 UTC 时间且包含微秒，metadata 也显式标注 UTC。

data、logs、screenshots、outputs、config.yaml 已在 `.gitignore` 排除，不应分享真实 profile 或未经检查的本机诊断目录。通知使用 Notifier 接口，后续 Webhook/ServerChan/PushPlus 实现可替换 ConsoleNotifier，当前没有外发通知。

## Web API

GET：`/status`、`/platforms`、`/products`、`/events?limit=100`、`/orders?limit=100`、`/health`。POST：`/start`（JSON `{}` 或 platforms/immediate）、`/stop`、`/resume`（JSON `{}` 或 platform）。接口说明在 `/docs`。

控制 POST 必须带 `X-Apple-Bot-Control: local`；拒绝跨站 Origin 和非本机 Host，默认只监听 `127.0.0.1:8765`。网页自动刷新只是读取本机状态，每 2 秒一次；可暂停，不会产生商品请求。不得把当前本机控制接口直接转发到公网。

## 下一阶段

按照你的开发顺序，先把 Apple 做完整：通过真实页面检查确定登录证明、SKU 控件与组合、库存/配送/取货含义，再逐步验证选择 → 购物袋 → 结算。每一步都补本地 fixture 与不提交的实页验收，核对全部订单字段，最后才评估提交动作。第一阶段选择器全部明确 UNKNOWN，不能仅凭网页文本或一次点击把它们改成“有效”。之后再开发 JD、Tmall 的真实购买流程。

技术参考：[Playwright persistent context](https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context)、[Pydantic models](https://docs.pydantic.dev/latest/concepts/models/)、[Chromium Fetch 导航检查](https://chromedevtools.github.io/devtools-protocol/tot/Fetch/)。
