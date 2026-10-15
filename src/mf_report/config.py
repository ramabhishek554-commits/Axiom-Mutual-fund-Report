from pathlib import Path
import os

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw_factsheets"
PROCESSED_DIR = DATA_DIR / "processed"
REFERENCE_DIR = DATA_DIR / "reference"
EXCEL_DIR = ROOT / "reports" / "excel"
DASHBOARD_DIR = ROOT / "reports" / "dashboard"
LOG_DIR = ROOT / "logs"
AMFI_FACTSHEET_URL = "https://www.amfiindia.com/online-center/download-factsheets"
MAX_DOWNLOAD_BYTES = int(os.getenv("MAX_DOWNLOAD_BYTES", str(30 * 1024 * 1024)))
HTTP_TIMEOUT_SECONDS = int(os.getenv("HTTP_TIMEOUT_SECONDS", "20"))
REQUEST_DELAY_SECONDS = float(os.getenv("REQUEST_DELAY_SECONDS", "1.0"))

def ensure_directories():
    for path in (RAW_DIR, PROCESSED_DIR, REFERENCE_DIR, EXCEL_DIR, DASHBOARD_DIR, LOG_DIR):
        path.mkdir(parents=True, exist_ok=True)
