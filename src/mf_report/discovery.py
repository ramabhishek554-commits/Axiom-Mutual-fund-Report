"""Factsheet discovery with static HTML and a JavaScript-rendered AMFI fallback.

AMFI's monthly factsheet landing page renders AMC cards; direct PDF anchors may only
appear after client-side interaction. Static discovery is retained as a fast path.
The browser path only visits the official AMFI page and follows links actually exposed
by that page. URLs are never guessed.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .config import (
    AMFI_FACTSHEET_URL,
    HTTP_TIMEOUT_SECONDS,
    MAX_DOWNLOAD_BYTES,
    PROCESSED_DIR,
    RAW_DIR,
    REQUEST_DELAY_SECONDS,
    ensure_directories,
)

LOG = logging.getLogger(__name__)
USER_AGENT = "AxiomMutualFundResearch/0.2 (public factsheet discovery; respectful rate limits)"
MONTHS = {
    "january": ("jan", "01"), "february": ("feb", "02"), "march": ("mar", "03"),
    "april": ("apr", "04"), "may": ("may", "05"), "june": ("jun", "06"),
    "july": ("jul", "07"), "august": ("aug", "08"), "september": ("sep", "09"),
    "october": ("oct", "10"), "november": ("nov", "11"), "december": ("dec", "12"),
}
URL_RE = re.compile(r"^(?:https?://|/)", re.I)


def _period_matches(text: str, month: str, year: int) -> bool:
    """Require both selected month and year, accepting common numeric date formats."""
    value = str(text or "").lower()
    month_name = str(month).strip().lower()
    aliases = MONTHS.get(month_name, (month_name[:3], ""))
    month_pattern = r"(?:%s)" % "|".join(re.escape(x) for x in (month_name, *aliases) if x)
    year_text = str(int(year))
    if re.search(rf"\b{month_pattern}\b", value) and re.search(rf"\b{re.escape(year_text)}\b", value):
        return True
    month_num = aliases[1]
    if month_num:
        patterns = (
            rf"\b{month_num}[-_/. ]{year_text}\b",
            rf"\b{year_text}[-_/. ]{month_num}\b",
            rf"\b{int(month_num)}[-_/. ]{year_text}\b",
            rf"\b{year_text}[-_/. ]{int(month_num)}\b",
        )
        return any(re.search(p, value) for p in patterns)
    return False


def _is_https_url(raw_url: str, base_url: str = AMFI_FACTSHEET_URL) -> str | None:
    url = urljoin(base_url, str(raw_url or "").strip())
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return None
    return url


def _record(url: str, label: str, month: str, year: int, context: str, method: str) -> dict:
    parsed = urlparse(url)
    label = " ".join(str(label or "").split()) or Path(parsed.path).name or "Official source link"
    return {
        "label": label[:240],
        "url": url,
        "is_direct_pdf": parsed.path.lower().endswith(".pdf") or "pdf" in label.lower(),
        "period_keyword_match": _period_matches(f"{url} {label} {context}", month, year),
        "requested_period": f"{month} {year}",
        "discovered_at_utc": datetime.now(timezone.utc).isoformat(),
        "discovery_source": AMFI_FACTSHEET_URL,
        "discovery_method": method,
    }


def _nearest_period_context(node, month: str, year: int) -> str:
    """Return the nearest compact ancestor text that explicitly names the requested period."""
    current = node
    for _ in range(6):
        if current is None:
            break
        value = " ".join(current.get_text(" ", strip=True).split())
        if value and len(value) <= 500 and _period_matches(value, month, year):
            return value
        current = current.parent
    return ""


def _static_candidates(html: str, month: str, year: int) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    found: list[dict] = []
    for anchor in soup.find_all("a", href=True):
        url = _is_https_url(anchor["href"])
        if not url:
            continue
        label = " ".join(
            [anchor.get_text(" ", strip=True), anchor.get("title", ""), anchor.get("aria-label", "")]
        ).strip()
        context = _nearest_period_context(anchor, month, year)
        parsed = urlparse(url)
        is_pdf = parsed.path.lower().endswith(".pdf") or "pdf" in label.lower()
        all_text = f"{label} {context} {url}".lower()
        has_factsheet_label = "factsheet" in all_text or "fact sheet" in all_text
        period_match = _period_matches(f"{url} {label} {context}", month, year)
        if period_match and (is_pdf or has_factsheet_label):
            found.append(_record(url, label, month, year, context, "static_html"))
    return _deduplicate(found)


def _payload_candidates(payload, month: str, year: int, source_url: str) -> list[dict]:
    """Find date-qualified PDF links in JSON responses observed during AMFI interaction."""
    found: list[dict] = []

    def walk(value, inherited_context: str = "") -> None:
        if isinstance(value, dict):
            scalar_context = " ".join(
                f"{key} {item}" for key, item in value.items()
                if item is None or isinstance(item, (str, int, float, bool))
            )
            context = f"{inherited_context} {scalar_context}"
            for key, item in value.items():
                if isinstance(item, str) and URL_RE.match(item.strip()):
                    url = _is_https_url(item, source_url)
                    if not url:
                        continue
                    parsed = urlparse(url)
                    key_label = str(key).replace("_", " ")
                    combined = f"{key_label} {context} {url}".lower()
                    has_pdf_signal = parsed.path.lower().endswith(".pdf") or "pdf" in combined
                    has_fact_signal = "factsheet" in combined or "fact sheet" in combined
                    if _period_matches(f"{context} {url}", month, year) and (has_pdf_signal or has_fact_signal):
                        found.append(_record(url, key_label or "Factsheet PDF", month, year, context, "amfi_json"))
                elif isinstance(item, (dict, list)):
                    walk(item, f"{context} {key}")
        elif isinstance(value, list):
            for item in value:
                walk(item, inherited_context)

    walk(payload)
    return found


def _playwright_candidates(month: str, year: int) -> tuple[list[dict], int]:
    """Render AMFI and inspect visible month-qualified links on AMC cards."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "AMFI returned no static factsheet links and Playwright is not installed. "
            "Run python -m pip install -r requirements.txt and "
            "python -m playwright install chromium, then restart Streamlit."
        ) from exc

    found: list[dict] = []
    network_items: list[tuple[str, object]] = []
    cards_inspected = 0
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:
            raise RuntimeError(
                "The JavaScript browser fallback could not start Chromium. Run "
                "python -m playwright install chromium in the active virtual environment, "
                "then restart Streamlit."
            ) from exc
        context = browser.new_context(accept_downloads=True, user_agent=USER_AGENT, locale="en-IN")
        page = context.new_page()
        page.set_default_timeout(1800)

        def capture_response(response) -> None:
            try:
                content_type = response.headers.get("content-type", "").lower()
                if "/api/" not in response.url.lower() and "json" not in content_type:
                    return
                body = response.text()
                if not body or len(body) > 2_000_000:
                    return
                network_items.append((response.url, json.loads(body)))
            except Exception:
                # Binary downloads, blocked responses and non-JSON replies are expected.
                return

        page.on("response", capture_response)
        try:
            page.goto(AMFI_FACTSHEET_URL, wait_until="domcontentloaded", timeout=HTTP_TIMEOUT_SECONDS * 1000)
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                page.wait_for_timeout(900)

            found.extend(_dom_candidates(page, month, year))
            headings = page.locator("h2, h3, h4, h5, h6").all_inner_texts()
            names = []
            for value in headings:
                name = " ".join(value.split())
                lowered = name.lower()
                if "mutual fund" in lowered and "factsheet" not in lowered and name not in names:
                    names.append(name)

            # Links in these AMC cards may appear only after client-side interaction.
            for name in names:
                try:
                    page.get_by_text(name, exact=True).first.click(timeout=1600)
                    page.wait_for_timeout(180)
                    _select_period_if_available(page, month, year)
                    page.wait_for_timeout(120)
                    found.extend(_dom_candidates(page, month, year))
                    cards_inspected += 1
                except Exception:
                    LOG.debug("Could not interact with AMFI card %r", name, exc_info=True)
                finally:
                    try:
                        page.keyboard.press("Escape")
                        page.wait_for_timeout(80)
                    except Exception:
                        pass
                    try:
                        if urlparse(page.url).hostname not in {"amfiindia.com", "www.amfiindia.com"}:
                            page.goto(AMFI_FACTSHEET_URL, wait_until="domcontentloaded", timeout=HTTP_TIMEOUT_SECONDS * 1000)
                    except Exception:
                        try:
                            page.goto(AMFI_FACTSHEET_URL, wait_until="domcontentloaded", timeout=HTTP_TIMEOUT_SECONDS * 1000)
                        except Exception:
                            break
            for source_url, payload in network_items:
                found.extend(_payload_candidates(payload, month, year, source_url))
        finally:
            browser.close()
    return _deduplicate(found), cards_inspected


