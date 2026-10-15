from datetime import date
from pathlib import Path
from urllib.parse import urlparse
import pandas as pd
import plotly.express as px
import streamlit as st
from src.mf_report.analytics import CATEGORIES, concentration_analysis, overlap_matrix, sector_exposure
from src.mf_report.config import AMFI_FACTSHEET_URL, ensure_directories
from src.mf_report.discovery import discover_factsheet_links, download_pdf
from src.mf_report.extract import extract_pdf_tables, validate_holdings
from src.mf_report.workbook import build_workbook, validate_workbook

st.set_page_config(page_title="Axiom | Mutual Fund Intelligence", page_icon="📊", layout="wide")
ensure_directories()
st.title("Axiom Mutual Fund Intelligence")
st.caption("Official-source-first portfolio research • transparent limitations • no fabricated market data")
st.warning("Research tool only. Not investment advice or a prediction of future returns.")

with st.sidebar:
    st.header("Analysis settings")
    amount = st.number_input("Assumed investment (₹)", min_value=0.0, value=100000.0, step=10000.0)
    st.caption("This estimates allocation from portfolio percentages; it is not the scheme's actual market value.")
    st.markdown(f"[Official AMFI factsheet page]({AMFI_FACTSHEET_URL})")

discover, upload, analyze, guide = st.tabs(
    ["Discover factsheets", "Upload / review holdings", "Analyze & export", "Guide"]
)

with discover:
    st.subheader("Discover official factsheet links")
    st.write(
        "Checks static AMFI links first, then uses a headless Chromium browser to inspect JavaScript-rendered "
        "AMC cards for PDFs explicitly associated with the selected month and year. Results without clear date evidence are omitted, " 
        "so some valid AMC PDFs may be missed rather than showing a potentially wrong month. Coverage is not guaranteed."
    )
    today = date.today()
    col1, col2 = st.columns(2)
    month = col1.selectbox(
        "Target month",
        ["September", "August", "October", "July", "June", "May", "April", "March",
         "February", "January", "December", "November"],
        index=0,
    )
    year = col2.number_input("Year", min_value=2015, max_value=today.year, value=today.year, step=1)

    if st.button("Search AMFI page", type="primary"):
        # Clear old results first; always provide visible feedback even for an empty result.
        st.session_state["candidate_links"] = []
        st.session_state["discovery_attempted"] = True
        st.session_state["discovery_period"] = f"{month} {int(year)}"
        st.session_state["discovery_error"] = None
        try:
            with st.spinner(f"Checking AMFI's page and opening AMC cards for {month} {int(year)}…"):
                st.session_state["candidate_links"] = discover_factsheet_links(month, int(year))
        except Exception as exc:
            st.session_state["discovery_error"] = f"{type(exc).__name__}: {exc}"

    links = st.session_state.get("candidate_links", [])
    if st.session_state.get("discovery_attempted"):
        period = st.session_state.get("discovery_period", f"{month} {int(year)}")
        error = st.session_state.get("discovery_error")
        if error:
            st.error(f"AMFI discovery failed for {period}. Details: {error}")
            st.markdown(f"[Open the official AMFI factsheet page]({AMFI_FACTSHEET_URL})")
            st.info("You can continue by downloading a factsheet from the official AMC site and uploading the PDF in the next tab.")
        elif links:
            pdf_count = sum(bool(x.get("is_direct_pdf")) for x in links)
            st.success(
                f"Search completed for {period}: {len(links)} PDF candidate(s) with matching month/year evidence."
            )
            st.info(
                "Only PDF candidates with explicit URL/label dates, unambiguous nearby date context, or confirmed month/year controls on the same AMC page/frame are shown. "
                "Links in a container listing multiple months are omitted unless the URL/label identifies the selected period or the AMC's month/year controls were confirmed in that same view. "
                "**Fund house hints remain unverified**: open the PDF and confirm the AMC, exact scheme, factsheet month, "
                "and portfolio valuation date before analysis."
            )
            st.dataframe(
                pd.DataFrame(links),
                use_container_width=True,
                hide_index=True,
                column_config={
                    "fund_house_hint": st.column_config.TextColumn("Likely AMC / fund house"),
                    "fund_house_evidence": st.column_config.TextColumn("Why this was identified"),
                    "period_evidence": st.column_config.TextColumn("Why month/year matched"),
                    "verification_status": st.column_config.TextColumn("Verification status"),
                    "url": st.column_config.LinkColumn("Open candidate URL"),
                    "source_context": st.column_config.TextColumn("Page context"),
                },
            )
            direct = {
                f"{x.get('fund_house_hint', 'Unknown')} | {x['label']} — {x['url']}": x
                for x in links if x["is_direct_pdf"]
            }
            if direct:
                choice = st.selectbox("Direct PDF candidate", list(direct))
                if st.button("Download selected PDF"):
                    try:
                        st.json(download_pdf(direct[choice]["url"]))
                    except Exception as exc:
                        st.error(f"Download failed: {type(exc).__name__}: {exc}")
            else:
                st.info("Candidates were found, but none is a direct PDF. Open the AMC link and select the requested month's factsheet there.")
        else:
            st.warning(
                f"The search completed for {period}, but neither the static parser nor the browser-assisted scan found "
                "a PDF link that could be tied confidently to the selected month. AMFI's interface may have changed, "
                "or the AMC factsheet may not be posted yet. This does not prove that factsheets are unavailable."
            )
            st.markdown(f"[Open the official AMFI factsheet page]({AMFI_FACTSHEET_URL})")
            st.markdown(
                "Open the relevant AMC card on AMFI, choose the requested month if it is offered, and download the PDF. "
                "Then use **Upload / review holdings** to process it. No holdings are fabricated. If the browser fallback "
                "cannot launch, install Chromium with the command documented in README.md and restart Streamlit."
            )

