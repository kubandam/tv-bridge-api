import logging
from fastapi import FastAPI, Depends, Header, HTTPException
from sqlmodel import Session

from app.db.engine import create_db_and_tables, get_session
from app.settings import settings
from app.routers.device import router as device_router

logger = logging.getLogger(__name__)

app = FastAPI(title="TV Bridge API", version="0.1.0")


@app.on_event("startup")
def on_startup():
    try:
        create_db_and_tables()
    except Exception as e:
        logger.error(f"Failed to initialize database during startup: {e}")
        # Don't crash the app - it will retry on first request due to pool_pre_ping=True
        # This allows the app to start even if DB is temporarily unavailable


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    if not settings.api_key:
        raise HTTPException(status_code=500, detail="API_KEY not configured on server")

    if not x_api_key:
        raise HTTPException(status_code=401, detail="Missing API key")

    if x_api_key != settings.api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")



@app.get("/health")
def health():
    return {"ok": True}


# Routers
app.include_router(
    device_router,
    prefix="/v1",
    dependencies=[Depends(require_api_key)],
)
