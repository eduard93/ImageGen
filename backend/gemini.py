"""All Google Gemini API interaction lives here and nowhere else.

If a future version of the ``google-genai`` SDK renames a field or method, this
is the ONLY file you need to touch. Everything is built from plain dicts so we
depend on as few SDK class names as possible.

Two code paths, chosen by the ``use_batch`` setting:
  * Batch API (default, what you asked for): submit an async job, poll it, then
    read the inline responses. Generating N images = submitting N requests in
    one job.
  * Synchronous fallback: call ``generate_content`` in a loop. Flip
    ``use_batch`` to "false" in Settings to use this.
"""
from __future__ import annotations

import base64
import mimetypes
from pathlib import Path

from google import genai
from google.genai import types

# Batch job states reported by the API. See docs: JOB_STATE_*.
_TERMINAL_STATES = {
    "JOB_STATE_SUCCEEDED",
    "JOB_STATE_FAILED",
    "JOB_STATE_CANCELLED",
    "JOB_STATE_EXPIRED",
}


def make_client(
    api_key: str,
    *,
    use_vertex: bool = False,
    project: str | None = None,
    location: str | None = None,
) -> genai.Client:
    """Create the SDK client for whichever backend is selected in Settings.

    Two mutually exclusive modes:
      * Gemini Developer API (default): simple API key from Google AI Studio.
      * Vertex AI: Google Cloud. Auth does NOT use the api_key - it comes from
        Application Default Credentials (``gcloud auth application-default
        login``) or a service-account key in GOOGLE_APPLICATION_CREDENTIALS.
    """
    if use_vertex:
        if not project:
            raise ValueError(
                "Vertex mode is on but no Google Cloud project is set. "
                "Add it in Settings."
            )
        return genai.Client(
            vertexai=True, project=project, location=(location or "global")
        )
    if not api_key:
        raise ValueError("No Gemini API key configured. Add one in Settings.")
    return genai.Client(api_key=api_key)


def _guess_mime(path: Path) -> str:
    return mimetypes.guess_type(str(path))[0] or "image/png"


def create_generate_config(
    resolution: str,
    system_instruction: str | None = None,
    person_generation: str | None = None,
    output_mime_type: str | None = None,
) -> types.GenerateContentConfig:
    """Typed generation config: image output, resolution, and safety OFF.

    image_size (1K/2K/4K) is honored by resolution-capable models
    (e.g. gemini-3-pro-image-preview); other models may ignore or reject it.

    ``system_instruction`` is applied only when non-empty; otherwise it's left
    unset (disabled) so the per-generation prompt fully drives the output.

    ``person_generation`` (DONT_ALLOW | ALLOW_ADULT | ALLOW_ALL) and
    ``output_mime_type`` are BOTH Vertex / Enterprise-only; on the Gemini
    Developer API they raise ValueError. The caller must pass None for each
    unless Vertex mode is active. (PNG output is lossless = max quality, so we
    request it on Vertex; on the Developer API the model picks the format.)
    """
    image_kwargs: dict = {"image_size": resolution}
    if person_generation:
        image_kwargs["person_generation"] = person_generation
    if output_mime_type:
        image_kwargs["output_mime_type"] = output_mime_type
    return types.GenerateContentConfig(
        system_instruction=(system_instruction or None),
        response_modalities=["TEXT", "IMAGE"],
        image_config=types.ImageConfig(**image_kwargs),
        safety_settings=[
            types.SafetySetting(
                category="HARM_CATEGORY_HARASSMENT",
                threshold="OFF",
            ),
            types.SafetySetting(
                category="HARM_CATEGORY_HATE_SPEECH",
                threshold="OFF",
            ),
            types.SafetySetting(
                category="HARM_CATEGORY_SEXUALLY_EXPLICIT",
                threshold="OFF",
            ),
            types.SafetySetting(
                category="HARM_CATEGORY_DANGEROUS_CONTENT",
                threshold="OFF",
            ),
        ],
    )


def build_request(
    prompt: str,
    references: list[tuple[str, Path]],
    resolution: str,
    system_instruction: str | None = None,
    person_generation: str | None = None,
    output_mime_type: str | None = None,
) -> dict:
    """Build one generateContent-style request dict (text + reference images).

    ``references`` is an ordered list of ``(label, path)`` pairs, where ``label``
    is the name the user typed in the prompt (e.g. ``Logo`` for ``@Logo``, or
    ``Image1`` for an unnamed upload).

    To make ``@Name`` references actually respected, each image's bytes are
    preceded by a short text part naming it. That way the model sees an explicit
    ``@Logo -> these bytes`` association instead of having to guess from ordering.

    The same dict shape works for both ``generate_content`` and the Batch API,
    so we only have to construct it once.
    """
    parts: list[dict] = [{"text": prompt}]
    for label, path in references:
        raw = path.read_bytes()
        parts.append({"text": f"Reference image @{label}:"})
        parts.append(
            {
                "inline_data": {
                    "mime_type": _guess_mime(path),
                    # Base64 string keeps the request pure-JSON, which the batch
                    # endpoint expects for inline requests.
                    "data": base64.b64encode(raw).decode("ascii"),
                }
            }
        )

    return {
        "contents": [{"role": "user", "parts": parts}],
        "config": create_generate_config(
            resolution, system_instruction, person_generation, output_mime_type
        ),
    }


