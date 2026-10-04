import io
from typing import Generator, List, Optional

import pytest

from ontology_lookup.loader import JsonOntologyLoader
from ontology_lookup.models import (
    CurieResolutionResponse,
    DatabaseInfo,
    OntologyInfo,
    SearchTermSummary,
    TermRecord,
    TermResponse,
)
from ontology_lookup.service import OntologyLookupService
from tests.test_loader import SAMPLE_MS_JSON


@pytest.fixture(scope="module")
def seeded_db_file(tmp_path_factory: pytest.TempPathFactory) -> str:
    """Create and seed a temporary test database."""
    tmp_dir = tmp_path_factory.mktemp("service_test")
    db_path = str(tmp_dir / "service_test.db")
    stream = io.BytesIO(SAMPLE_MS_JSON.encode("utf-8"))

    loader = JsonOntologyLoader(
        db_path=db_path,
        target_parents=["MS:1000463", "MS:1000031"],
    )
    loader.load_file(stream, ontology_name="ms", clean_db=True)
    return db_path


@pytest.fixture
def service(seeded_db_file: str) -> Generator[OntologyLookupService, None, None]:
    """Create an OntologyLookupService instance using context manager
    to maintain connection."""
    with OntologyLookupService(db_path=seeded_db_file) as svc:
        yield svc


def test_context_manager_lifecycle(seeded_db_file: str) -> None:
    """Verify that 'with' keyword maintains connection
    during the block and closes on exit."""
    svc = OntologyLookupService(db_path=seeded_db_file)
    assert svc._conn is None

    with svc as active_service:
        assert active_service._conn is not None
        term = active_service.get_term_by_accession("ms", "MS:1000031")
        assert term is not None
        assert active_service._conn is not None

    # Connection closed after exiting 'with' block
    assert svc._conn is None


def test_use_case_1_get_term_by_accession(service: OntologyLookupService) -> None:
    """Use Case 1: Find a CV term with ontology and accession (CURIE or IRI)."""
    # 1. Lookup by CURIE (case-insensitive)
    term1: Optional[TermResponse] = service.get_term_by_accession("ms", "ms:1000031")
    assert term1 is not None
    assert term1.curie == "MS:1000031"
    assert term1.ontology == "ms"
    assert term1.label == "instrument configuration"
    assert "instrument config" in term1.synonyms
    assert "MALDI instrument" in term1.synonyms
    assert "MS:1000463" in term1.children_of

    # 2. Lookup by full IRI
    term2: Optional[TermResponse] = service.get_term_by_accession(
        "ms", "http://purl.obolibrary.org/obo/MS_1000449"
    )
    assert term2 is not None
    assert term2.curie == "MS:1000449"
    assert term2.ontology == "ms"
    assert term2.label == "LTQ Orbitrap"
    assert "Orbitrap" in term2.synonyms
    assert "MS:1000031" in term2.children_of

    # 3. Nonexistent returns None
    missing = service.get_term_by_accession("ms", "MS:9999999")
    assert missing is None

    # 4. Mismatched ontology returns None (strict ontology isolation)
    wrong_ont = service.get_term_by_accession("edam", "MS:1000031")
    assert wrong_ont is None


def test_use_case_2_get_term_by_exact_label(service: OntologyLookupService) -> None:
    """Use Case 2: Find a CV term with ontology and label (exact match,
    case-insensitive)."""
    # Case-insensitive label match
    term: Optional[TermResponse] = service.get_term_by_exact_label("ms", "INSTRUMENT CONFIGURATION")
    assert term is not None
    assert term.curie == "MS:1000031"
    assert term.ontology == "ms"

    # Nonexistent label returns None
    missing = service.get_term_by_exact_label("ms", "Nonexistent Device")
    assert missing is None


def test_use_case_3_and_4_search(service: OntologyLookupService) -> None:
    """Use Cases 3 & 4: Full-text search over labels and synonyms (+ parent filter)."""
    # Use Case 3: Label and synonym match
    res_label: List[SearchTermSummary] = service.search_by_label("orbitrap")
    assert len(res_label) >= 1
    assert res_label[0].curie == "MS:1000449"

    res_synonym: List[SearchTermSummary] = service.search_by_label("MALDI")
    assert len(res_synonym) >= 1
    assert res_synonym[0].curie == "MS:1000031"

    # Use Case 4: Search constrained by parent filter
    res_parent: List[SearchTermSummary] = service.search_by_label(
        "orbitrap",
        ontology="ms",
        parent_curie="MS:1000463",
    )
    assert len(res_parent) == 1
    assert res_parent[0].curie == "MS:1000449"

    # Unrelated parent filter returns empty
    res_unrelated: List[SearchTermSummary] = service.search_by_label(
        "orbitrap",
        ontology="ms",
        parent_curie="MS:9999999",
    )
    assert len(res_unrelated) == 0


