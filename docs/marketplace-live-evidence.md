# 多平台实页记录

日期：2026-09-11。仅记录本次通过正式版 Chrome 看见的界面；本地模拟页面不算平台成功证明。

| 渠道 | 商品 | 购物车 | 结算 | 提交与回执 |
|---|---|---|---|---|
| 京东 | LIVE_OBSERVED | LIVE_OBSERVED，只读 | LIVE_OBSERVED，结算与支付合并 | NOT RUN |
| 天猫 | LIVE_OBSERVED | BLOCKED，入口在淘宝域 | NOT RUN | NOT RUN |
| 淘宝 | BLOCKED，浏览器工具拒绝访问 | NOT RUN | NOT RUN | NOT RUN |

## 京东

从京东搜索界面进入公开商品 [iPhone 17 Pro Max 商品页](https://item.jd.com/100278221408.html)。观察到：

- 商品标题 `.sku-title-name`，已选容量/颜色 `.specification-item-sku--selected`，缺货选项 `.specification-item-sku--lack`。
- 商品报价 `.product-price--main .product-price--value`，旁边明确标注“补贴领后价”。该数字不作为已确认实付价。
- `.top-name[title]` 显示 Apple 产品京东自营旗舰店；普通店铺入口打开 [该公开店铺](https://mall.jd.com/index-1000000127.html)。程序通过商品页入口与店铺标题共同绑定店铺 ID。
- 配送组件提供地区 ID 和预计到货文字。实际地区不进入此文档。
- 购物车使用新的 React 行结构 `[data-rowgid]`、`_goodTitle_`、`_cart-number_`、`checkItem`。已有购物车内容保留，未批量勾选、删除或结算这些商品。

点击测试商品的“立即购买”后，网页在 `pc-settlement-lite-pro.pf.jd.com` 的 iframe 中打开“确认订单”，没有创建订单。

- 商品单价/小计为 ¥9,299；商品页的 ¥8,799 是条件补贴价。
- 测试账户显示的 24 期为每期 ¥434.27，并明确包含每期利息；白条显示当前订单不可用。不能据此认定存在 24 期免息方案。
- 最终控件为“立即支付”，而非独立“提交订单”。程序不会点击该支付入口。
- 只读结算检查已接入：返回商品名、当前单价/小计/实付金额、24 期费用、最终按钮含义及暂停原因；不读取或返回银行卡、收货地址与联系方式。

尚未观察到支持独立创建未付款订单的当前京东结算分支。该分支选择器保持缺失，不能声明京东自动下单完成。

## 天猫

正常访问 [Apple Store 天猫官方商品页](https://detail.tmall.com/item.htm?id=974619066443)，页面未跳转到淘宝。选中黑色和 256GB 后 URL 出现公开 `skuId`；实页选择状态使用 `isSelected--` 类。

- 标题使用 `mainTitle--`，规格组 `skuItemClipX--`，选项有 `data-vid` 与 `data-disabled`。
- 店头链接正面指向 `apple.tmall.com`。程序使用该唯一公开域作为卖家标识，而不是把所有“旗舰店”文字当成可信卖家。
- 配送地区只在本机生成摘要；“8 天内发货”不直接推断为正常现货。
- 所见报价为“平台加补后”，广告是“12 期免息”，都不能证明所需的 24 期零费用结算方案。
- 商品检查能够展示公开身份、选中规格、条件价原因。购物车入口属于淘宝域，本轮未通过该入口继续。

## 淘宝限制

浏览器工具明确返回 `Browser use is not permitted on https://www.taobao.com`。本轮没有使用程序、备用浏览器、CDP 或其他接口绕过此拒绝。淘宝渠道已接入配置、共享登录会话、调度和订单保护；实页交易选择器仍未验证。

本轮未新建真实订单、未付款、未删除原购物车商品、未释放既有 Apple SUCCESS 订单锁。
