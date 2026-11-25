import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

COLLECTIONS = {
    "support": "Support incidents",
    "policies": "Service policies",
}


@dataclass(frozen=True)
class Settings:
    mode: str
    data_dir: Path
    mapping_key: str
    app_key: str
    skill_key: str
    provider: str

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv()
        mode = os.getenv("RAG_MODE", "demo")
        if mode not in {"demo", "azure"}:
            raise ValueError("RAG_MODE must be demo or azure")
        settings = cls(
            mode=mode,
            data_dir=Path(os.getenv("RAG_DATA_DIR", ".data")),
            mapping_key=os.getenv("RAG_MAPPING_KEY", ""),
            app_key=os.getenv("RAG_APP_KEY", ""),
            skill_key=os.getenv("RAG_SKILL_KEY", ""),
            provider=os.getenv("RAG_PII_PROVIDER", "azure"),
        )
        if mode == "azure":
            if not settings.mapping_key:
                raise ValueError("RAG_MAPPING_KEY is required in Azure mode")
            if len(settings.app_key) < 32 or len(settings.skill_key) < 32:
                raise ValueError("Set independent RAG_APP_KEY and RAG_SKILL_KEY (32+ characters)")
            if settings.app_key == settings.skill_key:
                raise ValueError("Application and skill keys must differ")
            for name in (
                "RAG_SEARCH_ENDPOINT",
                "RAG_PROJECT_ENDPOINT",
                "RAG_BLOB_ACCOUNT_URL",
                "RAG_MODEL_DEPLOYMENT",
            ):
                required(name)
        if settings.provider not in {"azure", "presidio"}:
            raise ValueError("RAG_PII_PROVIDER must be azure or presidio")
        return settings


def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value or "YOUR-" in value:
        raise ValueError(f"Configure {name} before using Azure mode")
    return value.rstrip("/")


def collection_name(collection: str, suffix: str) -> str:
    if collection not in COLLECTIONS:
        raise ValueError("Unknown collection")
    return f"pii-{collection}-{suffix}"
