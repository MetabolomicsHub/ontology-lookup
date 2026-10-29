import csv
import json
import sqlite3
from pathlib import Path

import pytest

from ontology_lookup.db import get_write_connection
from ontology_lookup.loader import JsonOntologyLoader, SssomOntologyLoader
from ontology_lookup.manager import OntologyDatabaseManager
from ontology_lookup.schema import initialize_schema
from ontology_lookup.service import OntologyLookupService


def write_mappings(path: Path, rows: list[tuple[str, str, str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        stream.write("# mapping_set_id: https://example.org/mappings\r\n")
        writer = csv.writer(stream, delimiter="\t")
        writer.writerow(["subject_id", "predicate_id", "object_id", "object_label"])
        writer.writerows(rows)


@pytest.fixture
def mapping_database(tmp_path: Path) -> tuple[Path, Path]:
    db_path = tmp_path / "ontology.db"
    conn = get_write_connection(str(db_path))
    initialize_schema(conn)
    with conn:
        for ontology in ("a", "b", "c", "aggregate"):
            conn.execute(
                "INSERT INTO ontologies (ontology, prefix) VALUES (?, ?)",
                (ontology, ontology.upper()),
            )
        conn.executemany(
            "INSERT INTO terms (curie, iri, ontology, label) VALUES (?, ?, ?, ?)",
            [
                (curie, f"https://example.org/{curie}", ontology, f"Term {curie}")
                for curie, ontology in (
                    ("A:1", "a"),
                    ("A:2", "a"),
                    ("A:3", "a"),
                    ("B:1", "b"),
                    ("C:1", "c"),
                    ("A:1", "aggregate"),
                )
            ],
        )
        conn.executemany(
            "INSERT INTO term_details VALUES (?, ?, ?, ?)",
            [("A:2", "a", "obsolete", "true"), ("A:1", "a", "synonym", "Unchanged")],
        )
    conn.close()
    folder = tmp_path / "sssom"
    folder.mkdir()
    # Source filename is deliberately unrelated to the owners' ontology prefixes.
    write_mappings(
        folder / "unrelated.ols.sssom.tsv",
        [
            ("A:1", "owl:equivalentClass", "B:1", ""),
            ("B:1", "owl:equivalentClass", "A:1", "duplicate reverse mapping"),
            ("a:1", "owl:equivalentClass", "b:1", "duplicate case variant"),
            ("A:1", "oboInOwl:hasDbXref", "C:1", "  Object label  "),
            ("A:2", "rdfs:equivalentClass", "C:1", ""),
            ("A:1", "oboInOwl:hasDbXref", "B:missing", "Missing term, installed ontology"),
            ("A:3", "owl:equivalentClass", "Z:1", "Missing object ontology"),
            ("A:3", "oboInOwl:hasDbXref", "Z:2", "Uninstalled ontology"),
            ("B:1", "oboInOwl:hasDbXref", "A:3", ""),
            ("B:1", "oboInOwl:hasDbXref", "A:3", "  \t  "),
            ("B:1", "rdfs:subClassOf", "A:3", "Not an xref"),
            ("B:1", "skos:exactMatch", "A:3", "Not selected"),
            ("A:1", "owl:equivalentClass", "https://example.org/1", "IRI, not CURIE"),
            ("A:1", "owl:equivalentClass", "B:", "Invalid CURIE"),
            ("", "owl:equivalentClass", "B:1", "Missing subject"),
        ],
    )
    write_mappings(folder / "empty.tsv", [])
    return db_path, folder


def test_sssom_import_filters_and_deduplicates(mapping_database: tuple[Path, Path]) -> None:
    db_path, folder = mapping_database
    loader = SssomOntologyLoader(str(db_path), batch_size=2)
    assert loader.load_directory(folder) == 8
    assert loader.load_directory(folder) == 0
    with sqlite3.connect(db_path) as conn:
        rows = set(conn.execute("SELECT * FROM term_details WHERE tag_key = 'xref'"))
        assert rows == {
            ("A:1", "a", "xref", "B:1"),
            ("B:1", "b", "xref", "A:1"),
            ("A:1", "a", "xref", "C:1"),
            ("C:1", "c", "xref", "A:1"),
            ("A:2", "a", "xref", "C:1"),
            ("C:1", "c", "xref", "A:2"),
            ("A:1", "a", "xref", "B:missing"),
            ("A:3", "a", "xref", "Z:1"),
        }
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0] == 6
        assert conn.execute(
            "SELECT tag_value FROM term_details WHERE tag_key = 'synonym'"
        ).fetchone() == ("Unchanged",)
        assert dict(conn.execute("SELECT ontology, num_of_details FROM ontologies")) == {
            "a": 7,
            "b": 1,
            "c": 2,
            "aggregate": 0,
        }


def test_sssom_import_is_atomic(mapping_database: tuple[Path, Path]) -> None:
    db_path, folder = mapping_database
    (folder / "z-invalid.tsv").write_text("subject_id\tobject_id\nA:1\tB:1\n")
    with pytest.raises(ValueError, match="z-invalid.tsv"):
        SssomOntologyLoader(str(db_path), batch_size=2).load_directory(folder)
    with sqlite3.connect(db_path) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM term_details WHERE tag_key = 'xref'").fetchone()[0]
            == 0
        )


def test_sssom_without_optional_label_column(mapping_database: tuple[Path, Path]) -> None:
    db_path, folder = mapping_database
    for path in folder.glob("*.tsv"):
        path.unlink()
    (folder / "minimal.tsv").write_text(
        "subject_id\tpredicate_id\tobject_id\n"
        "A:1\towl:equivalentClass\tB:1\n"
        "A:1\toboInOwl:hasDbXref\tC:1\n"
    )
    assert SssomOntologyLoader(str(db_path)).load_directory(folder) == 2


def test_sssom_missing_inputs(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="positive"):
        SssomOntologyLoader(str(tmp_path / "missing.db"), batch_size=0)
    loader = SssomOntologyLoader(str(tmp_path / "missing.db"))
    with pytest.raises(FileNotFoundError, match="SSSOM directory"):
        loader.load_directory(tmp_path / "missing")
    with pytest.raises(FileNotFoundError, match="Ontology database"):
        loader.load_directory(tmp_path)
    assert not (tmp_path / "missing.db").exists()


def test_find_reference_term(mapping_database: tuple[Path, Path]) -> None:
    db_path, folder = mapping_database
    assert OntologyDatabaseManager(db_path=db_path).load_sssom_directory(folder) == 8
    service = OntologyLookupService(str(db_path))
    try:
        assert [t.curie for t in service.find_reference_term("  b:1  ")] == ["A:1"]
        assert [t.curie for t in service.find_reference_term("A:1")] == ["B:1", "C:1"]
        assert [t.curie for t in service.find_reference_term("A:1", ontology=" C ")] == ["C:1"]
        assert service.find_reference_term("A:1", ontology="aggregate") == []
        assert service.find_reference_term("A:1", ontology="a") == []
        assert [t.curie for t in service.find_reference_term("C:1")] == ["A:1", "A:2"]
        assert [t.curie for t in service.find_reference_term("C:1", ignore_obsolete=True)] == [
            "A:1"
        ]
        assert [t.curie for t in service.find_reference_term("C:1", limit=1, offset=1)] == ["A:2"]
        assert service.find_reference_term("C:") == []
        assert service.find_reference_term("Unchanged") == []
        assert service.find_reference_term(" ") == []
        # A caller-owned connection must bypass cached results and remain usable.
        conn = get_write_connection(str(db_path))
        try:
            conn.execute("DELETE FROM term_details WHERE tag_key = 'xref'")
            assert service.find_reference_term("B:1", connection=conn) == []
            assert conn.execute("SELECT 1").fetchone()[0] == 1
        finally:
            conn.rollback()
            conn.close()
    finally:
        service.close()


def test_sssom_cli(mapping_database: tuple[Path, Path], capsys: pytest.CaptureFixture) -> None:
    from ontology_lookup.cli import main

    db_path, folder = mapping_database
    main(["admin", "load-sssom", "--db", str(db_path), "--dir", str(folder)])
    assert "Imported 8 new xref tags" in capsys.readouterr().out
    main(["find-reference-term", "--db", str(db_path), "--reference-curie", "B:1"])
    result = json.loads(capsys.readouterr().out)
    assert [term["curie"] for term in result] == ["A:1"]


def test_reference_search_api(mapping_database: tuple[Path, Path]) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ontology_lookup.api import create_app

    db_path, folder = mapping_database
    SssomOntologyLoader(str(db_path)).load_directory(folder)
    with TestClient(create_app(str(db_path))) as client:
        response = client.get("/search/references", params={"reference_curie": "b:1"})
        assert response.status_code == 200
        assert [term["curie"] for term in response.json()] == ["A:1"]
        response = client.get(
            "/search/references",
            params={"reference_curie": "C:1", "ignore_obsolete": True},
        )
        assert [term["curie"] for term in response.json()] == ["A:1"]
        assert (
            client.get(
                "/search/references", params={"reference_curie": "B:1", "limit": 0}
            ).status_code
            == 422
        )


@pytest.fixture
def ontology_sources(mapping_database: tuple[Path, Path]) -> tuple[Path, Path, Path]:
    db_path, sssom_dir = mapping_database
    json_dir = db_path.parent / "ontology_jsons"
    json_dir.mkdir()
    for ontology, curies in {"a": ["A:1", "A:2", "A:3"], "b": ["B:1"], "c": ["C:1"]}.items():
        (json_dir / f"{ontology}.json").write_text(
            json.dumps(
                {
                    "ontologies": [
                        {
                            "ontologyId": ontology,
                            "classes": [
                                {"curie": curie, "label": f"Term {curie}"} for curie in curies
                            ],
                        }
                    ]
                }
            )
        )
    return db_path, json_dir, sssom_dir


@pytest.mark.parametrize("entrypoint", ["method", "helper", "directory"])
def test_create_with_sssom(ontology_sources: tuple[Path, Path, Path], entrypoint: str) -> None:
    from ontology_lookup.manager import create_database

    db_path, json_dir, sssom_dir = ontology_sources
    manager = OntologyDatabaseManager(db_path=db_path)
    if entrypoint == "method":
        result = manager.create_database(source_dir=json_dir, sssom_dir=sssom_dir)
    elif entrypoint == "helper":
        result = create_database(db_path=db_path, source_dir=json_dir, sssom_dir=sssom_dir)
    else:
        result = manager.load_directory(source_dir=json_dir, clean=True, sssom_dir=sssom_dir)
    with sqlite3.connect(db_path) as conn:
        # DbXref targets in c.json must be installed before a's mappings are imported.
        assert (
            conn.execute("SELECT COUNT(*) FROM term_details WHERE tag_key = 'xref'").fetchone()[0]
            == 8
        )
        for ontology, counts in result.items():
            actual = conn.execute(
                "SELECT COUNT(*) FROM term_details WHERE ontology = ?", (ontology,)
            ).fetchone()[0]
            assert counts["details"] == actual
            assert (
                conn.execute(
                    "SELECT num_of_details FROM ontologies WHERE ontology = ?", (ontology,)
                ).fetchone()[0]
                == actual
            )


@pytest.mark.parametrize("entrypoint", ["update", "file", "loader"])
def test_update_restores_sssom(ontology_sources: tuple[Path, Path, Path], entrypoint: str) -> None:
    db_path, json_dir, sssom_dir = ontology_sources
    manager = OntologyDatabaseManager(db_path=db_path, default_input_dir=json_dir)
    # Omitting the new option retains JSON-only creation.
    manager.create_database()
    with sqlite3.connect(db_path) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM term_details WHERE tag_key = 'xref'").fetchone()[0]
            == 0
        )
    manager.load_sssom_directory(sssom_dir)
    assert manager.add_term_tag("C:1", "custom", "preserved", ontology="c")
    for _ in range(2):
        if entrypoint == "update":
            terms, details = manager.update_ontology("a", sssom_dir=sssom_dir)
        elif entrypoint == "file":
            terms, details = manager.load_file(json_dir / "a.json", sssom_dir=sssom_dir)
        else:
            terms, details = JsonOntologyLoader(str(db_path)).load_file(
                json_dir / "a.json", sssom_dir=sssom_dir
            )
        assert terms == 3
        with sqlite3.connect(db_path) as conn:
            assert (
                conn.execute("SELECT COUNT(*) FROM term_details WHERE tag_key = 'xref'").fetchone()[
                    0
                ]
                == 8
            )
            assert (
                details
                == conn.execute(
                    "SELECT COUNT(*) FROM term_details WHERE ontology = 'a'"
                ).fetchone()[0]
            )
            assert (
                conn.execute(
                    "SELECT tag_value FROM term_details WHERE tag_key = 'custom'"
                ).fetchone()[0]
                == "preserved"
            )
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_missing_sssom_folder_preserves_database(
    ontology_sources: tuple[Path, Path, Path],
) -> None:
    db_path, json_dir, _ = ontology_sources
    missing = json_dir / "missing"
    manager = OntologyDatabaseManager(db_path=db_path, default_input_dir=json_dir)
    with pytest.raises(FileNotFoundError, match="SSSOM directory"):
        manager.create_database(sssom_dir=missing)
    with pytest.raises(FileNotFoundError, match="SSSOM directory"):
        manager.update_ontology("a", sssom_dir=missing)
    with sqlite3.connect(db_path) as conn:
        assert (
            conn.execute("SELECT tag_value FROM term_details WHERE tag_key = 'synonym'").fetchone()[
                0
            ]
            == "Unchanged"
        )


