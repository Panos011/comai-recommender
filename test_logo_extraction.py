"""
Standalone test for logo extraction from Futurepedia tool pages.
Downloads and saves logos locally so you can inspect them.

Usage:
    python test_logo_extraction.py
"""

import json
import os
import random
import time
import requests
from requests.adapters import HTTPAdapter
from urllib.parse import quote_plus, urljoin, urlsplit, parse_qsl
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
import certifi

# ── Config ────────────────────────────────────────────────────────────────────
SCRAPER_KEY = os.getenv("SCRAPERAPI_KEY")
BASE_URL    = "https://www.futurepedia.io"
LOGO_DIR    = "test_logos"          # folder where logos are saved
REPORT_FILE = "logo_report.json"    # summary saved here

os.makedirs(LOGO_DIR, exist_ok=True)


# ── Parser detection ──────────────────────────────────────────────────────────
# Prefer lxml (fast), fall back to Python's built-in html.parser if it's missing.
def _pick_parser() -> str:
    try:
        BeautifulSoup("", "lxml")
        return "lxml"
    except Exception:
        print("[note] lxml not installed — falling back to built-in html.parser. "
              "Install lxml for faster parsing: pip install lxml")
        return "html.parser"


PARSER = _pick_parser()


TEST_URLS = [
    "https://www.futurepedia.io/tool/chatgpt",
    "https://www.futurepedia.io/tool/midjourney",
    "https://www.futurepedia.io/tool/perplexity-ai",
    "https://www.futurepedia.io/tool/claude",     # was claude-ai (404)
    "https://www.futurepedia.io/tool/copilot",    # was github-copilot (404)
]

UA_POOL = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]

# ── Session setup ─────────────────────────────────────────────────────────────
session = requests.Session()
session.verify = certifi.where()
session.headers.update({
    "User-Agent": random.choice(UA_POOL),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.futurepedia.io/ai-tools",
})
adapter = HTTPAdapter(max_retries=Retry(
    total=3, backoff_factor=0.5,
    status_forcelist=(429, 500, 502, 503, 504)
))
session.mount("https://", adapter)
session.mount("http://",  adapter)


def fetch(url: str) -> requests.Response:
    session.headers["User-Agent"] = random.choice(UA_POOL)
    if SCRAPER_KEY:
        wrapped = (
            f"https://api.scraperapi.com/?api_key={SCRAPER_KEY}"
            f"&keep_headers=true&country_code=us&url={quote_plus(url)}"
        )
        return session.get(wrapped, timeout=40, allow_redirects=True)
    return session.get(url, timeout=30, allow_redirects=True)


# ── Logo extraction ───────────────────────────────────────────────────────────

def extract_logo_from_next_data(soup) -> tuple[str, str]:
    tag = soup.find("script", id="__NEXT_DATA__", type="application/json")
    if not tag or not tag.string:
        return "", "no __NEXT_DATA__ tag"

    try:
        j = json.loads(tag.string)
    except json.JSONDecodeError:
        return "", "JSON parse error"

    page_props = j.get("props", {}).get("pageProps", {})
    print(f"      pageProps keys: {list(page_props.keys())[:10]}")

    candidates = {}

    def search_keys(obj, depth=0, path=""):
        if depth > 5 or not isinstance(obj, dict):
            return
        for k, v in obj.items():
            full_path = f"{path}.{k}" if path else k
            if isinstance(v, str) and any(x in k.lower() for x in ("logo", "image", "icon", "img", "photo", "avatar")):
                candidates[full_path] = v
            elif isinstance(v, dict):
                search_keys(v, depth + 1, full_path)
            elif isinstance(v, list) and v and isinstance(v[0], dict):
                search_keys(v[0], depth + 1, f"{full_path}[0]")

    search_keys(page_props)

    if candidates:
        print(f"      Image-like keys found in __NEXT_DATA__:")
        for k, v in candidates.items():
            print(f"        {k}: {v[:80]}")
        for k, v in candidates.items():
            if v.startswith("http") or v.startswith("/"):
                return urljoin(BASE_URL, v), k

    return "", "no image keys found"


