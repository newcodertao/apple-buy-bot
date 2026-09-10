# 第一轮交付验收

日期：2026-09-10（Asia/Shanghai）。目录：`D:\Apple\apple-buy-bot`。

## 总结果

**第一轮工程检查 PASS；真实购买验收 NOT RUN。**

- Ruff：`ruff check .` → **PASS**。
- pytest：`pytest -v`（通过虚拟环境 Python 模块执行）→ **141 passed，0 failed，0 skipped，32.35 秒**。
- pip 依赖一致性检查 → **PASS**。
- doctor → Python、Playwright、Chromium、配置、数据库、已填商品 URL、公共网络连接均 PASS；真实账号登录 UNKNOWN。
- Web 六个 GET 接口均 HTTP 200，Chromium 桌面 1280×900 / 手机 375×812 无页面错误或整页横向溢出；刷新暂停/恢复正常。
- Apple 真实商品页只读 inspect → PASS，未进行交易动作。

pytest 有 2 条上游弃用提示：Starlette TestClient 的 httpx 兼容路径和 AnyIO BlockingPortal 别名。未隐藏警告，不影响这次通过结果。

## 原始证据

| 文件 | 内容 |
|---|---|
| `outputs/ruff-final.txt` | 最终 Ruff 输出 |
| `outputs/pytest-final.txt` | 全量逐项 pytest 输出及警告 |
| `outputs/pytest-final.xml` | JUnit 机器可读结果 |
| `outputs/doctor-final.json` | 依赖、数据库、profile、时钟及公共网络诊断 |
| `outputs/web-smoke.json` | 真实本机 Web 检查结果 |
| `outputs/web-desktop.png` / `web-mobile.png` | 目视检查过的最终页面 |
| `outputs/web_smoke.py` | 本机只读 Web 检查复跑工具，不启动购物流程 |
| `outputs/apple-public-inspect/20260910_120644_692287_apple_inspect.json` | 实页控件清单与脱敏文本 |
| 同名 `.html` / `.png` / `.metadata.json` | 脱敏 DOM、布局截图、时间/URL |
| `requirements.lock.txt` | 本次环境依赖版本 |

## 覆盖范围

| 测试领域 | 已验证内容 |
|---|---|
| 配置 | 默认双重提交保护、URL 域/协议校验、未知/敏感配置键拒绝、非法价格/优先级/时区/频率拒绝、商品级覆盖 |
| SKU | 型号→容量→颜色排序，未知选项/无货/错误币种/超价排除 |
| 状态与存储 | 非法状态跃迁拒绝、记录失败不推进状态、审计持久化、六个独立 SQLite 连接只能有一个抢到锁 |
| 订单字段 | 错 SKU/型号/容量/颜色/平台/商品/数量/币种/价格/额外商品/地址缺失/验证页面全部拒绝提交 |
| 提交 | dry_run/auto_submit 的全部组合，配置演练不能被调用参数关闭，提交前第二次核对防止页面变化 |
| 并发恢复 | race/parallel 只能提交一次，确认失败后另一平台继续，停止/取消/重启不造成重复提交 |
| 不确定结果 | 提交超时、崩溃、缺少有效订单号均 UNKNOWN，锁跨重启保留，resume 不重复提交 |
| 人工接管 | 暂停期间无继续点击；resume 重查；加购结果不明不重放；已结束 UNKNOWN 工作者仍保持浏览器直到人工停止/关闭 |
| 锁核查 | profile 被占用时拒绝 reconcile；关闭后且人工确认无订单才清理；SUCCESS 保留 |
| 调度与节流 | 时区、假时钟、短等待可停止、指数退避、服务端 Retry-After 秒数/HTTP-date 不提前重试 |
| 真 Chromium | Cookie/localStorage 跨关闭重开、平台隔离、OS profile 锁、脱敏 inspect、未知选择器不点击、挑战检测、跨域重定向在请求前拒绝 |
| 登录机制 | Enter/关闭浏览器两种保存路径使用真实本地 Chromium 验证；为了不弹窗，测试子类实际 headless，生产函数要求 visible |
| CLI/Web | 所有请求命令解析、init 不覆盖、历史 status、无 URL 拒绝启动、重复启动/立即停止、同源/本机控制限制 |

## Apple 实页边界

当前页面：[选购 iPhone 18 Pro / Pro Max](https://www.apple.com.cn/shop/buy-iphone/iphone-18-pro)。本程序于 2026-09-10 12:06:44 UTC 完成只读检查，输出 21 buttons、77 links、111 role 元素、225 个含 data 属性的元素。实际 URL 和 UTC 时间保存在 metadata。

已能实际使用：启动独立 Chromium profile、打开官方公开页面、读取脱敏页面结构、输出 JSON/HTML/布局截图。登录保存机制通过本地站点验收，**并未登录真实 Apple 账号**。

以下选择器/语义仍全部 UNKNOWN：authenticated、model、capacity、color、price、availability、delivery、pickup、continue、add_to_bag、bag、checkout、order_review、submit_order、order_confirmation。

当前 `get_skus` / `check_stock` / 选择 / 加购 / 结算 / 验单 / 提交均明确停止，没有用假数据冒充实页结果。JD、Tmall 保留相同接口骨架，真实页面未验收。真实下单速度、开售拥堵、送达时间及成功率均 NOT RUN。

## 下一步

先执行 Phase 2：根据当前真实 Apple 页面，逐项建立有日期证据的选择器与完整 SKU/订单解析，再进行用户本机登录及不提交的购物袋/结算验收。通过之前，继续保持 `dry_run=true`、`auto_submit=false`。之后才进入 JD/Tmall 真实适配。
