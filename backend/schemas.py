"""Pydantic request/response models for the REST API."""
from __future__ import annotations

from pydantic import BaseModel, Field


class SettingsUpdate(BaseModel):
    gemini_api_key: str | None = None
    default_prompt: str | None = None
    system_instruction: str | None = None
    default_model: str | None = None
    models: list[str] | None = None
    resolutions: list[str] | None = None
    use_batch: str | None = None  # "true" / "false"
    theme: str | None = None      # "dark" / "light"
    # --- Vertex AI ---
    use_vertex: str | None = None       # "true" / "false"
    gcp_project: str | None = None
    gcp_location: str | None = None


class LibraryUpdate(BaseModel):
    library_name: str = Field(min_length=1, max_length=100)


class GenerationRename(BaseModel):
    name: str | None = Field(default=None, max_length=200)


class GenerationCreate(BaseModel):
    prompt: str = Field(min_length=1)
    model: str
    resolution: str = "2K"
    num_images: int = Field(default=1, ge=1, le=8)
    reference_image_ids: list[int] = Field(default_factory=list)
    # Resolved system instruction to use for this generation, or None/empty to
    # disable it. The frontend fills this from the global default or a custom one.
    system_instruction: str | None = None


class ImageOut(BaseModel):
    id: int
    original_name: str | None
    mime_type: str
    kind: str
    in_library: bool
    library_name: str | None
    created_at: str
    url: str  # convenience URL to fetch the bytes


class GenerationOut(BaseModel):
    id: int
    name: str | None
    prompt: str
    system_instruction: str | None
    model: str
    resolution: str
    num_images: int
    status: str
    error: str | None
    reference_image_ids: list[int]
    images: list[ImageOut]
    created_at: str
