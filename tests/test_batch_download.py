import hashlib
import json
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

from src.mf_report import batch_download


def test_batch_zip_includes_only_period_matched_direct_pdfs(tmp_path, monkeypatch):
    bodies = {
        "https://cdn.example.org/september-2026.pdf": b"%PDF-september-content",
    }

    def fake_download_pdf(url):
        body = bodies[url]
        filename = Path(url.rsplit("/", 1)[-1])
        path = tmp_path / filename
        path.write_bytes(body)
        return {
            "url": url,
            "file": str(path),
            "bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
            "downloaded_at_utc": "2026-10-15T00:00:00+00:00",
        }

    monkeypatch.setattr(batch_download, "download_pdf", fake_download_pdf)
    candidates = [
        {
            "label": "September factsheet",
            "url": "https://cdn.example.org/september-2026.pdf",
            "fund_house_hint": "HDFC Mutual Fund",
            "is_direct_pdf": True,
            "period_keyword_match": True,
            "period_evidence": "URL contains selected month/year",
            "discovery_method": "static_html",
        },
        {
            "label": "August factsheet",
            "url": "https://cdn.example.org/august-2026.pdf",
            "is_direct_pdf": True,
            "period_keyword_match": False,
        },
        {
            "label": "Non-PDF factsheet page",
            "url": "https://cdn.example.org/factsheets",
            "is_direct_pdf": False,
            "period_keyword_match": True,
        },
    ]

    payload, included, errors = batch_download.download_candidates_zip(
        candidates, "September", 2026
    )

    assert len(included) == 1
    assert included[0]["archive_file"] == "september-2026.pdf"
    assert included[0]["sha256"] == hashlib.sha256(bodies[
        "https://cdn.example.org/september-2026.pdf"
    ]).hexdigest()
    assert len(errors) == 2

    with ZipFile(BytesIO(payload)) as archive:
        names = archive.namelist()
        assert "september-2026.pdf" in names
        assert "download_manifest.json" in names
        assert "README.txt" in names
        assert "august-2026.pdf" not in names
        manifest = json.loads(archive.read("download_manifest.json"))
        assert manifest["requested_period"] == "September 2026"
        assert manifest["included_pdf_count"] == 1
        assert manifest["files"][0]["source_url"] == (
            "https://cdn.example.org/september-2026.pdf"
        )
        assert "Verify the AMC" in manifest["important_note"]


def test_batch_zip_enforces_combined_size_limit(tmp_path, monkeypatch):
    def fake_download_pdf(url):
        path = tmp_path / url.rsplit("/", 1)[-1]
        path.write_bytes(b"%PDF-123456")
        return {
            "url": url,
            "file": str(path),
            "bytes": path.stat().st_size,
            "sha256": "abc",
            "downloaded_at_utc": "2026-10-15T00:00:00+00:00",
        }

    monkeypatch.setattr(batch_download, "download_pdf", fake_download_pdf)
    candidates = [{
        "label": "September PDF",
        "url": "https://cdn.example.org/september-2026.pdf",
        "is_direct_pdf": True,
        "period_keyword_match": True,
    }]

    payload, included, errors = batch_download.download_candidates_zip(
        candidates, "September", 2026, max_archive_bytes=1
    )

    assert included == []
    assert len(errors) == 1
    with ZipFile(BytesIO(payload)) as archive:
        manifest = json.loads(archive.read("download_manifest.json"))
        assert manifest["included_pdf_count"] == 0
        assert manifest["errors_or_skipped_files"] == errors
