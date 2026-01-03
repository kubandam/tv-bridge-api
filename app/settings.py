from dotenv import load_dotenv
load_dotenv()

from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    database_url: str
    api_key: str
    # Optional: if clients don't send X-Device-Id header, server can fall back to this
    default_device_id: str | None = None

settings = Settings()
