import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from time import perf_counter
from uuid import uuid4

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.asset_tasks import router as asset_task_router
from app.api.character_tasks import router as character_task_router
from app.api.health import router as health_router
from app.core.config import Settings
from app.pipeline.asset_tasks import AssetTasks
from app.pipeline.character_extraction import CharacterExtraction
from app.pipeline.character_tasks import CharacterTasks

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    for module in ("httpx", "httpx2"):
        logging.getLogger(module).setLevel(logging.WARNING)
    settings = Settings()
    extraction = CharacterExtraction(settings)
    tasks = CharacterTasks(settings, extraction.run)
    asset_tasks = AssetTasks(
        settings, extraction.run, tasks.executor, extraction.detect_people
    )
    app.state.character_tasks = tasks
    app.state.asset_tasks = asset_tasks
    try:
        yield
    finally:
        await asyncio.gather(
            asyncio.to_thread(tasks.close), asyncio.to_thread(asset_tasks.close)
        )


app = FastAPI(title="video-asset-ai", openapi_url=None, lifespan=lifespan)
app.include_router(health_router)
app.include_router(character_task_router)
app.include_router(asset_task_router)


@app.exception_handler(RequestValidationError)
async def invalid_request(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    fields = sorted(
        {".".join(str(part) for part in error["loc"]) for error in exc.errors()}
    )
    logger.info("requestId=%s 参数校验失败 fields=%s", request.state.request_id, fields)
    return JSONResponse(
        status_code=422,
        content={
            "detail": {
                "code": "REQUEST_INVALID",
                "message": "请求参数格式错误或缺少必填字段",
            }
        },
    )


@app.middleware("http")
async def log_request(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    started_at = perf_counter()
    request_id = uuid4().hex
    request.state.request_id = request_id
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        logger.info(
            "requestId=%s taskId=%s method=%s route=%s status=%d 耗时 %.1f 毫秒",
            request_id,
            getattr(request.state, "task_id", None),
            request.method,
            getattr(request.scope.get("route"), "path", "<unmatched>"),
            status,
            (perf_counter() - started_at) * 1000,
        )
