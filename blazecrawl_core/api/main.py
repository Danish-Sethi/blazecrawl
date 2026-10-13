"""BlazeCrawl Core API (OSS single-node FastAPI application).

Exposes the narrow v0.1 contract: /health, /v1/scrape, /v1/map, /v1/crawl.
No billing, no tenancy, no region guard, no hosted identity — but the full
SSRF/egress security model is enforced on every outbound fetch.
"""

from __future__ import annotations

import base64
from contextlib import asynccontextmanager, suppress

from fastapi import Depends, FastAPI, HTTPException, status

from blazecrawl_core import __version__
from blazecrawl_core.api import auth
from blazecrawl_core.api.schemas import (
    CrawlJobResponse,
    CrawlRequest,
    MapRequest,
    ScrapeRequest,
    ScrapeResponse,
)
from blazecrawl_core.config import settings
from blazecrawl_core.engine import cache
from blazecrawl_core.engine.browser_pool import enforce_screenshot_cap, get_browser_pool
from blazecrawl_core.engine.crawler import get_crawl_manager
from blazecrawl_core.engine.mapper import map_site
from blazecrawl_core.engine.scraper import scrape
from blazecrawl_core.exceptions import BlazeCrawlError, ScreenshotTooLargeError, get_status_code
from blazecrawl_core.logging import get_logger
from blazecrawl_core.network.ssrf import SSRFValidationError

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Warm the browser pool so the first request is fast. Non-fatal if it
    # fails (static fetches still work); health will report degraded.
    try:
        await get_browser_pool().startup()
    except Exception as e:
        logger.warning("Browser pool failed to start; static-only mode", error=str(e))
    if auth.auth_enforced():
        auth.effective_api_key()  # ensure a key exists + is logged
    yield
    with suppress(Exception):
        await get_browser_pool().shutdown()


app = FastAPI(
    title="BlazeCrawl Core",
    version=__version__,
    description="Security-first, self-hostable web-data engine (scrape / crawl / map).",
    lifespan=lifespan,
)


@app.exception_handler(SSRFValidationError)
async def _ssrf_handler(request, exc):  # noqa: ANN001
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"error": "url_rejected", "message": str(exc)},
    )


@app.get("/health")
async def health():
    return {"status": "ok", "version": __version__, "mode": settings.BLAZECRAWL_MODE}


@app.get("/ready")
async def ready():
    pool = get_browser_pool()
    h = await pool.health_check()
    return {
        "ready": True,
        "browser": h,
        "cache": "redis" if settings.REDIS_URL else "memory",
        "queue": settings.CRAWL_QUEUE_BACKEND,
    }


@app.post("/v1/scrape", response_model=ScrapeResponse, dependencies=[Depends(auth.require_api_key)])
async def scrape_endpoint(payload: ScrapeRequest) -> ScrapeResponse:
    url = str(payload.url)
    try:
        if payload.use_cache:
            hit = await cache.get_cached(
                url,
                payload.formats,
                payload.only_main_content,
                payload.render,
                payload.user_agent,
                payload.full_page,
            )
            if hit is not None:
                if hit.get("screenshot"):
                    # Re-check against the current cap in case SCREENSHOT_MAX_BYTES was lowered since caching.
                    enforce_screenshot_cap(
                        base64.b64decode(hit["screenshot"]), settings.SCREENSHOT_MAX_BYTES
                    )
                return ScrapeResponse(success=True, data={**hit, "cached": True})

        result = await scrape(
            url,
            formats=payload.formats,
            only_main_content=payload.only_main_content,
            timeout_ms=payload.timeout_ms,
            render=payload.render,
            user_agent=payload.user_agent,
            full_page=payload.full_page,
        )
        data = result.to_dict()
        if payload.use_cache:
            await cache.set_cached(
                url,
                payload.formats,
                payload.only_main_content,
                data,
                render=payload.render,
                user_agent=payload.user_agent,
                full_page=payload.full_page,
            )
        return ScrapeResponse(success=True, data=data)
    except SSRFValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "url_rejected", "message": str(e)},
        ) from e
    except ScreenshotTooLargeError as e:
        raise HTTPException(
            status_code=413,
            detail={
                "error": "screenshot_too_large",
                "message": e.message,
                "max_bytes": settings.SCREENSHOT_MAX_BYTES,
            },
        ) from e
    except BlazeCrawlError as e:
        raise HTTPException(
            status_code=get_status_code(e),
            detail={"error": "scrape_failed", "message": e.message},
        ) from e
    except Exception as e:
        logger.error("Scrape failed", url=url, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "scrape_failed", "message": "Failed to scrape the URL."},
        ) from e


@app.post("/v1/map", dependencies=[Depends(auth.require_api_key)])
async def map_endpoint(payload: MapRequest):
    try:
        return await map_site(str(payload.url), include_sitemap=payload.include_sitemap)
    except SSRFValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "url_rejected", "message": str(e)},
        ) from e
    except Exception as e:
        logger.error("Map failed", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "map_failed", "message": "Failed to map the site."},
        ) from e


@app.post(
    "/v1/crawl",
    response_model=CrawlJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(auth.require_api_key)],
)
async def crawl_endpoint(payload: CrawlRequest) -> CrawlJobResponse:
    # Validate the seed up front so obviously-bad crawls fail fast.
    from blazecrawl_core.network.ssrf import validate_and_pin

    try:
        await validate_and_pin(str(payload.url))
    except SSRFValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "url_rejected", "message": str(e)},
        ) from e

    manager = get_crawl_manager()
    job = manager.create_job(
        str(payload.url),
        payload.max_pages,
        payload.max_depth,
        user_agent=payload.user_agent,
    )
    return CrawlJobResponse(
        success=True,
        job_id=job.job_id,
        status=job.status,
        status_url=f"/v1/crawl/{job.job_id}",
    )


@app.get("/v1/crawl/{job_id}", dependencies=[Depends(auth.require_api_key)])
async def crawl_status(job_id: str):
    manager = get_crawl_manager()
    job = manager.get_job(job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found", "message": "Crawl job not found."},
        )
    return {"success": True, **job.to_dict()}


@app.delete("/v1/crawl/{job_id}", dependencies=[Depends(auth.require_api_key)])
async def crawl_cancel(job_id: str):
    manager = get_crawl_manager()
    ok = await manager.cancel(job_id)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found", "message": "Crawl job not found."},
        )
    return {"success": True, "job_id": job_id, "status": "cancelled"}


def main() -> None:
    import uvicorn

    uvicorn.run(
        "blazecrawl_core.api.main:app",
        host=settings.BLAZECRAWL_HOST,
        port=settings.BLAZECRAWL_PORT,
        log_level="info",
    )


if __name__ == "__main__":
    main()
