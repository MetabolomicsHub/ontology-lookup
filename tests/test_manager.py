import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from ontology_lookup.manager import (
    DEFAULT_INPUT_DIR,
    OntologyDatabaseManager,
)

SAMPLE_MS_DATA: dict[str, Any] = {
    "ontologies": [
        {
            "ontologyId": "ms",
            "classes": [
                {
                    "curie": {"type": ["literal"], "value": "MS:1000000"},
                    "iri": "http://purl.obolibrary.org/obo/MS_1000000",
                    "label": [{"type": ["literal"], "value": "proteomic data"}],
                    "isObsolete": False,
                },
                {
                    "curie": {"type": ["literal"], "value": "MS:1000463"},
                    "iri": "http://purl.obolibrary.org/obo/MS_1000463",
                    "label": [{"type": ["literal"], "value": "instrument model"}],
                    "hierarchicalAncestor": ["http://purl.obolibrary.org/obo/MS_1000000"],
                    "isObsolete": False,
                },
                {
                    "curie": {"type": ["literal"], "value": "MS:1000031"},
                    "iri": "http://purl.obolibrary.org/obo/MS_1000031",
                    "label": [{"type": ["literal"], "value": "instrument configuration"}],
                    "synonym": [{"type": ["literal"], "value": "config"}],
                    "hierarchicalAncestor": ["http://purl.obolibrary.org/obo/MS_1000463"],
                    "isObsolete": False,
                },
            ],
        }
    ]
}

SAMPLE_OBI_DATA: dict[str, Any] = {
    "ontologies": [
        {
            "ontologyId": "obi",
            "classes": [
                {
                    "curie": {"type": ["literal"], "value": "OBI:0000001"},
                    "iri": "http://purl.obolibrary.org/obo/OBI_0000001",
                    "label": [{"type": ["literal"], "value": "investigation"}],
                    "isObsolete": False,
                },
                {
                    "curie": {"type": ["literal"], "value": "OBI:0000070"},
                    "iri": "http://purl.obolibrary.org/obo/OBI_0000070",
                    "label": [{"type": ["literal"], "value": "assay"}],
                    "hierarchicalAncestor": ["http://purl.obolibrary.org/obo/OBI_0000001"],
                    "isObsolete": False,
                },
                {
                    "curie": {"type": ["literal"], "value": "OBI:0000424"},
                    "iri": "http://purl.obolibrary.org/obo/OBI_0000424",
                    "label": [{"type": ["literal"], "value": "transcription profiling assay"}],
                    "synonym": [{"type": ["literal"], "value": "expression assay"}],
                    "hierarchicalAncestor": ["http://purl.obolibrary.org/obo/OBI_0000070"],
                    "isObsolete": False,
                },
            ],
        }
    ]
}

SAMPLE_OBI_UPDATED_DATA: dict[str, Any] = {
    "ontologies": [
        {
            "ontologyId": "obi",
            "classes": [
                {
                    "curie": {"type": ["literal"], "value": "OBI:0000001"},
                    "iri": "http://purl.obolibrary.org/obo/OBI_0000001",
                    "label": [{"type": ["literal"], "value": "investigation (updated)"}],
                    "isObsolete": False,
                },
                {
                    "curie": {"type": ["literal"], "value": "OBI:0000070"},
                    "iri": "http://purl.obolibrary.org/obo/OBI_0000070",
                    "label": [{"type": ["literal"], "value": "assay (updated)"}],
                    "isObsolete": False,
                },
                {
                    "curie": {"type": ["literal"], "value": "OBI:0000424"},
                    "iri": "http://purl.obolibrary.org/obo/OBI_0000424",
                    "label": [
                        {
                            "type": ["literal"],
                            "value": "transcription profiling assay (updated)",
                        }
                    ],
                    "isObsolete": False,
                },
            ],
        }
    ]
}


@pytest.fixture
def mock_json_dir(tmp_path: Path) -> Path:
    """Create a temporary directory with mock ontology JSON files."""
    json_dir = tmp_path / "mock_ontologies"
    json_dir.mkdir()

    with (json_dir / "ms.json").open("w") as f:
        json.dump(SAMPLE_MS_DATA, f)

    with (json_dir / "obi.json").open("w") as f:
        json.dump(SAMPLE_OBI_DATA, f)

    with (json_dir / "empty.json").open("w") as f:
        json.dump({"ontologies": []}, f)

    return json_dir


def test_default_input_dir_constant() -> None:
    """Verify DEFAULT_INPUT_DIR points to the expected path."""
    assert DEFAULT_INPUT_DIR == ".cache/ontology_jsons"


