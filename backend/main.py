"""FastAPI application: REST API + serves the static frontend.

Run with:  uv run python -m backend.main      (serves on port 8001)
      or:  uv run uvicorn backend.main:app --reload --port 8001
Then open: http://127.0.0.1:8001
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import config, db, gemini
from .schemas import (
    GenerationCreate,
    GenerationOut,
    GenerationRename,
    ImageOut,
    LibraryUpdate,
    SettingsUpdate,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("genai")

# Default port the app serves on. Change it here (and it applies to
# `python -m backend.main`); the uvicorn CLI can override with --port.
DEFAULT_PORT = 8001

POLL_INTERVAL_SECONDS = 5
POLL_TIMEOUT_SECONDS = 20 * 60  # give up on a batch job after 20 minutes

app = FastAPI(title="GenAI Image App")


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def _backup_db(keep: int = 15) -> None:
    """Snapshot the SQLite DB on startup so data is never lost to one mistake.

    Backups land in data/backups/. We keep the most recent ``keep`` files and
    prune older ones. This is cheap insurance - the DB is tiny.
    """
    if not config.DB_PATH.exists():
        return
    backups = config.DATA_DIR / "backups"
    backups.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = backups / f"app-{stamp}.db"
    try:
        # sqlite3 backup API produces a consistent copy even with WAL open.
        src = sqlite3.connect(config.DB_PATH)
        out = sqlite3.connect(dest)
        with out:
            src.backup(out)
        src.close()
        out.close()
        log.info("Backed up database to %s", dest)
    except Exception as exc:  # noqa: BLE001 - a failed backup must not block startup
        log.warning("Database backup failed: %s", exc)
        return
    # Prune old backups, newest kept.
    old = sorted(backups.glob("app-*.db"))[:-keep]
    for path in old:
        path.unlink(missing_ok=True)


@app.on_event("startup")
async def _startup() -> None:
    _backup_db()   # snapshot BEFORE we touch anything
    db.init_db()
    # Re-attach pollers for jobs that were mid-flight when the server stopped.
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM generations WHERE status IN ('pending', 'running')"
        ).fetchall()
    for row in rows:
        if row["batch_job_name"]:
            asyncio.create_task(_process_generation(row["id"], resume=True))
        else:
            _fail(row["id"], "Interrupted by server restart before submission.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _image_row_to_out(row) -> ImageOut:
    return ImageOut(
        id=row["id"],
        original_name=row["original_name"],
        mime_type=row["mime_type"],
        kind=row["kind"],
        in_library=bool(row["in_library"]),
        library_name=row["library_name"],
        created_at=row["created_at"],
        url=f"/api/images/{row['id']}/file",
    )


def _generation_row_to_out(conn, row) -> GenerationOut:
    img_rows = conn.execute(
        "SELECT i.* FROM images i "
        "JOIN generation_images gi ON gi.image_id = i.id "
        "WHERE gi.generation_id = ? ORDER BY i.id",
        (row["id"],),
    ).fetchall()
    return GenerationOut(
        id=row["id"],
        name=row["name"],
        prompt=row["prompt"],
        system_instruction=row["system_instruction"],
        model=row["model"],
        resolution=row["resolution"],
        num_images=row["num_images"],
        status=row["status"],
        error=row["error"],
        reference_image_ids=json.loads(row["reference_image_ids"]),
        images=[_image_row_to_out(r) for r in img_rows],
        created_at=row["created_at"],
    )


def _get_api_key(settings: dict) -> str:
    # Key saved in Settings wins; fall back to the GEMINI_API_KEY env var.
    return settings.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY", "")


def _set_status(gen_id: int, status: str, *, error: str | None = None,
                batch_job_name: str | None = None) -> None:
    with db.connect() as conn:
        conn.execute(
            "UPDATE generations SET status = ?, error = ?, "
            "batch_job_name = COALESCE(?, batch_job_name) WHERE id = ?",
            (status, error, batch_job_name, gen_id),
        )
        conn.commit()


def _fail(gen_id: int, message: str) -> None:
    log.warning("Generation %s failed: %s", gen_id, message)
    _set_status(gen_id, "failed", error=message)


def _log_model_response(gen_id: int, payload) -> None:
    """Dump the model's final response to the terminal (base64 truncated)."""
    try:
        text = json.dumps(payload, indent=2, default=str, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        text = str(payload)
    log.info(
        "===== Generation %s: model response =====\n%s\n"
        "=========================================",
        gen_id, text,
    )


def _save_generated_images(gen_id: int, images: list[tuple[bytes, str]]) -> None:
    with db.connect() as conn:
        for data, mime in images:
            ext = "jpg" if "jpeg" in mime else mime.split("/")[-1]
            filename = f"gen_{gen_id}_{uuid.uuid4().hex}.{ext}"
            (config.IMAGES_DIR / filename).write_bytes(data)
            cur = conn.execute(
                "INSERT INTO images(filename, original_name, mime_type, kind) "
                "VALUES(?, ?, ?, 'generated')",
                (filename, f"generation {gen_id}", mime),
            )
            conn.execute(
                "INSERT INTO generation_images(generation_id, image_id) VALUES(?, ?)",
                (gen_id, cur.lastrowid),
            )
        conn.commit()


# ---------------------------------------------------------------------------
# Background generation worker (one task per generation => runs concurrently)
# ---------------------------------------------------------------------------

async def _process_generation(gen_id: int, resume: bool = False) -> None:
    try:
        with db.connect() as conn:
            gen = conn.execute(
                "SELECT * FROM generations WHERE id = ?", (gen_id,)
            ).fetchone()
        if gen is None:
            return
        settings = db.get_settings()
        use_vertex = str(settings.get("use_vertex", "false")).lower() == "true"
        client = gemini.make_client(
            _get_api_key(settings),
            use_vertex=use_vertex,
            project=(settings.get("gcp_project") or None),
            location=(settings.get("gcp_location") or None),
        )

        # person_generation and output_mime_type are BOTH Vertex-only (the
        # Developer API rejects them). On Vertex we want person generation fully
        # permissive and lossless PNG output; on the Developer API we pass
        # neither and let the model pick the format.
        person_generation = "ALLOW_ALL" if use_vertex else None
        output_mime_type = "image/png" if use_vertex else None

        ref_ids = json.loads(gen["reference_image_ids"])
        references = _resolve_reference_refs(ref_ids)
        request = gemini.build_request(
            gen["prompt"], references, gen["resolution"],
            gen["system_instruction"], person_generation, output_mime_type,
        )

        use_batch = str(settings.get("use_batch", "true")).lower() == "true"

        if not use_batch:
            _set_status(gen_id, "running")
            images, debugs = await asyncio.to_thread(
                gemini.generate_sync, client, gen["model"], request, gen["num_images"]
            )
            _log_model_response(gen_id, debugs)
            _finish(gen_id, images)
            return

        # --- Batch path ---
        job_name = gen["batch_job_name"]
        if not (resume and job_name):
            job_name = await asyncio.to_thread(
                gemini.submit_batch, client, gen["model"], request,
                gen["num_images"], f"gen-{gen_id}",
            )
            _set_status(gen_id, "running", batch_job_name=job_name)

        elapsed = 0
        while elapsed < POLL_TIMEOUT_SECONDS:
            state, job = await asyncio.to_thread(gemini.poll_batch, client, job_name)
            if gemini.is_terminal(state):
                if gemini.is_success(state):
                    _log_model_response(gen_id, gemini.debug_batch(job))
                    images = gemini.collect_batch_images(job)
                    _finish(gen_id, images)
                else:
                    _fail(gen_id, f"Batch job ended in state {state}.")
                return
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            elapsed += POLL_INTERVAL_SECONDS
        _fail(gen_id, "Timed out waiting for the batch job to finish.")

    except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
        _fail(gen_id, f"{type(exc).__name__}: {exc}")


def _resolve_reference_refs(ref_ids: list[int]) -> list[tuple[str, Path]]:
    """Turn ordered reference ids into (label, path) pairs.

    Labels mirror what the frontend shows: library images use their custom name,
    unnamed uploads become Image1, Image2, … numbered by their position in the
    reference list (same order the user attached them).
    """
    if not ref_ids:
        return []
    with db.connect() as conn:
        placeholders = ",".join("?" * len(ref_ids))
        rows = conn.execute(
            f"SELECT id, filename, library_name FROM images WHERE id IN ({placeholders})",
            ref_ids,
        ).fetchall()
    by_id = {r["id"]: r for r in rows}

    refs: list[tuple[str, Path]] = []
    upload_n = 0
    for image_id in ref_ids:  # preserve attach order
        row = by_id.get(image_id)
        if row is None:
            continue
        if row["library_name"]:
            label = row["library_name"]
        else:
            upload_n += 1
            label = f"Image{upload_n}"
        refs.append((label, config.IMAGES_DIR / row["filename"]))
    return refs


def _finish(gen_id: int, images: list[tuple[bytes, str]]) -> None:
    if not images:
        _fail(gen_id, "The model returned no images.")
        return
    _save_generated_images(gen_id, images)
    _set_status(gen_id, "succeeded")
    log.info("Generation %s succeeded with %d image(s).", gen_id, len(images))


# ---------------------------------------------------------------------------
# Settings API
# ---------------------------------------------------------------------------

@app.get("/api/settings")
def get_settings() -> dict:
    return db.get_settings()


@app.put("/api/settings")
def put_settings(update: SettingsUpdate) -> dict:
    values = {k: v for k, v in update.model_dump().items() if v is not None}
    if values:
        db.update_settings(values)
    return db.get_settings()


# ---------------------------------------------------------------------------
# Images API
# ---------------------------------------------------------------------------

@app.post("/api/images", response_model=ImageOut)
async def upload_image(file: UploadFile = File(...)) -> ImageOut:
    raw = await file.read()
    mime = file.content_type or "image/png"
    ext = Path(file.filename or "").suffix.lstrip(".") or "png"
    filename = f"upload_{uuid.uuid4().hex}.{ext}"
    (config.IMAGES_DIR / filename).write_bytes(raw)
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO images(filename, original_name, mime_type, kind) "
            "VALUES(?, ?, ?, 'upload')",
            (filename, file.filename, mime),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM images WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _image_row_to_out(row)


@app.get("/api/images", response_model=list[ImageOut])
def list_images(library: bool = False) -> list[ImageOut]:
    query = "SELECT * FROM images"
    if library:
        query += " WHERE in_library = 1"
    query += " ORDER BY id DESC"
    with db.connect() as conn:
        rows = conn.execute(query).fetchall()
    return [_image_row_to_out(r) for r in rows]


@app.get("/api/images/{image_id}", response_model=ImageOut)
def get_image(image_id: int) -> ImageOut:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Image not found")
    return _image_row_to_out(row)


@app.get("/api/images/{image_id}/file")
def get_image_file(image_id: int) -> FileResponse:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Image not found")
    path = config.IMAGES_DIR / row["filename"]
    if not path.exists():
        raise HTTPException(404, "Image file missing on disk")
    download_name = (row["library_name"] or row["original_name"] or f"image_{image_id}")
    return FileResponse(path, media_type=row["mime_type"], filename=download_name)


@app.post("/api/images/{image_id}/library", response_model=ImageOut)
def add_to_library(image_id: int, body: LibraryUpdate) -> ImageOut:
    name = body.library_name.strip()
    with db.connect() as conn:
        if conn.execute("SELECT 1 FROM images WHERE id = ?", (image_id,)).fetchone() is None:
            raise HTTPException(404, "Image not found")
        clash = conn.execute(
            "SELECT id FROM images WHERE library_name = ? AND id != ?", (name, image_id)
        ).fetchone()
        if clash:
            raise HTTPException(409, f"The name '{name}' is already used in the library.")
        conn.execute(
            "UPDATE images SET in_library = 1, library_name = ? WHERE id = ?",
            (name, image_id),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
    return _image_row_to_out(row)


@app.delete("/api/images/{image_id}/library", response_model=ImageOut)
def remove_from_library(image_id: int) -> ImageOut:
    with db.connect() as conn:
        conn.execute(
            "UPDATE images SET in_library = 0, library_name = NULL WHERE id = ?",
            (image_id,),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Image not found")
    return _image_row_to_out(row)


@app.delete("/api/images/{image_id}")
def delete_image(image_id: int) -> dict:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Image not found")
        conn.execute("DELETE FROM images WHERE id = ?", (image_id,))
        conn.commit()
    (config.IMAGES_DIR / row["filename"]).unlink(missing_ok=True)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Generations API
# ---------------------------------------------------------------------------

@app.post("/api/generations", response_model=GenerationOut)
async def create_generation(body: GenerationCreate) -> GenerationOut:
    with db.connect() as conn:
        # Validate referenced images exist.
        for image_id in body.reference_image_ids:
            if conn.execute("SELECT 1 FROM images WHERE id = ?", (image_id,)).fetchone() is None:
                raise HTTPException(400, f"Reference image {image_id} does not exist.")
        system_instruction = (body.system_instruction or "").strip() or None
        cur = conn.execute(
            "INSERT INTO generations(prompt, model, resolution, num_images, status, "
            "reference_image_ids, system_instruction) VALUES(?, ?, ?, ?, 'pending', ?, ?)",
            (
                body.prompt, body.model, body.resolution, body.num_images,
                json.dumps(body.reference_image_ids), system_instruction,
            ),
        )
        conn.commit()
        gen_id = cur.lastrowid
        row = conn.execute("SELECT * FROM generations WHERE id = ?", (gen_id,)).fetchone()

    # Fire off the worker; it runs independently so multiple generations run
    # concurrently, each as its own job.
    asyncio.create_task(_process_generation(gen_id))

    with db.connect() as conn:
        return _generation_row_to_out(conn, row)


@app.get("/api/generations", response_model=list[GenerationOut])
def list_generations() -> list[GenerationOut]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM generations ORDER BY id DESC"
        ).fetchall()
        return [_generation_row_to_out(conn, r) for r in rows]


@app.get("/api/generations/{gen_id}", response_model=GenerationOut)
def get_generation(gen_id: int) -> GenerationOut:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM generations WHERE id = ?", (gen_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Generation not found")
        return _generation_row_to_out(conn, row)


@app.patch("/api/generations/{gen_id}", response_model=GenerationOut)
def rename_generation(gen_id: int, body: GenerationRename) -> GenerationOut:
    # Empty/blank name clears it, so the UI falls back to showing the prompt.
    name = (body.name or "").strip() or None
    with db.connect() as conn:
        if conn.execute("SELECT 1 FROM generations WHERE id = ?", (gen_id,)).fetchone() is None:
            raise HTTPException(404, "Generation not found")
        conn.execute("UPDATE generations SET name = ? WHERE id = ?", (name, gen_id))
        conn.commit()
        row = conn.execute("SELECT * FROM generations WHERE id = ?", (gen_id,)).fetchone()
        return _generation_row_to_out(conn, row)


@app.delete("/api/generations/{gen_id}")
def delete_generation(gen_id: int) -> dict:
    with db.connect() as conn:
        if conn.execute("SELECT 1 FROM generations WHERE id = ?", (gen_id,)).fetchone() is None:
            raise HTTPException(404, "Generation not found")
        conn.execute("DELETE FROM generations WHERE id = ?", (gen_id,))
        conn.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Static frontend (mounted last so it doesn't shadow /api routes)
# ---------------------------------------------------------------------------

app.mount("/", StaticFiles(directory=config.FRONTEND_DIR, html=True), name="frontend")


# ---------------------------------------------------------------------------
# Entry point: `uv run python -m backend.main`
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    # Pass the import string (not `app`) so --reload / auto-reload works.
    uvicorn.run("backend.main:app", host="127.0.0.1", port=DEFAULT_PORT, reload=True)
