"""Local Ontology Lookup Engine using SQLite, FTS5, and FastAPI."""

from ontology_lookup.config import DEFAULT_DATABASE_CONFIG, DatabaseCreationConfig
from ontology_lookup.loader import DEFAULT_TARGET_PARENTS, JsonOntologyLoader
from ontology_lookup.manager import (
    DEFAULT_INPUT_DIR,
    OntologyDatabaseManager,
    create_database,
)
from ontology_lookup.models import DatabaseInfo, OntologyInfo
from ontology_lookup.service import OntologyLookupService

__version__ = "0.1.0"
__all__ = [
    "DatabaseCreationConfig",
    "DEFAULT_DATABASE_CONFIG",
    "OntologyDatabaseManager",
    "create_database",
    "OntologyLookupService",
    "JsonOntologyLoader",
    "OntologyInfo",
    "DatabaseInfo",
    "DEFAULT_INPUT_DIR",
    "DEFAULT_TARGET_PARENTS",
]
