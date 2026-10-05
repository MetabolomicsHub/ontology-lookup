"""Local Ontology Lookup Engine using SQLite, FTS5, and FastAPI."""

from ontology_lookup.config import DEFAULT_DATABASE_CONFIG, DatabaseCreationConfig
from ontology_lookup.db import get_readonly_connection
from ontology_lookup.loader import DEFAULT_TARGET_PARENTS, JsonOntologyLoader
from ontology_lookup.manager import (
    DEFAULT_INPUT_DIR,
    OntologyDatabaseManager,
    create_database,
)
from ontology_lookup.models import (
    CurieResolutionResponse,
    DatabaseInfo,
    HealthResponse,
    IriResolutionResponse,
    OntologyInfo,
    OntologyMutationResponse,
    SearchTermSummary,
    TagDeleteResponse,
    TagOperationRequest,
    TagOperationResponse,
    TagUpdateRequest,
    TermRecord,
    TermResponse,
)
from ontology_lookup.service import OntologyLookupService

__version__ = "0.1.0"
__all__ = [
    "DEFAULT_DATABASE_CONFIG",
    "DEFAULT_INPUT_DIR",
    "DEFAULT_TARGET_PARENTS",
    "CurieResolutionResponse",
    "DatabaseCreationConfig",
    "DatabaseInfo",
    "HealthResponse",
    "IriResolutionResponse",
    "JsonOntologyLoader",
    "OntologyDatabaseManager",
    "OntologyInfo",
    "OntologyLookupService",
    "OntologyMutationResponse",
    "SearchTermSummary",
    "TagDeleteResponse",
    "TagOperationRequest",
    "TagOperationResponse",
    "TagUpdateRequest",
    "TermRecord",
    "TermResponse",
    "create_database",
    "get_readonly_connection",
]
