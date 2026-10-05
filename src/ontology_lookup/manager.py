import logging
import sqlite3
from pathlib import Path
from typing import Any

from ontology_lookup.config import DEFAULT_DATABASE_CONFIG, DatabaseCreationConfig
from ontology_lookup.db import enable_wal_mode, get_write_connection
from ontology_lookup.loader import JsonOntologyLoader
from ontology_lookup.schema import initialize_schema

logger = logging.getLogger(__name__)

DEFAULT_INPUT_DIR = str(DEFAULT_DATABASE_CONFIG.source_directory)


DEFAULT_PARENT_TERMS = (
    "MS:1000031",
    "MS:1000076",
    "MS:1000008",
    "MS:1003761",
    "MS:1000007",
    "MS:1003737",
    "MS:1002270",
    "MS:1003776",
    "EDAM:data_2894",
    "EDAM:format_1915",
    "MS:1001459",
)


class OntologyDatabaseManager:
    """Manages the creation, lifecycle, and updates of a multi-ontology SQLite database

    sourced from an input directory containing JSON files named by ontology short name.
    """

    def __init__(
        self,
        db_path: None | str | Path = None,
        default_input_dir: None | str | Path = None,
        target_parents: None | list[str] = None,
        batch_size: None | int = None,
        config: None | DatabaseCreationConfig = None,
    ) -> None:
        config = config or DEFAULT_DATABASE_CONFIG
        self.db_path_str = str(db_path if db_path is not None else config.database_path)
        self.db_file_path = Path(self.db_path_str)
        self.default_input_dir = Path(
            default_input_dir if default_input_dir is not None else config.source_directory
        )
        self.target_parents: list[str] = (
            list(target_parents) if target_parents is not None else list(config.parent_terms)
        )
        self.batch_size = batch_size if batch_size is not None else config.batch_size

    def list_available_ontologies(
        self,
        source_dir: None | str | Path = None,
    ) -> list[dict[str, Any]]:
        """List all available ontology JSON files in the input directory."""
        target_dir = Path(source_dir) if source_dir is not None else self.default_input_dir
        if not target_dir.exists() or not target_dir.is_dir():
            return []

        available: list[dict[str, Any]] = []
        for p in sorted(target_dir.glob("*.json")):
            short_name = p.stem.lower()
            size_mb = p.stat().st_size / (1024 * 1024)
            available.append(
                {
                    "ontology": short_name,
                    "filename": p.name,
                    "path": str(p),
                    "size_mb": round(size_mb, 2),
                }
            )
        return available

    def list_installed_ontologies(self) -> list[dict[str, Any]]:
        """Query the SQLite database for currently installed ontologies and term counts."""
        if not self.db_file_path.exists():
            return []

        conn = sqlite3.connect(self.db_path_str)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        try:
            # Query ontologies table directly (instant lookup without
            # scanning terms/details)
            query = """
            SELECT
                ontology,
                COALESCE(name, '') as name,
                COALESCE(description, '') as description,
                COALESCE(source_uri, '') as source_uri,
                COALESCE(prefix, UPPER(ontology)) as prefix,
                COALESCE(version, '') as version,
                COALESCE(num_of_terms, 0) as num_of_terms,
                COALESCE(num_of_obsoletes, 0) as num_of_obsoletes,
                COALESCE(num_of_details, 0) as num_of_details,
                COALESCE(iri_prefix, '') as iri_prefix
            FROM ontologies
            WHERE num_of_terms > 0
            ORDER BY ontology
            """
            rows = cur.execute(query).fetchall()

            # If ontologies table is empty, fallback to terms table
            if not rows:
                query_fallback = """
                SELECT
                    ontology,
                    '' as name,
                    '' as description,
                    '' as source_uri,
                    UPPER(ontology) as prefix,
                    '' as version,
                    COUNT(curie) as num_of_terms,
                    0 as num_of_obsoletes,
                    0 as num_of_details,
                    '' as iri_prefix
                FROM terms
                GROUP BY ontology
                ORDER BY ontology
                """
                rows = cur.execute(query_fallback).fetchall()

            results: list[dict[str, Any]] = []
            for r in rows:
                num_terms = r["num_of_terms"]
                d_count = r["num_of_details"]

                results.append(
                    {
                        "ontology": r["ontology"],
                        "name": r["name"],
                        "description": r["description"],
                        "source_uri": r["source_uri"],
                        "prefix": r["prefix"],
                        "version": r["version"],
                        "term_count": num_terms,
                        "num_of_terms": num_terms,
                        "num_of_obsoletes": r["num_of_obsoletes"],
                        "num_of_details": d_count,
                        "detail_count": d_count,
                        "iri_prefix": r["iri_prefix"],
                    }
                )
            return results
        finally:
            conn.close()

    def get_database_info(self) -> dict[str, Any]:
        """Retrieve database metadata including creation, last updated, and creators."""
        if not self.db_file_path.exists():
            return {}
        conn = sqlite3.connect(self.db_path_str)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        try:
            cols = [r[1] for r in cur.execute("PRAGMA table_info(database_info)").fetchall()]
            created_by_col = "created_by" if "created_by" in cols else "NULL as created_by"
            updated_by_col = "updated_by" if "updated_by" in cols else "NULL as updated_by"
            row = cur.execute(
                f"SELECT created_time, updated_time, "
                f"{created_by_col}, {updated_by_col} "
                "FROM database_info LIMIT 1"
            ).fetchone()
            if row:
                return {
                    "created_time": row["created_time"],
                    "updated_time": row["updated_time"],
                    "created_by": row["created_by"],
                    "updated_by": row["updated_by"],
                }
            return {}
        except Exception as ex:  # noqa: BLE001
            logger.warning("Error: %s", ex)
            return {}
        finally:
            conn.close()

    def load_directory(
        self,
        source_dir: None | str | Path = None,
        ontologies: None | list[str] = None,
        target_parents: None | list[str] = None,
        clean: bool = False,
        skip_empty: bool = True,
    ) -> dict[str, dict[str, int]]:
        """Process JSON files in the source directory and ingest into SQLite database.

        JSON file names correspond to ontology short names (e.g. ms.json -> 'ms').

        Args:
            source_dir: Directory containing ontology JSON files
             (default: DEFAULT_INPUT_DIR).
            ontologies: Optional list of ontology short names to process. If omitted,
                        all *.json files in the directory are processed.
            target_parents: Optional list of target parent CURIEs for recursive
                child-of links.
            clean: If True, deletes and re-creates the database from scratch.
            skip_empty: If True, files that result in 0 terms are skipped in
                the return results.

        Returns:
            Dictionary mapping ontology short name -> {'terms': count, 'details': count}.
        """
        target_dir = Path(source_dir) if source_dir is not None else self.default_input_dir
        if not target_dir.exists() or not target_dir.is_dir():
            logger.error("Input ontology directory not found: %s", target_dir)
            raise FileNotFoundError(f"Input ontology directory not found: {target_dir}")

        if clean and self.db_file_path.exists():
            logger.info("Cleaning existing database at '%s'", self.db_path_str)
            self.db_file_path.unlink(missing_ok=True)

        conn = get_write_connection(self.db_path_str)
        initialize_schema(conn)
        conn.close()

        # Find candidate json files
        all_json_files = {p.stem.lower(): p for p in sorted(target_dir.glob("*.json"))}

        target_names: list[str]
        if ontologies:
            target_names = [o.strip().lower() for o in ontologies if o.strip()]
        else:
            target_names = list(all_json_files.keys())

        logger.info(
            "Found %d JSON files in '%s'. Target ontologies to process (%d): %s",
            len(all_json_files),
            target_dir,
            len(target_names),
            ", ".join(target_names),
        )

        results: dict[str, dict[str, int]] = {}
        active_parents = target_parents if target_parents is not None else self.target_parents
        loader = JsonOntologyLoader(
            db_path=self.db_path_str,
            target_parents=active_parents,
            batch_size=self.batch_size,
        )

        for idx, name in enumerate(target_names, 1):
            if name not in all_json_files:
                logger.warning(
                    "[%d/%d][%s] Ontology JSON file '%s.json' not found in %s. Skipping.",
                    idx,
                    len(target_names),
                    name,
                    name,
                    target_dir,
                )
                print(f"[{name}] File '{name}.json' not found in {target_dir}. Skipping.")
                continue

            json_path = all_json_files[name]
            size_mb = json_path.stat().st_size / (1024 * 1024)
            logger.info(
                "[%d/%d][%s] Start loading ontology from '%s' (%.2f MB)...",
                idx,
                len(target_names),
                name,
                json_path.name,
                size_mb,
            )
            print(f"[{name}] Ingesting from {json_path.name} ({size_mb:.2f} MB)...")

            terms_cnt, details_cnt = loader.load_file(
                source=json_path,
                ontology_name=name,
                target_parents=active_parents,
                delete_existing=not clean,
            )

            if skip_empty and terms_cnt == 0:
                # Remove empty ontology record from ontologies table
                conn_clean = get_write_connection(self.db_path_str)
                try:
                    conn_clean.execute("DELETE FROM ontologies WHERE ontology = ?", (name,))
                    conn_clean.commit()
                finally:
                    conn_clean.close()

                logger.warning(
                    "[%d/%d][%s] No classes found in '%s' (empty ontology). Skipping.",
                    idx,
                    len(target_names),
                    name,
                    json_path.name,
                )
                print(f"[{name}] No classes found (empty ontology).")
                continue

            results[name] = {"terms": terms_cnt, "details": details_cnt}
            logger.info(
                "[%d/%d][%s] Load result: %d terms and %d details successfully ingested.",
                idx,
                len(target_names),
                name,
                terms_cnt,
                details_cnt,
            )
            print(f"[{name}] Ingested {terms_cnt} terms and {details_cnt} details.")
        self.optimize(vacuum=not clean)
        total_terms = sum(r["terms"] for r in results.values())
        total_details = sum(r["details"] for r in results.values())
        logger.info(
            "Batch loading completed: %d ontologies (%d terms, %d details) loaded into '%s'.",
            len(results),
            total_terms,
            total_details,
            self.db_path_str,
        )

        return results

    def optimize(self, vacuum: bool = True) -> None:
        """Update SQLite planner statistics and optionally reclaim unused pages."""
        logger.info("Optimizing database '%s' (enabling WAL mode)...", self.db_path_str)
        enable_wal_mode(self.db_path_str)
        vac_conn = sqlite3.connect(self.db_path_str)
        try:
            logger.info("Optimizing database '%s' optimize.", self.db_path_str)

            vac_conn.execute("PRAGMA optimize;")
            if vacuum:
                logger.info("Vacuuming database '%s'.", self.db_path_str)
                vac_conn.execute("VACUUM;")
            else:
                logger.info("Skipping VACUUM for clean database build.")
        finally:
            vac_conn.close()

    def load_file(
        self,
        file_path: None | str,
        Path,
        ontology_name: None | str = None,
        target_parents: None | list[str] = None,
        delete_existing: bool = True,
    ) -> tuple[int, int]:
        """Ingest a single ontology JSON file into the database."""
        ont_label = ontology_name or Path(file_path).stem.lower()
        logger.info("[%s] Ingesting single ontology file '%s'...", ont_label, file_path)
        active_parents = target_parents if target_parents is not None else self.target_parents
        loader = JsonOntologyLoader(
            db_path=self.db_path_str,
            target_parents=active_parents,
            batch_size=self.batch_size,
        )
        terms_cnt, details_cnt = loader.load_file(
            source=file_path,
            ontology_name=ontology_name,
            target_parents=active_parents,
            delete_existing=delete_existing,
        )
        logger.info(
            "[%s] File ingestion result: %d terms and %d details ingested.",
            ont_label,
            terms_cnt,
            details_cnt,
        )
        return terms_cnt, details_cnt

    def update_ontology(
        self,
        ontology_name: str,
        source_dir: None | str | Path = None,
        file_path: None | str | Path = None,
        target_parents: None | list[str] = None,
    ) -> tuple[int, int]:
        """Update a single ontology from either an explicit file path or by finding

        <ontology_name>.json in the source directory.
        """
        clean_name = ontology_name.lower().strip()
        final_file: Path

        if file_path is not None:
            final_file = Path(file_path).resolve()
        else:
            target_dir = Path(source_dir) if source_dir is not None else self.default_input_dir
            final_file = target_dir / f"{clean_name}.json"

        if not final_file.exists():
            logger.error("[%s] Ontology file not found: %s", clean_name, final_file)
            raise FileNotFoundError(f"Ontology file not found: {final_file}")

        logger.info("[%s] Start updating ontology from '%s'...", clean_name, final_file)
        print(f"[{clean_name}] Updating from {final_file}...")
        active_parents = target_parents if target_parents is not None else self.target_parents
        loader = JsonOntologyLoader(
            db_path=self.db_path_str,
            target_parents=active_parents,
            batch_size=self.batch_size,
        )
        terms_cnt, details_cnt = loader.load_file(
            source=final_file,
            ontology_name=clean_name,
            target_parents=active_parents,
            delete_existing=True,
        )
        logger.info(
            "[%s] Update result: %d terms and %d details successfully updated.",
            clean_name,
            terms_cnt,
            details_cnt,
        )
        print(f"[{clean_name}] Successfully updated: {terms_cnt} terms, {details_cnt} details.")
        return terms_cnt, details_cnt

    def delete_ontology(self, ontology_name: str) -> int:
        """Completely remove an ontology, its details, and its full-text search entries."""
        clean_key = ontology_name.lower().strip()
        if not self.db_file_path.exists():
            logger.warning(
                "Database '%s' does not exist; cannot delete ontology.", self.db_path_str
            )
            return 0

        logger.info(
            "[%s] Start deleting ontology from database '%s'...",
            clean_key,
            self.db_path_str,
        )
        conn = get_write_connection(self.db_path_str)
        cur = conn.cursor()
        try:
            row = cur.execute(
                "SELECT COUNT(*) as cnt FROM terms WHERE ontology = ?",
                (clean_key,),
            ).fetchone()
            count = row["cnt"] if row else 0

            cur.execute("DELETE FROM terms_fts WHERE ontology = ?", (clean_key,))
            cur.execute("DELETE FROM term_details WHERE ontology = ?", (clean_key,))
            cur.execute("DELETE FROM terms WHERE ontology = ?", (clean_key,))
            cur.execute("DELETE FROM ontologies WHERE ontology = ?", (clean_key,))
            cur.execute(
                "UPDATE database_info SET updated_time = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
            )
            conn.commit()
            logger.info(
                "[%s] Delete result: removed %d terms and associated data from '%s'.",
                clean_key,
                count,
                self.db_path_str,
            )
            return count
        finally:
            conn.close()

    def add_term_tag(
        self,
        curie: str,
        tag_key: str,
        tag_value: str,
        ontology: None | str = None,
    ) -> bool:
        """Add a key-value tag to a specific term."""
        clean_curie = curie.strip()
        clean_key = tag_key.strip()
        clean_val = tag_value.strip()

        conn = get_write_connection(self.db_path_str)
        cur = conn.cursor()
        try:
            if ontology:
                clean_ont = ontology.strip().lower()
                row = cur.execute(
                    "SELECT ontology FROM terms WHERE curie = ? AND ontology = ?",
                    (clean_curie, clean_ont),
                ).fetchone()
                if not row:
                    logger.warning(
                        "Term '%s' in ontology '%s' not found; cannot add tag.",
                        clean_curie,
                        clean_ont,
                    )
                    return False
                target_ontologies = [clean_ont]
            else:
                rows = cur.execute(
                    "SELECT ontology FROM terms WHERE curie = ?",
                    (clean_curie,),
                ).fetchall()
                if not rows:
                    logger.warning("Term '%s' not found; cannot add tag.", clean_curie)
                    return False
                target_ontologies = [r[0] for r in rows]

            for ont in target_ontologies:
                cur.execute(
                    """
                    INSERT OR IGNORE INTO term_details (curie, ontology, tag_key, tag_value)
                    VALUES (?, ?, ?, ?)
                    """,
                    (clean_curie, ont, clean_key, clean_val),
                )
            cur.execute(
                "UPDATE database_info SET updated_time = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
            )
            conn.commit()
            return True
        finally:
            conn.close()

    def update_term_tag(
        self,
        curie: str,
        tag_key: str,
        new_value: str,
        old_value: None | str = None,
        ontology: None | str = None,
    ) -> bool:
        """Update a tag value for a given term and tag key."""
        clean_curie = curie.strip()
        clean_key = tag_key.strip()
        clean_new = new_value.strip()

        conn = get_write_connection(self.db_path_str)
        cur = conn.cursor()
        try:
            where_clauses = ["curie = ?", "tag_key = ?"]
            params: list[Any] = [clean_new, clean_curie, clean_key]
            if ontology:
                where_clauses.append("ontology = ?")
                params.append(ontology.strip().lower())
            if old_value is not None:
                where_clauses.append("tag_value = ?")
                params.append(old_value.strip())

            sql = f"UPDATE term_details SET tag_value = ? WHERE {' AND '.join(where_clauses)}"
            cur.execute(sql, params)
            updated = cur.rowcount > 0
            if updated:
                cur.execute(
                    "UPDATE database_info SET updated_time = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
                )
                conn.commit()
            return updated
        finally:
            conn.close()

    def delete_term_tag(
        self,
        curie: str,
        tag_key: str,
        tag_value: None | str = None,
        ontology: None | str = None,
    ) -> int:
        """Delete tag(s) for a given term."""
        clean_curie = curie.strip()
        clean_key = tag_key.strip()

        conn = get_write_connection(self.db_path_str)
        cur = conn.cursor()
        try:
            where_clauses = ["curie = ?", "tag_key = ?"]
            params: list[Any] = [clean_curie, clean_key]
            if ontology:
                where_clauses.append("ontology = ?")
                params.append(ontology.strip().lower())
            if tag_value is not None:
                where_clauses.append("tag_value = ?")
                params.append(tag_value.strip())

            sql = f"DELETE FROM term_details WHERE {' AND '.join(where_clauses)}"
            cur.execute(sql, params)
            deleted = cur.rowcount
            if deleted > 0:
                cur.execute(
                    "UPDATE database_info SET updated_time = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
                )
                conn.commit()
            return deleted
        finally:
            conn.close()

    def create_database(
        self,
        source_dir: None | str | Path = None,
        ontologies: None | list[str] = None,
        target_parents: None | list[str] = None,
        clean: bool = True,
        skip_empty: bool = True,
    ) -> dict[str, dict[str, int]]:
        """Create database from ontologies in the source directory.

        If ontologies are not defined (None or empty), all ontologies in the directory are added.

        Args:
            source_dir: Directory containing ontology JSON files (default: self.default_input_dir).
            ontologies: Optional list of ontology short names to process. If None or empty,
                        all *.json files in the directory are loaded.
            target_parents: Optional list of target parent CURIEs for recursive child-of links.
            clean: If True (default), creates a clean database by removing any existing DB file.
            skip_empty: If True (default), files with 0 classes are excluded from return dictionary.

        Returns:
            Dictionary mapping ontology short name -> {'terms': count, 'details': count}.
        """
        logger.info(
            "create_database invoked on '%s' (clean=%s, ontologies=%s)",
            self.db_path_str,
            clean,
            ontologies,
        )
        return self.load_directory(
            source_dir=source_dir,
            ontologies=ontologies,
            target_parents=target_parents,
            clean=clean,
            skip_empty=skip_empty,
        )


