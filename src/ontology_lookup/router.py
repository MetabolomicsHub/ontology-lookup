import os
import sys
from collections.abc import Generator
from typing import Annotated

from ontology_lookup.models import (
    CurieResolutionResponse,
    IriResolutionResponse,
    OntologyInfo,
    SearchTermSummary,
    TermResponse,
)
from ontology_lookup.service import OntologyLookupService

FASTAPI_INSTALLED = False
try:
    import uvicorn
    from fastapi import APIRouter, FastAPI, HTTPException, Query, status
    from fastapi import Path as PathParam

    FASTAPI_INSTALLED = True
except ImportError as exc:
    raise ImportError(
        "FastAPI dependencies are required to use ontology_lookup.api. "
        "Please install them via: pip install 'ontology-lookup[fastapi]'"
    ) from exc


if FASTAPI_INSTALLED:

    class OntologySearchLookupRouter:
        def __init__(self, db_path: None | str, prefix: str, tags: list[str]):
            self.resolved_path = db_path or os.getenv("ONTOLOGY_DB_PATH", ".db/ontology_lookup.db")
            self.prefix = prefix
            self.tags = tags
            self.router = APIRouter(prefix=self.prefix, tags=self.tags)
            self.service: OntologyLookupService = self.get_service()

            def _infer_ontology(acc: str) -> None | str:
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
            @self.router.get(
                "/ontologies/{ontology}/terms/{accession:path}",
                response_model=TermResponse,
                summary="Lookup term by ontology and CURIE or IRI",
            )
            def get_term_by_ontology_and_accession(
                ontology: Annotated[str, PathParam()],
                accession: Annotated[str, PathParam()],
            ) -> TermResponse:
                """Lookup an ontology term by explicit ontology path and accession."""
                term = self.service.get_term_by_accession(ontology=ontology, accession=accession)
                if not term:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=f"Term not found for accession: {accession} in ontology: {ontology}",
                    )
                return term

            @self.router.get(
                "/terms/{accession:path}",
                response_model=TermResponse,
                summary="Lookup term by CURIE or IRI",
            )
            def get_term_by_accession(
                accession: Annotated[str, PathParam()],
                ontology: Annotated[
                    None | str, Query(description="Optional ontology short name override")
                ] = None,
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

                term = self.service.get_term_by_accession(
                    ontology=target_ontology, accession=accession
                )
                if not term:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=f"Term not found for accession: {accession} in ontology: {target_ontology}",
                    )
                return term

            # --- Use Cases 3 & 4: Exact match on labels and synonyms (+ parent filter) ---
            @self.router.get(
                "/search",
                response_model=list[SearchTermSummary],
                summary="Exact match on labels or synonyms",
            )
            def search_terms(
                q: Annotated[str, Query(min_length=1, description="Exact label or synonym")],
                search_in_synonyms: Annotated[
                    bool, Query(description="Include synonym matches")
                ] = True,
                ontology: Annotated[
                    None | list[str], Query(description="Optional; repeat for multiple ontologies")
                ] = None,
                parent_curie: Annotated[
                    None | list[str], Query(description="Optional filter by parent CURIE(s)")
                ] = None,
                limit: Annotated[
                    int, Query(ge=1, le=500, description="Max results to return")
                ] = 50,
                offset: Annotated[int, Query(ge=0, description="Offset for pagination")] = 0,
            ) -> list[SearchTermSummary]:
                """Find complete, case-insensitive label or synonym matches."""
                return self.service.search_by_label(
                    label_or_synonym=q,
                    ontology=ontology,
                    parent_curie=parent_curie,
                    search_in_synonyms=search_in_synonyms,
                    limit=limit,
                    offset=offset,
                )

            # --- Use Case 2: Find a CV term with ontology and label (Exact Match) ---
            @self.router.get(
                "/search/exact",
                response_model=TermResponse,
                summary="Exact match by ontology and label",
            )
            def get_term_by_exact_label(
                ontology: Annotated[str, Query(description="Ontology ID (e.g., 'ms')")],
                label: Annotated[str, Query(description="Exact label of the term")],
            ) -> TermResponse:
                """Exact match lookup by ontology and term label (case-insensitive)."""
                term = self.service.get_term_by_exact_label(ontology=ontology, label=label)
                if not term:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=f"Term not found with ontology '{ontology}' and label '{label}'",
                    )
                return term

            # --- Freetext Search ---
            @self.router.get(
                "/search/freetext",
                response_model=list[SearchTermSummary],
                summary="Freetext search across labels, synonyms, and definitions",
            )
            def freetext_search(
                q: Annotated[str, Query(min_length=1, description="Freetext search query")],
                ontology: Annotated[
                    None | list[str], Query(description="Optional; repeat for multiple ontologies")
                ] = None,
                parent_curie: Annotated[
                    None | list[str], Query(description="Optional filter by parent CURIE(s)")
                ] = None,
                limit: Annotated[
                    int, Query(ge=1, le=500, description="Max results to return")
                ] = 50,
                offset: Annotated[int, Query(ge=0, description="Offset for pagination")] = 0,
            ) -> list[SearchTermSummary]:
                """Full-text freetext search across term labels, synonyms, and definitions."""
                return self.service.freetext_search(
                    query=q,
                    ontology=ontology,
                    parent_curie=parent_curie,
                    limit=limit,
                    offset=offset,
                )

            # --- Use Case 6: Key-value tag search ---
            @self.router.get(
                "/search/tags",
                response_model=list[SearchTermSummary],
                summary="Search terms by key-value metadata tags",
            )
            def search_by_tag(
                tag_key: Annotated[
                    str, Query(description="Tag key (e.g., 'child-of', 'obsolete', 'synonym')")
                ],
                tag_value: Annotated[str, Query(description="Tag value to match")],
                ontology: Annotated[
                    None | str, Query(description="Optional ontology filter")
                ] = None,
                limit: Annotated[int, Query(ge=1, le=500, description="Max results")] = 50,
                offset: Annotated[int, Query(ge=0, description="Offset")] = 0,
            ) -> list[SearchTermSummary]:
                """Search ontology terms by arbitrary key-value details/tags (case-insensitive)."""
                return self.service.search_by_tag(
                    tag_key=tag_key,
                    tag_value=tag_value,
                    ontology=ontology,
                    limit=limit,
                    offset=offset,
                )

            # --- Use Case 9: Find terms by cross-reference ---
            @self.router.get(
                "/search/references",
                response_model=list[SearchTermSummary],
                summary="Find terms by an exact xref CURIE",
            )
            def find_reference_term(
                reference_curie: Annotated[
                    str, Query(min_length=1, description="Referenced CURIE")
                ],
                ontology: Annotated[
                    None | str, Query(description="Optional target ontology")
                ] = None,
                limit: Annotated[int, Query(ge=1, le=500)] = 50,
                offset: Annotated[int, Query(ge=0)] = 0,
                ignore_obsolete: bool = False,
            ) -> list[SearchTermSummary]:
                """Find terms whose xref tag equals the supplied CURIE (case-insensitive)."""
                return self.service.find_reference_term(
                    reference_curie=reference_curie,
                    ontology=ontology,
                    limit=limit,
                    offset=offset,
                    ignore_obsolete=ignore_obsolete,
                )

            # --- Use Case 5: Find CURIE of an IRI in an ontology ---
            @self.router.get(
                "/resolve/curie",
                response_model=CurieResolutionResponse,
                summary="Resolve IRI to CURIE",
            )
            def find_curie(
                iri: Annotated[str, Query(description="Full IRI of the term")],
                ontology: Annotated[
                    None | str, Query(description="Optional ontology name (e.g. 'ms')")
                ] = None,
            ) -> CurieResolutionResponse:
                """Resolve a full IRI to its primary CURIE (case-insensitive)."""
                result = self.service.find_curie(iri=iri, ontology=ontology)
                if not result:
                    msg = f"IRI '{iri}' not found" + (
                        f" in ontology '{ontology}'" if ontology else ""
                    )
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=msg,
                    )
                return result

            @self.router.get(
                "/resolve/iri",
                response_model=IriResolutionResponse,
                summary="Resolve CURIE to IRI",
            )
            def find_iri(
                curie: Annotated[str, Query(description="CURIE of the term")],
                ontology: Annotated[
                    None | str, Query(description="Optional ontology filter")
                ] = None,
            ) -> IriResolutionResponse:
                """Resolve a CURIE to its IRI, optionally scoped to an ontology."""
                result = self.service.find_iri(curie=curie, ontology=ontology)
                if not result:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=f"CURIE '{curie}' not found",
                    )
                return result

            # --- Convenience: Children of parent term ---
            @self.router.get(
                "/children/{parent_curie:path}",
                response_model=list[SearchTermSummary],
                summary="Get child terms of parent CURIE",
            )
            def get_children(
                parent_curie: Annotated[str, PathParam()],
                ontology: Annotated[
                    None | str, Query(description="Optional ontology filter")
                ] = None,
                limit: Annotated[int, Query(ge=1, le=500, description="Max results")] = 50,
                offset: Annotated[int, Query(ge=0, description="Offset")] = 0,
            ) -> list[SearchTermSummary]:
                """Get terms that are recursive children of the given parent CURIE."""
                return self.service.get_children(
                    parent_curie=parent_curie,
                    ontology=ontology,
                    limit=limit,
                    offset=offset,
                )

            @self.router.get(
                "/parent-terms",
                response_model=list[str],
                summary="List indexed hierarchy parent CURIEs",
            )
            def get_indexed_parent_terms() -> list[str]:
                """List parent CURIEs referenced by indexed child-of hierarchy tags."""
                return self.service.get_indexed_parent_terms()

            # --- Ontologies list endpoint ---
            @self.router.get(
                "/ontologies",
                response_model=list[OntologyInfo],
                summary="List all installed ontologies",
            )
            def list_ontologies() -> list[OntologyInfo]:
                """List all ontologies present in the database with metadata."""
                return self.service.list_ontologies()

            @self.router.get(
                "/ontologies/{ontology}",
                response_model=OntologyInfo,
                summary="Get metadata for a specific ontology",
            )
            def get_ontology(
                ontology: Annotated[str, PathParam()],
            ) -> OntologyInfo:
                """Retrieve metadata for a specific installed ontology."""
                info = self.service.get_ontology(ontology)
                if not info:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=f"Ontology '{ontology}' not found in database",
                    )
                return info

        def get_service(self) -> Generator[OntologyLookupService]:
            """Yield an OntologyLookupService and close its default connection afterward."""
            service = OntologyLookupService(db_path=self.resolved_path)
            try:
                yield service
            finally:
                service.close()


if FASTAPI_INSTALLED and __name__ == "__main__":
    db_path = None
    if len(sys.argv) > 1:
        db_path = sys.argv[1] or None
    app = FastAPI(
        title="Ontology Lookup Service",
        description="High-performance, local, case-insensitive ontology lookup engine.",
        version="0.1.0",
    )
    lookup_router = OntologySearchLookupRouter(
        db_path, prefix="/ontology-lookup", tags=["Ontology Lookup"]
    )
    app.include_router(lookup_router.router)
    uvicorn.run(app, host="0.0.0.0", port=8000, workers=1)
