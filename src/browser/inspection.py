"""Data-minimized diagnostics: unknown pages never export DOM content or attributes."""

import html
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from playwright.async_api import Error, Page

from src.core.exceptions import HumanRequired
from src.core.models import State

# Already observed public fields, not a general trust heuristic. Their values
# still need to match a narrow public-value grammar before any file is written.
_PUBLIC_FIELDS = {
    "apple": {
        "product": '[data-autom="summary-productName"]',
        "price": '[data-autom="full-price"]',
    },
    "jd": {
        "product": ".sku-title-name",
        "price": ".product-price--main .product-price--value",
    },
    "tmall": {
        "product": '[class^="mainTitle--"][title]',
        "price": '[class^="highlightPrice--"] [class^="text--"]',
    },
}
_PUBLIC_CONTROLS = ("add-to-cart", "continueButton", "summary-productName", "full-price")
_ACTIONS = {
    "inspect",
    "open_product",
    "verification",
    "human_required",
    "sku_unknown",
    "bag_verified",
    "checkout",
    "get_skus_unknown",
    "check_stock_unknown",
    "select_sku_unknown",
    "add_to_cart_unknown",
    "goto_checkout_unknown",
    "verify_order_unknown",
    "submit_order_unknown",
    "order_review",
    "submission_unknown",
}


def _public_product(url: str, platform: str) -> tuple[str, str]:
    """Return a known public route only; never retain arbitrary account paths."""
    try:
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or parts.username
            or parts.password
            or parts.port not in {None, 443}
        ):
            return "", ""
        host = parts.hostname
        if (
            platform == "apple"
            and host == "www.apple.com.cn"
            and re.fullmatch(
                r"/shop/buy-iphone/iphone-\d{1,2}(?:-(?:pro|max|plus|air|mini)){0,2}(?:/[a-z0-9]{6,16}/a)?/?",
                parts.path,
            )
        ):
            return "apple", "https://www.apple.com.cn" + "/".join(parts.path.split("/")[:4])
        if (
            platform == "jd"
            and host == "item.jd.com"
            and re.fullmatch(r"/\d{1,22}\.html", parts.path)
        ):
            return "jd", "https://item.jd.com" + parts.path
        if platform == "tmall" and host == "detail.tmall.com" and parts.path == "/item.htm":
            item = parse_qs(parts.query).get("id", [])
            if len(item) == 1 and re.fullmatch(r"\d{1,22}", item[0]):
                return "tmall", "https://detail.tmall.com/item.htm?" + urlencode({"id": item[0]})
    except ValueError:
        pass
    # Taobao has no verified public field map and deliberately remains minimal.
    return "", ""


_MINIMAL_DOM = r"""(settings) => {
  const result = {buttons:[], links:[], text:'', inputs:[], roles:[], data_attributes:[],
    product_fields:[], counts:{elements:document.querySelectorAll('*').length,
      buttons:document.querySelectorAll('button,[role="button"],input[type="submit"]').length,
      links:document.querySelectorAll('a').length,
      inputs:document.querySelectorAll('input,textarea,select').length,
      roles:document.querySelectorAll('[role]').length}, collection_mode:'minimal_structure'};
  // A product route can display a login/challenge overlay. Check input semantics,
  // never their values/labels. Unknown pages return before any DOM text is read.
  if (!settings.kind || document.querySelector('input[type="password"],'
      + 'input[autocomplete="one-time-code"],input[autocomplete="current-password"],'
      + 'input[autocomplete="new-password"],input[autocomplete="street-address"],'
      + 'input[autocomplete="cc-number"]')) return result;
  result.collection_mode = 'public_product_allowlist';
  const visible = e => e && e.getClientRects().length &&
    getComputedStyle(e).display !== 'none' && getComputedStyle(e).visibility !== 'hidden';
  const value = (key, raw) => {
    const text = raw.trim().replace(/\s+/g,' ');
    const patterns = {
      product:/^iPhone\s*\d{1,2}(?:\s*(?:Pro|Max|Plus|Air|mini)){0,2}(?:\s*(?:128GB|256GB|512GB|1TB|2TB))?(?:\s*(?:黑色|白色|银色|冰川蓝色|勃艮第酒红色|深蓝色|宇宙橙色|雾蓝色|薰衣草紫色|鼠尾草绿色|沙漠色钛金属|原色钛金属|白色钛金属|黑色钛金属))?$/i,
      price:/^(?:RMB|CNY|[¥￥])?\s*(?:\d{1,3}(?:,\d{3})+|\d{1,5})(?:\.\d{1,2})?\s*(?:元)?$/i
    };
    return text.length <= 120 && patterns[key]?.test(text) ? text : '';
  };
  for (const [key, selector] of Object.entries(settings.fields)) {
    const nodes = [...document.querySelectorAll(selector)].filter(visible);
    if (nodes.length !== 1 || nodes[0] === document.body
        || nodes[0] === document.documentElement) continue;
    const text = value(key, nodes[0].innerText || '');
    if (text) result.product_fields.push({field:key, text, attributes:{}});
  }
  if (settings.kind === 'apple') {
    for (const name of settings.controls) {
      const nodes = [...document.querySelectorAll('[data-autom="'+name+'"]')].filter(visible);
      if (nodes.length !== 1) continue;
      const element = nodes[0], rawTag = element.tagName.toLowerCase();
      const tag = ['button','input','div','span','h1','h2','p'].includes(rawTag)
        ? rawTag : 'element';
      const item = {tag, text:'', attributes:{'data-autom':name}};
      if (tag === 'button' || (tag === 'input' && element.type === 'submit')) {
        item.attributes.disabled = Boolean(element.disabled);
        result.buttons.push(item);
      }
      result.data_attributes.push(item);
    }
    for (const name of ['dimensionScreensize','dimensionCapacity','dimensionColor']) {
      for (const element of document.querySelectorAll('input[type="radio"][name="'+name+'"]')) {
        result.inputs.push({tag:'input', text:'', attributes:{type:'radio', name,
          checked:Boolean(element.checked), disabled:Boolean(element.disabled)}});
      }
    }
  }
  // No verified data-testid values exist yet. Never export arbitrary data, id,
  // class, aria labels, role names, hrefs or title attributes.
  return result;
}"""


