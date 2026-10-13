"""Request/response models for the BlazeCrawl Core public API (v0.1)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

from blazecrawl_core.engine.robots import DEFAULT_USER_AGENT
from blazecrawl_core.network.headers import validate_headers, validate_user_agent


class ScrapeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: HttpUrl
    formats: list[str] | None = Field(
        default=None,
        description=(
            "Subset of markdown, html, text, links, images, screenshot. Default ['markdown']. "
            "'screenshot' returns a base64 PNG in data.screenshot and requires browser rendering."
        ),
    )
    full_page: bool = Field(
        default=False,
        description="For formats including 'screenshot': capture the full scrollable page instead of the viewport.",
    )
    only_main_content: bool = True
    timeout_ms: int | None = Field(default=None, ge=1000, le=120000)
    render: Literal["auto", "static", "browser"] = "auto"
    use_cache: bool = True
    user_agent: str = Field(default=DEFAULT_USER_AGENT, min_length=1, max_length=200)
    headers: dict[str, str] | None = Field(
        default=None,
        description="Initial-origin HTTP headers; at most 32 headers / 8192 bytes. "
        "A case-insensitive User-Agent header overrides user_agent on that origin.",
    )

    @field_validator("headers")
    @classmethod
    def _headers(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        return validate_headers(value) if value is not None else None

    @field_validator("user_agent")
    @classmethod
    def _user_agent(cls, value: str) -> str:
        return validate_user_agent(value)

    @field_validator("url")
    @classmethod
    def _scheme(cls, v: HttpUrl) -> HttpUrl:
        if v.scheme not in ("http", "https"):
            raise ValueError("Only http and https URLs are allowed")
        return v

    @model_validator(mode="after")
    def _screenshot_needs_browser(self) -> ScrapeRequest:
        if self.render == "static" and "screenshot" in (self.formats or []):
            raise ValueError(
                "formats 'screenshot' requires browser rendering; use render auto or browser"
            )
        return self


class ScrapeResponse(BaseModel):
    success: bool
    data: dict[str, Any] | None = None
    error: str | None = None


class MapRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: HttpUrl
    include_sitemap: bool = True

    @field_validator("url")
    @classmethod
    def _scheme(cls, v: HttpUrl) -> HttpUrl:
        if v.scheme not in ("http", "https"):
            raise ValueError("Only http and https URLs are allowed")
        return v


class CrawlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: HttpUrl
    max_pages: int | None = Field(default=None, ge=1, le=1000)
    max_depth: int | None = Field(default=None, ge=0, le=5)
    user_agent: str = Field(default=DEFAULT_USER_AGENT, min_length=1, max_length=200)

    @field_validator("url")
    @classmethod
    def _scheme(cls, v: HttpUrl) -> HttpUrl:
        if v.scheme not in ("http", "https"):
            raise ValueError("Only http and https URLs are allowed")
        return v


class CrawlJobResponse(BaseModel):
    success: bool
    job_id: str
    status: str
    status_url: str
