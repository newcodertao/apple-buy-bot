"""Capture real local-browser diagnostics without exporting fixture personal data."""

import json
from urllib.parse import unquote

import pytest

from src.browser.inspection import capture_diagnostics
from src.browser.manager import BrowserManager
from src.core.models import Platform

pytestmark = pytest.mark.browser

PRIVATE = (
    "陈明远",
    "浙江省宁波市海曙区测试街88号",
    "482917",
    "赵晨曦",
    "广东省珠海市香洲区示例路66号",
    "fixture-person@example.test",
    "13800138000",
    "URL_PRIVATE_ACCOUNT",
    "TITLE_PRIVATE_ACCOUNT",
    "ACCOUNT_DYNAMIC_123",
)
PERSONAL_HTML = """
<title>TITLE_PRIVATE_ACCOUNT</title><body>
<div>陈明远</div><div>浙江省宁波市海曙区测试街88号</div><div>482917</div>
<button aria-label="赵晨曦" aria-labelledby="ACCOUNT_DYNAMIC_123">继续</button>
<section id="harmless" class="product-summary">
  <span>广东省珠海市</span><span>香洲区示例路66号</span>
  <span>fixture-person</span><span>@example.test</span>
  <span>138</span><span>0013</span><span>8000</span>
</section>
<a href="/account/URL_PRIVATE_ACCOUNT">个人中心</a>
<p data-testid="ACCOUNT_DYNAMIC_123" data-customer-name="陈明远">帐号资料</p>
<input value="陈明远" type="text"><input value="482917" autocomplete="one-time-code">
<div role="status" data-autom="full-price">赵晨曦</div>
<script>document.body.dataset.private = 'ACCOUNT_DYNAMIC_123'</script>
</body>"""
PRODUCT_HTML = """
<div data-autom="summary-productName">iPhone 18 Pro Max 512GB 黑色</div>
<div data-autom="full-price">RMB 12,999</div>
<input type="radio" name="dimensionCapacity" id="safe-capacity" value="512gb" checked>
<label for="safe-capacity">512GB</label>
<button data-autom="add-to-cart">添加到购物袋</button>
<button data-testid="ACCOUNT_DYNAMIC_123">赵晨曦</button>
"""


@pytest.mark.parametrize("kind", ["checkout", "unknown", "product", "product_challenge"])
async def test_capture_excludes_unlabelled_and_split_private_values(tmp_path, kind):
    manager = BrowserManager(tmp_path / "profiles", headless=True)
    try:
        page = await manager.open(Platform.APPLE)
        paths = {
            "checkout": "/shop/checkout/URL_PRIVATE_ACCOUNT",
            "unknown": "/unrecognized/URL_PRIVATE_ACCOUNT",
            "product": "/shop/buy-iphone/iphone-18-pro",
            "product_challenge": "/shop/buy-iphone/iphone-18-pro",
        }
        url = "https://www.apple.com.cn" + paths[kind] + "?token=URL_PRIVATE_ACCOUNT"
        body = PERSONAL_HTML + (PRODUCT_HTML if kind.startswith("product") else "")
        if kind == "product":
            body = body.replace('<input value="482917" autocomplete="one-time-code">', "")
        await page.route(
            "**/*", lambda route: route.fulfill(content_type="text/html; charset=utf-8", body=body)
        )
        await page.goto(url)
        await page.evaluate("""() => {
            for (const node of [document.body, document.documentElement]) {
                for (const property of ['innerText', 'textContent', 'innerHTML', 'outerHTML']) {
                    Object.defineProperty(node, property, {get() {
                        throw new Error('Diagnostic attempted to read entire page content');
                    }});
                }
            }
        }""")
        output = await capture_diagnostics(page, tmp_path / "audit", "apple", "inspect")
        emitted = "\n".join(
            unquote(path.read_text(encoding="utf-8"))
            for path in output.parent.iterdir()
            if path.suffix in {".json", ".html"}
        )
        leaked = [secret for secret in PRIVATE if secret in emitted]
        assert not leaked, f"Diagnostic leaked fixture personal fields: {leaked}"
        data = json.loads(output.read_text(encoding="utf-8"))
        assert data["metadata"].get("redacted") is not True
        assert data["metadata"]["collection_mode"] == (
            "public_product_allowlist" if kind == "product" else "minimal_structure"
        )
        assert data["text"] == ""
        if kind == "product":
            assert any("iPhone 18 Pro Max" in item["text"] for item in data["product_fields"])
            assert any(
                item["attributes"].get("data-autom") == "add-to-cart" for item in data["buttons"]
            )
        else:
            assert not any(
                data[key] for key in ("buttons", "links", "inputs", "roles", "data_attributes")
            )
            assert data["counts"]["elements"] > 0
        assert output.with_suffix(".png").read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    finally:
        await manager.close()
