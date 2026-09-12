# Apple 中国大陆页面观察与适配边界

## 2026-09-12 本轮：普通 Chrome 公开商品页

约 13:35（Asia/Shanghai），使用 Chrome 扩展连接打开 [iPhone 18 Pro 商品页](https://www.apple.com.cn/shop/buy-iphone/iphone-18-pro)，通过页面可见 radio 依次选择 Pro Max、黑色、512GB、不折抵换购、不加 AppleCare+ 服务计划。不是程序专用 profile，也没有调用 Adapter 替代普通 Runtime 验收。

页面最终摘要为 **iPhone 18 Pro Max 512GB 黑色，RMB 12,999**；五组选项均选中，配送仍为“暂未发售”。页面提示“9 月 12 日晚 8 点接受预购。下周五 18 日发售。”继续按钮实测 `BUTTON[data-autom="continueButton"]`、`type="button"`、`disabled=true`。没有点击或启用该按钮，没有修改购物袋；页头原来已有 1 件标记。

这次新增证据确认：完成全部规格选择后，Continue 仍不可用。因此 **Continue 启用后的页面、加购及结算为 NOT RUN**；不根据禁用按钮虚构下一页选择器。程序实际登录窗口已打开，但用户反馈持续转圈，尚未认证，单独记录于 VALIDATION.md。

## 历史：2026-09-10

以下观察不是本轮执行结果。所有交易页面均经用户当时授权在 Chrome 正常操作；没有直接调用下单接口。下列结构来自当时页面 DOM。随机 React ID、订单标识、账户和地址均不列入仓库。

## 商品配置

页面：[iPhone 18 Pro](https://www.apple.com.cn/shop/buy-iphone/iphone-18-pro) 与 [iPhone 17](https://www.apple.com.cn/shop/buy-iphone/iphone-17)。

| 元素 | 页面标记 | 判定 |
|---|---|---|
| 型号、颜色、容量 | input name: dimensionScreensize / dimensionColor / dimensionCapacity | 真实选中项和标签；单型号页可以没有型号 radio |
| 不折抵 | noTradeIn | 明确选中，不自动添加折抵设备 |
| 不加保障计划 | applecare-options 的“不加 AppleCare+ 服务计划”标签 | 不依赖两项相同的 value |
| 最终商品名、全价 | summary-productName / full-price | 单一摘要与配置相符，严格读取全价 |
| 配送、取货 | dudeInfo 或 deliveryQuotes / pickUpDetails | 页面使用两种配送结构；无控件不能推断配送可用 |
| 新品下一步 | continueButton | 当前禁用；启用后的分支仍 UNKNOWN |
| 在售加购 | add-to-cart | 原生 submit；只尝试一次，加购前先检查空袋 |
| 加购后查看购物袋 | proceed | 经 Chrome 实测；程序独立 profile 在此前跳转失败 |

radio 使用正常键盘 Space，购物袋菜单使用 Enter；避免 Apple 覆盖式标签/悬浮栏截住鼠标以及菜单 hover 与 click 相互切换。没有强制点击、隐藏字段注入或绕过验证。

## 购物袋与结算

购物袋容器 bag-content；bag-items 的直接 li 子项；bag-item-name、item-quantity-dropdown、Monthly_price、bagtotalvalue 与 bag-error-message。Monthly_price 在该页面显示商品行总额，并非月付金额。只接受一行目标商品与页面允许的 1 至 2 件，数量 2 通过下拉框改变，不能重复加购。

checkout 按钮为 shoppingCart.actions.checkout。进入 /shop/signIn 时交由用户登录；验证页面同样暂停。/shop/checkout 依次观察到：

1. fulfillment-continue-button：继续填写送货地址。
2. 已选 saved-address 与 shipping-continue-button：继续选择付款方式。联系方式可能已保存并脱敏，交由官网验证。
3. checkout-billingOptions-WECHAT 与银行分期 radio；continue-button-review：继续。
4. 回顾页 rs-iteminfo-title、rs-quantity-text、行总额、bagtotalvalue、可见地址摘要、rs-review-billing-details；continue-button-placeOrder：立即下单。

认证的肯定证据为官网退出登录入口。存在 profile、曾打开登录页或者用户在另一浏览器登录，均不是当前程序会话已认证的证明。

## 24 期免息

中国建设银行通过 radio 关联 label 图片的 alt 识别。银行 data-autom 为 checkout-billingOptions-installments 加数字标识；从实际选中控件推导同组 -24 期数项，不把数字当作永久银行 ID。

实际观察的 24 期标签显示 0% 年化利率、约 RMB 284/月、总计 RMB 6,799。代码要求 24 期、0% 年化利率和全额总计同时匹配；不凭宣传中的“最长 24 期”判定。回顾页再次确认银行和 24 个月。没有合格方案时交由人工处理，不自动改用有息方案。

微信分支在 /shop/checkout/thankyou 出现 rs-qr-ordernumber、rs-qr-header 和等待扫码提示，证实一笔待付款测试单。分期提交后的银行回执未观察；如开启提交而遇到未知回执，代码记 UNKNOWN 并保留锁，不能自动跳转付款或重试订单。

## 程序自己的实测差异

目标新品 SKU 读取成功。在售对照的独立程序 profile 能配置商品，但配送组件自身请求报 HTTP 541；加购后进入 404 页面。显示窗口与无窗口模式都未通过，不能简单归因于无窗口模式。没有据此伪造配送信息、请求或浏览器身份。已加入配送内容缺失时暂停的防护，并确认失败测试 profile 的购物袋为空。

这组失败与 Chrome 成功路径必须同时保留在验收结论中。日期以后页面变化、真实程序登录复用、新品预购后续页面及分期订单回执都需要重新实测。
