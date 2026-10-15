import pytest

from src.mf_report import discovery


class RedirectingResponse:
    url = "http://cdn.example.org/factsheet.pdf"
    headers = {"Content-Type": "application/pdf"}

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size=65536):
        yield b"%PDF-test"

    def close(self):
        return None


class FakeSession:
    def get(self, *args, **kwargs):
        return RedirectingResponse()


def test_pdf_download_rejects_https_to_http_redirect(tmp_path, monkeypatch):
    monkeypatch.setattr(discovery, "ensure_directories", lambda: None)
    monkeypatch.setattr(discovery, "REQUEST_DELAY_SECONDS", 0)
    monkeypatch.setattr(discovery, "PROCESSED_DIR", tmp_path / "processed")
    raw_dir = tmp_path / "raw"

    with pytest.raises(ValueError, match="redirected away from HTTPS"):
        discovery.download_pdf(
            "https://cdn.example.org/factsheet.pdf",
            destination_dir=raw_dir,
            session=FakeSession(),
        )

    assert not list(raw_dir.iterdir())
