"""Build traceable ZIP archives from selected month-qualified factsheet candidates."""
from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from .discovery import download_pdf

DEFAULT_MAX_ARCHIVE_BYTES = 100 * 1024 * 1024


def download_candidates_zip(
    candidates: list[dict],
    month: str,
    year: int,
    max_archive_bytes: int = DEFAULT_MAX_ARCHIVE_BYTES,
) -> tuple[bytes, list[dict], list[str]]:
    """Download qualified PDF candidates into a ZIP with a source/checksum manifest.

    Only direct PDF candidates carrying period-match evidence are considered. Each
    individual download still receives the normal HTTPS, PDF-signature and file-size
    checks from :func:`download_pdf`. The combined uncompressed PDF payload is capped
    so the Streamlit response cannot unexpectedly become enormous.
    """
    requested_period = f"{month} {int(year)}"
    included: list[dict] = []
    errors: list[str] = []
    total_bytes = 0
    archive_buffer = BytesIO()

    with ZipFile(archive_buffer, mode="w", compression=ZIP_DEFLATED) as archive:
        for candidate in candidates:
            label = str(candidate.get("label") or candidate.get("url") or "PDF candidate")
            url = str(candidate.get("url") or "").strip()
            if not candidate.get("is_direct_pdf"):
                errors.append(f"Skipped {label}: candidate is not marked as a direct PDF.")
                continue
            if not candidate.get("period_keyword_match"):
                errors.append(f"Skipped {label}: no confirmed match for {requested_period}.")
                continue
            if not url:
                errors.append(f"Skipped {label}: URL is missing.")
                continue

            try:
                download_record = download_pdf(url)
                file_path = Path(download_record["file"])
                file_size = file_path.stat().st_size
                if total_bytes + file_size > max_archive_bytes:
                    errors.append(
                        f"Downloaded {label}, but left it out of the ZIP because the combined "
                        f"PDF limit ({max_archive_bytes:,} bytes) would be exceeded. "
                        f"The file remains in the application's raw factsheet folder."
                    )
                    continue

                archive_name = file_path.name
                archive.write(file_path, arcname=archive_name)
                total_bytes += file_size
                included.append(
                    {
                        "fund_house_hint": candidate.get("fund_house_hint", "Unknown — verify manually"),
                        "label": label,
                        "source_url": url,
                        "final_url": download_record.get("url", url),
                        "archive_file": archive_name,
                        "bytes": file_size,
                        "sha256": download_record.get("sha256", ""),
                        "downloaded_at_utc": download_record.get("downloaded_at_utc", ""),
                        "requested_period": requested_period,
                        "period_evidence": candidate.get("period_evidence", ""),
                        "discovery_method": candidate.get("discovery_method", ""),
                    }
                )
            except Exception as exc:
                errors.append(f"Failed {label} ({url}): {type(exc).__name__}: {exc}")

        manifest = {
            "requested_period": requested_period,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "included_pdf_count": len(included),
            "combined_pdf_bytes": total_bytes,
            "selection_rule": (
                "Each included candidate had a month/year match in the URL, link label, "
                "unambiguous page context, or confirmed month/year selectors on the same AMC page/frame."
            ),
            "important_note": (
                "Discovery evidence is not a guarantee that the PDF contents are correct. "
                "Verify the AMC, exact scheme, factsheet publication month, and portfolio valuation date "
                "inside every PDF before using its holdings for analysis."
            ),
            "files": included,
            "errors_or_skipped_files": errors,
        }
        archive.writestr("download_manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
        archive.writestr(
            "README.txt",
            (
                f"Axiom Mutual Fund Intelligence — {requested_period}\n\n"
                f"PDFs included: {len(included)}\n"
                f"Requested period: {requested_period}\n\n"
                "Open download_manifest.json for the source URLs, checksum, discovery evidence, "
                "download timestamp and any skipped/failed files. Verify each PDF's actual cover/title, "
                "AMC, scheme name and portfolio valuation date before analysis. A factsheet publication "
                "month can differ from the portfolio valuation date printed inside it.\n"
            ),
        )

    return archive_buffer.getvalue(), included, errors
