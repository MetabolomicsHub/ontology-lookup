import io
from collections.abc import Generator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ontology_lookup.api import create_app
from ontology_lookup.db import enable_wal_mode, get_db, get_readonly_connection
from ontology_lookup.loader import JsonOntologyLoader
from tests.test_loader import SAMPLE_MS_JSON


@pytest.fixture(scope="module")
def seeded_db_path(tmp_path_factory: pytest.TempPathFactory) -> str:
    """Fixture to build a temporary SQLite database populated with sample terms."""
    tmp_dir = tmp_path_factory.mktemp("api_data")
    db_file = str(tmp_dir / "api_test.db")
    stream = io.BytesIO(SAMPLE_MS_JSON.encode("utf-8"))

    loader = JsonOntologyLoader(db_path=db_file, target_parents=["MS:1000463", "MS:1000031"])
    loader.load_file(stream, ontology_name="ms", clean_db=True)
    enable_wal_mode(db_file)
    return db_file


@pytest.fixture(scope="module")
def client(seeded_db_path: str) -> Generator[TestClient]:
    """TestClient configured with the seeded test database."""
    app = create_app(db_path=seeded_db_path)

    # Override get_db dependency to point to the seeded test database
    def override_get_db() -> Generator[Any]:
        conn = get_readonly_connection(seeded_db_path)
        try:
            yield conn
        finally:
            conn.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_health_endpoint(client: TestClient) -> None:
    """Test health check endpoint."""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["term_count"] == 5


def test_use_case_1_accession_curie_and_iri(client: TestClient) -> None:
    """Use Case 1: Find a CV term with accession (CURIE or IRI, case-insensitive)."""
    # 1. Lookup with uppercase CURIE
    res1 = client.get("/terms/MS:1000031")
    assert res1.status_code == 200
    data1: dict[str, Any] = res1.json()
    assert data1["curie"] == "MS:1000031"
    assert data1["label"] == "instrument configuration"
    assert "instrument config" in data1["synonyms"]
    assert "MS:1000463" in data1["children_of"]

    # 2. Case-insensitivity test with lowercase CURIE
    res2 = client.get("/terms/ms:1000031")
    assert res2.status_code == 200
    assert res2.json()["curie"] == "MS:1000031"

    # 3. Lookup with IRI (containing slashes)
    iri = "http://purl.obolibrary.org/obo/MS_1000031"
    res3 = client.get(f"/terms/{iri}")
    assert res3.status_code == 200
    assert res3.json()["curie"] == "MS:1000031"

    # 4. Lookup with explicit /ontologies/{ontology}/terms/{accession} route
    res_ont = client.get("/ontologies/ms/terms/MS:1000031")
    assert res_ont.status_code == 200
    assert res_ont.json()["curie"] == "MS:1000031"

    # 5. Nonexistent term returns 404
    res_404 = client.get("/terms/MS:9999999")
    assert res_404.status_code == 404

    # 6. Mismatched ontology returns 404
    res_mismatch = client.get("/ontologies/edam/terms/MS:1000031")
    assert res_mismatch.status_code == 404


def test_use_case_2_exact_label_match(client: TestClient) -> None:
    """Use Case 2: Find a CV term with ontology and label
    (case-insensitive exact match)."""
    # Exact label match with different casing
    response = client.get(
        "/search/exact",
        params={"ontology": "MS", "label": "INSTRUMENT CONFIGURATION"},
    )
    assert response.status_code == 200
    data: dict[str, Any] = response.json()
    assert data["curie"] == "MS:1000031"
    assert data["ontology"] == "ms"

    # Nonexistent label returns 404
    missing_response = client.get(
        "/search/exact",
        params={"ontology": "ms", "label": "Nonexistent Device"},
    )
    assert missing_response.status_code == 404


def test_use_cases_3_and_4_full_text_search(client: TestClient) -> None:
    """Use Cases 3 & 4: Full-text search across labels and synonyms,
    with optional parent filter."""
    # Search for term matching label
    res1 = client.get("/search", params={"q": "orbitrap", "ontology": "ms"})
    assert res1.status_code == 200
    items1: list[dict[str, Any]] = res1.json()
    assert len(items1) >= 1
    assert items1[0]["curie"] == "MS:1000449"

    # Search for term matching synonym ("MALDI instrument" matches MS:1000031)
    res2 = client.get("/search", params={"q": "MALDI", "ontology": "ms"})
    assert res2.status_code == 200
    items2: list[dict[str, Any]] = res2.json()
    assert len(items2) >= 1
    assert items2[0]["curie"] == "MS:1000031"

    # Search constrained by parent CURIE MS:1000463 (matches instrument descendant)
    res3 = client.get(
        "/search",
        params={"q": "orbitrap", "ontology": "ms", "parent_curie": "MS:1000463"},
    )
    assert res3.status_code == 200
    items3: list[dict[str, Any]] = res3.json()
    assert len(items3) == 1
    assert items3[0]["curie"] == "MS:1000449"

    # Search with unrelated parent CURIE returns no results
    res4 = client.get(
        "/search",
        params={"q": "orbitrap", "ontology": "ms", "parent_curie": "MS:9999999"},
    )
    assert res4.status_code == 200
    assert len(res4.json()) == 0


