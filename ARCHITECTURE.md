# Architecture

## What this is

A **simple, single-user, browser-based image-generation app** built on Google
Gemini. It's meant to run locally for one person, be easy to read, and be easy
to modify without a build step or heavy framework knowledge.

Design priorities, in order:

1. **Simple enough for a non-expert to change.** Plain JavaScript + Alpine.js on
   the front, small FastAPI backend, one SQLite file. No bundler, no TypeScript,
   no ORM.
2. **All state is just code + a `data/` folder.** Delete `data/` and you're back
   to a fresh install; copy it and you've moved the whole app.
3. **One place per concern.** Every Google API call lives in `gemini.py`; all UI
   logic in `app.js`; all styling in `styles.css`; all paths/defaults in
   `config.py`.

## The shape of the app

```
Browser (Alpine.js SPA)          Python (FastAPI)                 Google
┌────────────────────────┐        ┌──────────────────────────┐     ┌──────────┐
│ index.html  markup     │  REST  │ main.py   routes + back- │     │ Gemini   │
│ app.js      all logic  │◄──────►│           ground jobs    │────►│ Developer│
│ styles.css  all styling│  JSON  │ gemini.py ALL Google I/O │     │  API  OR │
└────────────────────────┘        │ db.py     SQLite access  │     │ Vertex AI│
        ▲                         │ config.py paths/defaults │     └──────────┘
        │ static files            │ schemas.py request models│
        └─────────────────────────┤                          │
                                  └───────────┬──────────────┘
                                              │
                                        data/ (app.db, images/, backups/)
```

The FastAPI app serves the frontend as static files (`StaticFiles` mounted at
`/`, after the `/api/*` routes so it doesn't shadow them). There is no separate
web server — open `http://127.0.0.1:8001` and you get the whole app.

## Frontend

- **Alpine.js via CDN, no build step.** The entire UI is one Alpine component,
  `app()`, defined in `app.js`. `index.html` is declarative markup bound to that
  component's state and methods.
- **Chatbot-style layout:** a left sidebar of past generations, a central
  "chat" transcript for the selected generation (the prompt as a user bubble,
  the results as an assistant bubble), and a composer docked at the bottom.
- **Composer** is where you write a prompt, attach reference images (upload,
  drag-and-drop, or pick from the library), choose model / resolution / image
  count, toggle a system instruction, and hit **Generate**.
- **Live status** via polling: after submitting, the frontend polls
  `GET /api/generations/{id}` every few seconds until the job reaches a terminal
  state, updating both the sidebar entry and the open view.
- **Modals** for the image library and settings; a full-screen **lightbox** for
  viewing any image; a small toast for notices.
- **Theme** (light/dark) is a persisted setting, applied by setting
  `data-theme` on `<html>`; all colors are CSS variables at the top of
  `styles.css`.

## Backend

- **FastAPI**, packaged with **uv**. Run with `uv run python -m backend.main`
  (serves on port 8001; the port lives in `DEFAULT_PORT` in `main.py`).
- **SQLite** (`data/app.db`), accessed through a deliberately tiny layer
  (`db.py`): one connection per call, rows as dicts. Fast enough for one user,
  nothing to learn.
- **Schema** lives in `db.py` as `CREATE TABLE IF NOT EXISTS` statements plus a
  small additive `_migrate()` (checks `PRAGMA table_info` and `ALTER TABLE`s in
  new columns) so older databases upgrade in place.
- **Settings** are key/value rows, values stored as JSON strings (booleans are
  the strings `"true"`/`"false"`). `config.DEFAULT_SETTINGS` seeds any missing
  keys on startup, so adding a new setting is: add a default, restart.

### Data model

- **`settings`** — key/value app configuration.
- **`images`** — one row per image file on disk (`data/images/`), with
  `kind` = `upload` | `generated`, and optional `in_library` + unique
  `library_name` for library entries. The **bytes live on disk; only metadata
  is in the DB.**
- **`generations`** — one row per request: prompt, resolved system instruction,
  model, resolution, image count, status, error, the batch job name, and the
  ordered list of reference image ids (JSON array).
- **`generation_images`** — links a generation to the images it produced.

## Image generation flow

1. **Submit.** `POST /api/generations` validates the referenced images, inserts
   a `pending` row, and fires an **asyncio background task** —
   `_process_generation(gen_id)`. The HTTP response returns immediately so the
   UI can start polling. Multiple generations run **concurrently**, each as its
   own independent task/job.
2. **Build the request.** The worker resolves the reference image ids into
   `(label, path)` pairs (`_resolve_reference_refs`) and calls
   `gemini.build_request(...)`, which produces a single `generateContent`-style
   dict that works for both the sync and batch paths.
3. **Call Google** in one of two ways, chosen by the `use_batch` setting:
   - **Batch (default):** submit N inline requests as one job, then poll it
     (5-second interval, 20-minute timeout) until terminal, then collect the
     inline image responses.
   - **Synchronous:** call `generate_content` once per image in a thread.
   Blocking SDK calls are wrapped in `asyncio.to_thread` so they don't stall the
   event loop.
4. **Persist.** Generated bytes are written to `data/images/`, `images` +
   `generation_images` rows are inserted, and the generation is marked
   `succeeded` (or `failed` with an error message the UI displays).
5. **Resume on restart.** On startup, any generation still `pending`/`running`
   with a saved batch job name is re-attached to a poller; others are marked
   failed ("interrupted by restart").

### How `@Name` references are respected

References aren't left to positional guessing. In `build_request`, each image's
bytes are preceded by a short text part — `Reference image @<label>:` — so the
model sees an explicit association between the name the user typed and the
following image. Labels are reconstructed on the backend to mirror what the UI
shows: library images use their custom name; unnamed uploads become
`Image1`, `Image2`, … in attach order.

### System instructions

There's a **global default** system instruction in settings. Per generation it
can be left on (prefilled from the default), turned off, or overridden with a
custom one. Whatever is resolved at submit time is stored on the generation row,
so re-opening or editing a past generation reproduces it exactly.

## Two Google backends

The same models are reachable two ways, switchable in **Settings** (`use_vertex`):

- **Gemini Developer API** (default) — a single API key from Google AI Studio.
  Simplest path; what most setups use.
- **Vertex AI** (Google Cloud) — authenticated via the GCP project +
  Application Default Credentials, no API key. Needed for enterprise controls.

`gemini.make_client(...)` builds the right client for the selected mode. Some
`ImageConfig` fields (`person_generation`, `output_mime_type`) are **accepted
only in Vertex mode** and raise `ValueError` on the Developer API, so the
backend passes them **only when Vertex is active** (fully-permissive person
generation + lossless PNG on Vertex; neither on the Developer API, where the
model chooses the output format).

## Durability

- **Startup backup:** every launch snapshots `data/app.db` into
  `data/backups/` (15 most recent kept) using SQLite's backup API. History is
  intended to be **persistent** — nothing is cleared on restart.
- Image files live in `data/images/`; a full backup is `app.db` + that folder.

## Saving results

Results are saved to disk with the **File System Access API**
(`showSaveFilePicker`) for a real "Save As" dialog, falling back to a normal
download in browsers that lack it. The suggested filename and extension come
from the image's actual MIME type.
