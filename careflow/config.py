"""Settings and .env loading.

The app looks for .env first in the repo folder, then in the folder above it
(as required by the course: the teaching staff keep their .env one level up).
Only PORTKEY_API_KEY is read as a secret. It is never printed or logged.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DB_PATH = Path(os.getenv("CAREFLOW_DB", str(DATA / "careflow.db")))
UPLOADS = DATA / "uploads"


def _load_env_file(path: Path) -> None:
    """Minimal .env reader: KEY=VALUE lines. Does not overwrite existing env vars."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_env() -> list[str]:
    """Load .env from repo folder, then parent folder. Returns which files were found."""
    found = []
    for candidate in (ROOT / ".env", ROOT.parent / ".env"):
        if candidate.is_file():
            _load_env_file(candidate)
            found.append(str(candidate))
    return found


ENV_FILES = load_env()

# --- Model (course rule: gpt-6-luna via Portkey only) -------------------------
MODEL = "gpt-6-luna"
BASE_URL = "https://api.portkey.ai/v1"

# Prices in USD per 1M tokens (third-party listings, Oct 2026; verify on OpenAI's pricing page).
PRICE_INPUT = 0.10
PRICE_CACHED_INPUT = 0.01
PRICE_OUTPUT = 0.50

# --- Safety / cost guards -----------------------------------------------------
DAILY_TOKEN_BUDGET = int(os.getenv("CAREFLOW_DAILY_TOKEN_BUDGET", "300000"))  # whole app, per day
USER_DAILY_BUDGET = int(os.getenv("CAREFLOW_USER_TOKEN_BUDGET", "150000"))    # per logged-in user, per day
PER_PATIENT_DAILY_BUDGET = int(os.getenv("CAREFLOW_PATIENT_TOKEN_BUDGET", "120000"))
MAX_OUTPUT_TOKENS = 2500
MAX_AGENT_STEPS = 5  # tool-calling loop cap for the Q&A agent
MAX_UPLOAD_MB = 15
MAX_PAGES = 6

# Image settings (cost lever): longest side in pixels sent to the model.
IMAGE_MAX_SIDE = int(os.getenv("CAREFLOW_IMAGE_MAX_SIDE", "1600"))
IMAGE_JPEG_QUALITY = 80

# Mock mode: lets the UI and tests run without an API key (canned model replies).
MOCK = os.getenv("CAREFLOW_MOCK", "0") == "1"

# --- Login security -----------------------------------------------------------
PBKDF2_ITERATIONS = int(os.getenv("CAREFLOW_PBKDF2_ITERATIONS", "600000"))  # OWASP 2023 for PBKDF2-SHA256
SESSION_IDLE_MINUTES = 30        # signed out after 30 min without activity
SESSION_MAX_HOURS = 12           # and after one shift at most
MAX_FAILED_LOGINS = 5            # then the account is locked ...
LOCKOUT_MINUTES = 15             # ... for this long
MIN_PASSWORD_LENGTH = 10
COOKIE_SECURE = os.getenv("CAREFLOW_HTTPS", "0") == "1"  # set when served over HTTPS

# --- Email reminders ----------------------------------------------------------
# Reminders are only ever sent after a consultant authorises them. Without SMTP settings
# they are written to data/outbox/ as .eml files (nothing leaves the computer).
OUTBOX = DATA / "outbox"
SMTP_HOST = os.getenv("CAREFLOW_SMTP_HOST")
SMTP_PORT = int(os.getenv("CAREFLOW_SMTP_PORT", "587"))
SMTP_USER = os.getenv("CAREFLOW_SMTP_USER")
SMTP_PASSWORD = os.getenv("CAREFLOW_SMTP_PASSWORD")
HOSPITAL_NAME = os.getenv("CAREFLOW_HOSPITAL_NAME", "CareFlow Demo Nursing Home")
HOSPITAL_EMAIL = os.getenv("CAREFLOW_HOSPITAL_EMAIL", "reminders@careflow-demo.invalid")
HOSPITAL_PHONE = os.getenv("CAREFLOW_HOSPITAL_PHONE", "+91-00000-00000 (demo)")


def api_key() -> str | None:
    return os.getenv("PORTKEY_API_KEY")
