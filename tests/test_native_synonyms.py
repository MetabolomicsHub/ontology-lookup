import io
import json
import logging
import sqlite3
from pathlib import Path

import pytest
from ijson.common import JSONError

from ontology_lookup.loader import JsonOntologyLoader, NativeSynonymIndex, extract_allowed_synonyms
from ontology_lookup.manager import OntologyDatabaseManager
from ontology_lookup.service import OntologyLookupService

OBO = "http://www.geneontology.org/formats/oboInOwl#"


def write_source(path: Path, ontology: str, entities: list[dict], kind="classes", prefix=None):
    path.write_text(
        json.dumps(
            {
                "ontologies": [
                    {
                        "ontologyId": ontology,
                        "preferredPrefix": prefix or ontology.upper(),
                        kind: entities,
                    }
                ]
            }
        )
    )


@pytest.fixture
def native_sources(tmp_path):
    folder = tmp_path / "sources"
    folder.mkdir()
    native = {
        "curie": {"value": "UBERON:0000468"},
        "label": "multicellular organism",
        OBO + "hasExactSynonym": {
            "type": ["reification"],
            "value": {"value": "multi-cellular organism"},
        },
        OBO + "hasRelatedSynonym": {"value": {"value": "whole organism"}},
        OBO + "hasBroadSynonym": {"value": "organism"},
        OBO + "hasNarrowSynonym": {"value": "animal"},
        "synonym": ["multi-cellular organism", "whole organism", "organism", "animal"],
        "synonymProperty": [
            OBO + "has" + scope + "Synonym" for scope in ("Exact", "Related", "Broad", "Narrow")
        ],
    }
    imported = {
        "curie": "UBERON:0000468",
        "label": "imported multicellular organism",
        OBO + "hasExactSynonym": [
            "multi-cellular organism",
            "whole organism",
            "organism",
            "animal",
        ],
        "prefLabel": "whole organism",
    }
    write_source(folder / "uberon.json", "uberon", [native])
    write_source(folder / "ceph.json", "ceph", [imported])
    return folder, native, imported


def new_manager(tmp_path, folder):
    return OntologyDatabaseManager(
        db_path=tmp_path / "ontology.db",
        default_input_dir=folder,
        default_sssom_dir=tmp_path / "no-automatic-sssom",
    )


def stored_synonyms(db):
    with sqlite3.connect(db) as conn:
        return set(
            conn.execute(
                "SELECT curie, ontology, tag_value FROM term_details WHERE tag_key = 'synonym'"
            )
        )


def test_native_synonyms_control_tags_and_search(tmp_path, native_sources):
    folder, _, _ = native_sources
    manager = new_manager(tmp_path, folder)
    manager.create_database()
    assert stored_synonyms(manager.db_path_str) == {
        ("UBERON:0000468", "uberon", "multi-cellular organism"),
        ("UBERON:0000468", "ceph", "multi-cellular organism"),
    }
    service = OntologyLookupService(manager.db_path_str)
    try:
        for excluded in ("whole organism", "organism", "animal"):
            assert service.search_by_label(excluded) == []
        assert {
            term.ontology.lower() for term in service.search_by_label("multi-cellular organism")
        } == {"ceph", "uberon"}
        with sqlite3.connect(manager.db_path_str) as conn:
            assert {row[0] for row in conn.execute("SELECT synonyms FROM terms_fts")} == {
                "multi-cellular organism"
            }
    finally:
        service.close()


def test_native_source_applies_even_when_not_selected(tmp_path, native_sources):
    folder, _, _ = native_sources
    manager = new_manager(tmp_path, folder)
    manager.create_database(ontologies=["ceph"])
    assert stored_synonyms(manager.db_path_str) == {
        ("UBERON:0000468", "ceph", "multi-cellular organism")
    }


@pytest.mark.parametrize("kind", ["classes", "individuals"])
def test_native_metadata_prefix_and_empty_synonyms(tmp_path, kind):
    folder = tmp_path / "sources"
    folder.mkdir()
    write_source(
        folder / "unrelated_filename.json",
        "native_ontology",
        [{"curie": "NATIVE:1", "label": "native", OBO + "hasRelatedSynonym": "banned"}],
        kind=kind,
        prefix="NATIVE",
    )
    write_source(
        folder / "imported.json",
        "imported",
        [
            {
                "curie": "native:1",
                "label": "imported",
                OBO + "hasExactSynonym": ["banned", "introduced"],
                "prefLabel": "introduced",
            }
        ],
        kind=kind,
    )
    manager = new_manager(tmp_path, folder)
    manager.create_database()
    assert stored_synonyms(manager.db_path_str) == set()


