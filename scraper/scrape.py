#!/usr/bin/env python3
"""
Price scraper for the static price-tracker page.

Reads data/config.json (the item list), visits each product URL with a real
headless Chromium (Playwright) so that bot-protected shops (La Redoute, fonQ)
serve the page, extracts the current price, and merges it into
data/prices.json — keeping a compact change-log history and the lowest price
seen since the tracking start date.

Design goals:
  * No API tokens. Runs unattended in GitHub Actions.
  * Graceful degradation: if a shop blocks us or a product 404s, that item
    keeps its last known price and is flagged 'error' instead of corrupting
    the history.
  * Robust extraction: JSON-LD  ->  price meta tags  ->  visible-text regex.
"""

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "data" / "config.json"
PRICES_PATH = ROOT / "data" / "prices.json"
ALERT_PATH = ROOT / "ALERT_BODY.md"  # written only when a new record-low is found

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


# --------------------------------------------------------------------------- #
# Number / price parsing
# --------------------------------------------------------------------------- #
def parse_price(raw):
    """Parse a price out of messy strings: '1.234,56', '679,00 €', '899', '54.99'."""
    if raw is None:
        return None
    s = str(raw).replace("\xa0", " ").strip()
    m = re.search(r"-?[0-9][0-9\.\, ]*[0-9]|-?[0-9]", s)
    if not m:
        return None
    num = m.group(0).replace(" ", "")
    if "," in num and "." in num:
        # Whichever separator comes last is the decimal separator.
        if num.rfind(",") > num.rfind("."):
            num = num.replace(".", "").replace(",", ".")
        else:
            num = num.replace(",", "")
    elif "," in num:
        frac = num.split(",")[-1]
        num = num.replace(",", ".") if len(frac) in (1, 2) else num.replace(",", "")
    else:
        if num.count(".") > 1:  # 1.234.567 -> thousands separators
            num = num.replace(".", "")
        else:
            # Single dot: 3 trailing digits => EU thousands separator (1.234 = 1234);
            # 1-2 trailing digits => decimal point (54.99). Avoids logging 1.23 for "1.234".
            frac = num.split(".")[-1]
            if len(frac) == 3:
                num = num.replace(".", "")
    try:
        val = round(float(num), 2)
        return val if val > 0 else None
    except ValueError:
        return None


