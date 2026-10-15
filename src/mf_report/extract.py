"""Best-effort table extraction; every PDF-derived row requires review."""
import re
from pathlib import Path
import pandas as pd
import pdfplumber

MONEY_RE = re.compile(r"^[₹Rs.\s]*[\d,]+(?:\.\d+)?$")

def normalize_company(value):
    text = str(value or "").strip().lower()
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"\b(ltd|limited|industries|industry|company|co|incorporated|inc)\b", " ", text)
    return " ".join(re.sub(r"[^a-z0-9]+"," ",text).split())

def _number(value):
    if value is None: return None
    text = str(value).strip().replace(",","").replace("%","")
    if not text or text in {"-","—","–","nan","None"}: return None
    try:
        x = float(text)
        return x if x >= 0 else None
    except ValueError:
        return None


def _fold_scheme_text(value):
    """Normalize scheme headings without removing meaningful scheme words."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).split())


def _looks_like_other_scheme_heading(line):
    """Conservatively identify likely fund titles near the top of a PDF page."""
    normalized = _fold_scheme_text(line)
    words = normalized.split()
    if len(words) < 3 or len(words) > 14:
        return False
    generic_prefixes = (
        "fund manager", "fund facts", "fund performance", "investment objective",
        "portfolio of", "portfolio disclosure", "category of scheme", "scheme information",
        "scheme details", "name of scheme", "mutual fund investments", "riskometer",
    )
    if any(normalized.startswith(prefix) for prefix in generic_prefixes):
        return False
    # AMC mastheads such as "HDFC Mutual Fund" identify the fund house, not a scheme.
    if normalized.endswith(" mutual fund") or normalized == "mutual fund":
        return False
    return bool(re.search(r"\b(?:fund|etf|fof)\b", normalized))


def audit_scheme_headings(page_texts, scheme):
    """Return whether the selected scheme appears in page headings and other scheme-title hints."""
    selected = _fold_scheme_text(scheme)
    selected_found = False
    other_titles = set()
    for page_text in page_texts:
        # Scheme titles are normally near the top; do not match a mention deep in a
        # holdings table or a generic body paragraph as evidence of the page's scheme.
        top_lines = [line.strip() for line in str(page_text or "").splitlines()[:12] if line.strip()]
        for line in top_lines:
            normalized = _fold_scheme_text(line)
            if selected and selected in normalized:
                selected_found = True
                continue
            if _looks_like_other_scheme_heading(line):
                other_titles.add(normalized)
    return selected_found, sorted(other_titles)


WEIGHT_HEADER_MARKERS = (
    "% to nav", "% of nav", "percent to nav", "percentage to nav",
    "% to net assets", "% of net assets", "percent of net assets",
    "percentage of net assets", "weight (%)", "weight %", "portfolio weight",
    "weight to nav",
)


def _header_weight_columns(cells):
    """Return indexes whose headers explicitly describe portfolio percentages."""
    result = []
    for index, cell in enumerate(cells):
        text = re.sub(r"\s+", " ", str(cell or "").strip().lower())
        if any(marker in text for marker in WEIGHT_HEADER_MARKERS):
            result.append(index)
    return result


def _find_table_headers(table):
    """Find the row that best identifies holding-weight and sector columns."""
    best_row, best_weights, best_sectors = None, [], []
    for row_index, row in enumerate((table or [])[:10]):
        cells = [str(cell or "").strip() for cell in (row or [])]
        weights = _header_weight_columns(cells)
        if len(weights) > len(best_weights):
            best_row = row_index
            best_weights = weights
            best_sectors = [
                index for index, cell in enumerate(cells)
                if re.search(r"\b(?:industry|sector)\b", str(cell or ""), re.I)
            ]
    return best_row, best_weights, best_sectors


def _infer_weight_columns(cells):
    """Fallback for tables without recognizable headers; supports side-by-side 3-cell blocks."""
    numeric_positions = [
        index for index, cell in enumerate(cells)
        if _number(cell) is not None and _number(cell) <= 100
    ]
    if not numeric_positions:
        return []
    if len(numeric_positions) >= 2 and len(cells) >= 6:
        differences = [
            right - left for left, right in zip(numeric_positions, numeric_positions[1:])
        ]
        # Side-by-side mini-tables often have identical repeated column spacing,
        # e.g. company / industry / weight repeated across the page.
        if differences and len(set(differences)) == 1 and differences[0] >= 3:
            return numeric_positions
    return [numeric_positions[-1]]


def _rows_to_holding_records(
    table,
    scheme,
    category,
    valuation_date="",
    source_url="",
    source_pdf="",
    page_no=1,
    table_no=1,
):
    """Extract holdings from a table, including two portfolio columns on one PDF row."""
    records = []
    header_row, weight_columns, sector_columns = _find_table_headers(table)
    if not weight_columns:
        weight_columns = []

    for row_index, row in enumerate(table or []):
        cells = [re.sub(r"\s+", " ", str(cell or "")).strip() for cell in (row or [])]
        if len(cells) < 2 or not any(cells):
            continue

        joined = " ".join(cells).lower()
        if any(marker in joined for marker in WEIGHT_HEADER_MARKERS):
            row_weights = _header_weight_columns(cells)
            if row_weights:
                weight_columns = row_weights
                sector_columns = [
                    index for index, cell in enumerate(cells)
                    if re.search(r"\b(?:industry|sector)\b", str(cell or ""), re.I)
                ]
                continue
        if header_row is not None and row_index == header_row:
            continue

        chosen_weight_columns = [
            index for index in weight_columns
            if index < len(cells) and _number(cells[index]) is not None
        ]
        if not chosen_weight_columns:
            chosen_weight_columns = _infer_weight_columns(cells)
        if not chosen_weight_columns:
            continue

        previous_weight_index = -1
        for weight_index in chosen_weight_columns:
            weight = _number(cells[weight_index])
            if weight is None or not 0 <= weight <= 100:
                previous_weight_index = weight_index
                continue

            segment_start = previous_weight_index + 1
            segment = cells[segment_start:weight_index]
            previous_weight_index = weight_index
            if not segment:
                continue

            company_index = None
            company = ""
            for local_index, value in enumerate(segment):
                normalized = re.sub(r"\s+", " ", value).strip().lower()
                if (
                    len(value) >= 3
                    and not MONEY_RE.match(value)
                    and _number(value) is None
                    and normalized not in {
                        "company/instrument", "company", "instrument", "industry",
                        "sector", "industry/rating", "industry+ /rating",
                    }
                ):
                    company_index = segment_start + local_index
                    company = value
                    break

            if not company or len(company) > 180:
                continue
            folded = re.sub(r"[^a-z0-9]+", " ", company.lower()).strip()
            if folded in {"sub total", "subtotal", "grand total", "total"}:
                continue
            if any(
                phrase in folded
                for phrase in (
                    "percentage to nav", "percentage to net assets", "company instrument",
                    "equity equity related", "debt debt related",
                )
            ):
                continue

            sector = ""
            for sector_index in sector_columns:
                if segment_start <= sector_index < weight_index and sector_index < len(cells):
                    if sector_index != company_index:
                        sector = cells[sector_index]
                        break

            records.append({
                "scheme": scheme,
                "category": category,
                "company": company,
                "normalized_company": normalize_company(company),
                "weight_pct": weight,
                "sector": sector,
                "instrument_type": "Unclassified",
                "valuation_date": valuation_date,
                "source_url": source_url,
                "source_pdf": source_pdf,
                "page": page_no,
                "table": table_no,
                "extraction_method": "pdfplumber_table_heuristic",
                "validation_status": "needs_manual_review",
                "raw_row": " | ".join(cells),
            })
    return records


def extract_pdf_tables(pdf_path, scheme, category, valuation_date="", source_url=""):
    records, path = [], Path(pdf_path)
    with pdfplumber.open(path) as pdf:
        page_texts = [page.extract_text() or "" for page in pdf.pages]
        selected_found, other_titles = audit_scheme_headings(page_texts, scheme)
        if not selected_found:
            raise ValueError(
                f'The selected scheme "{scheme}" was not found in the page headings. '
                "Upload the scheme-specific official factsheet, check the exact scheme name, "
                "or use OCR and a cleaned CSV if this is a scanned PDF. No holdings were tagged."
            )
        if other_titles:
            examples = ", ".join(other_titles[:4])
            raise ValueError(
                "This PDF appears to contain more than one scheme section "
                f"(other heading(s): {examples}). To prevent mislabelled holdings, upload a "
                "scheme-specific PDF or split the AMC-wide PDF into one scheme per file first. "
                "No holdings were tagged."
            )

        for page_no, page in enumerate(pdf.pages, 1):
            for table_no, table in enumerate(page.extract_tables() or [], 1):
                records.extend(
                    _rows_to_holding_records(
                        table,
                        scheme,
                        category,
                        valuation_date,
                        source_url,
                        path.name,
                        page_no,
                        table_no,
                    )
                )

    cols = [
        "scheme", "category", "company", "normalized_company", "weight_pct", "sector",
        "instrument_type", "valuation_date", "source_url", "source_pdf", "page", "table",
        "extraction_method", "validation_status", "raw_row",
    ]
    return pd.DataFrame(records, columns=cols)

def validate_holdings(df):
    rows = df.copy()
    required = {"scheme","category","company","weight_pct"}
    missing = sorted(required-set(rows.columns))
    if missing:
        return rows,pd.DataFrame([{"severity":"error","issue":"Missing required columns: "+", ".join(missing)}])
    defaults = {"sector":"","instrument_type":"Unclassified","valuation_date":"","source_url":"","source_pdf":"",
                "page":None,"validation_status":"needs_manual_review"}
    for c,v in defaults.items():
        if c not in rows: rows[c]=v
    rows["valuation_date"] = rows["valuation_date"].fillna("").astype(str).str.strip()
    rows["weight_pct"] = pd.to_numeric(rows["weight_pct"],errors="coerce")
    rows["normalized_company"] = rows["company"].map(normalize_company)
    issues=[]
    invalid=rows.weight_pct.isna() | ~rows.weight_pct.between(0,100)
    blank=rows.company.fillna("").astype(str).str.strip().eq("")
    for i in rows.index[invalid]:
        issues.append({"severity":"error","row":int(i)+2,"issue":"Weight must be numeric from 0 to 100."})
        rows.loc[i,"validation_status"]="invalid_weight"
    for i in rows.index[blank]:
        issues.append({"severity":"error","row":int(i)+2,"issue":"Company/issuer name is blank."})
        rows.loc[i,"validation_status"]="missing_company"
    duplicate_keys = rows[["scheme", "normalized_company"]].copy()
    if "valuation_date" in rows:
        duplicate_keys["valuation_date"] = rows["valuation_date"].fillna("").astype(str).str.strip()
    dup=duplicate_keys.duplicated(keep=False) & ~invalid & ~blank
    for i in rows.index[dup]:
        issues.append({
            "severity":"warning",
            "row":int(i)+2,
            "issue":"Duplicate normalized issuer within the same scheme and valuation date; review before aggregation."
        })

    # Portfolio weights must be checked snapshot-by-snapshot. Summing several
    # historical months together would create false >100% warnings.
    group_cols = ["scheme", "valuation_date"] if "valuation_date" in rows else ["scheme"]
    valid_rows = rows.loc[~invalid & ~blank]
    for keys,g in valid_rows.groupby(group_cols, dropna=False):
        if isinstance(keys, tuple):
            scheme = keys[0]
            valuation_date = str(keys[1] or "").strip()
        else:
            scheme, valuation_date = keys, ""
        date_note = f" as of {valuation_date}" if valuation_date and valuation_date.lower() != "nan" else ""
        total=g.weight_pct.sum()
        if total>100.5:
            issues.append({
                "severity":"warning",
                "scheme":scheme,
                "valuation_date":valuation_date,
                "issue":f"Reported weights sum to {total:.2f}%{date_note}; check duplicate/mixed holdings."
            })
        elif total<95:
            issues.append({
                "severity":"info",
                "scheme":scheme,
                "valuation_date":valuation_date,
                "issue":f"Reported weights sum to {total:.2f}%{date_note}; portfolio may be partial."
            })
    return rows,pd.DataFrame(issues)
