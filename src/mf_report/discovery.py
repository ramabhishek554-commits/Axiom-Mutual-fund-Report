"""Conservative discovery/download helpers for official factsheet links."""
import hashlib, json, re, time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup
from .config import AMFI_FACTSHEET_URL, HTTP_TIMEOUT_SECONDS, MAX_DOWNLOAD_BYTES, RAW_DIR, PROCESSED_DIR, REQUEST_DELAY_SECONDS, ensure_directories

USER_AGENT = "AxiomMutualFundResearch/0.1 (respectful factsheet discovery)"

def discover_factsheet_links(month, year, session=None):
    client = session or requests.Session()
    response = client.get(AMFI_FACTSHEET_URL, timeout=HTTP_TIMEOUT_SECONDS, headers={"User-Agent":USER_AGENT})
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    tokens = {str(month).lower(), str(month)[:3].lower(), str(year)}
    found, seen = [], set()
    for a in soup.find_all("a", href=True):
        url = urljoin(AMFI_FACTSHEET_URL, a["href"].strip())
        label = " ".join(a.get_text(" ", strip=True).split())
        p = urlparse(url)
        if p.scheme not in {"http","https"} or not p.netloc or url in seen:
            continue
        seen.add(url)
        pdf = p.path.lower().endswith(".pdf")
        period_match = any(t in (label + " " + url).lower() for t in tokens)
        if pdf or period_match:
            found.append({"label":label or "(link text unavailable)","url":url,"is_direct_pdf":pdf,
                "period_keyword_match":period_match,"requested_period":f"{month} {year}",
                "discovered_at_utc":datetime.now(timezone.utc).isoformat(),"discovery_source":AMFI_FACTSHEET_URL})
    return found

def download_pdf(url, destination_dir=None, session=None):
    p = urlparse(url)
    if p.scheme != "https" or not p.netloc:
        raise ValueError("Only HTTPS download URLs are accepted.")
    ensure_directories()
    dest_dir = Path(destination_dir or RAW_DIR)
    dest_dir.mkdir(parents=True, exist_ok=True)
    time.sleep(max(0, REQUEST_DELAY_SECONDS))
    response = (session or requests.Session()).get(url, stream=True, timeout=HTTP_TIMEOUT_SECONDS, headers={"User-Agent":USER_AGENT})
    response.raise_for_status()
    chunks, size = [], 0
    for chunk in response.iter_content(65536):
        if not chunk: continue
        size += len(chunk)
        if size > MAX_DOWNLOAD_BYTES: raise ValueError("Download exceeded configured size limit.")
        chunks.append(chunk)
    content = b"".join(chunks)
    if not content.startswith(b"%PDF-"):
        raise ValueError("Downloaded response is not a PDF.")
    digest = hashlib.sha256(content).hexdigest()
    name = re.sub(r"[^A-Za-z0-9._-]+","_",Path(p.path).name or "factsheet.pdf")
    if not name.lower().endswith(".pdf"): name += ".pdf"
    target = dest_dir / f"{digest[:10]}_{name}"
    if not target.exists(): target.write_bytes(content)
    record = {"url":url,"file":str(target),"bytes":size,"sha256":digest,
        "downloaded_at_utc":datetime.now(timezone.utc).isoformat(),"content_type":response.headers.get("Content-Type","")}
    manifest = PROCESSED_DIR / "download_manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    try: items = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else []
    except (ValueError,OSError): items = []
    if not any(x.get("sha256")==digest for x in items): items.append(record)
    manifest.write_text(json.dumps(items,indent=2,ensure_ascii=False),encoding="utf-8")
    return record