def test_use_case_6_tag_search(client: TestClient) -> None:
    """Search terms by arbitrary key-value metadata tags."""
    # Find terms with child-of tag
    res_child = client.get(
        "/search/tags",
        params={"tag_key": "child-of", "tag_value": "MS:1000463"},
    )
    assert res_child.status_code == 200
    curies = [t["curie"] for t in res_child.json()]
    assert "MS:1000031" in curies
    assert "MS:1000449" in curies

    # Find obsolete terms
    res_obsolete = client.get(
        "/search/tags",
        params={"tag_key": "obsolete", "tag_value": "true"},
    )
    assert res_obsolete.status_code == 200
    assert len(res_obsolete.json()) == 1
    assert res_obsolete.json()[0]["curie"] == "MS:1000999"


def test_use_case_5_find_curie(client: TestClient) -> None:
    """Use Case 5: Find CURIE of an IRI in an ontology."""
    iri = "http://purl.obolibrary.org/obo/MS_1000463"
    response = client.get(
        "/resolve/curie",
        params={"iri": iri, "ontology": "ms"},
    )
    assert response.status_code == 200
    assert response.json()["curie"] == "MS:1000463"

    # Nonexistent IRI returns 404
    missing_response = client.get(
        "/resolve/curie",
        params={"iri": "http://example.org/missing", "ontology": "ms"},
    )
    assert missing_response.status_code == 404


def test_ontologies_endpoint(client: TestClient) -> None:
    """Test /ontologies endpoint."""
    response = client.get("/ontologies")
    assert response.status_code == 200
    ontologies = response.json()
    assert len(ontologies) >= 1
    ms = next(o for o in ontologies if o["ontology"] == "ms")
    assert ms["prefix"] == "MS"
    assert ms["num_of_terms"] >= 5
    assert ms["num_of_obsoletes"] >= 1
    assert ms["num_of_details"] > 0
    assert ms["iri_prefix"] == "http://purl.obolibrary.org/obo/MS_"
    assert ms["version"] == "4.2.2"
    assert ms["name"] == "Mass Spectrometry Controlled Vocabulary"


def test_database_info_endpoint(client: TestClient) -> None:
    """Test /info endpoint."""
    response = client.get("/info")
    assert response.status_code == 200
    data = response.json()
    assert "created_time" in data
    assert "T" in data["created_time"]
    assert "created_by" in data


def test_freetext_endpoint_with_lists(client: TestClient) -> None:
    """Test /search/freetext endpoint with list of ontologies and parent_curies."""
    # List of ontologies
    res = client.get(
        "/search/freetext",
        params={"q": "orbitrap", "ontology": ["ms", "edam"]},
    )
    assert res.status_code == 200
    items = res.json()
    assert len(items) >= 1
    assert items[0]["curie"] == "MS:1000449"

    # List of parents
    res_parents = client.get(
        "/search/freetext",
        params={
            "q": "orbitrap",
            "ontology": "ms",
            "parent_curie": ["MS:1000463", "MS:1000031"],
        },
    )
    assert res_parents.status_code == 200
    assert len(res_parents.json()) == 1
    assert res_parents.json()[0]["curie"] == "MS:1000449"


def test_tag_mutation_endpoints(client: TestClient) -> None:
    """Test adding, updating, and deleting tags via REST API."""
    # 1. Add tag
    res_add = client.post(
        "/terms/MS:1000031/tags",
        json={"tag_key": "api_group", "tag_value": "instrumentation"},
    )
    assert res_add.status_code == 201
    assert res_add.json()["status"] == "created"
    assert res_add.json()["tag_key"] == "api_group"
    assert res_add.json()["tag_value"] == "instrumentation"

    # Add tag to nonexistent term -> 404
    res_missing = client.post(
        "/terms/MS:9999999/tags",
        json={"tag_key": "api_group", "tag_value": "val"},
    )
    assert res_missing.status_code == 404

    # 2. Update tag
    res_upd = client.put(
        "/terms/MS:1000031/tags/api_group",
        json={"tag_value": "hardware", "old_value": "instrumentation"},
    )
    assert res_upd.status_code == 200
    assert res_upd.json()["status"] == "updated"
    assert res_upd.json()["tag_value"] == "hardware"

    # Update nonexistent tag -> 404
    res_upd_missing = client.put(
        "/terms/MS:1000031/tags/nonexistent_key",
        json={"tag_value": "val"},
    )
    assert res_upd_missing.status_code == 404

    # 3. Delete tag
    res_del = client.delete("/terms/MS:1000031/tags/api_group?tag_value=hardware")
    assert res_del.status_code == 200
    assert res_del.json()["deleted_count"] == 1

    # Delete already deleted tag -> 404
    res_del_again = client.delete("/terms/MS:1000031/tags/api_group")
    assert res_del_again.status_code == 404


def test_ontology_get_and_delete_endpoints(client: TestClient) -> None:
    """Test GET and DELETE /ontologies/{ontology} endpoints."""
    # 1. GET /ontologies/ms
    res_get = client.get("/ontologies/ms")
    assert res_get.status_code == 200
    data = res_get.json()
    assert data["ontology"] == "ms"
    assert data["prefix"] == "MS"

    # GET nonexistent ontology -> 404
    res_none = client.get("/ontologies/nonexistent")
    assert res_none.status_code == 404

    # 2. DELETE /ontologies/ms
    res_del = client.delete("/ontologies/ms")
    assert res_del.status_code == 200
    del_data = res_del.json()
    assert del_data["ontology"] == "ms"
    assert del_data["action"] == "deleted"
    assert del_data["terms_affected"] > 0

    # DELETE again -> 404
    res_del_again = client.delete("/ontologies/ms")
    assert res_del_again.status_code == 404
