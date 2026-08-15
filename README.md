# Image Studio

A simple, single-user, browser-based image-generation app.

- **Frontend:** plain HTML + [Alpine.js](https://alpinejs.dev/) via CDN — no build step.
  Edit `frontend/index.html`, `frontend/app.js`, `frontend/styles.css` and refresh.
- **Backend:** Python + FastAPI, packaged with [uv](https://docs.astral.sh/uv/).
- **Database:** SQLite (`data/app.db`). Images are stored as files under
  `data/images/`; only their metadata lives in the database.
- **Image generation:** Google Gemini **Batch API** (one job per request; jobs
  run concurrently). A synchronous fallback is available via Settings.

## Setup

```bash
# 1. Install dependencies into a managed virtualenv
uv sync

# 2. Run the app
uv run uvicorn backend.main:app --reload

# 3. Open the UI
#    http://127.0.0.1:8000
```

Then open **Settings** in the app and paste your Gemini API key. (Alternatively,
copy `.env.example` to `.env` and set `GEMINI_API_KEY` as a fallback.)

## Using it

- **New generation** (top-left) starts a fresh chat.
- Type a prompt. Click **📎 Upload** to add reference images, or **🖼 From
  library** to reuse a saved one.
- Each uploaded image gets a default reference `@Image1`, `@Image2`, … Type `@`
  in the prompt to autocomplete references and library names.
- Save a reference to the library with **+ Library** (give it a name → use it as
  `@YourName` in any future prompt).
- Choose **model**, **resolution** (1K/2K/4K), and **number of images**, then
  **Generate**. Status updates live while the batch job runs.
- On each result, **⭳ Save** downloads the image to disk; **+ Library** stores it.
- Open any past generation on the left and click **✎ Edit this prompt as a new
  generation** to tweak and re-run it.

## Where things live

```
backend/
  main.py      FastAPI app + routes + the background batch poller
  gemini.py    ALL Google API calls (edit here if the SDK surface changes)
  db.py        SQLite schema + helpers
  config.py    paths + default settings
  schemas.py   request/response models
frontend/
  index.html   layout (Alpine markup)
  app.js       all UI logic
  styles.css   all styling (colors are variables at the top)
data/          created at runtime: app.db + images/  (git-ignored)
```

## Backups

On every startup the app snapshots `data/app.db` into `data/backups/`
(the 15 most recent are kept). To restore, stop the server and copy a snapshot
over the live DB:

```bash
cp data/backups/app-YYYYmmdd-HHMMSS.db data/app.db
```

Image files themselves live in `data/images/`; back up that folder too if you
want a full copy.

## Using Vertex AI instead of the API key

The app can talk to Gemini two ways, switchable in **Settings**:

- **Gemini Developer API** (default) — a single API key from
  [Google AI Studio](https://aistudio.google.com). Simplest; what most setups use.
- **Vertex AI** (Google Cloud) — authenticated with your GCP project instead of
  a key. Needed for enterprise controls. In this mode `person_generation` is
  always set to `ALLOW_ALL`.

One-time GCP setup (you already have a project):

```bash
# 1. Enable the Vertex AI API on your project
gcloud services enable aiplatform.googleapis.com --project YOUR_PROJECT_ID

# 2. Give the app credentials (Application Default Credentials).
#    Run this yourself in the terminal — it opens a browser to sign in:
gcloud auth application-default login
```

That's it — no API key needed in Vertex mode. Then in **Settings**:

1. Check **Use Vertex AI**.
2. Enter your **project ID** and a **location** (`global`, or a region like
   `us-central1`).

Notes:
- Billing must be enabled on the project (Vertex has no free tier).
- The **inline Batch API** used here is a Developer-API feature. On Vertex, batch
  requires a Cloud Storage bucket, so **turn off "Use the async Batch API"** in
  Settings when running Vertex (the synchronous path works the same).
- Auth uses your local `gcloud` login; if you deploy elsewhere, set
  `GOOGLE_APPLICATION_CREDENTIALS` to a service-account key instead.

## Notes on the Gemini API

All Google interaction is isolated in `backend/gemini.py`. If your installed
`google-genai` version names a field differently (e.g. how batch results are
returned), that one file is the only place to adjust. Model IDs, the model list,
resolutions, and batch-vs-sync are all editable from **Settings** without
touching code.
