"""JD desktop product UI inspected in Chrome on 2026-09-11.

Product and cart selectors reflect observed pages. The new checkout window is
read only in JDAdapter; independent order submission and receipt are unverified.
"""

EVIDENCE_STATUS = "PRODUCT_CART_OBSERVED_2026_09_11; CHECKOUT_READ_ONLY; RECEIPT_NOT_RUN"
SELECTORS: dict[str, str] = {
    "authenticated": 'a.nickname[href="//home.jd.com/"]',
    "product_name": ".sku-title-name",
    "product_price": ".product-price--main .product-price--value",
    "price_condition": ".product-price--activity-item--tag",
    "shop_name": ".top-name[title]",
    "region": ".logistics-address-main [data-id]",
    "stock": ".logistics-delivery-time",
    "color_options": '.specification-group:has-text("外观") '
    ".specification-item-sku:not(.specification-item-sku--lack)",
    "capacity_options": '.specification-group:has-text("存储容量") '
    ".specification-item-sku:not(.specification-item-sku--lack)",
    "selected_color": '.specification-group:has-text("外观") .specification-item-sku--selected',
    "selected_capacity": '.specification-group:has-text("存储容量") '
    ".specification-item-sku--selected",
    "selected_purchase": '.specification-group:has-text("购买方式") '
    ".specification-item-sku--selected",
    "product_quantity": "#buy-num",
    "add_to_cart": "#add-to-cart",
    "open_cart": 'a[href="//cart.jd.com/cart.action"]',
    "cart_items": '[data-rowgid]:has(a[class^="_goodTitle_"])',
    "cart_name": 'a[class^="_goodTitle_"]',
    "cart_quantity": '[class^="_cart-number_"] input',
    "cart_selected": 'input[name="checkItem"]',
    "cart_price": '[class^="_price-normal_"]',
    "cart_price_condition": '[class^="_price-icon_"]',
    "checkout": '[class^="_submitBtn_"]',
}
