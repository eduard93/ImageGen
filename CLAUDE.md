# CLAUDE.md

Guidance for working in this repo. Read `ARCHITECTURE.md` for the full design;
this file is the short list of what matters day to day.

## What this is

A single-user, local, browser-based Gemini image-generation app. Simplicity and
readability for a non-expert beat cleverness — keep it that way.

## Run / dev

```bash
uv sync                                     # install deps
uv run python -m backend.main               # serve at http://127.0.0.1:8001 (port 8001, auto-reload)
uv run python -c "..."                       # ALWAYS use `uv run`, not bare python
```

- Bare `python` fails with `ModuleNotFoundError: No module named 'google'` — the
  deps live in uv's managed venv. Use `uv run` for every Python invocation.
- The frontend has **no build step**. Edit `frontend/*` and refresh the browser.
- `--reload` picks up backend changes automatically.

## ⚠️ Never destroy `data/`

`data/` holds the user's real, persistent history (`app.db`, `images/`,
`backups/`). **Do not** run `rm -rf data`, delete image files, or overwrite the
DB as part of testing or cleanup — this has caused real data loss before. If you
need throwaway state, use a temp path you created. Only touch files you created.

## Where things live (one place per concern)

| Concern | File |
|---|---|
| ALL Google/Gemini API calls | `backend/gemini.py` |
| Routes + background job worker | `backend/main.py` |
| SQLite schema + access | `backend/db.py` |
| Paths + default settings | `backend/config.py` |
| Request/response models | `backend/schemas.py` |
| UI markup (Alpine bindings) | `frontend/index.html` |
| ALL UI logic (`app()` component) | `frontend/app.js` |
| ALL styling (CSS vars up top) | `frontend/styles.css` |

If the `google-genai` SDK surface changes, `gemini.py` is the **only** file to
touch. Keep it that way — don't leak SDK types/calls into `main.py`.

## The big gotcha: Vertex-only `ImageConfig` fields

Several `ImageConfig` fields are accepted **only** in Vertex/Enterprise mode and
raise `ValueError` on the Gemini **Developer API** (the default api-key mode),
failing *every* generation:

- `person_generation`
- `output_mime_type`

Rule: pass these **only when `use_vertex` is true**. In `main.py`:

```python
person_generation = "ALLOW_ALL" if use_vertex else None
output_mime_type  = "image/png" if use_vertex else None
```

`create_generate_config` adds each to `ImageConfig` **only if truthy**, so on the
Developer API they're simply omitted and the model picks the output format.
Before adding any new `ImageConfig`/`GenerateContentConfig` field, check whether
it's Vertex-only and gate it the same way.

## Two auth modes (Settings → `use_vertex`)

- **Developer API** (default): `genai.Client(api_key=...)`, key from AI Studio.
- **Vertex AI**: `genai.Client(vertexai=True, project=..., location=...)`, auth
  via `gcloud auth application-default login` (no api key). Requires GCP billing.

Both are built in `gemini.make_client(...)`. The toggle just switches which one
is used, so both stay usable.

## Batch vs. sync (Settings → `use_batch`)

- `use_batch = true` → inline **Batch API**: N images = N inline requests in one
  job; polled at 5s intervals, 20-min timeout.
- `use_batch = false` → **synchronous** `generate_content`, one call per image.
- **Inline batch is Developer-API only.** Vertex batch needs a GCS bucket, so on
  Vertex use synchronous mode.
- Blocking SDK calls are wrapped in `asyncio.to_thread`; one asyncio task per
  generation → generations run concurrently.

## Settings conventions

- Stored in the `settings` table as JSON strings; **booleans are the strings
  `"true"`/`"false"`**, not real booleans. Read with
  `str(settings.get(key)).lower() == "true"`.
- Adding a setting = add a default to `config.DEFAULT_SETTINGS`, add the field to
  `SettingsUpdate` in `schemas.py`, restart (missing keys are seeded on startup),
  then wire it into the Settings modal + `openSettings`/`saveSettings` in
  `app.js`.
- `PUT /api/settings` ignores `None` fields, so partial updates (e.g. the theme
  toggle sending only `{theme}`) work.

## DB conventions

- Connection-per-call, rows as dicts (`sqlite3.Row`).
- Schema is `CREATE TABLE IF NOT EXISTS` + additive `_migrate()` (check
  `PRAGMA table_info`, `ALTER TABLE ADD COLUMN`). **Never** rewrite/drop tables;
  only add columns so existing databases upgrade in place.
- Image **bytes live on disk** (`data/images/`); the DB stores only metadata.

## Reference images & `@Name`

`build_request` interleaves a `Reference image @<label>:` text part before each
image so the model honors named references instead of guessing by position.
`_resolve_reference_refs` rebuilds labels to match the UI: library images use
`library_name`; unnamed uploads become `Image1`, `Image2`, … in attach order.

## Debugging generations

Completed model responses are dumped to the terminal (the one running uvicorn)
with base64 image data truncated — see `_log_model_response` / `debug_*` in
`gemini.py`. Watch that terminal when a generation misbehaves.

## Verifying changes cheaply

Prefer constructing configs / hitting the DB over triggering real generations
(which spend API quota). Example smoke test:

```bash
uv run python -c "from backend import gemini, main, config, db; \
print(gemini.create_generate_config('2K').image_config)"
```
