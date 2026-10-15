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

def extract_pdf_tables(pdf_path, scheme, category, valuation_date="", source_url=""):
    records, path = [], Path(pdf_path)
    with pdfplumber.open(path) as pdf:
        for page_no, page in enumerate(pdf.pages, 1):
            for table_no, table in enumerate(page.extract_tables() or [], 1):
                for row in table or []:
                    cells = [re.sub(r"\s+"," ",str(c or "")).strip() for c in row]
                    if len(cells) < 2: continue
                    joined = " ".join(cells).lower()
                    if any(x in joined for x in ("name of instrument","percentage to net assets","portfolio disclosure")) and not any(_number(c) is not None for c in cells[1:]):
                        continue
                    company = next((c for c in cells if len(c)>=3 and not MONEY_RE.match(c) and _number(c) is None), "")
                    nums = [_number(c) for c in cells]
                    nums = [n for n in nums if n is not None]
                    if not company or len(company)>180 or not nums or nums[-1]>100: continue
                    records.append({"scheme":scheme,"category":category,"company":company,
                        "normalized_company":normalize_company(company),"weight_pct":nums[-1],"sector":"",
                        "instrument_type":"Unclassified","valuation_date":valuation_date,"source_url":source_url,
                        "source_pdf":path.name,"page":page_no,"table":table_no,
                        "extraction_method":"pdfplumber_table_heuristic","validation_status":"needs_manual_review",
                        "raw_row":" | ".join(cells)})
    cols = ["scheme","category","company","normalized_company","weight_pct","sector","instrument_type",
        "valuation_date","source_url","source_pdf","page","table","extraction_method","validation_status","raw_row"]
    return pd.DataFrame(records,columns=cols)

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
    dup=rows.duplicated(["scheme","normalized_company"],keep=False) & ~invalid & ~blank
    for i in rows.index[dup]:
        issues.append({"severity":"warning","row":int(i)+2,"issue":"Duplicate normalized issuer in scheme; review before aggregation."})
    for scheme,g in rows.loc[~invalid & ~blank].groupby("scheme"):
        total=g.weight_pct.sum()
        if total>100.5: issues.append({"severity":"warning","scheme":scheme,"issue":f"Reported weights sum to {total:.2f}%; check duplicate/mixed holdings."})
        elif total<95: issues.append({"severity":"info","scheme":scheme,"issue":f"Reported weights sum to {total:.2f}%; portfolio may be partial."})
    return rows,pd.DataFrame(issues)
