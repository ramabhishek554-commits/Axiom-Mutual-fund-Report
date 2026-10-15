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
            rf"(?<!\d)0?{month_num}[-_/. ]{year_text}(?!\d)",
            rf"(?<!\d){year_text}[-_/. ]0?{month_num}(?!\d)",
            rf"(?<!\d){int(month_num)}[-_/. ]{year_text}(?!\d)",
            rf"(?<!\d){year_text}[-_/. ]{int(month_num)}(?!\d)",
        )
        return any(re.search(p, value) for p in patterns)
    return False


def _context_has_competing_period(text: str, month: str, year: int) -> bool:
    """Flag nearby text that mentions a different month/year as well as the target."""
    value = str(text or "").lower()
    target_month = month.strip().lower()
    target_year = int(year)

    # Month-name references such as "August 2026" or "Sep-2025".
    for month_name, (short_name, _number) in MONTHS.items():
        pattern = re.compile(
            rf"(?<![a-z])(?:{re.escape(month_name)}|{re.escape(short_name)})"
            rf"[\\s._/-]+(20\\d{{2}})(?!\\d)",
            re.I,
        )
        for match in pattern.finditer(value):
            matched_year = int(match.group(1))
            if month_name != target_month or matched_year != target_year:
                return True

    # Numeric month/year forms such as 08-2026 or 2026/08.
    numeric = re.compile(
        r"(?<!\\d)(0?[1-9]|1[0-2])[-_/. ](20\\d{2})(?!\\d)"
        r"|(?<!\\d)(20\\d{2})[-_/. ](0?[1-9]|1[0-2])(?!\\d)"
    )
    target_number = int(MONTHS.get(target_month, ("", "0"))[1])
    for match in numeric.finditer(value):
        if match.group(1) is not None:
            matched_month, matched_year = int(match.group(1)), int(match.group(2))
        else:
            matched_year, matched_month = int(match.group(3)), int(match.group(4))
        if matched_month != target_number or matched_year != target_year:
            return True
    return False


def _period_evidence(
    url: str, label: str, context: str, month: str, year: int
) -> tuple[bool, str]:
    """Use explicit URL/label evidence first; use context only when date-unambiguous."""
    if _period_matches(url, month, year):
        return True, "URL contains selected month/year"
    if _period_matches(label, month, year):
        return True, "Link label contains selected month/year"
    if _period_matches(context, month, year):
        if _context_has_competing_period(context, month, year):
            return False, "Nearby context contains multiple months/years"
        return True, "Nearby context uniquely identifies selected month/year"
    return False, "No clear month/year evidence"


def _is_https_url(raw_url: str, base_url: str = AMFI_FACTSHEET_URL) -> str | None:
    url = urljoin(base_url, str(raw_url or "").strip())
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return None
    return url



