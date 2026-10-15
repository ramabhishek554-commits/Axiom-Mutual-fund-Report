from src.mf_report.discovery import (
    _payload_candidates,
    _period_matches,
    _period_evidence,
    _context_has_competing_period,
    _dom_selected_period_pdfs,
    _confirmed_period_pdfs,
    _identify_fund_house,
    discover_factsheet_links,
)

class FakeResponse:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        return None

class FakeSession:
    def __init__(self, text):
        self.response = FakeResponse(text)

    def get(self, *args, **kwargs):
        return self.response

def test_period_matching_accepts_month_name_and_numeric_format():
    assert _period_matches("September 2026 factsheet.pdf", "September", 2026)
    assert _period_matches("factsheet_09-2026.pdf", "September", 2026)
    assert not _period_matches("August 2026 factsheet.pdf", "September", 2026)

def test_static_discovery_finds_selected_period_pdf():
    html = """
    <html><body>
      <section><h3>September 2026</h3>
        <a href="https://files.example.org/september-2026-factsheet.pdf">Download PDF</a>
      </section>
    </body></html>
    """
    results = discover_factsheet_links("September", 2026, session=FakeSession(html))
    assert len(results) == 1
    assert results[0]["url"] == "https://files.example.org/september-2026-factsheet.pdf"
    assert results[0]["is_direct_pdf"] is True
    assert results[0]["discovery_method"] == "static_html"

def test_static_discovery_does_not_return_other_month():
    html = '<a href="https://files.example.org/august-2026-factsheet.pdf">August 2026 PDF</a>'
    results = discover_factsheet_links("September", 2026, session=FakeSession(html))
    assert results == []

def test_payload_discovery_uses_month_and_year_metadata():
    payload = {
        "data": [
            {"month": "September", "year": 2026, "pdfUrl": "https://files.example.org/factsheet.pdf"},
            {"month": "August", "year": 2026, "pdfUrl": "https://files.example.org/old-factsheet.pdf"},
        ]
    }
    results = _payload_candidates(payload, "September", 2026, "https://www.amfiindia.com/api/factsheets")
    assert [item["url"] for item in results] == ["https://files.example.org/factsheet.pdf"]
    assert results[0]["discovery_method"] == "amfi_json"

def test_discovery_rejects_insecure_http_links():
    html = '<a href="http://files.example.org/September-2026-factsheet.pdf">September 2026 PDF</a>'
    assert discover_factsheet_links("September", 2026, session=FakeSession(html)) == []


def test_fund_house_hint_from_hdfc_domain():
    fund_house, evidence = _identify_fund_house(
        "https://files.hdfcfund.com/s3fs-public/2026-09/HDFC-factsheet.pdf",
        "Download PDF",
        "September 2026 factsheet",
    )
    assert fund_house == "HDFC Mutual Fund"
    assert "URL/domain" in evidence


def test_fund_house_hint_from_surrounding_context():
    fund_house, evidence = _identify_fund_house(
        "https://cdn.example.org/factsheet-september-2026.pdf",
        "Download PDF",
        "HDFC Mutual Fund | September 2026 | Factsheet",
    )
    assert fund_house == "HDFC Mutual Fund"
    assert "context" in evidence


def test_fund_house_remains_unknown_when_not_supported_by_evidence():
    fund_house, evidence = _identify_fund_house(
        "https://cdn.example.org/september-2026.pdf",
        "Download PDF",
        "September 2026 factsheet",
    )
    assert fund_house == "Unknown — verify manually"
    assert evidence == "No reliable AMC name detected"


def test_selected_period_pdf_scan_excludes_other_month_and_undated_pdf():
    class FakeLocator:
        def evaluate_all(self, _script):
            return [
                {
                    "href": "https://cdn.example.org/September-2026-factsheet.pdf",
                    "label": "Download PDF",
                    "context": "September 2026 factsheet",
                },
                {
                    "href": "https://cdn.example.org/August-2026-factsheet.pdf",
                    "label": "Download PDF",
                    "context": "August 2026 factsheet",
                },
                {
                    "href": "https://cdn.example.org/factsheet.pdf",
                    "label": "Download PDF",
                    "context": "Download latest",
                },
            ]

    class FakePage:
        def locator(self, _selector):
            return FakeLocator()

    results = _dom_selected_period_pdfs(FakePage(), "September", 2026)
    assert [row["url"] for row in results] == [
        "https://cdn.example.org/September-2026-factsheet.pdf"
    ]
    assert all(row["period_keyword_match"] for row in results)


def test_confirmed_results_only_contain_date_matched_pdf_candidates():
    records = [
        {
            "url": "https://cdn.example.org/September-2026.pdf",
            "is_direct_pdf": True,
            "period_keyword_match": True,
        },
        {
            "url": "https://cdn.example.org/August-2026.pdf",
            "is_direct_pdf": True,
            "period_keyword_match": False,
        },
        {
            "url": "https://cdn.example.org/September-2026",
            "is_direct_pdf": False,
            "period_keyword_match": True,
        },
    ]
    results = _confirmed_period_pdfs(records)
    assert [row["url"] for row in results] == ["https://cdn.example.org/September-2026.pdf"]



def test_period_evidence_accepts_unambiguous_context():
    matched, evidence = _period_evidence(
        "https://cdn.example.org/factsheet.pdf",
        "Download PDF",
        "HDFC Mutual Fund — September 2026 factsheet",
        "September",
        2026,
    )
    assert matched is True
    assert evidence == "Nearby context uniquely identifies selected month/year"


def test_period_evidence_rejects_context_listing_multiple_periods():
    context = "Monthly factsheets: September 2026, August 2026 and July 2026"
    assert _context_has_competing_period(context, "September", 2026) is True
    matched, evidence = _period_evidence(
        "https://cdn.example.org/factsheet.pdf",
        "Download PDF",
        context,
        "September",
        2026,
    )
    assert matched is False
    assert evidence == "Nearby context contains multiple months/years"


def test_explicit_pdf_url_period_wins_over_broad_context():
    matched, evidence = _period_evidence(
        "https://cdn.example.org/september-2026-factsheet.pdf",
        "Download PDF",
        "Monthly factsheets: September 2026, August 2026 and July 2026",
        "September",
        2026,
    )
    assert matched is True
    assert evidence == "URL contains selected month/year"


def test_period_evidence_handles_numeric_competing_months():
    assert _context_has_competing_period(
        "Available reports: 09-2026 and 08-2026", "September", 2026
    ) is True
