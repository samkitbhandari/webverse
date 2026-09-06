"""Central configuration for NEXUS Omega.

Everything that touches the filesystem or an external provider reads its
settings from here, so a deployment can be relocated or run fully offline by
changing environment variables only.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(ROOT / ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    # --- filesystem ---------------------------------------------------------
    root: Path = ROOT
    incoming_dir: Path = ROOT / "incoming_files"
    vault_dir: Path = ROOT / "vault"
    data_dir: Path = ROOT / "data"

    @property
    def kuzu_dir(self) -> Path:
        return self.data_dir / "kuzu"

    @property
    def lance_dir(self) -> Path:
        return self.data_dir / "lancedb"

    @property
    def state_file(self) -> Path:
        return self.data_dir / "state.json"

    # --- providers ----------------------------------------------------------
    # "auto" picks the first provider with a usable key, else falls back to the
    # offline deterministic extractor so the whole pipeline still runs.
    llm_provider: Literal["auto", "openai", "anthropic", "offline"] = Field(
        default="auto",
        validation_alias=AliasChoices("NEXUS_LLM_PROVIDER", "LLM_PROVIDER"),
    )
    openai_api_key: str | None = Field(
        default=None, validation_alias=AliasChoices("NEXUS_OPENAI_API_KEY", "OPENAI_API_KEY")
    )
    anthropic_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("NEXUS_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"),
    )
    openai_model: str = "gpt-4o-mini"
    anthropic_model: str = "claude-sonnet-5"
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536

    # --- ingestion ----------------------------------------------------------
    debounce_seconds: float = 1.2          # collapse editor save-storms
    max_chunk_chars: int = 2400
    min_chunk_chars: int = 180

    # --- reasoning ----------------------------------------------------------
    impact_max_depth: int = 6
    impact_min_score: float = 0.02         # prune negligible propagation paths
    entity_match_threshold: float = 0.86   # combined lexical+semantic match
    entity_review_threshold: float = 0.72  # below match, above this => candidate
    contradiction_numeric_tolerance: float = 0.02   # 2% => not a contradiction

    # --- api ----------------------------------------------------------------
    host: str = "127.0.0.1"
    port: int = 8000
    cors_origins: list[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]

    def ensure_dirs(self) -> None:
        for p in (self.incoming_dir, self.vault_dir, self.data_dir, self.kuzu_dir.parent):
            p.mkdir(parents=True, exist_ok=True)


settings = Settings()