def _select_period_if_available(page, month: str, year: int) -> None:
    """Select standard HTML month/year controls if an AMC card exposes them."""
    selects = page.locator("select")
    try:
        count = min(selects.count(), 12)
    except Exception:
        return
    month_aliases = {month.lower(), month[:3].lower()}
    for idx in range(count):
        select = selects.nth(idx)
        try:
            options = select.locator("option").evaluate_all(
                "(items) => items.map(o => ({text:(o.textContent||'').trim(), value:o.value}))"
            )
            target = next(
                (o for o in options if o["text"].lower() in month_aliases or o["text"].lower().startswith(month.lower())),
                None,
            )
            if not target:
                target = next((o for o in options if o["text"].strip() == str(year)), None)
            if target and target["value"]:
                select.select_option(value=target["value"], timeout=900)
                page.wait_for_timeout(100)
        except Exception:
            continue


def _dom_candidates(page, month: str, year: int) -> list[dict]:
    try:
        links = page.locator("a[href]").evaluate_all(
            """(anchors, target) => anchors.map(a => {
              let node = a;
              let matchedContext = '';
              let nearestText = '';
              for (let i = 0; node && i < 6; i++, node = node.parentElement) {
                const text = (node.innerText || '').replace(/\\s+/g, ' ').trim();
                if (!nearestText && text) nearestText = text.slice(0, 500);
                if (text.length <= 500) {
                  const lower = text.toLowerCase();
                  const hasName = [target.month.toLowerCase(), target.short.toLowerCase()]
                    .some(token => new RegExp('\\\\b' + token + '\\\\b', 'i').test(lower));
                  const hasYear = new RegExp('\\\\b' + target.year + '\\\\b').test(lower);
                  const numeric = new RegExp('\\\\b(?:0?' + target.month_num + ')[-_/. ]' + target.year +
                    '\\\\b|\\\\b' + target.year + '[-_/. ]0?' + target.month_num + '\\\\b').test(lower);
                  if ((hasName && hasYear) || numeric) {
                    matchedContext = text;
                    break;
                  }
                }
              }
              return {
                href: a.href,
                label: [a.innerText || '', a.getAttribute('aria-label') || '', a.title || ''].join(' ').trim(),
                context: matchedContext || nearestText
              };
            })""",
            {"month": month, "short": month.slice(0, 3), "month_num": MONTHS[month.toLowerCase()][1], "year": String(year)}
        )
    except Exception:
        return []
    found = []
    for item in links:
        url = _is_https_url(item.get("href", ""))
        if not url:
            continue
        label = item.get("label", "")
        context = item.get("context", "")
        parsed = urlparse(url)
        all_text = f"{url} {label} {context}"
        is_pdf = parsed.path.lower().endswith(".pdf") or "pdf" in label.lower()
        has_factsheet_label = "factsheet" in all_text.lower() or "fact sheet" in all_text.lower()
        if _period_matches(all_text, month, year) and (is_pdf or has_factsheet_label):
            found.append(_record(url, label, month, year, context, "javascript_dom"))
    return found


