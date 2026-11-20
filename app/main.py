from fastapi import FastAPI

from app.api.health import router

app = FastAPI(title="video-asset-ai", openapi_url=None)
app.include_router(router)