def test_update_installs_sssom_object_ontology(
    ontology_sources: tuple[Path, Path, Path],
) -> None:
    db_path, json_dir, sssom_dir = ontology_sources
    manager = OntologyDatabaseManager(db_path=db_path, default_input_dir=json_dir)
    manager.create_database(ontologies=["a", "b"], sssom_dir=sssom_dir)
    with sqlite3.connect(db_path) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM term_details "
                "WHERE curie = 'A:1' AND tag_key = 'xref' AND tag_value = 'C:1'"
            ).fetchone()[0]
            == 0
        )
    manager.update_ontology("c", sssom_dir=sssom_dir)
    with sqlite3.connect(db_path) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM term_details WHERE tag_key = 'xref'").fetchone()[0]
            == 8
        )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_cli_creation_and_update_with_sssom(
    ontology_sources: tuple[Path, Path, Path], capsys: pytest.CaptureFixture
) -> None:
    from ontology_lookup.cli import main

    db_path, json_dir, sssom_dir = ontology_sources
    options = ["--db", str(db_path), "--dir", str(json_dir), "--sssom-dir", str(sssom_dir)]
    main(["admin", "init-db", "--clean", *options])
    capsys.readouterr()
    main(["admin", "update-ontology", "--name", "a", *options])
    capsys.readouterr()
    with sqlite3.connect(db_path) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM term_details WHERE tag_key = 'xref'").fetchone()[0]
            == 8
        )


def test_api_update_with_sssom(ontology_sources: tuple[Path, Path, Path]) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ontology_lookup.api import create_app

    db_path, json_dir, sssom_dir = ontology_sources
    manager = OntologyDatabaseManager(db_path=db_path, default_input_dir=json_dir)
    manager.create_database(sssom_dir=sssom_dir)
    with TestClient(create_app(str(db_path))) as client:
        response = client.put(
            "/ontologies/a", params={"source_dir": str(json_dir), "sssom_dir": str(sssom_dir)}
        )
        assert response.status_code == 200
        with sqlite3.connect(db_path) as conn:
            assert (
                response.json()["details_affected"]
                == conn.execute(
                    "SELECT COUNT(*) FROM term_details WHERE ontology = 'a'"
                ).fetchone()[0]
            )
            assert (
                conn.execute("SELECT COUNT(*) FROM term_details WHERE tag_key = 'xref'").fetchone()[
                    0
                ]
                == 8
            )