with upload:
    st.subheader("Upload factsheet and extract candidate holdings")
    st.caption("Extraction is heuristic. All extracted rows require manual verification against the source PDF.")
    pdf = st.file_uploader("Official AMC factsheet PDF", type=["pdf"], key="pdf")
    with st.form("pdf_metadata"):
        scheme = st.text_input("Exact scheme name")
        category = st.selectbox("Category verified from official disclosure", CATEGORIES)
        valuation_date = st.text_input("Portfolio valuation date (YYYY-MM-DD if known)")
        source_url = st.text_input("Official factsheet URL")
        submitted = st.form_submit_button("Extract tables for review")
    if submitted:
        if not pdf or not scheme.strip():
            st.error("Upload a PDF and enter its exact scheme name.")
        else:
            try:
                filename = Path(pdf.name).name
                local = Path("data/raw_factsheets") / filename
                local.parent.mkdir(parents=True, exist_ok=True)
                local.write_bytes(pdf.getvalue())
                extracted = extract_pdf_tables(local, scheme.strip(), category, valuation_date.strip(), source_url.strip())
                if extracted.empty:
                    st.warning("No usable tables found. Scanned PDFs or irregular layouts may need OCR/manual CSV cleanup.")
                st.session_state["holdings"] = pd.concat(
                    [st.session_state.get("holdings", pd.DataFrame()), extracted], ignore_index=True
                )
                st.success(f"Added {len(extracted)} candidate rows; review before analysis.")
            except Exception as exc:
                st.error(f"Extraction failed: {type(exc).__name__}: {exc}")
    st.divider()
    csv = st.file_uploader("Or upload cleaned holdings CSV", type=["csv"], key="csv")
    if csv:
        try:
            candidate = pd.read_csv(csv)
            st.dataframe(candidate.head(30), use_container_width=True, hide_index=True)
            if st.button("Use uploaded CSV"):
                st.session_state["holdings"] = candidate
                st.success(f"Loaded {len(candidate)} rows.")
        except Exception as exc:
            st.error(f"CSV could not be read: {type(exc).__name__}: {exc}")
    current = st.session_state.get("holdings", pd.DataFrame())
    if not current.empty:
        edited = st.data_editor(current, num_rows="dynamic", use_container_width=True, key="editor")
        if st.button("Save edited holdings"):
            st.session_state["holdings"] = edited
            st.success("Edits saved in this session.")
        st.download_button("Download holdings CSV", current.to_csv(index=False).encode("utf-8"),
                           "holdings_review.csv", "text/csv")

