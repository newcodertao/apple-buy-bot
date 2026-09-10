"""Redacted local diagnostics; never export cookies, storage, or raw page HTML."""

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from playwright.async_api import Error, Page

from src.core.exceptions import HumanRequired
from src.core.logging import safe_url

_REDACTED_DOM = r"""() => {
  const secret = new RegExp('password|passwd|cookie|token|session|secret|authorization|payment|'
    + 'credit.?card|cvv|address|account|phone|mobile|identity|shipping|billing|'
    + '身份证|密码|验证码|手机号|收货地址', 'i');
  const redact = value => String(value || '')
    .replace(/https?:\/\/[^\s<>"']+/gi, value => {
      try { const u = new URL(value); return u.origin + u.pathname; }
      catch { return '[REDACTED URL]'; }
    })
    .replace(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi, '[REDACTED EMAIL]')
    .replace(/\b(?:password|passwd|token|cookie|secret|authorization|session|cvv)\s*[=:：]\s*[^\s<]+/gi,
      '[REDACTED]')
    .replace(/(?:密码|验证码|手机号|身份证号|收货地址)\s*[:：=]?\s*[^\s<]+/g, '[REDACTED]')
    .replace(/\+?\d[\d ()-]{6,}\d[Xx]?/g, '[REDACTED NUMBER]')
    .replace(/[A-Za-z0-9_+\/=.-]{24,}/g, '[REDACTED VALUE]');
  const body = document.body;
  if (!body) return {html: '<!doctype html><html><body></body></html>', inventory: {
    buttons: [], links: [], text: '', inputs: [], roles: [], data_attributes: []}};
  const clone = body.cloneNode(true);
  const originals = [body, ...body.querySelectorAll('*')];
  const copies = [clone, ...clone.querySelectorAll('*')];
  for (let i = 0; i < originals.length; i++) {
    const source = originals[i], target = copies[i];
    const style = getComputedStyle(source);
    const signature = [source.id, source.getAttribute('name'), source.className].join(' ');
    if (i && (['SCRIPT','STYLE','NOSCRIPT','TEMPLATE','IFRAME','OBJECT','EMBED','SVG','CANVAS']
        .includes(source.tagName)
        || source.hidden || source.getAttribute('aria-hidden') === 'true'
        || source.getAttribute('type') === 'hidden'
        || style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0'
        || secret.test(signature))) {
      target.remove(); continue;
    }
    for (const attribute of [...target.attributes]) {
      const key = attribute.name;
      const value = attribute.value;
      target.removeAttribute(key);
      if (key.startsWith('data-') && /^data-[a-z][a-z0-9_-]{0,40}$/.test(key)) {
        target.setAttribute(key, '[REDACTED]');
      } else if (key === 'href') {
        try {
          const u = new URL(value, location.href);
          if (['http:', 'https:'].includes(u.protocol)) {
            target.setAttribute(key, redact(u.origin + u.pathname));
          }
        } catch { /* Invalid URLs carry no diagnostic value. */ }
      } else if (['role', 'type', 'id', 'name', 'aria-label', 'aria-labelledby',
                  'aria-disabled', 'aria-checked', 'aria-selected', 'disabled'].includes(key)) {
        target.setAttribute(key, redact(value));
      }
    }
    if (['INPUT','TEXTAREA'].includes(source.tagName) || source.isContentEditable) {
      target.textContent = '';
    }
  }
  const walker = document.createTreeWalker(clone, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_COMMENT);
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  for (const node of nodes) {
    if (node.nodeType === Node.COMMENT_NODE) node.remove();
    else node.textContent = redact(node.textContent);
  }
  const info = element => ({tag: element.tagName.toLowerCase(),
    text: element.textContent.trim().replace(/\s+/g,' ').slice(0, 400),
    attributes: Object.fromEntries([...element.attributes].map(a => [a.name, a.value]))});
  const all = [...clone.querySelectorAll('*')];
  return {html: '<!doctype html><html><head><meta charset="utf-8">'
    + '<title>Redacted diagnostic</title></head>' + clone.outerHTML + '</html>',
    inventory: {
      buttons: [...clone.querySelectorAll('button,[role="button"],input[type="submit"]')].map(info),
      links: [...clone.querySelectorAll('a')].map(info),
      text: clone.textContent.trim().replace(/\s+/g, ' ').slice(0, 100000),
      inputs: [...clone.querySelectorAll('input,textarea,select')].map(info),
      roles: [...clone.querySelectorAll('[role]')].map(info),
      data_attributes: all.filter(el => [...el.attributes]
        .some(a => a.name.startsWith('data-'))).map(info)
    }};
}"""


async def capture_diagnostics(
    page: Page, output_dir: Path, platform: str, action: str, state: str = ""
) -> Path:
    """Return inventory JSON; companion HTML and image contain redacted evidence."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC)
    safe_action = re.sub(r"[^a-zA-Z0-9_-]", "_", action)[:48]
    stem = f"{timestamp:%Y%m%d_%H%M%S_%f}_{platform}_{safe_action}"
    json_path = output_dir / f"{stem}.json"
    html_path = output_dir / f"{stem}.html"
    image_path = output_dir / f"{stem}.png"
    metadata_path = output_dir / f"{stem}.metadata.json"
    try:
        evidence = await page.evaluate(_REDACTED_DOM)
        # Screenshots retain layout only. Hide ALL text and visual media because
        # account details can appear outside form fields or in CSS/image content.
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
            "Could not save redacted browser diagnostics; browser needs attention"
        ) from None
    metadata = {
        "timestamp": timestamp.isoformat(),
        "platform": platform,
        "action": safe_action,
        "state": state,
        "url": safe_url(page.url),
        "redacted": True,
        "screenshot_mode": "layout_only_all_text_and_media_masked",
        "html": html_path.name,
        "screenshot": image_path.name,
        "note": "Local diagnostic. Input values, hidden content, scripts and data values removed.",
    }
    json_path.write_text(
        json.dumps({"metadata": metadata, **evidence["inventory"]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    html_path.write_text(evidence["html"], encoding="utf-8")
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return json_path