def test_list_available_ontologies(mock_json_dir: Path) -> None:
    """Verify listing available JSON files in the source directory."""
    mgr = OntologyDatabaseManager(default_input_dir=mock_json_dir)
    available = mgr.list_available_ontologies()
    assert len(available) == 3
    ont_names = [a["ontology"] for a in available]
    assert "ms" in ont_names
    assert "obi" in ont_names
    assert "empty" in ont_names


def test_load_directory_and_list_installed(tmp_path: Path, mock_json_dir: Path) -> None:
    """Test creating database by loading directory of JSON files."""
    db_file = str(tmp_path / "test_dir.db")
    mgr = OntologyDatabaseManager(db_path=db_file, default_input_dir=mock_json_dir)

    results = mgr.load_directory(clean=True)
    assert "ms" in results
    assert results["ms"]["terms"] == 3
    assert "obi" in results
    assert results["obi"]["terms"] == 3
    assert "empty" not in results  # empty skipped by default

    installed: list[dict[str, Any]] = mgr.list_installed_ontologies()
    assert len(installed) == 2
    ontologies = {item["ontology"] for item in installed}
    assert "ms" in ontologies
    assert "obi" in ontologies

    # Check prefix, description, source_uri fields
    ms_info = next(item for item in installed if item["ontology"] == "ms")
    assert "prefix" in ms_info
    assert "description" in ms_info
    assert "source_uri" in ms_info

    # Check database info
    info = mgr.get_database_info()
    assert "created_time" in info
    assert "updated_time" in info


def test_load_directory_filtered(tmp_path: Path, mock_json_dir: Path) -> None:
    """Test loading only specified ontologies from the source directory."""
    db_file = str(tmp_path / "test_filter.db")
    mgr = OntologyDatabaseManager(db_path=db_file, default_input_dir=mock_json_dir)

    results = mgr.load_directory(ontologies=["ms"], clean=True)
    assert "ms" in results
    assert "obi" not in results
    assert len(results) == 1

    installed = mgr.list_installed_ontologies()
    assert len(installed) == 1
    assert installed[0]["ontology"] == "ms"


def test_update_ontology_in_place(tmp_path: Path, mock_json_dir: Path) -> None:
    """Test updating an ontology from a modified JSON file."""
    db_file = str(tmp_path / "test_update.db")
    mgr = OntologyDatabaseManager(db_path=db_file, default_input_dir=mock_json_dir)

    mgr.load_directory(clean=True)
    file = mock_json_dir / "obi.json"
    # Overwrite obi.json with updated content
    with file.open("w") as f:
        json.dump(SAMPLE_OBI_UPDATED_DATA, f)

    terms_cnt, _ = mgr.update_ontology("obi")
    assert terms_cnt == 3

    # Check updated label in SQLite
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    row = cur.execute("SELECT label FROM terms WHERE curie = 'OBI:0000001'").fetchone()
    assert row is not None
    assert row["label"] == "investigation (updated)"

    # Verify MS terms remained untouched
    ms_row = cur.execute("SELECT label FROM terms WHERE curie = 'MS:1000000'").fetchone()
    assert ms_row is not None
    assert ms_row["label"] == "proteomic data"

    conn.close()


def test_delete_ontology(tmp_path: Path, mock_json_dir: Path) -> None:
    """Test completely removing an ontology from the database."""
    db_file = str(tmp_path / "test_delete.db")
    mgr = OntologyDatabaseManager(db_path=db_file, default_input_dir=mock_json_dir)

    mgr.load_directory(clean=True)
    deleted = mgr.delete_ontology("obi")
    assert deleted == 3

    conn = sqlite3.connect(db_file)
    cur = conn.cursor()
    assert cur.execute("SELECT COUNT(*) FROM terms WHERE ontology = 'obi'").fetchone()[0] == 0
    assert (
        cur.execute("SELECT COUNT(*) FROM term_details WHERE curie LIKE 'OBI:%'").fetchone()[0] == 0
    )
    assert cur.execute("SELECT COUNT(*) FROM terms WHERE ontology = 'ms'").fetchone()[0] == 3
    conn.close()


def test_missing_directory(tmp_path: Path) -> None:
    """Test error when source directory does not exist."""
    mgr = OntologyDatabaseManager(default_input_dir=tmp_path / "nonexistent")
    with pytest.raises(FileNotFoundError):
        mgr.load_directory()


