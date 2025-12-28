from sqlmodel import SQLModel, create_engine, Session
from app.settings import settings

engine = create_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
)


def create_db_and_tables() -> None:
    SQLModel.metadata.create_all(engine)


def get_session():
    with Session(engine) as session:
        yield session