def create_database(
    db_path: None | str | Path = None,
    source_dir: None | str | Path = None,
    ontologies: None | list[str] = None,
    target_parents: None | list[str] = None,
    clean: bool = True,
    batch_size: None | int = None,
    config: None | DatabaseCreationConfig = None,
) -> dict[str, dict[str, int]]:
    """Create database from ontologies in the source directory.

    If ontologies are not defined (None or empty), all ontologies in the directory are added.

    Args:
        db_path: Path to target SQLite database file.
        source_dir: Directory containing ontology JSON files (default: DEFAULT_INPUT_DIR).
        ontologies: Optional list of ontology short names. If None or empty, loads all.
        target_parents: Optional list of target parent CURIEs for recursive child-of links.
        clean: If True (default), removes existing database before creation.
        batch_size: Batch size for SQLite batch insertions.

    Returns:
        Dictionary mapping ontology short name -> {'terms': count, 'details': count}.
    """
    logger.info(
        "create_database helper: target db='%s', source_dir='%s'",
        db_path or DEFAULT_DATABASE_CONFIG.database_path,
        source_dir or DEFAULT_DATABASE_CONFIG.source_directory,
    )
    mgr = OntologyDatabaseManager(
        db_path=db_path,
        default_input_dir=source_dir,
        target_parents=target_parents,
        batch_size=batch_size,
        config=config,
    )
    return mgr.create_database(
        source_dir=source_dir,
        ontologies=ontologies,
        target_parents=target_parents,
        clean=clean,
    )


def set_basic_logging_config(level: int = logging.INFO) -> None:
    """Configure basic root logger with timestamp, level, caller, and message."""
    handler = logging.StreamHandler()
    logging.basicConfig(
        level=level,
        format="[%(asctime)s] %(levelname)s [%(name)s.%(funcName)s:%(lineno)d] %(message)s",
        datefmt="%d/%b/%Y %H:%M:%S",
        handlers=[handler],
    )