def test_use_case_5_find_curie(service: OntologyLookupService) -> None:
    """Use Case 5: Find CURIE of an IRI in an ontology."""
    res: Optional[CurieResolutionResponse] = service.find_curie(
        "http://purl.obolibrary.org/obo/MS_1000000", "ms"
    )
    assert res is not None
    assert isinstance(res, CurieResolutionResponse)
    assert res.curie == "MS:1000000"

    # Nonexistent returns None
    missing = service.find_curie("http://example.com/unknown", "ms")
    assert missing is None


def test_use_case_6_search_by_tag(service: OntologyLookupService) -> None:
    """Use Case 6: Key-value metadata tag search."""
    # Search by obsolete tag
    obs: List[SearchTermSummary] = service.search_by_tag("obsolete", "true")
    assert len(obs) == 1
    assert obs[0].curie == "MS:1000999"

    # Search by child-of tag
    children: List[SearchTermSummary] = service.search_by_tag("child-of", "MS:1000463")
    child_curies = {c.curie for c in children}
    assert "MS:1000031" in child_curies
    assert "MS:1000449" in child_curies


def test_convenience_get_children(service: OntologyLookupService) -> None:
    """Test get_children convenience method."""
    children = service.get_children("MS:1000031")
    assert len(children) == 1
    assert children[0].curie == "MS:1000449"


def test_get_health(service: OntologyLookupService) -> None:
    """Test health check method."""
    health = service.get_health()
    assert health.status == "ok"
    assert health.term_count == 5


def test_assemble_term_response_pydantic_model(service: OntologyLookupService) -> None:
    """Verify assemble_term_response accepts a Pydantic TermRecord
    and returns a TermResponse."""
    rec = TermRecord(
        curie="MS:1000031",
        iri="http://purl.obolibrary.org/obo/MS_1000031",
        ontology="ms",
        label="instrument configuration",
    )
    term = service.assemble_term_response(rec)
    assert isinstance(term, TermResponse)
    assert term.curie == "MS:1000031"
    assert "instrument config" in term.synonyms
    assert "MS:1000463" in term.children_of


def test_list_ontologies_and_database_info(service: OntologyLookupService) -> None:
    """Verify list_ontologies and get_database_info return proper Pydantic models."""
    ontologies = service.list_ontologies()
    assert len(ontologies) >= 1
    ms_ont = next(o for o in ontologies if o.ontology == "ms")
    assert isinstance(ms_ont, OntologyInfo)
    assert ms_ont.prefix == "MS"
    assert ms_ont.name == "Mass Spectrometry Controlled Vocabulary"
    assert ms_ont.version == "4.2.2"
    assert ms_ont.num_of_terms == 5
    assert ms_ont.num_of_obsoletes == 1
    assert ms_ont.num_of_details > 0
    assert ms_ont.iri_prefix == "http://purl.obolibrary.org/obo/MS_"
    assert ms_ont.term_count == 5

    single_ont = service.get_ontology("ms")
    assert single_ont is not None
    assert single_ont.ontology == "ms"
    assert single_ont.name == "Mass Spectrometry Controlled Vocabulary"
    assert single_ont.version == "4.2.2"
    assert single_ont.num_of_terms == 5
    assert single_ont.num_of_obsoletes == 1
    assert single_ont.iri_prefix == "http://purl.obolibrary.org/obo/MS_"

    assert service.get_ontology("nonexistent") is None

    db_info = service.get_database_info()
    assert isinstance(db_info, DatabaseInfo)
    assert db_info.created_time is not None
    assert "T" in db_info.created_time
    assert db_info.created_by is not None


def test_freetext_search_list_inputs(service: OntologyLookupService) -> None:
    """Verify freetext_search supports list inputs for ontology and parent_curie."""
    # 1. Search matching description/definition
    res_def = service.freetext_search("Thermo Scientific")
    assert len(res_def) >= 1
    assert any(r.curie == "MS:1000449" for r in res_def)

    # 2. Search with list of ontologies
    res_ont_list = service.freetext_search(
        "orbitrap",
        ontology=["ms", "edam"],
    )
    assert len(res_ont_list) >= 1
    assert any(r.curie == "MS:1000449" for r in res_ont_list)

    # 3. Search with list of parent CURIEs
    res_parents_list = service.freetext_search(
        "orbitrap",
        ontology=["ms"],
        parent_curie=["MS:1000463", "MS:1000031"],
    )
    assert len(res_parents_list) >= 1
    assert res_parents_list[0].curie == "MS:1000449"


def test_is_leaf_tag(service: OntologyLookupService) -> None:
    """Verify is_leaf tag with value '1' is added if term has no child."""
    # Leaf term (MS:1000449 has no children)
    leaf_term = service.get_term_by_accession("ms", "MS:1000449")
    assert leaf_term is not None
    assert leaf_term.tags.get("is_leaf") == "1"

    # Non-leaf term (MS:1000031 has child MS:1000449)
    non_leaf_term = service.get_term_by_accession("ms", "MS:1000031")
    assert non_leaf_term is not None
    assert "is_leaf" not in non_leaf_term.tags

    # Search by tag is_leaf='1'
    leaves = service.search_by_tag("is_leaf", "1")
    leaf_curies = {t.curie for t in leaves}
    assert "MS:1000449" in leaf_curies
    assert "MS:1000031" not in leaf_curies