def test_missing_native_term_and_source_fall_back(tmp_path):
    folder = tmp_path / "sources"
    folder.mkdir()
    write_source(folder / "native.json", "native", [{"curie": "NATIVE:other", "label": "other"}])
    entities = [
        {
            "curie": curie,
            "label": "term",
            OBO + "hasExactSynonym": "allowed",
            OBO + "hasRelatedSynonym": "excluded",
        }
        for curie in ("NATIVE:missing", "ABSENT:1")
    ]
    write_source(folder / "imported.json", "imported", entities)
    manager = new_manager(tmp_path, folder)
    manager.create_database(ontologies=["imported"])
    assert stored_synonyms(manager.db_path_str) == {
        ("NATIVE:missing", "imported", "allowed"),
        ("ABSENT:1", "imported", "allowed"),
    }


def test_native_exact_overlap_and_unscoped_remain_allowed(tmp_path):
    folder = tmp_path / "sources"
    folder.mkdir()
    write_source(
        folder / "native.json",
        "native",
        [
            {
                "curie": "NATIVE:1",
                "label": "term",
                OBO + "hasExactSynonym": "shared",
                OBO + "hasRelatedSynonym": "shared",
                OBO + "hasSynonym": "unscoped",
            }
        ],
    )
    write_source(
        folder / "imported.json",
        "imported",
        [{"curie": "NATIVE:1", "label": "term", OBO + "hasRelatedSynonym": ["shared", "unscoped"]}],
    )
    manager = new_manager(tmp_path, folder)
    manager.create_database(ontologies=["imported"])
    assert stored_synonyms(manager.db_path_str) == {
        ("NATIVE:1", "imported", "shared"),
        ("NATIVE:1", "imported", "unscoped"),
    }


def test_replacement_update_removes_old_tag_and_fts(tmp_path, native_sources):
    folder, native, _ = native_sources
    manager = new_manager(tmp_path, folder)
    # Model a prior import made without the native source.
    (folder / "uberon.json").unlink()
    manager.create_database(ontologies=["ceph"])
    assert ("UBERON:0000468", "ceph", "whole organism") in stored_synonyms(manager.db_path_str)
    write_source(folder / "uberon.json", "uberon", [native])
    manager.update_ontology("ceph")
    assert stored_synonyms(manager.db_path_str) == {
        ("UBERON:0000468", "ceph", "multi-cellular organism")
    }
    with sqlite3.connect(manager.db_path_str) as conn:
        assert conn.execute("SELECT synonyms FROM terms_fts WHERE ontology='ceph'").fetchall() == [
            ("multi-cellular organism",)
        ]


def test_direct_file_and_stream_native_resolution(tmp_path, native_sources):
    folder, _, imported = native_sources
    loader = JsonOntologyLoader(str(tmp_path / "direct.db"))
    loader.load_file(folder / "ceph.json")
    assert stored_synonyms(loader.db_path) == {
        ("UBERON:0000468", "ceph", "multi-cellular organism")
    }
    payload = json.dumps({"ontologies": [{"ontologyId": "ceph", "classes": [imported]}]}).encode()
    loader.load_file(io.BytesIO(payload), ontology_name="ceph", native_source_dir=folder)
    assert stored_synonyms(loader.db_path) == {
        ("UBERON:0000468", "ceph", "multi-cellular organism")
    }
    loader.load_file(io.BytesIO(payload), ontology_name="ceph")
    assert ("UBERON:0000468", "ceph", "whole organism") in stored_synonyms(loader.db_path)


def test_related_preferred_label_does_not_bypass_filter(tmp_path):
    payload = {
        "ontologies": [
            {
                "ontologyId": "native",
                "classes": [
                    {
                        "curie": "NATIVE:1",
                        "label": "term",
                        "prefLabel": "related alias",
                        OBO + "hasRelatedSynonym": "related alias",
                    }
                ],
            }
        ]
    }
    loader = JsonOntologyLoader(str(tmp_path / "direct.db"))
    loader.load_file(io.BytesIO(json.dumps(payload).encode()), ontology_name="native")
    assert stored_synonyms(loader.db_path) == set()


@pytest.mark.parametrize("scope", ["Related", "Broad", "Narrow"])
def test_aggregate_case_variants_cannot_reintroduce_excluded_synonyms(scope):
    entity = {
        OBO + "hasExactSynonym": "allowed",
        OBO + f"has{scope}Synonym": {"value": {"value": "excluded alias"}},
        "synonymProperty": OBO + "hasExactSynonym",
        "synonym": ["allowed", "EXCLUDED ALIAS"],
    }
    assert extract_allowed_synonyms(entity) == {"allowed"}


