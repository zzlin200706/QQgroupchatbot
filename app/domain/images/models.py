"""Validated values exchanged by image services and vision providers."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, field_validator


@dataclass(frozen=True)
class ResolvedImage:
    data: bytes
    content_type: str

    def __post_init__(self) -> None:
        if not self.data:
            raise ValueError("resolved image data must not be empty")
        if not self.content_type.startswith("image/"):
            raise ValueError("resolved image content_type must be an image type")


class ImageDescription(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    description: str = Field(min_length=1)

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("image description must not be blank")
        return normalized