# Fund-house aliases are deliberately used only as hints. Source pages and URLs
# can be generic or CDN-hosted, so the application must still ask the user to verify.
_FUND_HOUSE_ALIASES = [
    ("HDFC Mutual Fund", ("hdfc mutual fund", "hdfc mutual", "hdfcfund", "hdfc")),
    ("Axis Mutual Fund", ("axis mutual fund", "axis mutual", "axismf", "axismf.com", "axismf.in")),
    ("ICICI Prudential Mutual Fund", ("icici prudential mutual fund", "icici prudential", "icicipru", "icicipruamc")),
    ("SBI Mutual Fund", ("sbi mutual fund", "sbi mutual", "sbimf", "sbimf.com")),
    ("PPFAS Mutual Fund", ("ppfas mutual fund", "parag parikh", "ppfas", "amc.ppfas")),
    ("Nippon India Mutual Fund", ("nippon india mutual fund", "nippon india", "nipponmf")),
    ("Kotak Mahindra Mutual Fund", ("kotak mahindra mutual fund", "kotak mutual fund", "kotakmf")),
    ("Mirae Asset Mutual Fund", ("mirae asset mutual fund", "mirae asset", "miraeasset")),
    ("DSP Mutual Fund", ("dsp mutual fund", "dsp mutual", "dspim")),
    ("Aditya Birla Sun Life Mutual Fund", ("aditya birla sun life", "absl mutual fund", "birla sun life", "abslamc")),
    ("Tata Mutual Fund", ("tata mutual fund", "tata mutual", "tatamutualfund")),
    ("UTI Mutual Fund", ("uti mutual fund", "uti mutual", "utimf")),
    ("Franklin Templeton Mutual Fund", ("franklin templeton", "franklintempleton")),
    ("Canara Robeco Mutual Fund", ("canara robeco", "canararobeco")),
    ("HSBC Mutual Fund", ("hsbc mutual fund", "hsbc asset management", "hsbc mf")),
    ("Edelweiss Mutual Fund", ("edelweiss mutual fund", "edelweiss mf", "edelweiss")),
    ("Bandhan Mutual Fund", ("bandhan mutual fund", "bandhan mf")),
    ("Quant Mutual Fund", ("quant mutual fund", "quant mf", "quantum mutual fund")),
    ("Motilal Oswal Mutual Fund", ("motilal oswal mutual fund", "motilal oswal", "motilaloswal")),
    ("Invesco Mutual Fund", ("invesco mutual fund", "invesco")),
    ("LIC Mutual Fund", ("lic mutual fund", "lic mf", "licmf")),
    ("JM Financial Mutual Fund", ("jm financial mutual fund", "jm financial", "jmf")),
    ("Mahindra Manulife Mutual Fund", ("mahindra manulife", "mahindramanulife")),
    ("Union Mutual Fund", ("union mutual fund", "union mf")),
    ("Bank of India Mutual Fund", ("bank of india mutual fund", "boi mutual fund", "boimf")),
    ("Baroda BNP Paribas Mutual Fund", ("baroda bnp paribas", "barodabnpparibas")),
    ("PGIM India Mutual Fund", ("pgim india mutual fund", "pgim india", "pgim")),
    ("Navi Mutual Fund", ("navi mutual fund", "navi mf")),
    ("Bajaj Finserv Mutual Fund", ("bajaj finserv mutual fund", "bajaj finserv amc")),
]


