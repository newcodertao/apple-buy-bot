"""Apple Tmall product components inspected on 2026-09-11; no checkout proof."""

EVIDENCE_STATUS = "PRODUCT_DOM_OBSERVED_2026_09_11; CHECKOUT_NOT_VERIFIED"
SELECTORS: dict[str, str] = {
    "authenticated": 'a.site-nav-login-info-nick[href^="//i.taobao.com/my_itaobao"]',
    "product_name": '[class^="mainTitle--"][title]',
    "product_price": '[class^="highlightPrice--"] [class^="text--"]',
    "price_condition": '[class^="highlightPrice--"] [class^="title--"],'
    '[class^="highlightPrice--"] [class^="desc--"]',
    "shop_link": '.logo.first a[href^="//apple.tmall.com/"]',
    "region": '[class^="deliveryAddrWrap--"] > span',
    "stock": '[class^="shipping--"]',
    "color_options": '[class^="skuItemClipX--"]:has(span[title="机身颜色"]) '
    '[data-vid][data-disabled="false"]',
    "capacity_options": '[class^="skuItemClipX--"]:has(span[title="存储容量"]) '
    '[data-vid][data-disabled="false"]',
    "selected_color": '[class^="skuItemClipX--"]:has(span[title="机身颜色"]) '
    '[data-vid][class*="isSelected--"]',
    "selected_capacity": '[class^="skuItemClipX--"]:has(span[title="存储容量"]) '
    '[data-vid][class*="isSelected--"]',
    "add_to_cart": 'button:has-text("加入购物车")',
}
