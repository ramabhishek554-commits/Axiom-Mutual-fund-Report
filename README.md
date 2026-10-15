# Axiom Mutual Fund Intelligence & Excel Analyzer

A Python/Streamlit research tool for reviewing Indian mutual-fund portfolios across Large Cap, Mid Cap and Small Cap categories. It discovers public links on AMFI's factsheet page, extracts candidate PDF tables for human review, calculates portfolio overlap/concentration/sector summaries, and exports an Excel workbook.

> **Data integrity first:** no fund holdings are fabricated. Extracted PDF rows are heuristic and require manual review. Verify the exact scheme, category, source and portfolio valuation date from the official AMC/AMFI document. This is not investment advice or a return prediction.

## Features

- Static AMFI link parsing, followed by Playwright/Chromium interaction with JavaScript-rendered AMC cards when the static page contains no month-qualified PDF links.
- Strict month/year-qualified PDF filtering: explicit URL/label dates are preferred, and nearby page context is accepted only when it identifies one unambiguous period. PDFs without clear evidence are omitted rather than guessed.
- PDF/CSV upload, reviewable holdings extraction, portfolio overlap, sector and concentration analysis.
- Downloadable Excel workbook with the 11 requested sheets, plus automated tests and CI.

## Quick start (Python 3.10+)

Windows PowerShell:
```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m playwright install chromium
python -m streamlit run app.py
```

macOS/Linux:
```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m playwright install chromium
streamlit run app.py
```

## Workflow

1. Open **Discover factsheets** and search the target month/year at the official [AMFI factsheet page](https://www.amfiindia.com/online-center/download-factsheets).
2. Follow the official AMC link. Only PDF candidates with clear month/year evidence are displayed. If a page container lists several months and the PDF URL/label is generic, that candidate is intentionally omitted to avoid showing the wrong period. This conservative filter can miss valid PDFs; verify the PDF cover and portfolio valuation date before using it.
3. Upload a factsheet PDF. Enter the exact scheme name, verify the Large/Mid/Small Cap classification using the official disclosure, and record the portfolio valuation date printed in the document. Publication date and portfolio valuation date are distinct.
4. Review and edit every extracted row. The PDF parser is heuristic; it does not reliably interpret every multi-column, debt, derivative or scanned layout. Scanned PDFs may need external OCR followed by manual CSV cleanup.
5. Alternatively upload a cleaned CSV with required columns `scheme, category, company, weight_pct`. Recommended extra columns: `sector, instrument_type, valuation_date, source_url, source_pdf, page, validation_status`. Use percentage units: 12.5 means 12.5%. Mark an issuer `Equity` in `instrument_type` only after checking the source.
6. Review quality findings, then download the 11-sheet workbook.

## Workbook sheets

1. Executive Summary
2. Large Cap Funds
3. Mid Cap Funds
4. Small Cap Funds
5. All Holdings
6. Company Comparison
7. Fund Overlap
8. Sector Exposure
9. Concentration Analysis
10. Historical Changes
11. Sources and Validation

## Calculation definitions

- Estimated investor allocation = assumed amount × holding percentage / 100. Default assumed amount is ₹1,00,000; it is not a disclosed scheme market value.
- Pairwise equity overlap (%) = sum of minimum matched issuer weights, when weights are expressed as percentage points. Comparisons with incompatible known valuation dates are returned as unknown.
- HHI = sum of squared decimal issuer weights. Effective holdings = 1 / HHI.
- Sector labels are taken only from supplied data; unknown sectors remain unknown.
- Historical weight changes do not establish purchases or sales. Concentration and overlap can be misleading if holdings are partial or dates are not comparable.
- Multiple plans/options sharing one portfolio should not be counted as independent portfolios.

## Tests

```bash
pytest -q
```

CI checks Python 3.10 and 3.12, compilation and tests. Synthetic records in tests validate arithmetic only; they are not market data.

## Local data folders

The app creates `data/raw_factsheets`, `data/processed`, `data/reference`, `reports/excel`, `reports/dashboard`, and `logs`. These local data/output folders are git-ignored. PDF downloads are HTTPS-only, size-limited and SHA-256 recorded in a manifest. Respect AMFI/AMC site terms and rate limits.

## Limitations

- Factsheet discovery checks static links, then uses Playwright/Chromium to inspect AMFI's JavaScript-rendered AMC cards. It still cannot guarantee complete coverage if the site's interaction or API changes. After installing requirements, run `python -m playwright install chromium` once in the active virtual environment.
- PDF table extraction is best-effort and every row requires review.
- OCR is not bundled.
- Category verification and sector mapping are user/source-driven, not inferred from scheme names.
- No performance forecast, buy/sell recommendation or personalized suitability analysis is included.