def test_create_database_method_all_ontologies(tmp_path: Path, mock_json_dir: Path) -> None:
    """Test create_database adds all ontologies when ontologies parameter isgit addc
    undefined (None)."""
    db_file = str(tmp_path / "test_create_all.db")
    mgr = OntologyDatabaseManager(db_path=db_file, default_input_dir=mock_json_dir)

    # ontologies is None -> adds all
    res = mgr.create_database()
    assert "ms" in res
    assert "obi" in res
    assert res["ms"]["terms"] == 3
    assert res["obi"]["terms"] == 3

    installed = mgr.list_installed_ontologies()
    assert len(installed) == 2


def test_create_database_function_empty_list_adds_all(tmp_path: Path, mock_json_dir: Path) -> None:
    """Test module create_database adds all ontologies when ontologies is empty list."""
    from ontology_lookup.manager import create_database as create_db_fn

    db_file = str(tmp_path / "test_fn_all.db")
    res = create_db_fn(db_path=db_file, source_dir=mock_json_dir, ontologies=[])
    assert "ms" in res
    assert "obi" in res


def test_create_database_filtered(tmp_path: Path, mock_json_dir: Path) -> None:
    """Test create_database with defined ontologies loads only requested ontologies."""
    db_file = str(tmp_path / "test_create_filter.db")
    mgr = OntologyDatabaseManager(db_path=db_file, default_input_dir=mock_json_dir)

    res = mgr.create_database(ontologies=["ms"])
    assert "ms" in res
    assert "obi" not in res


def test_tag_management_lifecycle(tmp_path: Path, mock_json_dir: Path) -> None:
    """Test adding, updating, and deleting term tags."""
    db_file = str(tmp_path / "test_tags.db")
    mgr = OntologyDatabaseManager(db_path=db_file, default_input_dir=mock_json_dir)
    mgr.load_directory(clean=True)

    # 1. Add tag to existing term
    added = mgr.add_term_tag("MS:1000031", "custom_group", "chromatography")
    assert added is True

    # Check in DB
    conn = sqlite3.connect(db_file)
    cur = conn.cursor()
    row = cur.execute(
        "SELECT tag_value FROM term_details WHERE curie = 'MS:1000031' AND tag_key = 'custom_group'"
    ).fetchone()
    assert row is not None
    assert row[0] == "chromatography"

    # Add tag to nonexistent term
    missing_added = mgr.add_term_tag("MS:9999999", "custom_group", "val")
    assert missing_added is False

    # 2. Update tag
    updated = mgr.update_term_tag(
        "MS:1000031", "custom_group", "spectrometry", old_value="chromatography"
    )
    assert updated is True

    row = cur.execute(
        "SELECT tag_value FROM term_details WHERE curie = 'MS:1000031' AND tag_key = 'custom_group'"
    ).fetchone()
    assert row is not None
    assert row[0] == "spectrometry"

    # Update nonexistent tag
    not_updated = mgr.update_term_tag("MS:1000031", "nonexistent_key", "val")
    assert not_updated is False

    # 3. Delete tag
    deleted = mgr.delete_term_tag("MS:1000031", "custom_group", "spectrometry")
    assert deleted == 1

    row = cur.execute(
        "SELECT tag_value FROM term_details WHERE curie = 'MS:1000031' AND tag_key = 'custom_group'"
    ).fetchone()
    assert row is None
    conn.close()


def test_cli_tag_and_delete_commands(
    tmp_path: Path, mock_json_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Test CLI commands for delete-ontology, add-tag, update-tag, delete-tag."""
    from ontology_lookup.cli import main

    db_file = str(tmp_path / "test_cli.db")
    mgr = OntologyDatabaseManager(db_path=db_file, default_input_dir=mock_json_dir)
    mgr.load_directory(clean=True)

    # 1. CLI add-tag
    main(
        ["admin", "add-tag", "-c", "MS:1000031", "-k", "domain", "-v", "proteomics", "-d", db_file]
    )
    captured = capsys.readouterr()
    assert "Added tag 'domain=proteomics'" in captured.out

    # 2. CLI update-tag
    main(
        [
            "admin",
            "update-tag",
            "-c",
            "MS:1000031",
            "-k",
            "domain",
            "-v",
            "mass_spec",
            "-d",
            db_file,
        ]
    )
    captured = capsys.readouterr()
    assert "Updated tag 'domain'" in captured.out

    # 3. CLI delete-tag
    main(
        [
            "admin",
            "delete-tag",
            "-c",
            "MS:1000031",
            "-k",
            "domain",
            "-v",
            "mass_spec",
            "-d",
            db_file,
        ]
    )
    captured = capsys.readouterr()
    assert "Deleted 1 tag(s)" in captured.out

    # 4. CLI delete-ontology
    main(["admin", "delete-ontology", "-n", "obi", "-d", db_file])
    captured = capsys.readouterr()
    assert "Successfully deleted 'obi'" in captured.out
