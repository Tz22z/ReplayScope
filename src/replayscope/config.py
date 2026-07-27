from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql://replayscope:replayscope@localhost:5433/replayscope"
    storage_root: Path = Path(".replayscope/cas")
    model_config = SettingsConfigDict(env_prefix="REPLAYSCOPE_", env_file=".env")
