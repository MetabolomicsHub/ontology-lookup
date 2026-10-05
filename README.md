# Ontology Lookup: Multi-Ontology Search Service

A size-optimized local ontology lookup engine, Python library, and REST API built with **Python**, **SQLite (FTS5 + WAL)**, and **FastAPI**.

Designed for biological and scientific ontologies (such as HUPO PSI-MS, EDAM, OBI, GO, EFO, ChEBI, CL, DOID) hosted on EMBL-EBI OLS4, this engine provides sub-millisecond, case-insensitive query resolution, hierarchical graph traversal, and concurrent read scalability.

---

## Table of Contents

1. [Installation, Development and Test](#installation-development-and-test)
2. [Architecture and Design](#architecture-and-design)
3. [Download ontology sources from OLS4](#download-ontology-sources-from-ols4)
4. [Build Multi-ontology Lookup Database](#build-multi-ontology-lookup-database)
   - [CLI Commands](#cli-build-and-database-management)
     - [Create Database](#1-create-database-cli)
     - [Add Ontologies](#2-add-ontologies-cli)
     - [Delete Ontologies](#3-delete-ontologies-cli)
     - [Add, Update, and Delete Term Tags](#4-addupdatedelete-term-tags-cli)
     - [Inspect Database and Ontologies](#5-inspect-database-and-ontologies-cli)
   - [Python API](#python-api-build-and-database-management)
     - [Create Database](#1-create-database-api)
     - [Add Ontologies](#2-add-ontologies-api)
     - [Delete Ontologies](#3-delete-ontologies-api)
     - [Add, Update, and Delete Term Tags](#4-addupdatedelete-term-tags-api)
     - [Inspect Database and Ontologies](#5-inspect-database-and-ontologies-api)
5. [Search on Multi-ontology Lookup Database](#search-on-multi-ontology-lookup-database)
   - [Starting the REST API Server](#starting-the-rest-api-server)
   - [API Endpoints Reference Table](#api-endpoints-reference-table)
   - [Use Case 1: Term Lookup by CURIE or IRI (Accession)](#use-case-1-term-lookup-by-curie-or-iri-accession)
   - [Use Case 2: Exact Label Match](#use-case-2-exact-label-match)
   - [Use Case 3: Exact Match Search across Labels and Synonyms](#use-case-3-exact-match-search-across-labels-and-synonyms)
   - [Use Case 4: Hierarchy-Constrained Search (Parent CURIE Filter)](#use-case-4-hierarchy-constrained-search-parent-curie-filter)
   - [Use Case 5: CURIE and IRI Resolution](#use-case-5-curie-and-iri-resolution)
   - [Use Case 6: Key-Value Metadata Tag Search](#use-case-6-key-value-metadata-tag-search)
   - [Use Case 7: Recursive Descendants / Children Lookup](#use-case-7-recursive-descendants--children-lookup)
   - [Use Case 8: Freetext Search (Labels, Synonyms, and Definitions)](#use-case-8-freetext-search-labels-synonyms-and-definitions)

---

## Installation, Development and Test

This project requires **Python >= 3.10** and uses [`uv`](https://docs.astral.sh/uv/) for fast, reproducible dependency management and environment isolation.

### 1. Installation

Clone the repository and install all dependencies:

```bash
# Clone the repository
git clone https://github.com/your-org/ontology_lookup.git
cd ontology_lookup

# Sync virtual environment and install dependencies with uv
uv sync
```

Alternatively, install in development mode with standard `pip`:

```bash
pip install -e ".[dev]"
```

### 2. Environment Configuration

Default paths and ingestion parameters are managed via a `.env` file. Copy `.env.example` to start:

```bash
cp .env.example .env
```

Configurable variables:

| Variable | Default Value | Description |
| --- | --- | --- |
| `ONTOLOGY_SOURCE_DIR` | `.cache/ontology_jsons` | Default directory containing OLS4 ontology JSON files |
| `ONTOLOGY_DB_PATH` | `.db/ontology_lookup.db` | Target SQLite database file path |
| `ONTOLOGY_BATCH_SIZE` | `5000` | SQLite batch insert transaction chunk size |
| `ONTOLOGY_PARENT_TERMS` | `MS:1000031,MS:1000076,MS:1000008,MS:1003761,...` | Comma-separated target parent CURIEs precomputed for transitive closure lookups |

> **Precedence Order**: Command-line flags (e.g. `--db`, `--dir`) override process environment variables, which override values in `.env`.

### 3. Development & Quality Checks

Run Ruff linter, formatting checks, and pre-commit hooks:

```bash
# Run Ruff linting
uv run ruff check .

# Automatically apply safe fixes
uv run ruff check --fix .

# Check formatting
uv run ruff format --check .

# Format code
uv run ruff format .

# Install pre-commit hooks
uv run pre-commit install
```

### 4. Running Tests

Run the full test suite across the loader, database manager, query service, and REST API:

```bash
# Run all unit and integration tests
uv run pytest -v

# Run tests with short summary output
uv run pytest -q
```

---

## Architecture and Design

```
                     +---------------------------------------+
                     |    OLS4 Compressed Source Dumps       |
                     |   (https://ftp.ebi.ac.uk/.../*.tgz)   |
                     +-------------------+-------------------+
                                         |
                                         v (download & unpack)
                     +---------------------------------------+
                     |      Local Cache: .cache/ontology_jsons/ |
                     |      ms.json, edam.json, obi.json ... |
                     +-------------------+-------------------+
                                         |
                                         v
                     +---------------------------------------+
                     |      Streaming Ingestion (ijson)      |
                     |  - Memory-bounded JSON streaming      |
                     |  - Transitive closure precomputation  |
                     |  - Leaf node detection (is_leaf tag)  |
                     |  - Metadata tag normalization         |
                     +-------------------+-------------------+
                                         |
                                         v (Batch Ingest  + WAL)
                 +-------------------------------------------------------+
                 |               Optimized SQLite Database               |
                 |  +-------------------------------------------------+  |
                 |  | terms: (curie, ontology PK, iri, label)         |  |
                 |  | term_details (WITHOUT ROWID):                   |  |
                 |  |   (curie, ontology, tag_key, tag_value PK)      |  |
                 |  | terms_fts (FTS5 unicode61 porter):              |  |
                 |  |   (curie UNINDEXED, ontology UNINDEXED, ...)    |  |
                 |  | ontologies: (ontology PK, name, prefix, counts) |  |
                 |  | database_info: (created_time, updated_time)     |  |
                 |  +-------------------------------------------------+  |
                 +---------------------------+---------------------------+
                                             |
                     +-----------------------+-----------------------+
                     |                                               |
                     v (Direct / Embedded)                           v (Network / HTTP)
       +-------------------------------+               +-------------------------------+
       |    OntologyLookupService      |               |      FastAPI Web Service      |
       |  - Reusable default connection|               |  - URI `mode=ro` concurrency  |
       |  - B-Tree index scans (NOCASE)|               |  - 2 GB mmap_size             |
       |  - Precomputed child-of tags  |               |  - Sub-millisecond endpoints  |
       +-------------------------------+               +-------------------------------+
```

### Key Architectural Decisions

1. **Streaming Multi-Gigabyte Ingestion (`ijson`)**:
   OLS4 ontology files (e.g., `chebi.json`, `ncbitaxon.json`, `pr.json`) range from several hundred megabytes to multiple gigabytes. The ingestion pipeline streams class nodes via `ijson.items` to maintain a constant, low-memory footprint regardless of file size.

2. **Cross-Ontology Referencing & Composite Primary Key (`curie, ontology`)**:
   Ontologies frequently reference or import terms defined in other ontologies (for instance, `ms.json` references `OBI:0200114` and `UO:0000001` which are also defined in `obi.json` and `uo.json`). In the `terms` table, the composite primary key `(curie, ontology)` guarantees that every term is uniquely tracked within each referencing ontology, preserving ontology-specific labels, definitions, and relationships without collision. `term_details` similarly links via `FOREIGN KEY (curie, ontology) REFERENCES terms(curie, ontology) ON DELETE CASCADE`.

3. **Index-Organized Metadata (`WITHOUT ROWID`)**:
   Metadata tags (`synonym`, `child-of`, `obsolete`, `is_leaf`, `definition`) are stored in `term_details` with a composite primary key `(curie, ontology, tag_key, tag_value)`. Eliminating the standard 64-bit SQLite integer rowid cuts index overhead in half and clusters related tags contiguously on disk for blazing-fast sequential page reads.

4. **Case-Insensitive Zero-Cost Matching (`COLLATE NOCASE`)**:
   All identifier and text columns use SQLite's native `COLLATE NOCASE`. Queries match mixed-cased CURIEs (e.g. `ms:1000031` vs `MS:1000031`) or labels via instant B-Tree index lookups without calling `LOWER()` at runtime.

5. **Full-Text Autocomplete & Relevance Search (`FTS5`)**:
   SQLite's FTS5 engine (`unicode61` tokenizer with Porter stemming) indexes term labels, synonyms, and definitions with unindexed `ontology` scoping. It enables prefix matching, ranking, and tokenization without requiring external search daemons (e.g. Elasticsearch).

6. **Infinite Read Concurrency (WAL + URI `mode=ro` + `mmap`)**:
   - Write-Ahead Logging (`PRAGMA journal_mode = WAL;`) allows readers and writers to operate concurrently without locking.
   - Read-only connections (`file:db.sqlite?mode=ro`) bypass write-lock coordination.
   - `PRAGMA mmap_size = 2147483648;` memory-maps up to 2GB of the database directly into the OS page cache for sub-millisecond retrieval.
   - FastAPI executes synchronous query endpoints in worker thread pools to handle hundreds of concurrent requests efficiently.

---

## Download ontology sources from OLS4

The EMBL-EBI Ontology Lookup Service (OLS4) distributes standardized JSON representations of all hosted ontologies.

- **Direct Download URL**: [https://ftp.ebi.ac.uk/pub/databases/spot/ols/latest/ontology_jsons.tgz](https://ftp.ebi.ac.uk/pub/databases/spot/ols/latest/ontology_jsons.tgz)
- **Default Local Location**: `.cache/ontology_jsons`

### 1. Download and Extract the Archive

Execute the following commands to download and unpack the ontology JSON files into the `.cache` directory:

```bash
# 1. Create target download directory
mkdir -p .cache

# 2. Download the compressed archive
curl -L https://ftp.ebi.ac.uk/pub/databases/spot/ols/latest/ontology_jsons.tgz -o .cache/ontology_jsons.tgz

# 3. Extract the archive into .cache/
tar -xzf .cache/ontology_jsons.tgz -C .cache/
```

After extraction, the `.cache/ontology_jsons/` directory will contain individual JSON files named by ontology short name:

```text
.cache/ontology_jsons/
├── ms.json           (Mass Spectrometry Controlled Vocabulary)
├── edam.json         (EDAM Bioinformatics operations, data, formats)
├── obi.json          (Ontology for Biomedical Investigations)
├── bto.json          (BRENDA Tissue Ontology)
├── cl.json           (Cell Ontology)
├── clo.json          (Cell Line Ontology)
├── chmo.json         (Chemical Methods Ontology)
├── go.json           (Gene Ontology)
└── ... (over 350+ ontologies)
```

### 2. Verify Available Ontologies

Use the CLI to inspect available JSON sources in `.cache/ontology_jsons`:

```bash
uv run ontology-lookup list-ontologies --available --dir .cache/ontology_jsons
```

---

## Build Multi-ontology Lookup Database

The lookup engine provides both **Command-Line Interface (CLI)** and **Python Library API** for creating databases, adding ontologies, deleting ontologies, and managing custom term tags.

### CLI: Build and Database Management

All CLI operations are invoked via `uv run ontology-lookup` (or `ontology-lookup` if installed in your environment).

#### 1. Create Database (CLI)

Initialize a new multi-ontology SQLite database from JSON files in the source directory.

```bash
# Build database with core proteomics and biological ontologies:
uv run ontology-lookup admin init-db \
  --dir .cache/ontology_jsons \
  --ontologies ms,edam,obi,msio \
  --db .db/ontology_lookup.db \
  --clean
```

Parameters:

- `--dir`, `-i`: Directory containing ontology JSON files (default: `.cache/ontology_jsons`).
- `--ontologies`, `-o`: Comma-separated list of short names to ingest (e.g. `ms,edam,obi`). If omitted, all `*.json` files found in the directory are loaded.
- `--db`, `-d`: Target SQLite database path (default: `.db/ontology_lookup.db`).
- `--parents`, `-p`: Comma-separated target parent CURIEs for precomputing recursive `child-of` links.
- `--clean`: Recreates the database from scratch, removing any existing file.
- `--batch-size`, `-b`: Insert chunk size (default: `5000`).

Alternatively, build a database from a single standalone JSON file:

```bash
uv run ontology-lookup admin build \
  --source .cache/ontology_jsons/ms.json \
  --ontology ms \
  --db .db/ontology_lookup.db \
  --clean
```

#### 2. Add Ontologies (CLI)

Add or refresh an individual ontology in an existing database without modifying other ontologies:

```bash
# Add or update 'bto' (BRENDA Tissue Ontology) from source directory:
uv run ontology-lookup admin update-ontology \
  --name bto \
  --dir .cache/ontology_jsons \
  --db .db/ontology_lookup.db
```

Or provide an explicit JSON file path:

```bash
uv run ontology-lookup admin update-ontology \
  --name cl \
  --source /path/to/custom_cl.json \
  --db .db/ontology_lookup.db
```

#### 3. Delete Ontologies (CLI)

Completely delete an ontology, its terms, hierarchy links, tags, and full-text search entries:

```bash
# Delete 'obi' from the database:
uv run ontology-lookup admin delete-ontology \
  --name obi \
  --db .db/ontology_lookup.db
```

#### 4. Add/Update/Delete Term Tags (CLI)

Tags allow associating arbitrary key-value metadata (e.g. curation flags, custom categories, sub-classifications) with specific terms. An optional `--ontology / -o` flag allows scoping the tag to a specific ontology representation of the term.

```bash
# Add a custom tag to a term (optionally scoped with -o ms):
uv run ontology-lookup admin add-tag \
  --curie MS:1000031 \
  --key custom_category \
  --value instrumentation \
  --ontology ms \
  --db .db/ontology_lookup.db

# Update an existing tag value:
uv run ontology-lookup admin update-tag \
  --curie MS:1000031 \
  --key custom_category \
  --value core_hardware \
  --old-value instrumentation \
  --ontology ms \
  --db .db/ontology_lookup.db

# Delete a specific tag value:
uv run ontology-lookup admin delete-tag \
  --curie MS:1000031 \
  --key custom_category \
  --value core_hardware \
  --ontology ms \
  --db .db/ontology_lookup.db

# Delete all tags matching a key for a term:
uv run ontology-lookup admin delete-tag \
  --curie MS:1000031 \
  --key custom_category \
  --db .db/ontology_lookup.db
```

#### 5. Inspect Database and Ontologies (CLI)

```bash
# List all installed ontologies, term counts, versions, and IRI prefixes:
uv run ontology-lookup list-ontologies --db .db/ontology_lookup.db

# View database creation and last-updated metadata:
uv run ontology-lookup database-info --db .db/ontology_lookup.db
```

---

### Python API: Build and Database Management

The `OntologyDatabaseManager` ([src/ontology_lookup/manager.py](src/ontology_lookup/manager.py)) class provides programmatic control over database lifecycle operations.

#### 1. Create Database (API)

```python
from ontology_lookup.manager import OntologyDatabaseManager, create_database

# Option A: Using the OntologyDatabaseManager class
mgr = OntologyDatabaseManager(
    db_path=".db/ontology_lookup.db",
    default_input_dir=".cache/ontology_jsons",
    batch_size=5000,
)

# Load selected ontologies (or pass ontologies=None to load all available JSONs)
results = mgr.create_database(
    source_dir=".cache/ontology_jsons",
    ontologies=["ms", "edam", "obi"],
    clean=True,
)
print("Ingestion results:", results)
# Output: {'ms': {'terms': 3542, 'details': 15230}, 'edam': {'terms': 3820, 'details': 12400}, ...}

# Option B: Using the module-level helper function
results = create_database(
    db_path=".db/ontology_lookup.db",
    source_dir=".cache/ontology_jsons",
    ontologies=["ms", "edam"],
    clean=True,
)
```

#### 2. Add Ontologies (API)

**Python API**:

```python
from ontology_lookup.manager import OntologyDatabaseManager

mgr = OntologyDatabaseManager(db_path=".db/ontology_lookup.db")

# Ingest or update 'chmo' from default source directory:
terms_count, details_count = mgr.update_ontology(
    ontology_name="chmo",
    source_dir=".cache/ontology_jsons",
)
print(f"Added CHMO: {terms_count} terms, {details_count} details.")

# Ingest from an explicit file path:
terms, details = mgr.load_file(
    file_path=".cache/ontology_jsons/bto.json",
    ontology_name="bto",
)
```

**REST API**:

```bash
# Add or refresh 'chmo' from default source directory
curl -X POST "http://localhost:8000/ontologies/chmo"

# Add or update from specific source directory
curl -X PUT "http://localhost:8000/ontologies/bto?source_dir=.cache/ontology_jsons"
```

#### 3. Delete Ontologies (API)

**Python API**:

```python
from ontology_lookup.manager import OntologyDatabaseManager

mgr = OntologyDatabaseManager(db_path=".db/ontology_lookup.db")

# Completely remove an ontology and all its associated terms, details, and FTS entries:
removed_terms = mgr.delete_ontology("obi")
print(f"Removed {removed_terms} terms from the database.")
```

**REST API**:

```bash
# Delete ontology 'obi' and all its terms/details
curl -X DELETE "http://localhost:8000/ontologies/obi"
```

#### 4. Add/Update/Delete Term Tags (API)

**Python API**:

```python
from ontology_lookup.manager import OntologyDatabaseManager

mgr = OntologyDatabaseManager(db_path=".db/ontology_lookup.db")

# 1. Add custom tag to term (optionally scoped to a specific ontology)
success = mgr.add_term_tag(
    curie="MS:1000031",
    tag_key="curation_status",
    tag_value="verified",
    ontology="ms",
)

# 2. Update tag value
mgr.update_term_tag(
    curie="MS:1000031",
    tag_key="curation_status",
    new_value="approved",
    old_value="verified",
    ontology="ms",
)

# 3. Delete tag
mgr.delete_term_tag(
    curie="MS:1000031",
    tag_key="curation_status",
    tag_value="approved",
    ontology="ms",
)
```

**REST API**:

```bash
# 1. Add tag to term (accepts optional ?ontology=ms query parameter)
curl -X POST "http://localhost:8000/terms/MS:1000031/tags?ontology=ms" \
  -H "Content-Type: application/json" \
  -d '{"tag_key": "curation_status", "tag_value": "verified"}'

# 2. Update existing tag value
curl -X PUT "http://localhost:8000/terms/MS:1000031/tags/curation_status?ontology=ms" \
  -H "Content-Type: application/json" \
  -d '{"tag_value": "approved", "old_value": "verified"}'

# 3. Delete specific tag value
curl -X DELETE "http://localhost:8000/terms/MS:1000031/tags/curation_status?tag_value=approved&ontology=ms"

# 4. Delete all tags matching tag_key for the term
curl -X DELETE "http://localhost:8000/terms/MS:1000031/tags/curation_status"
```

#### 5. Inspect Database and Ontologies (API)

**Python API**:

```python
from ontology_lookup.manager import OntologyDatabaseManager

mgr = OntologyDatabaseManager(db_path=".db/ontology_lookup.db")

# List installed ontologies and summary counts
installed = mgr.list_installed_ontologies()
for ont in installed:
    print(f"{ont['ontology']} (v{ont['version']}): {ont['num_of_terms']} terms")

# Inspect database timestamp info
info = mgr.get_database_info()
print(f"Created: {info['created_time']}, Updated: {info['updated_time']}")
```

**REST API**:

```bash
# List all installed ontologies
curl -X GET "http://localhost:8000/ontologies"

# Get metadata for a specific ontology
curl -X GET "http://localhost:8000/ontologies/ms"

# Inspect database metadata
curl -X GET "http://localhost:8000/info"
```

---

## Search on Multi-ontology Lookup Database

The query layer supports **8 primary use cases** available via both the **CLI** and **API** (Python service + REST HTTP endpoints).
The `search` and `freetext-search` use cases accept multiple ontology filters: pass comma-separated names in the CLI, a list in Python, or repeat the `ontology` query parameter in HTTP requests.

### Starting the REST API Server

To enable HTTP access to your database, start the FastAPI server:

```bash
uv run ontology-lookup serve --db .db/ontology_lookup.db --host 0.0.0.0 --port 8000
```

Interactive OpenAPI documentation and Swagger UI will be available at:

- **Swagger UI**: `http://localhost:8000/docs`
- **ReDoc UI**: `http://localhost:8000/redoc`

### API Endpoints Reference Table

| Endpoint | HTTP Method | Query / Path / Body Parameters | Description |
| --- | --- | --- | --- |
| `/terms/{accession}` | `GET` | `accession` (path), `ontology` (opt) | Lookup term by CURIE or full IRI (case-insensitive) |
| `/terms/{accession}/tags` | `POST` | `accession` (path), body `{"tag_key", "tag_value"}` | Add a key-value tag to a term |
| `/terms/{accession}/tags/{tag_key}` | `PUT` | `accession` (path), `tag_key` (path), body `{"tag_value", "old_value"}` | Update a tag value for a term |
| `/terms/{accession}/tags/{tag_key}` | `DELETE` | `accession` (path), `tag_key` (path), `tag_value` (opt query) | Delete one or all tags matching key for a term |
| `/ontologies/{ontology}/terms/{accession}` | `GET` | `ontology` (path), `accession` (path) | Scoped lookup within explicit ontology |
| `/search/exact` | `GET` | `ontology`, `label` | Exact match lookup on term label |
| `/search` | `GET` | `q`, `search_in_synonyms`, repeatable `ontology` and `parent_curie`, `limit`, `offset` | Case-insensitive exact match on labels or synonyms |
| `/search/freetext` | `GET` | `q`, repeatable `ontology` and `parent_curie`, `limit`, `offset` | Ranked freetext search across labels, synonyms, and definitions |
| `/search/tags` | `GET` | `tag_key`, `tag_value`, `ontology` (opt), `limit`, `offset` | Search terms by metadata tags (e.g. `is_leaf`, `obsolete`) |
| `/resolve/curie` | `GET` | `iri`, `ontology` (opt) | Resolve a full IRI back to its primary CURIE |
| `/resolve/iri` | `GET` | `curie`, `ontology` (opt) | Resolve a CURIE to its IRI |
| `/children/{parent_curie}` | `GET` | `parent_curie` (path), `ontology` (opt), `limit`, `offset` | Get all recursive descendants of a parent CURIE |
| `/parent-terms` | `GET` | *None* | List parent CURIEs referenced by indexed `child-of` tags |
| `/ontologies` | `GET` | *None* | List installed ontologies with version, terms, obsoletes, and details |
| `/ontologies/{ontology}` | `GET` | `ontology` (path) | Get metadata for a specific installed ontology |
| `/ontologies/{ontology}` | `POST` / `PUT` | `ontology` (path), `source_dir` (opt), `source_file` (opt) | Add or update an ontology from source JSON |
| `/ontologies/{ontology}` | `DELETE` | `ontology` (path) | Delete an ontology, its terms, hierarchy links, and FTS entries |
| `/info` | `GET` | *None* | Retrieve database creation and last-updated metadata |
| `/health` | `GET` | *None* | Service health status and total terms count |

---

### Use Case 1: Term Lookup by CURIE or IRI (Accession)

Fetch the complete record for a term, including aggregated synonyms, hierarchy links (`children_of`), and metadata tags. Case-insensitive.

> [!NOTE]
> **Cross-Ontology Term Referencing**: When a term is referenced or imported by multiple ontologies (e.g. `OBI:0200114` is referenced in `ms` and defined in `obi`), specify `--ontology` to fetch the exact representation for that ontology.

#### CLI Examples

```bash
# Example 1: Lookup by CURIE
uv run ontology-lookup get-term-by-accession --ontology ms --accession MS:1000031 --db .db/ontology_lookup.db

# Example 2: Lookup by lowercase CURIE (case-insensitive)
uv run ontology-lookup get-term-by-accession --ontology ms --accession ms:1000449 --db .db/ontology_lookup.db

# Example 3: Lookup cross-referenced term in distinct ontologies
uv run ontology-lookup get-term-by-accession --ontology ms --accession OBI:0200114 --db .db/ontology_lookup.db
uv run ontology-lookup get-term-by-accession --ontology obi --accession OBI:0200114 --db .db/ontology_lookup.db
```

#### API Examples

Service methods use a reusable read-only default connection. The service creates it
when initialized and validates it before use. Close the service when you are finished.
Use `get_default_connection()` for direct access, or pass your own connection to a
use-case method. A supplied connection stays open after the method returns:

```python
from ontology_lookup.service import OntologyLookupService

svc = OntologyLookupService(db_path=".db/ontology_lookup.db")
conn = svc.get_default_connection()
row = conn.execute("SELECT COUNT(*) FROM terms").fetchone()
print(row[0])
term = svc.get_term_by_accession("ms", "MS:1000031", connection=conn)
svc.close()
```

Service use-case methods can be called directly. Close the service after use:

```python
from ontology_lookup.service import OntologyLookupService

svc = OntologyLookupService(db_path=".db/ontology_lookup.db")
# Example 1: Lookup by CURIE
term1 = svc.get_term_by_accession(ontology="ms", accession="MS:1000031")
print(term1.label, term1.synonyms, term1.children_of)

# Example 2: Lookup by lowercase CURIE
term2 = svc.get_term_by_accession(ontology="ms", accession="ms:1000449")
print(term2.curie, term2.label)

# Example 3: Cross-referenced term lookup across ontologies
term_ms = svc.get_term_by_accession(ontology="ms", accession="OBI:0200114")
term_obi = svc.get_term_by_accession(ontology="obi", accession="OBI:0200114")
print(f"MS:  {term_ms.curie} -> {term_ms.label}")
print(f"OBI: {term_obi.curie} -> {term_obi.label} (tags: {list(term_obi.tags.keys())})")
svc.close()
```

**REST API (HTTP)**:

```bash
# Example 1: Lookup by CURIE
curl -X GET "http://localhost:8000/terms/MS:1000031"

# Example 2: Lookup by lowercase CURIE with explicit ontology
curl -X GET "http://localhost:8000/ontologies/ms/terms/ms:1000449"

# Example 3: Cross-referenced term lookup in distinct ontologies
curl -X GET "http://localhost:8000/ontologies/ms/terms/OBI:0200114"
curl -X GET "http://localhost:8000/ontologies/obi/terms/OBI:0200114"
```

---

### Use Case 2: Exact Label Match

Look up a term by exact label within an ontology (case-insensitive).

#### CLI Examples

```bash
# Example 1: Exact instrument label (matches MS:1000031)
uv run ontology-lookup get-term-by-label --ontology ms --label "instrument model" --db .db/ontology_lookup.db

# Example 2: Case-insensitive label query (matches MS:1000449)
uv run ontology-lookup get-term-by-label --ontology ms --label "LTQ ORBITRAP" --db .db/ontology_lookup.db

# Example 3: Exact match in another ontology (matches OBI:0000070)
uv run ontology-lookup get-term-by-label --ontology obi --label "assay" --db .db/ontology_lookup.db
```

#### API Examples

```python
from ontology_lookup.service import OntologyLookupService

svc = OntologyLookupService(db_path=".db/ontology_lookup.db")
# Example 1: Exact label
term1 = svc.get_term_by_exact_label(ontology="ms", label="instrument model")
print(term1.curie, term1.label)

# Example 2: Case-insensitive label
term2 = svc.get_term_by_exact_label(ontology="ms", label="LTQ ORBITRAP")
print(term2.curie, term2.label)

# Example 3: Match in OBI
term3 = svc.get_term_by_exact_label(ontology="obi", label="assay")
print(term3.curie if term3 else "Not found")
svc.close()
```

**REST API (HTTP)**:

```bash
# Example 1: Exact label
curl -X GET "http://localhost:8000/search/exact?ontology=ms&label=instrument+model"

# Example 2: Case-insensitive uppercase label
curl -X GET "http://localhost:8000/search/exact?ontology=ms&label=LTQ+ORBITRAP"

# Example 3: Biological assay in OBI
curl -X GET "http://localhost:8000/search/exact?ontology=obi&label=assay"
```

---

### Use Case 3: Exact Match Search across Labels and Synonyms

Case-insensitive exact matching against complete labels and synonyms. Partial matches and definitions are excluded. Use `freetext-search` (Use Case 8) for ranked keyword and definition search. Optional ontology and parent CURIE filters can narrow the results.

#### CLI Examples

```bash
# Example 1: Match a complete label
uv run ontology-lookup search-by-label --label-or-synonym "orbitrap" -o ms --db .db/ontology_lookup.db

# Example 2: Query by synonym (e.g. 'MALDI' matches MS:1000031)
uv run ontology-lookup search-by-label --label-or-synonym "MALDI" -o ms --db .db/ontology_lookup.db

# Example 3: Exact label or synonym across multiple ontologies
uv run ontology-lookup search-by-label --label-or-synonym "instrument" -o ms,edam -l 10 --db .db/ontology_lookup.db
```

#### API Examples

```python
from ontology_lookup.service import OntologyLookupService

svc = OntologyLookupService(db_path=".db/ontology_lookup.db")
# Example 1: Search label
results1 = svc.search_by_label(label_or_synonym="orbitrap", ontology="ms", limit=5)
for r in results1:
    print(f"[{r.curie}] {r.label}")

# Example 2: Search synonym
results2 = svc.search_by_label(label_or_synonym="MALDI", ontology="ms")
for r in results2:
    print(f"[{r.curie}] {r.label}")

# Example 3: Exact match across multiple ontologies
results3 = svc.search_by_label(label_or_synonym="instrument", ontology=["ms", "edam"], limit=10)
print(f"Found {len(results3)} results")
svc.close()
```

**REST API (HTTP)**:

```bash
# Example 1: Query label
curl -X GET "http://localhost:8000/search?q=orbitrap&ontology=ms"

# Example 2: Query synonym
curl -X GET "http://localhost:8000/search?q=MALDI&ontology=ms"

# Example 3: Multi-ontology search with limit
curl -X GET "http://localhost:8000/search?q=instrument&ontology=ms&ontology=edam&limit=10"
```

---

### Use Case 4: Hierarchy-Constrained Search (Parent CURIE Filter)

Restrict exact label or synonym matches to descendants of specific parent terms using indexed hierarchy tags.

#### CLI Examples

```bash
# Example 1: Search only descendants of 'instrument model' (MS:1000031)
uv run ontology-lookup search-by-label --label-or-synonym "LTQ Orbitrap" -o ms -p MS:1000031 --db .db/ontology_lookup.db

# Example 2: Search within 'ionization type' (MS:1000008)
uv run ontology-lookup search-by-label --label-or-synonym "electrospray ionization" -o ms -p MS:1000008 --db .db/ontology_lookup.db

# Example 3: Filter across multiple parent CURIEs
uv run ontology-lookup search-by-label --label-or-synonym "MALDI" -o ms -p MS:1000031,MS:1000008 --db .db/ontology_lookup.db
```

#### API Examples

```python
from ontology_lookup.service import OntologyLookupService

svc = OntologyLookupService(db_path=".db/ontology_lookup.db")
# Example 1: Restrict search to instrument models
res1 = svc.search_by_label(
    label_or_synonym="LTQ Orbitrap", ontology="ms", parent_curie="MS:1000031"
)
print(f"Found {len(res1)} matches under MS:1000031")

# Example 2: Search within ionization type hierarchy
res2 = svc.search_by_label(
    label_or_synonym="electrospray ionization", ontology="ms", parent_curie="MS:1000008"
)
for r in res2:
    print(r.curie, r.label)

# Example 3: Filter across multiple parent CURIEs
res3 = svc.search_by_label(
    label_or_synonym="MALDI",
    ontology=["ms"],
    parent_curie=["MS:1000031", "MS:1000008"],
)
print(f"Multi-parent matches: {len(res3)}")
svc.close()
```

**REST API (HTTP)**:

```bash
# Example 1: Parent filter under instrument model
curl -X GET "http://localhost:8000/search?q=LTQ+Orbitrap&ontology=ms&parent_curie=MS:1000031"

# Example 2: Hierarchy filter under ionization type
curl -X GET "http://localhost:8000/search?q=electrospray+ionization&ontology=ms&parent_curie=MS:1000008"

# Example 3: Multiple parent CURIE filters
curl -X GET "http://localhost:8000/search?q=MALDI&ontology=ms&parent_curie=MS:1000031&parent_curie=MS:1000008"
```

---

### List Indexed Parent Terms

List the distinct parent CURIEs referenced by indexed `child-of` hierarchy tags:

```bash
uv run ontology-lookup list-parent-terms --db .db/ontology_lookup.db
```

The same use case is available from the Python service and REST API:

```python
from ontology_lookup.service import OntologyLookupService

svc = OntologyLookupService(db_path=".db/ontology_lookup.db")
for parent_curie in svc.get_indexed_parent_terms():
    print(parent_curie)
svc.close()
```

```bash
curl -X GET "http://localhost:8000/parent-terms"
```

---

### Use Case 5: CURIE and IRI Resolution

Map bidirectional relationships between persistent IRIs and compact CURIE accessions.

#### CLI Examples

```bash
# Example 1: Resolve full IRI to CURIE
uv run ontology-lookup iri-to-curie --iri "http://purl.obolibrary.org/obo/MS_1000031" --ontology ms --db .db/ontology_lookup.db

# Example 2: Resolve CURIE to IRI
uv run ontology-lookup curie-to-iri --curie "MS:1000031" --ontology ms --db .db/ontology_lookup.db

# Example 3: Resolve IRI across entire database (unscoped)
uv run ontology-lookup iri-to-curie --iri "http://edamontology.org/format_1915" --db .db/ontology_lookup.db
```

#### API Examples

```python
from ontology_lookup.service import OntologyLookupService

svc = OntologyLookupService(db_path=".db/ontology_lookup.db")
# Example 1: IRI to CURIE
res1 = svc.find_curie(iri="http://purl.obolibrary.org/obo/MS_1000031", ontology="ms")
print(f"CURIE: {res1.curie if res1 else 'Not found'}")

# Example 2: CURIE to IRI
res2 = svc.find_iri(curie="MS:1000031", ontology="ms")
print(f"IRI: {res2.iri if res2 else 'Not found'}")

# Example 3: Unscoped IRI resolution
res3 = svc.find_curie(iri="http://edamontology.org/format_1915")
print(f"Unscoped CURIE: {res3.curie if res3 else 'Not found'}")
svc.close()
```

**REST API (HTTP)**:

```bash
# Example 1: Resolve full IRI to CURIE
curl -X GET "http://localhost:8000/resolve/curie?iri=http%3A%2F%2Fpurl.obolibrary.org%2Fobo%2FMS_1000031&ontology=ms"

# Example 2: Resolve CURIE to full IRI
curl -X GET "http://localhost:8000/resolve/iri?curie=MS:1000031&ontology=ms"

# Example 3: Resolve IRI across entire database (unscoped)
curl -X GET "http://localhost:8000/resolve/curie?iri=http%3A%2F%2Fedamontology.org%2Fformat_1915"
```

---

### Use Case 6: Key-Value Metadata Tag Search

Query terms by normalized tags such as `is_leaf`, `obsolete`, or custom annotations.

#### CLI Examples

```bash
# Example 1: Find obsolete terms in an ontology
uv run ontology-lookup search-by-tag --tag-key obsolete --tag-value true -o ms --db .db/ontology_lookup.db

# Example 2: Find leaf terms (terms without child terms)
uv run ontology-lookup search-by-tag --tag-key is_leaf --tag-value 1 -o ms -l 10 --db .db/ontology_lookup.db

# Example 3: Search terms by custom tag key and value
uv run ontology-lookup search-by-tag --tag-key custom_category --tag-value instrumentation --db .db/ontology_lookup.db
```

#### API Examples

```python
from ontology_lookup.service import OntologyLookupService

svc = OntologyLookupService(db_path=".db/ontology_lookup.db")
# Example 1: Search obsolete terms
obsoletes = svc.search_by_tag(tag_key="obsolete", tag_value="true", ontology="ms")
print(f"Found {len(obsoletes)} obsolete terms in MS")

# Example 2: Search leaf terms
leaves = svc.search_by_tag(tag_key="is_leaf", tag_value="1", ontology="ms", limit=5)
for leaf in leaves:
    print(f"Leaf term: {leaf.curie} - {leaf.label}")

# Example 3: Search by custom tag
custom_terms = svc.search_by_tag(tag_key="custom_category", tag_value="instrumentation")
print(f"Tagged terms count: {len(custom_terms)}")
svc.close()
```

**REST API (HTTP)**:

```bash
# Example 1: Find obsolete terms
curl -X GET "http://localhost:8000/search/tags?tag_key=obsolete&tag_value=true&ontology=ms"

# Example 2: Find leaf nodes
curl -X GET "http://localhost:8000/search/tags?tag_key=is_leaf&tag_value=1&ontology=ms&limit=10"

# Example 3: Query custom user metadata tags
curl -X GET "http://localhost:8000/search/tags?tag_key=custom_category&tag_value=instrumentation"
```

---

### Use Case 7: Recursive Descendants / Children Lookup

Retrieve all terms that inherit from a specified parent CURIE.

#### CLI Examples

```bash
# Example 1: Find descendants of instrument model (MS:1000031)
uv run ontology-lookup get-children --parent MS:1000031 -o ms --db .db/ontology_lookup.db

# Example 2: Find descendants of ionization type (MS:1000008)
uv run ontology-lookup get-children --parent MS:1000008 -o ms --db .db/ontology_lookup.db

# Example 3: Paginated descendants query with limit and offset
uv run ontology-lookup get-children --parent MS:1000031 -o ms --limit 5 --offset 0 --db .db/ontology_lookup.db
```

#### API Examples

```python
from ontology_lookup.service import OntologyLookupService

svc = OntologyLookupService(db_path=".db/ontology_lookup.db")
# Example 1: Children of instrument model
children1 = svc.get_children(parent_curie="MS:1000031", ontology="ms")
print(f"Children of MS:1000031: {len(children1)}")

# Example 2: Children of ionization type
children2 = svc.get_children(parent_curie="MS:1000008", ontology="ms")
for child in children2:
    print(f" -> {child.curie}: {child.label}")

# Example 3: Paginated children lookup
children3 = svc.get_children(parent_curie="MS:1000031", ontology="ms", limit=5, offset=5)
print(f"Page 2 count: {len(children3)}")
svc.close()
```

**REST API (HTTP)**:

```bash
# Example 1: Recursive children of instrument model
curl -X GET "http://localhost:8000/children/MS:1000031?ontology=ms"

# Example 2: Recursive children of ionization type
curl -X GET "http://localhost:8000/children/MS:1000008?ontology=ms"

# Example 3: Paginated children
curl -X GET "http://localhost:8000/children/MS:1000031?ontology=ms&limit=5&offset=5"
```

---

### Use Case 8: Freetext Search (Labels, Synonyms, and Definitions)

Perform ranked search across term labels, synonyms, and detailed text definitions using SQLite FTS5 BM25-based ranking.

#### CLI Examples

```bash
# Example 1: Match definition text (e.g. manufacturer name)
uv run ontology-lookup freetext-search -q "Thermo Scientific" -o ms --db .db/ontology_lookup.db

# Example 2: Search specific hybrid spectrometer types
uv run ontology-lookup freetext-search -q "orbitrap mass spectrometer" -o ms -l 5 --db .db/ontology_lookup.db

# Example 3: Search multiple ontologies with a parent constraint
uv run ontology-lookup freetext-search -q "quadrupole" -o ms,edam -p MS:1000031 --db .db/ontology_lookup.db
```

#### API Examples

```python
from ontology_lookup.service import OntologyLookupService

svc = OntologyLookupService(db_path=".db/ontology_lookup.db")
# Example 1: Search definition text
results1 = svc.freetext_search(query="Thermo Scientific", ontology="ms")
for r in results1:
    print(f"[{r.curie}] {r.label} (Rank: {r.rank:.2f})")

# Example 2: Multi-keyword ranked search
results2 = svc.freetext_search(query="orbitrap mass spectrometer", ontology=["ms"], limit=5)
for r in results2:
    print(f"[{r.curie}] {r.label}")

# Example 3: Multi-ontology freetext search with parent filter
results3 = svc.freetext_search(
    query="quadrupole",
    ontology=["ms", "edam"],
    parent_curie=["MS:1000031"],
)
print(f"Matches found: {len(results3)}")
svc.close()
```

**REST API (HTTP)**:

```bash
# Example 1: Search definition text
curl -X GET "http://localhost:8000/search/freetext?q=Thermo+Scientific&ontology=ms"

# Example 2: Multi-keyword ranked search
curl -X GET "http://localhost:8000/search/freetext?q=orbitrap+mass+spectrometer&ontology=ms&limit=5"

# Example 3: Multi-ontology freetext query with parent filter
curl -X GET "http://localhost:8000/search/freetext?q=quadrupole&ontology=ms&ontology=edam&parent_curie=MS:1000031"
```
