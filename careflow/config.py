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


def api_key() -> str | None:
    return os.getenv("PORTKEY_API_KEY")