def _identify_fund_house(url: str, label: str, context: str) -> tuple[str, str]:
    """Return a cautious AMC hint and the matching evidence, never a verification claim."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    label_context = f"{label or ''} {context or ''}".lower()
    compact_url = re.sub(r"[^a-z0-9]", "", f"{host} {parsed.path}".lower())

    for fund_house, aliases in _FUND_HOUSE_ALIASES:
        for alias in aliases:
            normalized_alias = alias.lower()
            compact_alias = re.sub(r"[^a-z0-9]", "", normalized_alias)
            # A recognizable AMC/domain slug in the link is the strongest available hint.
            if compact_alias and len(compact_alias) >= 5 and compact_alias in compact_url:
                return fund_house, "URL/domain hint — verify official source"
            if re.search(rf"(?<![a-z0-9]){re.escape(normalized_alias)}(?![a-z0-9])", label_context):
                return fund_house, "Page label/context hint — verify official source"
    return "Unknown — verify manually", "No reliable AMC name detected"


def _record(url: str, label: str, month: str, year: int, context: str, method: str) -> dict:
    parsed = urlparse(url)
    label = " ".join(str(label or "").split()) or Path(parsed.path).name or "Official source link"
    fund_house, fund_house_evidence = _identify_fund_house(url, label, context)
    period_matches, period_evidence = _period_evidence(url, label, context, month, year)
    return {
        "fund_house_hint": fund_house,
        "fund_house_evidence": fund_house_evidence,
        "label": label[:240],
        "url": url,
        "source_context": " ".join(str(context or "").split())[:500],
        "period_evidence": period_evidence,
        "is_direct_pdf": parsed.path.lower().endswith(".pdf") or "pdf" in label.lower() or "pdf" in url.lower(),
        "period_keyword_match": period_matches,
        "requested_period": f"{month} {year}",
        "discovered_at_utc": datetime.now(timezone.utc).isoformat(),
        "discovery_source": AMFI_FACTSHEET_URL,
        "discovery_method": method,
        "verification_status": "Unverified — open the PDF and confirm AMC, scheme, and portfolio valuation date",
    }


def _nearest_period_context(node, month: str, year: int) -> str:
    """Return the nearest compact ancestor text that explicitly names the requested period."""
    current = node
    for _ in range(6):
        if current is None:
            break
        value = " ".join(current.get_text(" ", strip=True).split())
        if (
            value
            and len(value) <= 500
            and _period_matches(value, month, year)
            and not _context_has_competing_period(value, month, year)
        ):
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
        period_match, _evidence = _period_evidence(url, label, context, month, year)
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
                    period_match, _evidence = _period_evidence(url, key_label, context, month, year)
                    if period_match and (has_pdf_signal or has_fact_signal):
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

            found.extend(_scan_page_and_frames(page, month, year))
            try:
                headings = page.locator("h1, h2, h3, h4, h5, h6").all_inner_texts()
            except Exception:
                headings = []
            names = []
            for value in headings:
                name = " ".join(value.split())
                lowered = name.lower()
                if "mutual fund" in lowered and "factsheet" not in lowered and name not in names:
                    names.append(name)

            # Cards may be clickable only through a parent, modal, iframe, or popup.
            for name in names:
                navigation_failed = False
                try:
                    heading = page.get_by_text(name, exact=True).first
                    target = heading
                    for depth in range(1, 5):
                        candidate = heading.locator("xpath=" + "/".join([".."] * depth))
                        try:
                            clickable = candidate.evaluate(
                                """el => {
                                  const style = getComputedStyle(el);
                                  return ['A','BUTTON'].includes(el.tagName) ||
                                    el.getAttribute('role') === 'button' ||
                                    el.hasAttribute('onclick') ||
                                    style.cursor === 'pointer' ||
                                    !!el.querySelector('a[href],button,[role="button"]');
                                }"""
                            )
                            if clickable:
                                target = candidate
                                break
                        except Exception:
                            continue

                    existing_pages = set(context.pages)
                    try:
                        target.click(timeout=1600)
                    except Exception:
                        # A card's click handler is sometimes attached to its ancestor.
                        clicked = False
                        for depth in range(1, 5):
                            try:
                                heading.locator("xpath=" + "/".join([".."] * depth)).click(timeout=700)
                                clicked = True
                                break
                            except Exception:
                                continue
                        if not clicked:
                            raise
                    page.wait_for_timeout(350)
                    found.extend(_scan_page_and_frames(page, month, year))
                    cards_inspected += 1

                    # Inspect new tabs/popups opened by an AMC card before closing them.
                    for popup in [p for p in context.pages if p not in existing_pages and p is not page]:
                        try:
                            popup.wait_for_load_state("domcontentloaded", timeout=5000)
                            popup.wait_for_timeout(300)
                            found.extend(_scan_page_and_frames(popup, month, year))
                        except Exception:
                            LOG.debug("Could not inspect AMC popup for %r", name, exc_info=True)
                        finally:
                            try:
                                popup.close()
                            except Exception:
                                pass
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
                            # Inspect the navigated AMC page before returning to AMFI.
                            found.extend(_scan_page_and_frames(page, month, year))
                            page.goto(AMFI_FACTSHEET_URL, wait_until="domcontentloaded", timeout=HTTP_TIMEOUT_SECONDS * 1000)
                            page.wait_for_timeout(250)
                    except Exception:
                        try:
                            page.goto(AMFI_FACTSHEET_URL, wait_until="domcontentloaded", timeout=HTTP_TIMEOUT_SECONDS * 1000)
                        except Exception:
                            navigation_failed = True
                if navigation_failed:
                    LOG.warning("Could not restore the AMFI page after inspecting card %r; stopping card scan.", name)
                    break
            for source_url, payload in network_items:
                found.extend(_payload_candidates(payload, month, year, source_url))
        finally:
            browser.close()
    return _deduplicate(found), cards_inspected



def _scan_page_and_frames(page, month: str, year: int) -> list[dict]:
    """Scan the page and every accessible frame for dated links and selected-period PDFs."""
    found: list[dict] = []
    try:
        frames = list(page.frames)
    except Exception:
        frames = [page]

    for frame in frames or [page]:
        try:
            found.extend(_dom_candidates(frame, month, year))
        except Exception:
            LOG.debug("Could not scan frame links", exc_info=True)
        try:
            selected_period = _select_period_if_available(frame, month, year)
            if selected_period:
                frame.wait_for_timeout(250)
                found.extend(_dom_candidates(frame, month, year))
                found.extend(_dom_selected_period_pdfs(frame, month, year))
        except Exception:
            LOG.debug("Could not inspect period selectors in frame", exc_info=True)
    return _deduplicate(found)


def _select_period_if_available(page, month: str, year: int) -> bool:
    """Select native month/year controls and confirm both requested values were found."""
    selects = page.locator("select")
    try:
        count = min(selects.count(), 12)
    except Exception:
        return False

    month_aliases = {month.lower(), month[:3].lower()}
    month_selected = False
    year_selected = False
    year_text = str(int(year))

    for idx in range(count):
        select = selects.nth(idx)
        try:
            options = select.locator("option").evaluate_all(
                "(items) => items.map(o => ({text:(o.textContent||'').trim(), value:o.value}))"
            )
            target = None
            target_has_month = False
            target_has_year = False

            for option in options:
                label = str(option.get("text", "")).strip()
                value = str(option.get("value", "")).strip()
                normalized = label.lower()
                combined = f"{label} {value}".lower()
                starts_month = any(
                    normalized == alias
                    or normalized.startswith(alias + " ")
                    or normalized.startswith(alias + "-")
                    or normalized.startswith(alias + "/")
                    for alias in month_aliases if alias
                )
                numeric_month = MONTHS[month.lower()][1]
                numeric_match = bool(
                    re.search(rf"(?<!\d)0?{numeric_month}[-_/. ]{year_text}(?!\d)", combined)
                    or re.search(rf"(?<!\d){year_text}[-_/. ]0?{numeric_month}(?!\d)", combined)
                )
                if starts_month or numeric_match:
                    target = option
                    target_has_month = True
                    target_has_year = year_text in combined or numeric_match
                    break

            if target is None:
                target = next(
                    (
                        option for option in options
                        if str(option.get("text", "")).strip() == year_text
                        or str(option.get("value", "")).strip() == year_text
                    ),
                    None,
                )
                if target:
                    target_has_year = True

            if target and target.get("value") is not None:
                select.select_option(value=str(target["value"]), timeout=900)
                if target_has_month:
                    month_selected = True
                if target_has_year:
                    year_selected = True
        except Exception:
            continue

    return month_selected and year_selected


def _dom_selected_period_pdfs(page, month: str, year: int) -> list[dict]:
    """Collect visible PDF downloads after the requested month and year were selected."""
    try:
        links = page.locator("a[href]").evaluate_all(
            """anchors => anchors.filter(a => a.getClientRects().length > 0)
              .map(a => ({
                href: a.href,
                label: [a.innerText || '', a.getAttribute('aria-label') || '', a.title || ''].join(' ').trim(),
                context: ((a.parentElement && a.parentElement.innerText) || '').replace(/\\s+/g, ' ').trim().slice(0, 500)
              }))"""
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
        is_pdf = parsed.path.lower().endswith(".pdf") or "pdf" in label.lower() or "pdf" in url.lower()
        if not is_pdf:
            continue

        # Do not assume a PDF belongs to the selected period merely because a
        # selector was touched. The actual URL, label, or nearby card text must
        # contain the requested month and year; otherwise this link is omitted.
        period_match, _evidence = _period_evidence(url, label, context, month, year)
        if not period_match:
            continue
        found.append(_record(url, label, month, year, context, "javascript_dom_selected_period"))
    return _deduplicate(found)

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
        period_match, _evidence = _period_evidence(url, label, context, month, year)
        if period_match and (is_pdf or has_factsheet_label):
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



def _confirmed_period_pdfs(records: list[dict]) -> list[dict]:
    """Only retain PDF candidates with explicit evidence for the selected month and year."""
    return [
        item for item in _deduplicate(records)
        if item.get("is_direct_pdf") and item.get("period_keyword_match")
    ]


def discover_factsheet_links(month: str, year: int, session: requests.Session | None = None) -> list[dict]:
    """Discover selected-month factsheet links from the official AMFI factsheet page.

    Static parsing is the fast path. If it finds no period-qualified results, a
    Chromium pass inspects JavaScript-rendered AMC cards. Supplying a session disables
    browser startup to keep tests deterministic.
    """
    client = session or requests.Session()
    static_error = None
    try:
        response = client.get(
            AMFI_FACTSHEET_URL,
            timeout=HTTP_TIMEOUT_SECONDS,
            headers={"User-Agent": USER_AGENT},
        )
        response.raise_for_status()
        found = _static_candidates(response.text, month, int(year))
        confirmed_pdfs = _confirmed_period_pdfs(found)
        if confirmed_pdfs:
            return confirmed_pdfs
    except Exception as exc:
        if session is not None:
            raise
        static_error = exc
        LOG.warning("Static AMFI page fetch failed; trying browser fallback: %s", exc)

    if session is not None:
        return []
    dynamic, inspected = _playwright_candidates(month, int(year))
    found = _confirmed_period_pdfs(dynamic)
    if not found:
        if static_error is not None:
            LOG.warning("The AMFI browser fallback found no matching links after static fetch error: %s", static_error)
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