def extract_logo_from_img_tag(soup, tool_slug: str) -> tuple[str, str]:
    """
    Score every <img> on the page and return the one most likely to be the
    tool's logo. Futurepedia's tool logo has three reliable signals:

      1. A distinctive card class:  aspect-square + rounded-xl + object-fill
      2. alt text that ends in "logo" (e.g. "Perplexity Logo")  — but NOT the
         site's own "Futurepedia" header logo
      3. It is served from the tool asset CDN (cdn2/cdn.futurepedia.io) and is
         a direct image, NOT the /api/og social-card endpoint

    Site chrome (/_next/image, futurepedia-logo*, decorative icons) is pushed
    to a negative score so it can never win.
    """
    best = None  # (score, url, reasons)

    for img in soup.find_all("img", src=True):
        src = img.get("src", "")
        alt = (img.get("alt", "") or "").strip()
        cls = " ".join(img.get("class", [])).lower()

        if not src or src.startswith("data:"):
            continue

        score = 0
        reasons = []

        # Signal 1 — the tool-logo card styling
        if "aspect-square" in cls and "rounded-xl" in cls and "object-fill" in cls:
            score += 3
            reasons.append("logo-card class")

        # Signal 2 — alt ends in "logo" (and isn't the Futurepedia site logo)
        alt_l = alt.lower()
        if alt_l.endswith("logo") and "futurepedia" not in alt_l:
            score += 2
            reasons.append(f"alt='{alt}'")

        # Signal 3 — direct image on the tool CDN (not the og social card)
        if ("cdn2.futurepedia.io" in src or "cdn.futurepedia.io" in src) and "/api/og" not in src:
            score += 2
            reasons.append("tool CDN host")

        # Penalise obvious site chrome so it never wins by accident
        if "/_next/image" in src or "futurepedia-logo" in src.lower():
            score -= 5

        if score > 0 and (best is None or score > best[0]):
            best = (score, urljoin(BASE_URL, src), "; ".join(reasons))

    if best:
        return best[1], f"score={best[0]} ({best[2]})"

    # Nothing scored — dump the imgs so we can inspect why
    print(f"      All <img> tags found (first 8):")
    for i, img in enumerate(soup.find_all("img", src=True)[:8]):
        src = img.get("src", "")[:80]
        alt = img.get("alt", "")
        cls = " ".join(img.get("class", []))
        print(f"        [{i}] src={src} | alt={alt} | class={cls}")

    return "", "no matching img tag"


def extract_logo_from_og(soup) -> tuple[str, str]:
    """
    Last resort. Futurepedia's og:image is a generated /api/og social card,
    not a clean logo — but it embeds the real CDN image in an &image= query
    param. We unwrap that so we at least get a direct image rather than a
    titled banner. Flagged as low-confidence because it may still be a wide
    banner crop rather than a square logo.
    """
    og = soup.find("meta", property="og:image")
    if not (og and og.get("content")):
        return "", "no og:image"

    content = og["content"].strip()

    # Unwrap the embedded image= param if present
    parsed = urlsplit(content)
    if "/api/og" in parsed.path:
        params = dict(parse_qsl(parsed.query))
        embedded = params.get("image")
        if embedded:
            return embedded, "og:image (unwrapped, low confidence)"

    return content, "og:image (raw, low confidence)"


# ── Download helper ───────────────────────────────────────────────────────────

# Map response content-type → file extension
_CONTENT_TYPE_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/svg+xml": "svg",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/x-icon": "ico",
    "image/vnd.microsoft.icon": "ico",
}


