"""Filesystem paths and default settings for the app.

Everything the app writes at runtime lives under ``data/`` next to this repo,
so the whole application state is: this code + the ``data/`` folder.
"""
from __future__ import annotations

from pathlib import Path

# Repo root = the folder that contains both `backend/` and `frontend/`.
BASE_DIR = Path(__file__).resolve().parent.parent

DATA_DIR = BASE_DIR / "data"
IMAGES_DIR = DATA_DIR / "images"
DB_PATH = DATA_DIR / "app.db"
FRONTEND_DIR = BASE_DIR / "frontend"

# Create the runtime folders on import so nothing else has to worry about it.
DATA_DIR.mkdir(parents=True, exist_ok=True)
IMAGES_DIR.mkdir(parents=True, exist_ok=True)


# --- Default settings -------------------------------------------------------
# These seed the `settings` table the first time the DB is created. After that,
# edit them in the Settings screen of the app (they are stored in SQLite).

DEFAULT_MODELS = [
    "gemini-3-pro-image-preview",  # "Nano Banana Pro" - supports 1K/2K/4K
    "gemini-2.5-flash-image",      # legacy, faster/cheaper
]

DEFAULT_SETTINGS = {
    "gemini_api_key": "",
    "default_prompt": "",
    # Global default system instruction. Per generation it can be toggled on/off
    # or overridden with a custom one.
    "system_instruction": "",
    "default_model": DEFAULT_MODELS[0],
    "models": DEFAULT_MODELS,          # list shown in the model dropdown
    "resolutions": ["1K", "2K", "4K"],  # allowed resolution options
    "theme": "dark",                    # UI theme: "dark" | "light"
    # If "true", generation goes through the async Batch API (what you asked
    # for). Flip to "false" to use the plain synchronous generate_content call
    # instead - handy if the Batch API misbehaves for image models.
    "use_batch": "true",
    # --- Vertex AI (Google Cloud) instead of the Gemini Developer API ---------
    # When "true", the app authenticates through Google Cloud (Application
    # Default Credentials) using the project/location below, instead of the
    # gemini_api_key above. See README for the one-time GCP setup.
    "use_vertex": "false",
    "gcp_project": "",       # your Google Cloud project id
    "gcp_location": "global",  # e.g. "global" or "us-central1"
}
