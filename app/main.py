from fastapi import FastAPI, Depends, Header, HTTPException
from sqlmodel import Session

from app.db.engine import create_db_and_tables, get_session
from app.settings import settings
from app.routers.sessions import router as sessions_router


app = FastAPI(title="TV Bridge API", version="0.1.0")


@app.on_event("startup")
def on_startup():
    create_db_and_tables()


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    if not x_api_key or x_api_key != settings.api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")


@app.get("/health")
def health():
    return {"ok": True}


# Routers
app.include_router(
    sessions_router,
    prefix="/v1",
    dependencies=[Depends(require_api_key)],
)