def _walk(node):
    """Yield every dict found in a nested JSON-LD structure (incl. @graph)."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def price_from_jsonld(scripts):
    """Lowest offer price found across all JSON-LD blocks (good for 'vanaf' prices)."""
    candidates = []
    for raw in scripts:
        text = (raw or "").strip().rstrip(";")
        try:
            data = json.loads(text)
        except Exception:
            continue
        for obj in _walk(data):
            offers = obj.get("offers")
            if not offers:
                continue
            for off in offers if isinstance(offers, list) else [offers]:
                if not isinstance(off, dict):
                    continue
                for key in ("price", "lowPrice", "highPrice"):
                    if off.get(key) not in (None, ""):
                        p = parse_price(off[key])
                        if p:
                            candidates.append(p)
    return min(candidates) if candidates else None


# --------------------------------------------------------------------------- #
# Per-page extraction
# --------------------------------------------------------------------------- #
def extract_price(page):
    """Return (price, method) using the most reliable available signal."""
    # 1) JSON-LD structured data
    scripts = page.eval_on_selector_all(
        'script[type="application/ld+json"]', "els => els.map(e => e.textContent)"
    )
    p = price_from_jsonld(scripts)
    if p:
        return p, "json-ld"

    # 2) Price meta tags / microdata
    meta_selectors = [
        'meta[property="product:price:amount"]',
        'meta[property="og:price:amount"]',
        'meta[itemprop="price"]',
        '[itemprop="price"]',
    ]
    for sel in meta_selectors:
        try:
            el = page.query_selector(sel)
        except Exception:
            el = None
        if not el:
            continue
        val = el.get_attribute("content") or el.get_attribute("value") or el.inner_text()
        p = parse_price(val)
        if p:
            return p, f"meta:{sel}"

    # 3) Visible-text fallback — first euro-amount on the page
    try:
        body = page.inner_text("body")
    except Exception:
        body = ""
    # Prefer an amount near a "vanaf" (from) label, else the first euro amount.
    for pattern in (
        r"vanaf[^0-9]{0,12}([0-9][0-9\.\, ]*[0-9])\s*€",
        r"€\s*([0-9][0-9\.\, ]*[0-9])",
        r"([0-9][0-9\.\, ]*[0-9])\s*€",
    ):
        m = re.search(pattern, body, re.IGNORECASE)
        if m:
            p = parse_price(m.group(1))
            if p and p >= 1:
                return p, "text-regex"
    return None, None


def extract_images(page, base_url):
    """Collect product image URLs from JSON-LD `image` + og:image / twitter:image."""
    urls = []
    try:
        scripts = page.eval_on_selector_all(
            'script[type="application/ld+json"]', "els => els.map(e => e.textContent)")
    except Exception:
        scripts = []
    for raw in scripts:
        try:
            data = json.loads((raw or "").strip().rstrip(";"))
        except Exception:
            continue
        for obj in _walk(data):
            img = obj.get("image")
            if not img:
                continue
            for x in (img if isinstance(img, list) else [img]):
                if isinstance(x, str):
                    urls.append(x)
                elif isinstance(x, dict) and x.get("url"):
                    urls.append(x["url"])
    for sel in ('meta[property="og:image"]',
                'meta[property="og:image:secure_url"]',
                'meta[name="twitter:image"]'):
        try:
            for el in page.query_selector_all(sel):
                c = el.get_attribute("content")
                if c:
                    urls.append(c)
        except Exception:
            pass
    out = []
    for u in urls:
        if not u:
            continue
        u = u.strip()
        if u.startswith("//"):
            u = "https:" + u
        elif u.startswith("/"):
            u = urljoin(base_url, u)
        if u.startswith("http") and u not in out:
            out.append(u)
        if len(out) >= 6:
            break
    return out


def warm_up(context, host, warmed):
    """Visit a site's homepage once to acquire bot-protection cookies (Akamai etc.)
    and dismiss the cookie banner, so subsequent product pages aren't 403'd."""
    if not host or host in warmed:
        return
    warmed.add(host)
    page = context.new_page()
    try:
        page.goto(f"https://{host}/", wait_until="domcontentloaded", timeout=45000)
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        for sel in ("#onetrust-accept-btn-handler",
                    "button#onetrust-accept-btn-handler",
                    "button:has-text('Alles accepteren')",
                    "button:has-text('Alles aanvaarden')",
                    "button:has-text('Accepteren')",
                    "button[aria-label*='accept' i]"):
            try:
                el = page.query_selector(sel)
                if el:
                    el.click(timeout=2000)
                    break
            except Exception:
                pass
        page.wait_for_timeout(1500)
    except Exception:
        pass
    finally:
        page.close()


def scrape_url(context, url):
    """Scrape one product URL. Return {price, method, status, message}."""
    page = context.new_page()
    referer = f"https://{urlparse(url).netloc}/"
    try:
        for attempt in (1, 2):
            try:
                resp = page.goto(url, wait_until="domcontentloaded", timeout=45000, referer=referer)
            except Exception as e:
                if attempt == 2:
                    return {"price": None, "method": None, "status": "error",
                            "message": f"Navigatie mislukt: {type(e).__name__}"}
                page.wait_for_timeout(2500)
                continue

            status = resp.status if resp else 0
            if status and status >= 400:
                if attempt == 2 or status != 404:
                    return {"price": None, "method": None, "status": "error",
                            "message": f"HTTP {status}"}
                page.wait_for_timeout(2000)
                continue

            try:
                page.wait_for_load_state("networkidle", timeout=12000)
            except Exception:
                pass

            price, method = extract_price(page)
            if price is not None:
                return {"price": price, "method": method, "status": "ok", "message": "",
                        "images": extract_images(page, url)}

            if attempt == 1:
                page.wait_for_timeout(2500)  # let late JS-injected data settle, retry once
                continue
            return {"price": None, "method": None, "status": "error",
                    "message": "Geen prijs gevonden op de pagina."}
    finally:
        page.close()


# --------------------------------------------------------------------------- #
# History merge
# --------------------------------------------------------------------------- #
def merge(existing, item, scrape, today, now_iso, url):
    """Produce the updated per-item record."""
    rec = {
        "id": item["id"],
        "category": item["category"],
        "name": item["name"],
        "url": url,
        "currency": existing.get("currency", "EUR") if existing else "EUR",
    }
    history = list(existing.get("history", [])) if existing else []

    if scrape["status"] == "ok":
        price = scrape["price"]
        if history and history[-1].get("date") == today:
            history[-1] = {"date": today, "price": price}  # update today's point
        elif not history or history[-1].get("price") != price:
            history.append({"date": today, "price": price})  # record a change
        rec["currentPrice"] = price
        rec["status"] = "ok"
        rec["message"] = "" if scrape["method"] in (None, "json-ld") else f"bron: {scrape['method']}"
        rec["lastChecked"] = now_iso
    else:
        # Keep last known values; flag the failure.
        rec["currentPrice"] = existing.get("currentPrice") if existing else None
        rec["status"] = scrape["status"] if (existing and existing.get("history")) else "pending"
        rec["message"] = scrape["message"]
        rec["lastChecked"] = now_iso

    rec["images"] = (scrape.get("images") if scrape.get("status") == "ok" and scrape.get("images")
                     else (existing.get("images") if existing else None)) or []
    rec["history"] = history
    if history:
        lo = min(history, key=lambda h: h["price"])
        hi = max(history, key=lambda h: h["price"])
        rec["lowestPrice"], rec["lowestDate"] = lo["price"], lo["date"]
        rec["highestPrice"], rec["highestDate"] = hi["price"], hi["date"]
    else:
        rec["lowestPrice"] = rec["lowestDate"] = None
        rec["highestPrice"] = rec["highestDate"] = None
    return rec


def scrape_config_item(context, item, warmed):
    """Scrape a config item (single url OR multiple colour variants).

    Returns (result, variant_snaps, effective_url):
      result        = {price, method, status, message} at the ITEM level
                      (item price = cheapest variant for multi-variant items)
      variant_snaps = per-colour snapshot list, or None for single-url items
      effective_url = the URL the card links to (cheapest variant, or the url)
    """
    if item.get("variants"):
        snaps = []
        for v in item["variants"]:
            warm_up(context, urlparse(v["url"]).netloc, warmed)
            r = scrape_url(context, v["url"])
            snaps.append({
                "label": v.get("label", ""), "url": v["url"],
                "currentPrice": r["price"], "status": r["status"], "message": r["message"],
                "images": r.get("images", []),
            })
        priced = [s for s in snaps if s["status"] == "ok" and s["currentPrice"] is not None]
        if priced:
            best = min(priced, key=lambda s: s["currentPrice"])
            result = {"price": best["currentPrice"], "method": "variants", "status": "ok", "message": "",
                      "images": best.get("images", [])}
            return result, snaps, best["url"]
        any_err = any(s["status"] == "error" for s in snaps)
        msg = "; ".join(f"{s['label']}: {s['message']}" for s in snaps if s["message"]) or "Geen prijs gevonden."
        result = {"price": None, "method": None, "status": "error" if any_err else "pending", "message": msg}
        return result, snaps, item["variants"][0]["url"]

    warm_up(context, urlparse(item["url"]).netloc, warmed)
    return scrape_url(context, item["url"]), None, item["url"]


# --------------------------------------------------------------------------- #
# Alerts
# --------------------------------------------------------------------------- #
def write_alert(new_lows, today):
    """Write a Markdown issue body describing each new record-low price."""
    def eur(v):
        return "€ " + f"{v:.2f}".replace(".", ",")

    lines = [
        f"## 💸 Nieuwe laagste prijs sinds tracking ({today})",
        "",
        "| Item | Vorige laagste | Nieuwe prijs | Daling |",
        "|---|---|---|---|",
    ]
    for nl in new_lows:
        diff = nl["oldLow"] - nl["newPrice"]
        pct = round(diff / nl["oldLow"] * 100) if nl["oldLow"] else 0
        lines.append(
            f"| [{nl['name']}]({nl['url']}) | {eur(nl['oldLow'])} | "
            f"**{eur(nl['newPrice'])}** | −{eur(diff)} (−{pct}%) |"
        )
    page_url = os.environ.get("PAGE_URL")
    if page_url:
        lines += ["", f"🔗 [Open de prijstracker]({page_url})"]
    ALERT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    try:
        prices = json.loads(PRICES_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        prices = {"items": []}

    tz = None
    if ZoneInfo:
        try:
            tz = ZoneInfo(config.get("timezone", "Europe/Brussels"))
        except Exception:
            tz = None  # falls back to UTC if the tz database is unavailable
    now = datetime.now(tz)
    today = now.date().isoformat()
    now_iso = now.isoformat(timespec="seconds")

    if ALERT_PATH.exists():
        ALERT_PATH.unlink()  # clear any stale alert from a previous (local) run

    existing_by_id = {it.get("id"): it for it in prices.get("items", [])}
    updated_items = []
    summary = []
    new_lows = []
    warmed = set()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        context = browser.new_context(
            user_agent=USER_AGENT,
            locale="nl-BE",
            timezone_id="Europe/Brussels",
            viewport={"width": 1366, "height": 900},
            extra_http_headers={
                "Accept-Language": "nl-BE,nl;q=0.9,en;q=0.6",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Upgrade-Insecure-Requests": "1",
            },
        )
        for item in config["items"]:
            print(f"[scrape] {item['id']} … ", end="", flush=True)
            if item.get("manual"):
                # Manually-tracked item (shop blocks automated scraping): never
                # scrape it; carry the existing record forward, just flag it 'manual'.
                ex = existing_by_id.get(item["id"]) or {}
                rec = dict(ex)
                rec["id"], rec["category"], rec["name"] = item["id"], item["category"], item["name"]
                rec["url"] = item.get("url", ex.get("url", ""))
                rec.setdefault("currency", config.get("currency", "EUR"))
                rec.setdefault("currentPrice", None)
                rec.setdefault("history", [])
                rec.setdefault("lowestPrice", rec.get("currentPrice"))
                rec.setdefault("lowestDate", None)
                rec.setdefault("highestPrice", rec.get("currentPrice"))
                rec.setdefault("highestDate", None)
                rec["status"] = "manual"
                rec["message"] = "Handmatig bijgehouden (shop blokkeert auto-scraping)."
                updated_items.append(rec)
                ps = f"€{rec['currentPrice']}" if rec.get("currentPrice") is not None else "—"
                print(f"MANUAL {ps} (overgeslagen)")
                summary.append((item["id"], "MANUAL", ps))
                continue

            result, snaps, eff_url = scrape_config_item(context, item, warmed)
            existing = existing_by_id.get(item["id"])
            rec = merge(existing, item, result, today, now_iso, eff_url)
            if snaps is not None:
                rec["variants"] = snaps
            updated_items.append(rec)
            # New record-low = a successful scrape that beats the PREVIOUS lowest.
            prev_low = (existing or {}).get("lowestPrice")
            if (result["status"] == "ok" and result["price"] is not None
                    and isinstance(prev_low, (int, float))
                    and result["price"] < prev_low - 0.005):
                new_lows.append({"name": item["name"], "url": eff_url,
                                 "oldLow": prev_low, "newPrice": result["price"]})
            tag = result["status"].upper()
            price_str = f"€{result['price']}" if result["price"] is not None else "—"
            print(f"{tag} {price_str} ({result['method'] or result['message']})")
            summary.append((item["id"], tag, price_str))
        context.close()
        browser.close()

    out = {
        "trackingStartDate": config.get("trackingStartDate"),
        "currency": config.get("currency", "EUR"),
        "timezone": config.get("timezone", "Europe/Brussels"),
        "lastUpdated": now_iso,
        "items": updated_items,
    }
    PRICES_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if new_lows:
        write_alert(new_lows, today)
        print(f"\n🔔 {len(new_lows)} nieuwe laagste prijs(zen) — alert geschreven naar {ALERT_PATH.name}")

    print("\n=== Summary ===")
    for sid, tag, price_str in summary:
        print(f"  {sid:28} {tag:8} {price_str}")
    ok = sum(1 for _, t, _ in summary if t == "OK")
    print(f"{ok}/{len(summary)} items updated successfully.")
    # Always exit 0 so partial successes still get committed.
    return 0


if __name__ == "__main__":
    sys.exit(main())
