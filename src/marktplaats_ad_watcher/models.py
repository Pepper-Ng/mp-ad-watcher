from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator


class Ad(BaseModel):
    id: str
    title: str
    url: str
    description: str | None = None
    price: str | None = None
    location: str | None = None
    seller: str | None = None
    image_urls: list[str] = Field(default_factory=list)
    listing_facts: dict[str, str] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict, exclude=True)

    @field_validator("id", "title", "url")
    @classmethod
    def non_empty(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Value must not be empty.")
        return stripped

    def prompt_text(self, *, include_image_urls: bool = False) -> str:
        parts = [
            f"ID: {self.id}",
            f"Title: {self.title}",
            f"URL: {self.url}",
        ]

        if self.price:
            parts.append(f"Price: {self.price}")
        if self.location:
            parts.append(f"Location: {self.location}")
        if self.seller:
            parts.append(f"Seller: {self.seller}")
        if self.description:
            parts.append(f"Description: {self.description}")
        if self.listing_facts:
            facts = "\n".join(
                f"{name}: {value}" for name, value in self.listing_facts.items()
            )
            parts.append(f"Listing facts:\n{facts}")
        if include_image_urls and self.image_urls:
            parts.append("Image URLs:\n" + "\n".join(self.image_urls))

        return "\n".join(parts)


class EvaluationResult(BaseModel):
    relevant: bool
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str
    signals: list[str] = Field(default_factory=list)
    concerns: list[str] = Field(default_factory=list)
    next_action: Literal["notify", "ignore", "review"] = "ignore"

    @field_validator("reason")
    @classmethod
    def reason_must_be_present(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Reason must not be empty.")
        return stripped


class EvaluatedAd(BaseModel):
    ad: Ad
    result: EvaluationResult
    evaluated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    profile_id: str | None = None
    profile_name: str | None = None

    @field_validator("profile_id", "profile_name")
    @classmethod
    def profile_metadata_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("Profile metadata must not be blank.")
        return stripped


class TelegramSendResult(BaseModel):
    sent: bool
    reason: str | None = None
    message_id: int | None = None


class EvaluationFailure(BaseModel):
    ad_id: str
    title: str
    url: str
    error: str
    stage: Literal["model", "listing_details"] = "model"
    retry_exhausted: bool = False


class WatcherRunSummary(BaseModel):
    fetched_count: int
    kept_count: int
    filtered_count: int
    new_count: int
    evaluated_count: int
    notified_count: int
    ignored_count: int = 0
    review_count: int = 0
    notify_action_count: int = 0
    bootstrapped_count: int = 0
    evaluation_failed_count: int = 0
    evaluation_failures: list[EvaluationFailure] = Field(default_factory=list)


class SearchHealth(BaseModel):
    url: HttpUrl
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    ad_count: int
