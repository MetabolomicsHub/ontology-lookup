import io
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

import pytest

from ontology_lookup.loader import (
    ALLOWED_SYNONYM_PROPERTIES,
    JsonOntologyLoader,
    extract_all_strs,
    extract_allowed_synonyms,
    extract_first_str,
    iri_to_curie,
)

SAMPLE_MS_DICT: dict[str, Any] = {
    "ontologies": [
        {
            "ontologyId": "ms",
            "title": "Mass Spectrometry Controlled Vocabulary",
            "preferredPrefix": "MS",
            "version": "4.2.2",
            "description": "Mass spectrometry controlled vocabulary",
            "ontologyPurl": "http://purl.obolibrary.org/obo/ms.owl",
            "classes": [
                {
                    "curie": {"type": ["literal"], "value": "MS:1000000"},
                    "iri": "http://purl.obolibrary.org/obo/MS_1000000",
                    "label": [{"type": ["literal"], "value": "proteomic data"}],
                    "definition": [{"type": ["literal"], "value": "Proteomic data definition."}],
                    "isObsolete": False,
                },
                {
                    "curie": {"type": ["literal"], "value": "MS:1000463"},
                    "iri": "http://purl.obolibrary.org/obo/MS_1000463",
                    "label": [{"type": ["literal"], "value": "instrument model"}],
                    "definition": [{"type": ["literal"], "value": "Instrument model description."}],
                    "hierarchicalAncestor": ["http://purl.obolibrary.org/obo/MS_1000000"],
                    "isObsolete": False,
                },
                {
                    "curie": {"type": ["literal"], "value": "MS:1000031"},
                    "iri": "http://purl.obolibrary.org/obo/MS_1000031",
                    "label": [{"type": ["literal"], "value": "instrument configuration"}],
                    "synonymProperty": "http://www.geneontology.org/formats/oboInOwl#hasExactSynonym",
                    "synonym": [
                        {"type": ["literal"], "value": "instrument config"},
                        {"type": ["literal"], "value": "MALDI instrument"},
                        {"type": ["literal"], "value": "MALDI"},
                    ],
                    "hierarchicalAncestor": [
                        "http://purl.obolibrary.org/obo/MS_1000000",
                        "http://purl.obolibrary.org/obo/MS_1000463",
                    ],
                    "isObsolete": False,
                },
                {
                    "curie": {"type": ["literal"], "value": "MS:1000449"},
                    "iri": "http://purl.obolibrary.org/obo/MS_1000449",
                    "label": [{"type": ["literal"], "value": "LTQ Orbitrap"}],
                    "synonymProperty": "http://www.geneontology.org/formats/oboInOwl#hasExactSynonym",
                    "synonym": [{"type": ["literal"], "value": "Orbitrap"}],
                    "definition": [
                        {
                            "type": ["literal"],
                            "value": "Thermo Scientific LTQ Orbitrap mass spectrometer.",
                        }
                    ],
                    "hierarchicalAncestor": [
                        "http://purl.obolibrary.org/obo/MS_1000000",
                        "http://purl.obolibrary.org/obo/MS_1000463",
                        "http://purl.obolibrary.org/obo/MS_1000031",
                    ],
                    "isObsolete": False,
                },
                {
                    "curie": {"type": ["literal"], "value": "MS:1000999"},
                    "iri": "http://purl.obolibrary.org/obo/MS_1000999",
                    "label": [{"type": ["literal"], "value": "obsolete detector"}],
                    "definition": [{"type": ["literal"], "value": "Deprecated term."}],
                    "isObsolete": True,
                },
                {
                    "curie": {"type": ["literal"], "value": "MS:1001000"},
                    "iri": "http://purl.obolibrary.org/obo/MS_1001000",
                    "label": [{"type": ["literal"], "value": "preferred term"}],
                    "http://www.w3.org/2004/02/skos/core#prefLabel": [
                        {"type": ["literal"], "value": "preferred label"}
                    ],
                },
                {
                    "curie": {"type": ["literal"], "value": "MS:1001001"},
                    "iri": "http://purl.obolibrary.org/obo/MS_1001001",
                    "label": [{"type": ["literal"], "value": "same label"}],
                    "prefLabel": [{"type": ["literal"], "value": "same label"}],
                },
            ],
        }
    ]
}

SAMPLE_MS_JSON = json.dumps(SAMPLE_MS_DICT)


@pytest.mark.parametrize("property_iri", sorted(ALLOWED_SYNONYM_PROPERTIES))
def test_allowed_synonym_source_properties(property_iri: str) -> None:
    assert extract_allowed_synonyms({property_iri: {"value": "direct"}}) == {"direct"}
    assert extract_allowed_synonyms(
        {
            "synonym": {"value": {"value": "aggregated"}},
            "synonymProperty": {"value": property_iri},
        }
    ) == {"aggregated"}


