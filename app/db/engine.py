import time
import logging
from sqlmodel import SQLModel, create_engine, Session
from sqlalchemy.exc import OperationalError
from app.settings import settings

logger = logging.getLogger(__name__)

# Normalize database URL: Render provides postgresql:// but we need postgresql+psycopg://
database_url = settings.database_url
if database_url.startswith("postgresql://") and "+psycopg" not in database_url:
    database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    logger.info("Normalized database URL to use psycopg driver")

engine = create_engine(
    database_url,
    echo=False,
    pool_pre_ping=True,
)


def create_db_and_tables() -> None:
    """Create database tables with retry logic for production environments."""
    max_retries = 5
    retry_delay = 2  # seconds
    
    for attempt in range(max_retries):
        try:
            SQLModel.metadata.create_all(engine)
            logger.info("Database tables created successfully")
            return
        except OperationalError as e:
            if attempt < max_retries - 1:
                logger.warning(
                    f"Database connection failed (attempt {attempt + 1}/{max_retries}): {e}. "
                    f"Retrying in {retry_delay} seconds..."
                )
                time.sleep(retry_delay)
            else:
                logger.error(f"Failed to connect to database after {max_retries} attempts: {e}")
                raise


def get_session():
    with Session(engine) as session:
        yield session