# --- extracting image bytes out of a response -------------------------------

def _extract_images(response) -> list[tuple[bytes, str]]:
    """Pull (bytes, mime_type) for every inline image part in a response."""
    out: list[tuple[bytes, str]] = []
    for candidate in getattr(response, "candidates", None) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", None) or []:
            inline = getattr(part, "inline_data", None)
            data = getattr(inline, "data", None) if inline else None
            if not data:
                continue
            if isinstance(data, str):  # some SDK versions hand back base64 text
                data = base64.b64decode(data)
            mime = getattr(inline, "mime_type", None) or "image/png"
            out.append((data, mime))
    return out


# --- batch path -------------------------------------------------------------

def submit_batch(
    client: genai.Client, model: str, request: dict, num_images: int, display_name: str
) -> str:
    """Submit one batch job containing ``num_images`` copies of ``request``.

    Returns the job resource name used for polling.
    """
    inline_requests = [request for _ in range(max(1, num_images))]
    job = client.batches.create(
        model=model,
        src=inline_requests,
        config={"display_name": display_name},
    )
    return job.name


def poll_batch(client: genai.Client, job_name: str):
    """One poll. Returns (state_string, job_object)."""
    job = client.batches.get(name=job_name)
    state = getattr(getattr(job, "state", None), "name", None) or str(job.state)
    return state, job


def is_terminal(state: str) -> bool:
    return state in _TERMINAL_STATES


def is_success(state: str) -> bool:
    return state == "JOB_STATE_SUCCEEDED"


# --- debug dump (for the terminal) -----------------------------------------

def _truncate(obj):
    """Recursively copy a response dict, shortening base64 image blobs."""
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if key == "data" and isinstance(value, (str, bytes)) and len(value) > 24:
                head = value[:16]
                if isinstance(head, bytes):
                    head = head.decode("latin-1", "replace")
                out[key] = f"{head}…<{len(value)} bytes truncated>"
            else:
                out[key] = _truncate(value)
        return out
    if isinstance(obj, list):
        return [_truncate(v) for v in obj]
    if isinstance(obj, bytes):
        return f"<{len(obj)} bytes>"
    return obj


def debug_response(response) -> dict:
    """A JSON-serializable, base64-truncated view of one model response."""
    try:
        data = response.model_dump()  # pydantic model in the SDK
    except Exception:  # noqa: BLE001 - fall back to a plain repr
        data = {"repr": str(response)}
    return _truncate(data)


def debug_batch(job) -> list:
    """Truncated debug view of every inline response in a batch job."""
    dest = getattr(job, "dest", None)
    inlined = getattr(dest, "inlined_responses", None) or []
    out = []
    for item in inlined:
        response = getattr(item, "response", None)
        error = getattr(item, "error", None)
        if response is not None:
            out.append(debug_response(response))
        elif error is not None:
            out.append({"error": str(error)})
    return out


def collect_batch_images(job) -> list[tuple[bytes, str]]:
    """Gather images from every inline response in a finished batch job.

    Raises RuntimeError with the first error the API reported, if any.
    """
    dest = getattr(job, "dest", None)
    inlined = getattr(dest, "inlined_responses", None) or []
    images: list[tuple[bytes, str]] = []
    first_error: str | None = None
    for item in inlined:
        err = getattr(item, "error", None)
        if err and first_error is None:
            first_error = str(err)
        response = getattr(item, "response", None)
        if response is not None:
            images.extend(_extract_images(response))
    if not images and first_error:
        raise RuntimeError(first_error)
    return images


# --- synchronous fallback path ---------------------------------------------

def generate_sync(
    client: genai.Client, model: str, request: dict, num_images: int
) -> tuple[list[tuple[bytes, str]], list]:
    """Generate images without the Batch API - one call per image.

    Returns (images, debug_responses) so the caller can log the raw responses.
    """
    images: list[tuple[bytes, str]] = []
    debugs: list = []
    for _ in range(max(1, num_images)):
        response = client.models.generate_content(
            model=model,
            contents=request["contents"],
            config=request["config"],
        )
        debugs.append(debug_response(response))
        images.extend(_extract_images(response))
    return images, debugs