@pytest.mark.parametrize("scope", ["Related", "Broad", "Narrow"])
def test_excluded_synonyms_do_not_leak_from_aggregate(scope: str) -> None:
    prefix = "http://www.geneontology.org/formats/oboInOwl#"
    assert (
        extract_allowed_synonyms(
            {
                "synonym": ["excluded"],
                "synonymProperty": prefix + f"has{scope}Synonym",
            }
        )
        == set()
    )
    entity = {
        prefix + "hasExactSynonym": [{"value": "exact"}, {"value": "shared"}],
        prefix + "hasSynonym": {"value": "unscoped"},
        prefix + f"has{scope}Synonym": [{"value": "excluded"}, {"value": "shared"}],
        "synonym": ["exact", "unscoped", "excluded", "shared", "unattributed"],
        "synonymProperty": [prefix + "hasExactSynonym", prefix + f"has{scope}Synonym"],
    }
    assert extract_allowed_synonyms(entity) == {"exact", "unscoped", "shared"}


@pytest.mark.parametrize(
    "properties",
    [
        None,
        [],
        ["unknown"],
        [
            "http://www.geneontology.org/formats/oboInOwl#hasExactSynonym",
            "http://www.geneontology.org/formats/oboInOwl#hasRelatedSynonym",
        ],
    ],
)
def test_aggregate_without_identifiable_allowed_scope(properties: Any) -> None:
    assert (
        extract_allowed_synonyms(
            {
                "synonym": ["unattributed"],
                "synonymProperty": properties,
                "http://www.geneontology.org/formats/oboInOwl#hasSynonymType": "abbreviation",
                "oboSynonymTypeName": "abbreviation",
                "http://purl.org/sig/ont/fma/synonym": "other source",
            }
        )
        == set()
    )


def test_helper_functions() -> None:
    """Test string and CURIE extraction helper functions."""
    assert extract_first_str({"value": "test"}) == "test"
    assert extract_first_str([{"value": "first"}, "second"]) == "first"
    assert extract_first_str(None) is None
    assert extract_first_str(123) == "123"

    assert extract_all_strs([{"value": "a"}, "b", 42]) == ["a", "b", "42"]

    assert iri_to_curie("http://purl.obolibrary.org/obo/MS_1000001") == "MS:1000001"
    assert iri_to_curie("http://edamontology.org/data_0006") == "data:0006"
    assert iri_to_curie("http://www.w3.org/2002/07/owl#Thing") == "owl:Thing"


def test_loader_ingestion_and_hierarchy(tmp_path: Path) -> None:
    """Test JSON loader streaming, metadata extraction, hierarchy, and FTS5 storage."""
    db_file = str(tmp_path / "test_loader.db")
    stream = io.BytesIO(SAMPLE_MS_JSON.encode("utf-8"))

    loader = JsonOntologyLoader(db_path=db_file, target_parents=["MS:1000463", "MS:1000031"])
    terms_count, details_count = loader.load_file(
        source=stream,
        ontology_name="ms",
        clean_db=True,
    )

    assert terms_count == 7
    assert details_count > 0

    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # 1. Verify terms table
    term_rows: list[sqlite3.Row] = cur.execute("SELECT * FROM terms ORDER BY curie").fetchall()
    assert len(term_rows) == 7
    curies = [r["curie"] for r in term_rows]
    assert "MS:1000463" in curies
    assert "MS:1000031" in curies
    assert "MS:1000449" in curies

    # 2. Verify hierarchy links in term_details only for target parents
    children_463 = [
        r["curie"]
        for r in cur.execute(
            "SELECT curie FROM term_details WHERE tag_key = 'child-of' AND tag_value = 'MS:1000463'"
        ).fetchall()
    ]
    assert "MS:1000031" in children_463
    assert "MS:1000449" in children_463
    assert "MS:1000463" not in children_463

    children_031 = [
        r["curie"]
        for r in cur.execute(
            "SELECT curie FROM term_details WHERE tag_key = 'child-of' AND tag_value = 'MS:1000031'"
        ).fetchall()
    ]
    assert "MS:1000449" in children_031
    assert "MS:1000031" not in children_031

    # Verify non-target parents (e.g. MS:1000000) are NOT added as child-of
    children_000 = cur.execute(
        "SELECT curie FROM term_details WHERE tag_key = 'child-of' AND tag_value = 'MS:1000000'"
    ).fetchall()
    assert len(children_000) == 0

    # 3. Verify synonyms
    synonyms: list[tuple[str, str]] = [
        (r["curie"], r["tag_value"])
        for r in cur.execute(
            "SELECT curie, tag_value FROM term_details WHERE tag_key = 'synonym'"
        ).fetchall()
    ]
    assert ("MS:1000031", "instrument config") in synonyms
    assert ("MS:1000031", "MALDI instrument") in synonyms
    assert ("MS:1000449", "Orbitrap") in synonyms
    assert ("MS:1001000", "preferred label") in synonyms
    assert ("MS:1001001", "same label") not in synonyms

    # 4. Verify obsolete tag
    obsolete_term = cur.execute(
        "SELECT curie FROM term_details WHERE tag_key = 'obsolete' AND tag_value = 'true'"
    ).fetchone()
    assert obsolete_term is not None
    assert obsolete_term["curie"] == "MS:1000999"

    # 5. Verify FTS5 search
    fts_rows = cur.execute(
        "SELECT curie FROM terms_fts WHERE terms_fts MATCH 'orbitrap*'"
    ).fetchall()
    assert len(fts_rows) == 1
    assert fts_rows[0]["curie"] == "MS:1000449"

    # 6. Verify ontologies table metadata
    ont_row = cur.execute(
        "SELECT ontology, description, source_uri, prefix FROM ontologies WHERE ontology = 'ms'"
    ).fetchone()
    assert ont_row is not None
    assert ont_row["ontology"] == "ms"
    assert ont_row["prefix"] == "MS"
    assert ont_row["description"] == "Mass spectrometry controlled vocabulary"
    assert ont_row["source_uri"] == "http://purl.obolibrary.org/obo/ms.owl"

    # 7. Verify database_info updated_time
    info_row = cur.execute("SELECT created_time, updated_time FROM database_info").fetchone()
    assert info_row is not None
    assert info_row["created_time"] is not None
    assert info_row["updated_time"] is not None

    conn.close()


