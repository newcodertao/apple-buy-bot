"""Visible DOM verified in Chrome on 2026-09-10; see docs/apple-live-evidence.md.

Random React IDs and bag item UUIDs are deliberately excluded.
Installment confirmation and the pre-order Continue branch still need validation.
"""

SELECTORS: dict[str, str | None] = {
    "root": "#root",
    "model": 'input[name="dimensionScreensize"]',
    "color": 'input[name="dimensionColor"]',
    "capacity": 'input[name="dimensionCapacity"]',
    "no_trade_in": "#noTradeIn",
    "applecare": 'input[name="applecare-options"]',
    "summary": '[data-autom="summary-productName"]',
    "price": '[data-autom="full-price"]',
    "delivery": '[data-autom="dudeInfo"], [data-autom="deliveryQuotes"]',
    "pickup": '[data-autom="pickUpDetails"]',
    "continue": '[data-autom="continueButton"]',
    "add_to_bag": '[data-autom="add-to-cart"]',
    "view_bag": '[data-autom="proceed"]',
    "bag_menu": "#globalnav-menubutton-link-bag",
    "bag": "#bag-content",
    "bag_items": '[data-autom="bag-items"] > li',
    "bag_name": '[data-autom="bag-item-name"]',
    "bag_quantity": '[data-autom="item-quantity-dropdown"]',
    "bag_price": '[data-autom="Monthly_price"]',
    "bag_total": '[data-autom="bagtotalvalue"]',
    "bag_error": '[data-autom="bag-error-message"]',
    "checkout": '[id="shoppingCart.actions.checkout"]',
    "signin": "#signin-container",
    "authenticated": '#globalnav a[href*="/shop/signOut"]',
    "login_link": '#globalnav a[href*="/shop/signIn"]',
    "fulfillment_continue": '[data-autom="fulfillment-continue-button"]',
    "shipping_continue": '[data-autom="shipping-continue-button"]',
    "saved_address": '[data-autom="saved-address"]',
    "payment_wechat": '[data-autom="checkout-billingOptions-WECHAT"]',
    "installment_options": 'input[data-autom^="checkout-billingOptions-installments"]',
    "change_payment": '[data-autom="billing-changeLink"]',
    "review_payment_details": ".rs-review-billing-details",
    "review_continue": '[data-autom="continue-button-review"]',
    "review_name": "h2.rs-iteminfo-title",
    "review_quantity": ".rs-quantity-text",
    "review_payment": ".rs-review-billing-details img",
    "order_review": ".rs-review-billing",
    "submit_order": '[data-autom="continue-button-placeOrder"]',
    "order_confirmation": ".rs-qr-ordernumber",
    "payment_heading": ".rs-qr-header",
}
NO_APPLECARE_LABEL = "不加 AppleCare+ 服务计划"
LOGIN_URL = "https://secure.www.apple.com.cn/shop/account/home"
EVIDENCE_STATUS = "CHROME_UNPAID_ORDER_VERIFIED_2026_09_10"
LIVE_VALIDATION = {
    "product": "LIVE_OBSERVED",
    "cart": "LIVE_OBSERVED",
    "checkout": "LIVE_OBSERVED",
    "receipt": "WECHAT_ONLY_LIVE_OBSERVED",
    "continue": "NOT_RUN",
    "installment_full_disclosure": "NOT_RUN",
}
ADDRESS_FIELDS = ("lastName", "firstName", "state", "city", "district", "street", "countryCode")

# Only rendered product controls. No cookies, page stores or private APIs.
PRODUCT_SNAPSHOT = r"""(s) => {
    const visible = e => e && e.getClientRects().length
        && getComputedStyle(e).visibility !== 'hidden';
    const texts = key => Array.from(document.querySelectorAll(s[key]))
        .filter(visible).map(e=>e.innerText);
    const options = key => Array.from(document.querySelectorAll(s[key])).map(e=>({
        value:e.value, checked:e.checked, disabled:e.disabled,
        label:Array.from(e.labels||[]).filter(visible).map(x=>x.innerText).join(' ')
    })).filter(e=>e.label);
    const button = key => Array.from(document.querySelectorAll(s[key])).filter(visible)
        .map(e=>({text:e.innerText,
            enabled:!e.disabled && e.getAttribute('aria-disabled')!=='true'}));
    return {models:options('model'), colors:options('color'), capacities:options('capacity'),
        summary:texts('summary'), price:texts('price'),
        delivery:texts('delivery'), pickup:texts('pickup'),
        add:button('add_to_bag'), next:button('continue'),
        no_trade_in:document.querySelector(s.no_trade_in)?.checked===true,
        care:options('applecare').filter(e=>e.checked).map(e=>e.label)};
}"""
