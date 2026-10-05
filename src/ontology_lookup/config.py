"""Configuration for creating and populating ontology databases."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field

DEFAULT_SOURCE_DIRECTORY = Path(".cache/ontology_jsons")
DEFAULT_PARENT_TERMS = []
DEFAULT_DATABASE_PATH = Path(".db/ontology_lookup.db")
DEFAULT_BATCH_SIZE = 5000


class DatabaseCreationConfig(BaseModel):
    """Validated inputs that control database creation and ontology ingestion."""

    model_config = ConfigDict(frozen=True)

    source_directory: Path = DEFAULT_SOURCE_DIRECTORY
    parent_terms: tuple[str, ...] = DEFAULT_PARENT_TERMS
    database_path: Path = DEFAULT_DATABASE_PATH
    batch_size: int = Field(default=DEFAULT_BATCH_SIZE, gt=0)

    @classmethod
    def from_env(cls, env_file: None | str | Path = None) -> DatabaseCreationConfig:
        """Load defaults from the project .env file, overridden by process environment."""
        default_env_file = Path(__file__).resolve().parents[2] / ".env"
        values = dotenv_values(env_file or default_env_file)
        values.update(os.environ)

        source_directory = values.get("ONTOLOGY_SOURCE_DIR")
        parent_terms = values.get("ONTOLOGY_PARENT_TERMS")
        database_path = values.get("ONTOLOGY_DB_PATH")
        batch_size = values.get("ONTOLOGY_BATCH_SIZE")

        return cls(
            source_directory=Path(source_directory)
            if source_directory
            else DEFAULT_SOURCE_DIRECTORY,
            parent_terms=(
                tuple(term.strip() for term in parent_terms.split(",") if term.strip())
                if parent_terms is not None
                else DEFAULT_PARENT_TERMS
            ),
            database_path=Path(database_path) if database_path else DEFAULT_DATABASE_PATH,
            batch_size=int(batch_size) if batch_size else DEFAULT_BATCH_SIZE,
        )


DEFAULT_DATABASE_CONFIG = DatabaseCreationConfig.from_env()