async def capture_diagnostics(
    page: Page, output_dir: Path, platform: str, action: str, state: str = ""
) -> Path:
    """Return minimized inventory plus generated HTML and a masked layout image."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC)
    safe_platform = platform if platform in {"apple", "jd", "tmall", "taobao"} else "unknown"
    safe_action = action if action in _ACTIONS else "diagnostic"
    safe_state = state if state in {item.value for item in State} else ""
    kind, public_url = _public_product(page.url, safe_platform)
    if safe_state in {"CHECKOUT", "VERIFYING", "WAITING_HUMAN", "SUBMITTING", "SUCCESS"}:
        kind, public_url = "", ""
    stem = f"{timestamp:%Y%m%d_%H%M%S_%f}_{safe_platform}_{safe_action}"
    json_path = output_dir / f"{stem}.json"
    html_path = output_dir / f"{stem}.html"
    image_path = output_dir / f"{stem}.png"
    metadata_path = output_dir / f"{stem}.metadata.json"
    try:
        evidence = await page.evaluate(
            _MINIMAL_DOM,
            {
                "kind": kind,
                "fields": _PUBLIC_FIELDS.get(kind, {}),
                "controls": _PUBLIC_CONTROLS,
            },
        )
        # Retain layout only; no text or media from account or product pages.
        await page.screenshot(
            path=str(image_path),
            full_page=False,
            animations="disabled",
            mask=[
                page.locator(
                    "input,textarea,select,[contenteditable],img,picture,svg,canvas,"
                    "video,iframe,object,embed"
                )
            ],
            mask_color="#20252e",
            style="""
              *, *::before, *::after {
                color: transparent !important; -webkit-text-fill-color: transparent !important;
                text-shadow: none !important; background-image: none !important;
                caret-color: transparent !important;
              }
              *::before, *::after { content: none !important; }
            """,
        )
    except Error:
        raise HumanRequired(
            "Could not save minimized browser diagnostics; browser needs attention"
        ) from None
    mode = evidence.pop("collection_mode")
    metadata = {
        "timestamp": timestamp.isoformat(),
        "platform": safe_platform,
        "action": safe_action,
        "state": safe_state,
        "url": public_url if mode == "public_product_allowlist" else "[omitted]",
        "collection_mode": mode,
        "privacy_guarantee": "not_certified",
        "screenshot_mode": "layout_only_text_and_media_masking_requested",
        "html": html_path.name,
        "screenshot": image_path.name,
        "note": "Generated diagnostic, not page HTML. Unknown pages include counts only. "
        "Product values use a strict allowlist. Review local artifacts before sharing.",
    }
    inventory = {"metadata": metadata, **evidence}
    json_path.write_text(json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8")
    # Build an inert report from minimized data, never clone/serialize page DOM.
    html_path.write_text(
        '<!doctype html><html><head><meta charset="utf-8"><title>Minimized diagnostic</title>'
        "</head><body><pre>"
        + html.escape(json.dumps(evidence, ensure_ascii=False, indent=2))
        + "</pre></body></html>",
        encoding="utf-8",
    )
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return json_path
