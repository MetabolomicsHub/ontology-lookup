from typing import Dict, List, Optional, Union

from pydantic import BaseModel, Field


class TermRecord(BaseModel):
    """Base ontology term record."""

    curie: str
    iri: str
    ontology: str
    label: str


class SearchTermSummary(TermRecord):
    """Compact summary of a term returned from full-text or exact search queries."""

    rank: Optional[float] = None


class TermResponse(TermRecord):
    """Full ontology term details including aggregated synonyms, hierarchy, and tags."""

    synonyms: List[str] = Field(default_factory=list)
    children_of: List[str] = Field(
        default_factory=list,
        description="Target parent CURIEs that this term is a descendant of",
    )
    tags: Dict[str, Union[str, List[str]]] = Field(
        default_factory=dict,
        description="Key-value metadata annotations associated with the term",
    )


class CurieResolutionResponse(BaseModel):
    """Response containing resolved CURIE for a given IRI and ontology."""

    curie: str


class IriResolutionResponse(BaseModel):
    """Response containing resolved IRI for a given IRI and ontology."""

    iri: str


class HealthResponse(BaseModel):
    """Health check response for the service."""

    status: str
    database_path: str
    term_count: int


class OntologyInfo(BaseModel):
    """Metadata describing an installed ontology."""

    ontology: str
    name: Optional[str] = None
    description: Optional[str] = None
    source_uri: Optional[str] = None
    prefix: Optional[str] = None
    version: Optional[str] = None
    term_count: Optional[int] = None
    num_of_terms: int = 0
    num_of_obsoletes: int = 0
    num_of_details: int = 0
    iri_prefix: Optional[str] = None


class DatabaseInfo(BaseModel):
    """Metadata describing the database."""

    created_time: str
    updated_time: Optional[str] = None
    created_by: Optional[str] = None
    updated_by: Optional[str] = None


class TagOperationRequest(BaseModel):
    """Payload to add a key-value tag to a term."""

    tag_key: str = Field(..., description="Tag key (e.g. 'custom_group')")
    tag_value: str = Field(..., description="Tag value to associate with the term")


class TagUpdateRequest(BaseModel):
    """Payload to update a tag value for a term."""

    tag_value: str = Field(..., description="New tag value")
    old_value: Optional[str] = Field(None, description="Optional previous value to target")


class TagOperationResponse(BaseModel):
    """Response returned after adding or updating a tag."""

    curie: str
    tag_key: str
    tag_value: str
    status: str


class TagDeleteResponse(BaseModel):
    """Response returned after deleting tag(s)."""

    curie: str
    tag_key: str
    deleted_count: int


class OntologyMutationResponse(BaseModel):
    """Response returned after adding, updating, or deleting an ontology."""

    ontology: str
    action: str
    terms_affected: int
    details_affected: Optional[int] = None
