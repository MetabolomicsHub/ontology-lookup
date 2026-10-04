import os
from typing import Generator, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, status

from ontology_lookup.manager import OntologyDatabaseManager
from ontology_lookup.models import (
    CurieResolutionResponse,
    DatabaseInfo,
    HealthResponse,
    OntologyInfo,
    OntologyMutationResponse,
    SearchTermSummary,
    TagDeleteResponse,
    TagOperationRequest,
    TagOperationResponse,
    TagUpdateRequest,
    TermResponse,
)
from ontology_lookup.service import OntologyLookupService


def create_app(db_path: Optional[str] = None) -> FastAPI:
    """FastAPI application factory for the Ontology Lookup Service."""
    resolved_path = db_path or os.getenv("ONTOLOGY_DB_PATH", ".db/ontology_lookup.db")

    app = FastAPI(
        title="Ontology Lookup Service",
        description="High-performance, local, case-insensitive ontology lookup engine.",
        version="0.1.0",
    )

    def get_service() -> Generator[OntologyLookupService, None, None]:
        """Dependency yielding an OntologyLookupService with an open connection context."""
        with OntologyLookupService(db_path=resolved_path) as svc:
            yield svc

    def get_manager() -> Generator[OntologyDatabaseManager, None, None]:
        """Dependency yielding an OntologyDatabaseManager for write/mutation operations."""
        yield OntologyDatabaseManager(db_path=resolved_path)

    def _infer_ontology(acc: str) -> Optional[str]:
        clean = acc.strip()
        if ":" in clean and not clean.startswith(("http://", "https://", "urn:")):
            return clean.split(":", 1)[0].lower()
        if clean.startswith(("http://", "https://")):
            if "obolibrary.org/obo/" in clean:
                frag = clean.split("obolibrary.org/obo/", 1)[-1]
                if "_" in frag:
                    return frag.split("_", 1)[0].lower()
            if "edamontology.org" in clean:
                return "edam"
        return None

    def _resolve_curie(acc: str, svc: OntologyLookupService) -> str:
        clean = acc.strip()
        if clean.startswith(("http://", "https://", "urn:")):
            curie_res = svc.find_curie(clean)
            if curie_res:
                return curie_res.curie
        return clean

    # --- Use Case 1: Find a CV term with accession (CURIE or IRI) and ontology ---
    @app.get(
        "/ontologies/{ontology}/terms/{accession:path}",
        response_model=TermResponse,
        summary="Lookup term by ontology and CURIE or IRI",
    )
    def get_term_by_ontology_and_accession(
        ontology: str,
        accession: str,
        service: OntologyLookupService = Depends(get_service),
    ) -> TermResponse:
        """Lookup an ontology term by explicit ontology path and accession."""
        term = service.get_term(ontology=ontology, accession=accession)
        if not term:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Term not found for accession: {accession} in ontology: {ontology}",
            )
        return term

    # --- Term Tag Management ---
    @app.post(
        "/terms/{accession:path}/tags",
        response_model=TagOperationResponse,
        status_code=status.HTTP_201_CREATED,
        summary="Add a key-value tag to a term",
    )
    def add_term_tag(
        accession: str,
        tag: TagOperationRequest,
        ontology: Optional[str] = Query(
            None, description="Optional ontology identifier to scope tag"
        ),
        service: OntologyLookupService = Depends(get_service),
        manager: OntologyDatabaseManager = Depends(get_manager),
    ) -> TagOperationResponse:
        """Associate a key-value tag with a specific term."""
        curie = _resolve_curie(accession, service)
        success = manager.add_term_tag(
            curie=curie,
            tag_key=tag.tag_key,
            tag_value=tag.tag_value,
            ontology=ontology,
        )
        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Term '{curie}' not found; cannot add tag",
            )
        return TagOperationResponse(
            curie=curie,
            tag_key=tag.tag_key,
            tag_value=tag.tag_value,
            status="created",
        )

    @app.put(
        "/terms/{accession:path}/tags/{tag_key}",
        response_model=TagOperationResponse,
        summary="Update a tag value for a term",
    )
    def update_term_tag(
        accession: str,
        tag_key: str,
        payload: TagUpdateRequest,
        ontology: Optional[str] = Query(
            None, description="Optional ontology identifier to scope tag"
        ),
        service: OntologyLookupService = Depends(get_service),
        manager: OntologyDatabaseManager = Depends(get_manager),
    ) -> TagOperationResponse:
        """Update an existing tag value for a term."""
        curie = _resolve_curie(accession, service)
        success = manager.update_term_tag(
            curie=curie,
            tag_key=tag_key,
            new_value=payload.tag_value,
            old_value=payload.old_value,
            ontology=ontology,
        )
        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No matching tag '{tag_key}' found to update for term '{curie}'",
            )
        return TagOperationResponse(
            curie=curie,
            tag_key=tag_key,
            tag_value=payload.tag_value,
            status="updated",
        )

    @app.delete(
        "/terms/{accession:path}/tags/{tag_key}",
        response_model=TagDeleteResponse,
        summary="Delete tag(s) from a term",
    )
    def delete_term_tag(
        accession: str,
        tag_key: str,
        tag_value: Optional[str] = Query(None, description="Optional specific tag value to delete"),
        ontology: Optional[str] = Query(
            None, description="Optional ontology identifier to scope tag"
        ),
        service: OntologyLookupService = Depends(get_service),
        manager: OntologyDatabaseManager = Depends(get_manager),
    ) -> TagDeleteResponse:
        """Delete one or all tags matching tag_key for a term."""
        curie = _resolve_curie(accession, service)
        count = manager.delete_term_tag(
            curie=curie,
            tag_key=tag_key,
            tag_value=tag_value,
            ontology=ontology,
        )
        if count == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No tag '{tag_key}' found to delete for term '{curie}'",
            )
        return TagDeleteResponse(
            curie=curie,
            tag_key=tag_key,
            deleted_count=count,
        )

    @app.get(
        "/terms/{accession:path}",
        response_model=TermResponse,
        summary="Lookup term by CURIE or IRI",
    )
    def get_term_by_accession(
        accession: str,
        ontology: Optional[str] = Query(None, description="Optional ontology short name override"),
        service: OntologyLookupService = Depends(get_service),
    ) -> TermResponse:
        """Lookup an ontology term by its CURIE (e.g. MS:1000463) or full IRI.

        Case-insensitive.
        """
        target_ontology = ontology or _infer_ontology(accession)
        if not target_ontology:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Ontology could not be determined for accession: {accession}. "
                "Specify 'ontology' parameter.",
            )

        term = service.get_term(ontology=target_ontology, accession=accession)
        if not term:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Term not found for accession: {accession} in ontology: {target_ontology}",
            )
        return term

    # --- Use Case 2: Find a CV term with ontology and label (Exact Match) ---
    @app.get(
        "/search/exact",
        response_model=TermResponse,
        summary="Exact match by ontology and label",
    )
    def get_exact_label(
        ontology: str = Query(..., description="Ontology ID (e.g., 'ms')"),
        label: str = Query(..., description="Exact label of the term"),
        service: OntologyLookupService = Depends(get_service),
    ) -> TermResponse:
        """Exact match lookup by ontology and term label (case-insensitive)."""
        term = service.get_exact_label(ontology=ontology, label=label)
        if not term:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Term not found with ontology '{ontology}' and label '{label}'",
            )
        return term

    # --- Use Cases 3 & 4: Full-text search across labels and synonyms (+ parent filter) ---
    @app.get(
        "/search",
        response_model=List[SearchTermSummary],
        summary="Full-text search on labels and synonyms",
    )
    def search_terms(
        q: str = Query(..., min_length=1, description="Search query string"),
        ontology: Optional[List[str]] = Query(
            None, description="Optional; repeat for multiple ontologies"
        ),
        parent_curie: Optional[List[str]] = Query(
            None,
            description="Optional filter by predetermined parent CURIE(s)",
        ),
        limit: int = Query(50, ge=1, le=500, description="Max results to return"),
        offset: int = Query(0, ge=0, description="Offset for pagination"),
        service: OntologyLookupService = Depends(get_service),
    ) -> List[SearchTermSummary]:
        """Full-text search using SQLite FTS5 over labels and synonyms with optional

        parent hierarchy filter.
        """
        return service.search(
            query=q,
            ontology=ontology,
            parent_curie=parent_curie,
            limit=limit,
            offset=offset,
        )

    # --- Freetext Search ---
    @app.get(
        "/search/freetext",
        response_model=List[SearchTermSummary],
        summary="Freetext search across labels, synonyms, and definitions",
    )
    def freetext_search(
        q: str = Query(..., min_length=1, description="Freetext search query"),
        ontology: Optional[List[str]] = Query(
            None, description="Optional; repeat for multiple ontologies"
        ),
        parent_curie: Optional[List[str]] = Query(
            None,
            description="Optional filter by predetermined parent CURIE(s)",
        ),
        limit: int = Query(50, ge=1, le=500, description="Max results to return"),
        offset: int = Query(0, ge=0, description="Offset for pagination"),
        service: OntologyLookupService = Depends(get_service),
    ) -> List[SearchTermSummary]:
        """Full-text freetext search across term labels, synonyms, and definitions."""
        return service.freetext_search(
            query=q,
            ontology=ontology,
            parent_curie=parent_curie,
            limit=limit,
            offset=offset,
        )

    # --- Use Case 6: Key-value tag search ---
    @app.get(
        "/search/tags",
        response_model=List[SearchTermSummary],
        summary="Search terms by key-value metadata tags",
    )
    def search_by_tag(
        tag_key: str = Query(..., description="Tag key (e.g., 'child-of', 'obsolete', 'synonym')"),
        tag_value: str = Query(..., description="Tag value to match"),
        ontology: Optional[str] = Query(None, description="Optional ontology filter"),
        limit: int = Query(50, ge=1, le=500, description="Max results"),
        offset: int = Query(0, ge=0, description="Offset"),
        service: OntologyLookupService = Depends(get_service),
    ) -> List[SearchTermSummary]:
        """Search ontology terms by arbitrary key-value details/tags (case-insensitive)."""
        return service.search_by_tag(
            tag_key=tag_key,
            tag_value=tag_value,
            ontology=ontology,
            limit=limit,
            offset=offset,
        )

    # --- Use Case 5: Find CURIE of an IRI in an ontology ---
    @app.get(
        "/resolve/curie",
        response_model=CurieResolutionResponse,
        summary="Resolve IRI to CURIE",
    )
    def find_curie(
        iri: str = Query(..., description="Full IRI of the term"),
        ontology: str = Query(..., description="Ontology name (e.g. 'ms')"),
        service: OntologyLookupService = Depends(get_service),
    ) -> CurieResolutionResponse:
        """Resolve a full IRI to its primary CURIE within an ontology (case-insensitive)."""
        result = service.find_curie(iri=iri, ontology=ontology)
        if not result:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"IRI '{iri}' not found in ontology '{ontology}'",
            )
        return result

    # --- Convenience: Children of parent term ---
    @app.get(
        "/children/{parent_curie:path}",
        response_model=List[SearchTermSummary],
        summary="Get child terms of parent CURIE",
    )
    def get_children(
        parent_curie: str,
        ontology: Optional[str] = Query(None, description="Optional ontology filter"),
        limit: int = Query(50, ge=1, le=500, description="Max results"),
        offset: int = Query(0, ge=0, description="Offset"),
        service: OntologyLookupService = Depends(get_service),
    ) -> List[SearchTermSummary]:
        """Get terms that are recursive children of the given parent CURIE."""
        return service.get_children(
            parent_curie=parent_curie,
            ontology=ontology,
            limit=limit,
            offset=offset,
        )

    # --- Health check endpoint ---
    @app.get("/health", response_model=HealthResponse, summary="Health status")
    def health(service: OntologyLookupService = Depends(get_service)) -> HealthResponse:
        """Health check reporting database status and term count."""
        return service.get_health()

    # --- Ontologies list endpoint ---
    @app.get(
        "/ontologies",
        response_model=List[OntologyInfo],
        summary="List all installed ontologies",
    )
    def list_ontologies(
        service: OntologyLookupService = Depends(get_service),
    ) -> List[OntologyInfo]:
        """List all ontologies present in the database with metadata."""
        return service.list_ontologies()

    @app.get(
        "/ontologies/{ontology}",
        response_model=OntologyInfo,
        summary="Get metadata for a specific ontology",
    )
    def get_ontology(
        ontology: str,
        service: OntologyLookupService = Depends(get_service),
    ) -> OntologyInfo:
        """Retrieve metadata for a specific installed ontology."""
        info = service.get_ontology(ontology)
        if not info:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Ontology '{ontology}' not found in database",
            )
        return info

    @app.delete(
        "/ontologies/{ontology}",
        response_model=OntologyMutationResponse,
        summary="Delete an ontology and all its terms",
    )
    def delete_ontology(
        ontology: str,
        manager: OntologyDatabaseManager = Depends(get_manager),
    ) -> OntologyMutationResponse:
        """Delete an ontology, its terms, hierarchy links, and full-text search entries."""
        count = manager.delete_ontology(ontology)
        if count == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Ontology '{ontology}' not found or had no terms to delete",
            )
        return OntologyMutationResponse(
            ontology=ontology,
            action="deleted",
            terms_affected=count,
        )

    @app.post(
        "/ontologies/{ontology}",
        response_model=OntologyMutationResponse,
        summary="Add or update an ontology from source JSON",
    )
    @app.put(
        "/ontologies/{ontology}",
        response_model=OntologyMutationResponse,
        summary="Add or update an ontology from source JSON",
    )
    def update_ontology(
        ontology: str,
        source_dir: Optional[str] = Query(
            None, description="Optional directory containing ontology JSON"
        ),
        source_file: Optional[str] = Query(None, description="Optional explicit JSON file path"),
        manager: OntologyDatabaseManager = Depends(get_manager),
    ) -> OntologyMutationResponse:
        """Add or refresh an individual ontology from JSON source."""
        try:
            terms_cnt, details_cnt = manager.update_ontology(
                ontology_name=ontology,
                source_dir=source_dir,
                file_path=source_file,
            )
            return OntologyMutationResponse(
                ontology=ontology,
                action="updated",
                terms_affected=terms_cnt,
                details_affected=details_cnt,
            )
        except FileNotFoundError as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(e),
            ) from e

    # --- Database metadata info endpoint ---
    @app.get("/info", response_model=DatabaseInfo, summary="Database info")
    def database_info(
        service: OntologyLookupService = Depends(get_service),
    ) -> DatabaseInfo:
        """Get database metadata including created_time and updated_time."""
        return service.get_database_info()

    return app


# Default app instance
app = create_app()