def test_invalid_native_source_does_not_replace_existing_database(tmp_path, native_sources, caplog):
    folder, _, _ = native_sources
    manager = new_manager(tmp_path, folder)
    manager.create_database()
    previous = stored_synonyms(manager.db_path_str)
    (folder / "uberon.json").write_text('{"ontologies": [{"classes": [invalid JSON')
    caplog.clear()
    with pytest.raises(JSONError):
        manager.create_database()
    assert stored_synonyms(manager.db_path_str) == previous
    assert "Database import failed after" in caplog.text
    assert "Database import completed" not in caplog.text


def test_native_index_is_shared_and_cleaned(tmp_path, native_sources, monkeypatch):
    folder, _, _ = native_sources
    entered = []
    original = NativeSynonymIndex.__enter__

    def counted(index):
        value = original(index)
        entered.append(Path(index._temporary.name))
        return value

    monkeypatch.setattr(NativeSynonymIndex, "__enter__", counted)
    new_manager(tmp_path, folder).create_database()
    assert len(entered) == 1
    assert not entered[0].exists()


def test_build_timing_includes_all_phases(tmp_path, native_sources, monkeypatch, caplog, capsys):
    folder, _, _ = native_sources
    manager = new_manager(tmp_path, folder)
    phases = []
    ticks = iter([100.0, 105.0, 110.0, 112.0, 115.0, 150.0])

    def clock():
        tick = next(ticks)
        if tick == 150.0:
            assert phases == ["sssom", "optimize"]
        return tick

    monkeypatch.setattr("ontology_lookup.loader.time.perf_counter", clock)
    monkeypatch.setattr(manager, "load_sssom_directory", lambda source: phases.append("sssom"))
    monkeypatch.setattr(manager, "optimize", lambda vacuum: phases.append("optimize"))
    with caplog.at_level(logging.INFO):
        manager.create_database(ontologies=["ceph"], sssom_dir=folder)
    assert "[ceph] Imported 1 terms" in caplog.text
    assert "in 2.000 seconds" in caplog.text
    assert "in 10.000 seconds" in caplog.text
    assert "Database import completed: 1 ontologies" in caplog.text
    assert "in 50.000 seconds" in caplog.text
    output = capsys.readouterr().out
    assert "in 10.000 seconds" in output
    assert "1 ontologies in 50.000 seconds" in output


def test_failed_import_logs_elapsed_without_success(tmp_path, native_sources, monkeypatch, caplog):
    folder, _, _ = native_sources
    manager = new_manager(tmp_path, folder)
    ticks = iter([100.0, 101.0, 102.0, 105.0, 109.0])
    monkeypatch.setattr("ontology_lookup.loader.time.perf_counter", lambda: next(ticks))

    def fail(*args, **kwargs):
        raise ValueError("simulated JSON import failure")

    monkeypatch.setattr(JsonOntologyLoader, "_load_file", fail)
    with caplog.at_level(logging.INFO), pytest.raises(ValueError, match="simulated"):
        manager.create_database(ontologies=["ceph"])
    assert "[ceph] Ontology import failed after 3.000 seconds" in caplog.text
    assert "Database import failed after 9.000 seconds" in caplog.text
    assert "Database import completed" not in caplog.text
    assert "[ceph] Imported" not in caplog.text


def test_private_directory_logs_its_own_timings(tmp_path, native_sources, monkeypatch, caplog):
    folder, _, _ = native_sources
    manager = new_manager(tmp_path, folder)
    ticks = iter([100.0, 103.0, 108.0, 120.0])
    monkeypatch.setattr("ontology_lookup.manager.time.perf_counter", lambda: next(ticks))
    # No loader duration is set: the manager must measure its own elapsed time.
    monkeypatch.setattr(JsonOntologyLoader, "load_file", lambda *args, **kwargs: (1, 2))
    monkeypatch.setattr(manager, "optimize", lambda vacuum: None)
    with caplog.at_level(logging.INFO):
        result = manager._load_directory(ontologies=["ceph"])
    assert result == {"ceph": {"terms": 1, "details": 2}}
    assert "Load result: 1 terms and 2 details ingested in 5.000 seconds" in caplog.text
    assert (
        "Database import completed: 1 ontologies (1 terms, 2 details) in 20.000 seconds"
        in caplog.text
    )