def test_loader_individuals(tmp_path: Path) -> None:
    """Test ingestion of individuals (such as from ROR) into terms and details."""
    db_file = str(tmp_path / "test_individuals.db")
    sample_ror_dict: Dict[str, Any] = {
        "ontologies": [
            {
                "ontologyId": "ror",
                "title": "Research Organization Registry",
                "preferredPrefix": "ror",
                "version": "2.0",
                "numberOfClasses": {"type": ["literal"], "value": 1},
                "numberOfIndividuals": {"type": ["literal"], "value": 2},
                "classes": [
                    {
                        "curie": {"type": ["literal"], "value": "ror:class1"},
                        "iri": "http://purl.obolibrary.org/obo/ror#class1",
                        "label": [{"type": ["literal"], "value": "Organization Class"}],
                        "isObsolete": False,
                    }
                ],
                "individuals": [
                    {
                        "curie": {"type": ["literal"], "value": "ror:000025p04"},
                        "iri": "https://ror.org/000025p04",
                        "label": [
                            {
                                "type": ["literal"],
                                "value": "Thailand Science Research and Innovation",
                            }
                        ],
                        "http://www.geneontology.org/formats/oboInOwl#hasRelatedSynonym": [
                            {"type": ["literal"], "value": "TSRI"}
                        ],
                        "http://purl.org/dc/terms/description": [
                            {
                                "type": ["literal"],
                                "value": "Thailand research funding organization.",
                            }
                        ],
                        "directParent": ["http://purl.obolibrary.org/obo/ror#class1"],
                        "isObsolete": False,
                    },
                    {
                        "curie": {"type": ["literal"], "value": "ror:00003g016"},
                        "iri": "https://ror.org/00003g016",
                        "label": [{"type": ["literal"], "value": "World Water Watch"}],
                        "isObsolete": True,
                    },
                ],
            }
        ]
    }

    stream = io.BytesIO(json.dumps(sample_ror_dict).encode("utf-8"))
    loader = JsonOntologyLoader(db_path=db_file)
    terms_count, details_count = loader.load_file(
        source=stream,
        ontology_name="ror",
        clean_db=True,
    )

    assert terms_count == 3
    assert details_count > 0

    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # 1. Verify terms table contains both class and individuals
    rows: List[sqlite3.Row] = cur.execute(
        "SELECT curie, label FROM terms ORDER BY curie"
    ).fetchall()
    curies = [r["curie"] for r in rows]
    assert curies == ["ror:000025p04", "ror:00003g016", "ror:class1"]

    # 2. Verify individual entity_type tag
    ind_tags: List[str] = [
        r["curie"]
        for r in cur.execute(
            "SELECT curie FROM term_details WHERE tag_key = 'entity_type' AND tag_value = 'individual'"
        ).fetchall()
    ]
    assert set(ind_tags) == {"ror:000025p04", "ror:00003g016"}

    # 3. Related synonyms are excluded from individuals; definitions are preserved.
    syn_row = cur.execute(
        "SELECT tag_value FROM term_details WHERE curie = 'ror:000025p04' AND tag_key = 'synonym'"
    ).fetchone()
    assert syn_row is None

    desc_row = cur.execute(
        "SELECT tag_value FROM term_details WHERE curie = 'ror:000025p04' AND tag_key = 'definition'"
    ).fetchone()
    assert desc_row is not None
    assert desc_row["tag_value"] == "Thailand research funding organization."

    # 4. Verify obsolete flag on individual
    obs_row = cur.execute(
        "SELECT tag_value FROM term_details WHERE curie = 'ror:00003g016' AND tag_key = 'obsolete'"
    ).fetchone()
    assert obs_row is not None
    assert obs_row["tag_value"] == "true"

    # 5. Verify FTS5 search on individuals
    fts_rows = cur.execute(
        "SELECT curie FROM terms_fts WHERE terms_fts MATCH '\"Thailand Science\"*'"
    ).fetchall()
    assert len(fts_rows) == 1
    assert fts_rows[0]["curie"] == "ror:000025p04"

    conn.close()
