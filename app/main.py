import logging
from fastapi import FastAPI, Depends, Header, HTTPException
from sqlmodel import Session
 
from app.db.engine import create_db_and_tables, get_session
from app.settings import settings
from app.routers.device import router as device_router
from app.routers.monitor import router as monitor_router, monitor_dashboard, live_dashboard
from app.routers.rpi import router as rpi_router
from app.routers.labeling import router as labeling_router, labeling_dashboard
from app.routers.review import router as review_router, review_dashboard, admin_dashboard, serve_frame_image
from app.routers.detect import router as detect_router, detect_dashboard

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

# Monitor router - data endpoint requires API key
app.include_router(
    monitor_router,
    prefix="/v1",
    dependencies=[Depends(require_api_key)],
)

# RPi control router
app.include_router(
    rpi_router,
    prefix="/v1/rpi",
    dependencies=[Depends(require_api_key)],
)

# Labeling router
app.include_router(
    labeling_router,
    prefix="/v1",
    dependencies=[Depends(require_api_key)],
)

# Public monitor dashboard (api_key passed as query param and validated internally)
app.get("/monitor")(monitor_dashboard)

# Public live dashboard
app.get("/live")(live_dashboard)

# Public labeling dashboard (api_key passed as query param and validated internally)
app.get("/labeling")(labeling_dashboard)

# Review/history labeling router
app.include_router(
    review_router,
    prefix="/v1",
    dependencies=[Depends(require_api_key)],
)

# Public review dashboard
app.get("/review")(review_dashboard)

# Public admin dashboard
app.get("/admin")(admin_dashboard)

# Detect router (server-side CLIP inference)
app.include_router(
    detect_router,
    prefix="/v1",
    dependencies=[Depends(require_api_key)],
)

# Public detect dashboard
app.get("/detect")(detect_dashboard)

# Public frame image proxy (api_key in query param, for <img src> use)
app.get("/frames/{frame_id}.jpg")(serve_frame_image)