with analyze:
    holdings = st.session_state.get("holdings", pd.DataFrame())
    if holdings.empty:
        st.info("No holdings loaded. Discover links or upload a factsheet/cleaned CSV. No sample market data is displayed.")
    else:
        clean, issues = validate_holdings(holdings)
        valid = clean[
            clean.weight_pct.notna()
            & clean.company.fillna("").astype(str).str.strip().ne("")
            & ~clean.validation_status.eq("invalid_weight")
        ]
        a, b, c = st.columns(3)
        a.metric("Rows", len(clean))
        b.metric("Rows with usable names/weights", len(valid))
        c.metric("Quality findings", len(issues))
        if not issues.empty:
            st.dataframe(issues, use_container_width=True, hide_index=True)
        display = valid.copy()
        display["estimated_allocation_inr"] = display.weight_pct * amount / 100
        st.subheader("Holdings and illustrative allocation")
        st.dataframe(display, use_container_width=True, hide_index=True)
        concentration = concentration_analysis(valid)
        if not concentration.empty:
            st.subheader("Concentration")
            st.dataframe(concentration, use_container_width=True, hide_index=True)
            st.plotly_chart(
                px.bar(concentration, x="scheme", y="top_10_weight_pct",
                       title="Top 10 issuer weight by scheme",
                       labels={"top_10_weight_pct": "Top 10 weight (%)"}),
                use_container_width=True,
            )
        sectors = sector_exposure(valid)
        if not sectors.empty:
            st.subheader("Sector exposure (only supplied mappings)")
            st.dataframe(sectors, use_container_width=True, hide_index=True)
            mapped = sectors[sectors.sector.ne("Unknown / unmapped")]
            if not mapped.empty:
                st.plotly_chart(
                    px.bar(mapped, x="scheme", y="weight_pct", color="sector", title="Sector exposure"),
                    use_container_width=True,
                )
        matrix = overlap_matrix(valid)
        if not matrix.empty:
            st.subheader("Pairwise equity overlap")
            st.dataframe(matrix, use_container_width=True, hide_index=True)
            st.plotly_chart(
                px.imshow(matrix.set_index("scheme"), text_auto=".1f", aspect="auto", title="Overlap (%)"),
                use_container_width=True,
            )
        workbook = build_workbook(valid, amount)
        errors = validate_workbook(workbook)
        if errors:
            st.error("; ".join(errors))
        else:
            st.download_button(
                "Download 11-sheet Excel workbook", workbook, "axiom_mutual_fund_analysis.xlsx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", type="primary",
            )
        st.caption(
            "Check dates and data completeness before interpreting overlap or concentration. "
            "Weight shifts do not establish fund purchases/sales."
        )

with guide:
    st.subheader("Workflow and limitations")
    st.markdown("""
1. Search the official AMFI factsheet page, then open the relevant AMC source.
2. Download the desired month (September, August or latest suitable publication) from AMFI/AMC. Record the PDF publication date separately from the portfolio valuation date.
3. Verify scheme category from official disclosure. Do not count multiple plans/options sharing the same portfolio as independent portfolios.
4. Extract candidate holdings or upload a cleaned CSV; review each row, issuer, weight, sector, asset type and valuation date.
5. Export the 11-sheet workbook and retain source PDFs locally for audit.

**Data policy:** no synthetic fund data is included. Sector labels are never guessed. Unknown instrument types are excluded from equity overlap/concentration until classified as Equity. OCR is not bundled; scanned PDFs may require an OCR tool and manual CSV cleanup. Discovery may miss JavaScript-generated or nested AMC links. Downloads are rate-limited and size-limited.

**Formula notes:** estimated allocation = assumed amount × weight (%) / 100. Pairwise overlap = sum of minimum matched equity issuer weights in percentage units. HHI uses decimal weights; effective holdings = 1 / HHI. Partial holdings produce incomplete statistics.
""")
