from dotenv import load_dotenv
load_dotenv()

from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    database_url: str
    api_key: str
    default_device_id: str | None = None

    # Heartbeat
    heartbeat_timeout_seconds: int = 30

    # How often to sample frames into frame_history (seconds)
    history_sample_interval_s: int = 3

    # Cloudflare R2 (S3-compatible)
    account_id: str
    access_key: str
    secret_access_key: str
    r2_bucket_name: str = "tv-frames"

    # Image log (legacy in-memory)
    max_image_log_size: int = 100

settings = Settings()