def _deduplicate(records: list[dict]) -> list[dict]:
    merged = {}
    for item in records:
        url = item.get("url")
        if not url:
            continue
        if url not in merged:
            merged[url] = item
        else:
            prior = merged[url]
            if len(str(item.get("label", ""))) > len(str(prior.get("label", ""))):
                prior["label"] = item["label"]
            prior["is_direct_pdf"] = bool(prior.get("is_direct_pdf") or item.get("is_direct_pdf"))
    return list(merged.values())


def discover_factsheet_links(month: str, year: int, session: requests.Session | None = None) -> list[dict]:
    """Discover selected-month factsheet links from the official AMFI factsheet page.

    Static parsing is the fast path. If it finds no period-qualified results, a
    Chromium pass inspects the JavaScript-rendered AMC cards. Supplying a session
    disables browser startup to keep tests deterministic.
    """
    client = session or requests.Session()
    response = client.get(
        AMFI_FACTSHEET_URL,
        timeout=HTTP_TIMEOUT_SECONDS,
        headers={"User-Agent": USER_AGENT},
    )
    response.raise_for_status()
    found = _static_candidates(response.text, month, int(year))
    if found:
        return found
    if session is not None:
        return []
    dynamic, inspected = _playwright_candidates(month, int(year))
    found = _deduplicate(dynamic)
    if not found:
        LOG.info(
            "AMFI discovery finished for %s %s: no date-qualified PDF links found; inspected %s AMC card(s).",
            month, year, inspected,
        )
    return found


