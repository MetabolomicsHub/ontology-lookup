import sqlite3

import pytest

from ontology_lookup.db import (
    enable_wal_mode,
    get_readonly_connection,
    get_write_connection,
)
from ontology_lookup.schema import initialize_schema


def test_schema_initialization(tmp_path: pytest.TempPathFactory) -> None:
    """Test table and index creation."""
    db_file = str(tmp_path / "test_schema.db")
    conn = get_write_connection(db_file)
    initialize_schema(conn)

    cur = conn.cursor()
    # Check tables exist
    tables = [
        row[0]
        for row in cur.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual table')"
        ).fetchall()
    ]
    assert "terms" in tables
    assert "term_details" in tables
    assert "terms_fts" in tables
    assert "ontologies" in tables
    assert "database_info" in tables

    # Check ontologies table columns
    ont_cols = [row[1] for row in cur.execute("PRAGMA table_info(ontologies)").fetchall()]
    assert "ontology" in ont_cols
    assert "name" in ont_cols
    assert "description" in ont_cols
    assert "source_uri" in ont_cols
    assert "prefix" in ont_cols
    assert "version" in ont_cols
    assert "num_of_terms" in ont_cols
    assert "num_of_obsoletes" in ont_cols
    assert "num_of_details" in ont_cols
    assert "iri_prefix" in ont_cols

    # Check database_info default row and columns
    db_cols = [row[1] for row in cur.execute("PRAGMA table_info(database_info)").fetchall()]
    assert "created_by" in db_cols
    info_row = cur.execute(
        "SELECT created_time, updated_time, created_by FROM database_info"
    ).fetchone()
    assert info_row is not None
    assert info_row[0] is not None
    assert "T" in info_row[0]  # ISO format
    assert info_row[2] is not None

    # Check indices exist
    indices: list[str] = [
        row[0]
        for row in cur.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
    ]
    assert "idx_terms_iri" in indices
    assert "idx_ontology_label" in indices
    assert "idx_details_kv" in indices

    conn.close()


def test_wal_and_readonly_connection(tmp_path: pytest.TempPathFactory) -> None:
    """Test enabling WAL mode and read-only connection behavior."""
    db_file = str(tmp_path / "test_wal.db")
    write_conn = get_write_connection(db_file)
    initialize_schema(write_conn)
    write_conn.execute(
        "INSERT INTO terms (curie, iri, ontology, label) VALUES (?, ?, ?, ?)",
        ("TEST:1", "http://example.org/1", "test", "label 1"),
    )
    write_conn.commit()
    write_conn.close()

    enable_wal_mode(db_file)

    # Read-only connection should succeed reading
    ro_conn = get_readonly_connection(db_file)
    cur = ro_conn.cursor()
    row = cur.execute("SELECT label FROM terms WHERE curie = 'TEST:1'").fetchone()
    assert row is not None
    assert row["label"] == "label 1"

    # Attempting to write on read-only connection should fail
    with pytest.raises(sqlite3.OperationalError):
        cur.execute(
            "INSERT INTO terms (curie, iri, ontology, label) VALUES (?, ?, ?, ?)",
            ("TEST:2", "http://example.org/2", "test", "label 2"),
        )

    ro_conn.close()


def test_cross_ontology_referenced_terms(tmp_path: pytest.TempPathFactory) -> None:
    """Verify that terms referenced across ontologies are stored with (curie, ontology) PK."""
    db_file = str(tmp_path / "test_cross_ont.db")
    write_conn = get_write_connection(db_file)
    initialize_schema(write_conn)
    cur = write_conn.cursor()

    # Same CURIE referenced in two distinct ontologies
    shared_curie = "OBI:0200114"
    iri = "http://purl.obolibrary.org/obo/OBI_0200114"

    cur.execute(
        "INSERT INTO terms (curie, iri, ontology, label) VALUES (?, ?, ?, ?)",
        (shared_curie, iri, "ms", "OBI_0200114"),
    )
    cur.execute(
        "INSERT INTO terms (curie, iri, ontology, label) VALUES (?, ?, ?, ?)",
        (shared_curie, iri, "obi", "euclidean distance calculation"),
    )
    cur.execute(
        "INSERT INTO term_details (curie, ontology, tag_key, tag_value) VALUES (?, ?, ?, ?)",
        (shared_curie, "ms", "definition", "MS reference definition"),
    )
    cur.execute(
        "INSERT INTO term_details (curie, ontology, tag_key, tag_value) VALUES (?, ?, ?, ?)",
        (shared_curie, "obi", "definition", "OBI primary definition"),
    )
    write_conn.commit()

    # Both terms must exist in the terms table
    cur.execute(
        "SELECT ontology, label FROM terms WHERE curie = ? ORDER BY ontology",
        (shared_curie,),
    )
    rows = cur.fetchall()
    assert len(rows) == 2
    assert rows[0][0] == "ms" and rows[0][1] == "OBI_0200114"
    assert rows[1][0] == "obi" and rows[1][1] == "euclidean distance calculation"

    # Attempting duplicate (curie, ontology) should raise IntegrityError
    with pytest.raises(sqlite3.IntegrityError):
        cur.execute(
            "INSERT INTO terms (curie, iri, ontology, label) VALUES (?, ?, ?, ?)",
            (shared_curie, iri, "ms", "duplicate entry"),
        )

    # Deleting ontology 'ms' cascades only for 'ms' details and leaves 'obi' untouched
    cur.execute("DELETE FROM terms WHERE ontology = 'ms'")
    write_conn.commit()

    cur.execute("SELECT ontology, label FROM terms WHERE curie = ?", (shared_curie,))
    remaining_terms = cur.fetchall()
    assert len(remaining_terms) == 1
    assert remaining_terms[0][0] == "obi"

    cur.execute("SELECT ontology, tag_value FROM term_details WHERE curie = ?", (shared_curie,))
    remaining_details = cur.fetchall()
    assert len(remaining_details) == 1
    assert remaining_details[0][0] == "obi"
    assert remaining_details[0][1] == "OBI primary definition"

    write_conn.close()