def detect_image_ext(content: bytes, content_type: str = "") -> str:
    """
    Determine the true image format. Priority:
      1. Magic bytes (authoritative — this is what the file actually IS)
      2. Content-Type response header
      3. Fallback to png

    This is what fixes the midjourney case: the URL ends in '.svg' but the
    CDN rasterises it to PNG when '?w=256' is applied, so the bytes are PNG.
    """
    head = content[:16]

    # 1. Magic-byte sniffing
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
        return "gif"
    if head[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "webp"
    if head.startswith(b"\x00\x00\x01\x00"):
        return "ico"
    # SVG is text — look for an <svg root near the start (allow leading <?xml / BOM)
    sniff = content[:512].lstrip().lower()
    if sniff.startswith(b"<?xml") or sniff.startswith(b"<svg") or b"<svg" in sniff[:200]:
        return "svg"

    # 2. Content-Type header
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in _CONTENT_TYPE_EXT:
        return _CONTENT_TYPE_EXT[ct]

    # 3. Fallback
    return "png"


def download_logo(logo_url: str, slug: str) -> dict:
    """
    Downloads the logo and saves it as LOGO_DIR/<slug>.<ext>, where <ext> is
    derived from the ACTUAL bytes returned — never from the URL string.
    Returns a dict with save_path, file_size_kb, ext, and status.
    """
    if not logo_url:
        return {"save_path": None, "file_size_kb": 0, "ext": None, "status": "skipped — no URL"}

    try:
        r = session.get(logo_url, timeout=15)
        if r.status_code != 200 or len(r.content) == 0:
            return {
                "save_path": None, "file_size_kb": 0, "ext": None,
                "status": f"HTTP {r.status_code} — not saved"
            }

        ext = detect_image_ext(r.content, r.headers.get("Content-Type", ""))
        save_path = os.path.join(LOGO_DIR, f"{slug}.{ext}")

        # Clean up any stale wrong-extension copy from earlier runs
        for old_ext in ("png", "jpg", "jpeg", "svg", "webp", "gif", "ico"):
            stale = os.path.join(LOGO_DIR, f"{slug}.{old_ext}")
            if old_ext != ext and os.path.exists(stale):
                os.remove(stale)

        with open(save_path, "wb") as f:
            f.write(r.content)

        size_kb = round(len(r.content) / 1024, 1)

        # Flag the case where the URL lied about the format
        url_ext = logo_url.split("?")[0].rsplit(".", 1)[-1].lower()
        note = ""
        if url_ext in ("png", "jpg", "jpeg", "svg", "webp", "gif", "ico") and url_ext != ext:
            note = f"  [URL said .{url_ext}, actually {ext}]"

        return {
            "save_path": save_path,
            "file_size_kb": size_kb,
            "ext": ext,
            "status": f"saved ({size_kb} KB, .{ext}){note}"
        }
    except Exception as e:
        return {"save_path": None, "file_size_kb": 0, "ext": None, "status": f"download error: {e}"}


# ── Main test loop ────────────────────────────────────────────────────────────

def test_logo_extraction():
    print("=" * 65)
    print("LOGO EXTRACTION TEST")
    print(f"Logos will be saved to: {os.path.abspath(LOGO_DIR)}/")
    print("=" * 65)

    results = []

    for url in TEST_URLS:
        slug = url.rstrip("/").split("/tool/")[-1].lower()
        print(f"\n{'─'*65}")
        print(f"Tool : {slug}")
        print(f"URL  : {url}")

        try:
            r = fetch(url)
            print(f"HTTP : {r.status_code}  ({len(r.content):,} bytes)")

            # Skip dead pages — Futurepedia's 404 has no tool data to extract
            if r.status_code == 404:
                print("  ⏭  Page not found (404) — slug doesn't exist, skipping.")
                results.append({
                    "tool": slug,
                    "logo_url": None,
                    "method": "404 — page not found",
                    "save_path": None,
                    "file_size_kb": 0,
                    "download_status": "skipped — 404",
                })
                time.sleep(1.5)
                continue

            soup = BeautifulSoup(r.content, PARSER)

            # Method 1 — kept for older pages, but Futurepedia no longer ships
            # __NEXT_DATA__ on tool pages (App Router migration), so expect (nothing)
            print("\n  [Method 1] __NEXT_DATA__ JSON (legacy, usually empty now):")
            logo1, path1 = extract_logo_from_next_data(soup)
            print(f"      Result : {logo1 or '(nothing)'}")
            print(f"      Path   : {path1}")

            # Method 2 — PRIMARY: scored <img> matching
            print("\n  [Method 2] <img> tag matching (primary):")
            logo2, reason2 = extract_logo_from_img_tag(soup, slug)
            print(f"      Result : {logo2 or '(nothing)'}")
            print(f"      Match  : {reason2}")

            # Method 3 — LAST RESORT: og:image (banner, not a clean logo)
            print("\n  [Method 3] og:image meta tag (last resort, low confidence):")
            logo3, reason3 = extract_logo_from_og(soup)
            print(f"      Result : {logo3 or '(nothing)'}")
            print(f"      Source : {reason3}")

            # Pick best URL and record which method found it.
            # Order reflects reliability: scored img match first, og only if
            # nothing else found it.
            if logo2:
                final_url, method_used = logo2, f"img tag ({reason2})"
            elif logo1:
                final_url, method_used = logo1, f"__NEXT_DATA__ ({path1})"
            elif logo3:
                final_url, method_used = logo3, f"og:image ({reason3})"
            else:
                final_url, method_used = "", "none"

            print(f"\n  ✓ Final logo URL : {final_url or 'NOT FOUND'}")
            print(f"    Method used    : {method_used}")

            # Download it
            dl = download_logo(final_url, slug)
            print(f"    Download status: {dl['status']}")
            if dl["save_path"]:
                print(f"    Saved to       : {os.path.abspath(dl['save_path'])}")

            results.append({
                "tool": slug,
                "logo_url": final_url or None,
                "method": method_used,
                "save_path": dl["save_path"],
                "file_size_kb": dl["file_size_kb"],
                "download_status": dl["status"],
            })

        except Exception as e:
            print(f"  ✗ Error: {e}")
            results.append({
                "tool": slug,
                "logo_url": None,
                "method": "error",
                "save_path": None,
                "file_size_kb": 0,
                "download_status": str(e),
            })

        time.sleep(1.5)

    # ── Summary ───────────────────────────────────────────────────────────────
    found     = [r for r in results if r.get("save_path")]
    not_found = [r for r in results if not r.get("save_path")]

    print(f"\n\n{'='*65}")
    print("SUMMARY")
    print(f"{'='*65}")
    print(f"Downloaded : {len(found)}/{len(results)}")
    print(f"Missing    : {len(not_found)}/{len(results)}")

    if found:
        print(f"\nLogos saved in: {os.path.abspath(LOGO_DIR)}/")
        for r in found:
            print(f"  ✓  {r['tool']:<25} {r['file_size_kb']} KB   [{r['method'].split('(')[0].strip()}]")

    if not_found:
        print("\nFailed:")
        for r in not_found:
            print(f"  ✗  {r['tool']}: {r['download_status']}")

    # Save JSON report
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"\nFull report saved to: {os.path.abspath(REPORT_FILE)}")


if __name__ == "__main__":
    test_logo_extraction()