def download_pdf(url: str, destination_dir: Path | None = None, session: requests.Session | None = None) -> dict:
    """Download a PDF with a size limit, signature check, SHA-256, and manifest entry."""
    url = _is_https_url(url, AMFI_FACTSHEET_URL)
    if not url:
        raise ValueError("Only valid HTTPS download URLs are accepted.")
    ensure_directories()
    target_dir = Path(destination_dir or RAW_DIR)
    target_dir.mkdir(parents=True, exist_ok=True)
    time.sleep(max(0.0, REQUEST_DELAY_SECONDS))
    response = (session or requests.Session()).get(
        url, stream=True, timeout=HTTP_TIMEOUT_SECONDS, headers={"User-Agent": USER_AGENT}
    )
    response.raise_for_status()
    content_type = response.headers.get("Content-Type", "").lower()
    chunks, size = [], 0
    for chunk in response.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        size += len(chunk)
        if size > MAX_DOWNLOAD_BYTES:
            raise ValueError(f"Download exceeds configured size limit ({MAX_DOWNLOAD_BYTES} bytes).")
        chunks.append(chunk)
    content = b"".join(chunks)
    if not content.startswith(b"%PDF-"):
        raise ValueError("Response does not begin with the PDF signature; no file was saved.")
    digest = hashlib.sha256(content).hexdigest()
    parsed = urlparse(url)
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(parsed.path).name or "factsheet.pdf")
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    destination = target_dir / f"{digest[:10]}_{name}"
    if not destination.exists():
        destination.write_bytes(content)
    record = {
        "url": url, "file": str(destination), "bytes": size, "sha256": digest,
        "downloaded_at_utc": datetime.now(timezone.utc).isoformat(), "content_type": content_type,
    }
    manifest = PROCESSED_DIR / "download_manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else []
    except (json.JSONDecodeError, OSError):
        existing = []
    if not any(item.get("sha256") == digest for item in existing):
        existing.append(record)
    manifest.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
    return record
