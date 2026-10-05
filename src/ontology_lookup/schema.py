import os
import sqlite3

# DDL for core tables
CREATE_TERMS_TABLE = """
CREATE TABLE IF NOT EXISTS terms (
    curie TEXT NOT NULL COLLATE NOCASE,
    iri TEXT NOT NULL COLLATE NOCASE,
    ontology TEXT NOT NULL COLLATE NOCASE,
    label TEXT NOT NULL COLLATE NOCASE,
    PRIMARY KEY (curie, ontology)
);
"""

CREATE_TERM_DETAILS_TABLE = """
CREATE TABLE IF NOT EXISTS term_details (
    curie TEXT NOT NULL COLLATE NOCASE,
    ontology TEXT NOT NULL COLLATE NOCASE,
    tag_key TEXT NOT NULL COLLATE NOCASE,
    tag_value TEXT NOT NULL COLLATE NOCASE,
    PRIMARY KEY (curie, ontology, tag_key, tag_value),
    FOREIGN KEY (curie, ontology) REFERENCES terms(curie, ontology) ON DELETE CASCADE
) WITHOUT ROWID;
"""

CREATE_TERMS_FTS_TABLE = """
CREATE VIRTUAL TABLE IF NOT EXISTS terms_fts USING fts5(
    curie UNINDEXED,
    ontology UNINDEXED,
    label,
    synonyms,
    description,
    tokenize = 'porter unicode61'
);
"""

CREATE_ONTOLOGIES_TABLE = """
CREATE TABLE IF NOT EXISTS ontologies (
    ontology TEXT PRIMARY KEY COLLATE NOCASE,
    name TEXT,
    source_uri TEXT,
    prefix TEXT,
    version TEXT,
    num_of_terms INTEGER DEFAULT 0,
    num_of_obsoletes INTEGER DEFAULT 0,
    num_of_details INTEGER DEFAULT 0,
    iri_prefix TEXT DEFAULT '',
    description TEXT
);
"""

CREATE_DATABASE_INFO_TABLE = """
CREATE TABLE IF NOT EXISTS database_info (
    created_time TEXT NOT NULL,
    updated_time TEXT,
    created_by TEXT,
    updated_by TEXT
);
"""

# Indices for exact matching and fast joins
CREATE_INDICES: list[str] = [
    "CREATE INDEX IF NOT EXISTS idx_terms_curie ON terms(curie);",
    "CREATE INDEX IF NOT EXISTS idx_terms_iri ON terms(iri, ontology);",
    "CREATE INDEX IF NOT EXISTS idx_terms_iri_only ON terms(iri);",
    "CREATE INDEX IF NOT EXISTS idx_ontology_label ON terms(ontology, label);",
    "CREATE INDEX IF NOT EXISTS idx_terms_ontology ON terms(ontology);",
    "CREATE INDEX IF NOT EXISTS idx_details_curie_ont ON term_details(curie, ontology);",
    "CREATE INDEX IF NOT EXISTS idx_details_kv ON term_details(tag_key, tag_value);",
    "CREATE INDEX IF NOT EXISTS idx_details_ont_kv ON term_details(ontology, tag_key, tag_value);",
]


def initialize_schema(conn: sqlite3.Connection, created_by: None | str = None) -> None:
    """Initialize all schema tables and virtual tables in the database."""
    cur = conn.cursor()
    cur.execute(CREATE_TERMS_TABLE)
    cur.execute(CREATE_TERM_DETAILS_TABLE)
    cur.execute(CREATE_TERMS_FTS_TABLE)
    cur.execute(CREATE_ONTOLOGIES_TABLE)
    cur.execute(CREATE_DATABASE_INFO_TABLE)
    for idx_sql in CREATE_INDICES:
        cur.execute(idx_sql)

    # Initialize database_info row if not present
    creator = created_by or os.environ.get("USER") or "ontology_lookup"
    cur.execute("SELECT COUNT(*) FROM database_info")
    if cur.fetchone()[0] == 0:
        cur.execute(
            "INSERT INTO database_info (created_time, updated_time, created_by) "
            "VALUES (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), "
            "strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), ?)",
            (creator,),
        )
    conn.commit()
