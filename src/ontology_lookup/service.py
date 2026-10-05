from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Generator
from contextlib import contextmanager
from copy import deepcopy
from functools import wraps
from pathlib import Path
from threading import RLock
from typing import Any

from cachetools import TTLCache, cached

from ontology_lookup import (
    CurieResolutionResponse,
    DatabaseInfo,
    HealthResponse,
    IriResolutionResponse,
    OntologyInfo,
    SearchTermSummary,
    TermRecord,
    TermResponse,
)
from ontology_lookup.db import get_readonly_connection

_CACHE_TTL_SECONDS = 60
_USE_CASE_CACHE = TTLCache(maxsize=4096, ttl=_CACHE_TTL_SECONDS)
_USE_CASE_CACHE_LOCK = RLock()


def _normalize_cache_key(value: Any) -> Any:
    """Normalize query inputs so case-insensitive calls share cache entries."""
    if isinstance(value, str):
        return value.strip().casefold()
    if isinstance(value, (list, tuple, set)):
        normalized = tuple(_normalize_cache_key(item) for item in value)
        return tuple(sorted(normalized)) if isinstance(value, set) else normalized
    if isinstance(value, dict):
        return tuple(
            sorted(
                (_normalize_cache_key(key), _normalize_cache_key(item))
                for key, item in value.items()
            )
        )
    return value


def _use_case_cache_key(method: Callable) -> Callable:
    """Create a cache key function scoped to a use-case method and database."""

    def make_key(service: OntologyLookupService, *args: Any, **kwargs: Any) -> tuple:
        db_path = str(Path(service.db_path).expanduser().resolve())
        return (
            method.__qualname__,
            db_path,
            _normalize_cache_key(args),
            _normalize_cache_key(kwargs),
        )

    return make_key


def cached_use_case(method: Callable) -> Callable:
    """Cache a use-case result for 60 seconds using normalized input arguments."""
    cached_method = cached(
        _USE_CASE_CACHE,
        key=_use_case_cache_key(method),
        lock=_USE_CASE_CACHE_LOCK,
    )(method)

    @wraps(method)
    def wrapped(service: OntologyLookupService, *args: Any, **kwargs: Any) -> Any:
        # Return detached values so a caller cannot mutate shared cached results.
        return deepcopy(cached_method(service, *args, **kwargs))

    return wrapped


class OntologyLookupService:
    """Service providing high-performance query methods for local ontology lookup use cases.

    Each use case opens and closes its own read-only database connection. Use
    :meth:`get_connection` when direct access to a connection is needed.
    """

    def __init__(self, db_path: str = ".db/ontology_lookup.db") -> None:
        self.db_path = db_path

    @contextmanager
    def get_connection(self) -> Generator[sqlite3.Connection]:
        """Yield a new read-only connection and close it when the context exits."""
        conn = get_readonly_connection(self.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def _get_connection(self) -> tuple[sqlite3.Connection, bool]:
        """Return a fresh read-only connection that the caller must close."""
        return get_readonly_connection(self.db_path), True

    @staticmethod
    def sanitize_fts_query(q: str) -> str:
        """Sanitize query string for SQLite FTS5 syntax safety while preserving prefix matching."""
        cleaned = re.sub(r'[^\w\s*"-]', " ", q, flags=re.UNICODE).strip()
        if not cleaned:
            return '""'
        if "*" in cleaned or '"' in cleaned:
            return cleaned
        tokens = cleaned.split()
        if len(tokens) == 1:
            return f'"{tokens[0]}"*'
        return " ".join(f'"{token}"*' for token in tokens)

    def assemble_term_response(
        self,
        term: TermRecord,
    ) -> TermResponse:
        """Fetch term details and assemble a full TermResponse."""
        curie = term.curie
        db, should_close = self._get_connection()
        try:
            details_cur = db.cursor()
            if term.ontology:
                details_rows = details_cur.execute(
                    "SELECT tag_key, tag_value FROM term_details WHERE curie = ? AND ontology = ?",
                    (curie, term.ontology.strip().lower()),
                ).fetchall()
            else:
                details_rows = details_cur.execute(
                    "SELECT tag_key, tag_value FROM term_details WHERE curie = ?",
                    (curie,),
                ).fetchall()

            synonyms: list[str] = []
            children_of: list[str] = []
            tags_dict: dict[str, list[str]] = defaultdict(list)

            for d in details_rows:
                k, v = d["tag_key"], d["tag_value"]
                if k == "synonym":
                    synonyms.append(v)
                elif k == "child-of":
                    children_of.append(v)
                else:
                    tags_dict[k].append(v)

            final_tags: dict[str, None | str, list[str]] = {}
            for k, v_list in tags_dict.items():
                if len(v_list) == 1:
                    final_tags[k] = v_list[0]
                else:
                    final_tags[k] = v_list

            return TermResponse(
                curie=term.curie,
                iri=term.iri,
                ontology=term.ontology,
                label=term.label,
                synonyms=synonyms,
                children_of=children_of,
                tags=final_tags,
            )
        finally:
            if should_close:
                db.close()

    # --- Use Case 1: Find a CV term with ontology and accession (CURIE or IRI) ---
    @cached_use_case
    def get_term_by_accession(
        self,
        ontology: None | str,
        accession: str,
    ) -> None | TermResponse:
        """Lookup an ontology term by ontology short name and its CURIE or full IRI.

        Case-insensitive. Returns None if not found.
        """
        db, should_close = self._get_connection()
        try:
            cur = db.cursor()
            ontology = ontology or ""
            clean_ontology = ontology.strip().lower()
            if accession.lower().startswith(("http://", "https://", "urn:")):
                if not clean_ontology:
                    response = self.find_curie(accession)
                    if response:
                        clean_ontology = response.curie.split(":")[0]
                row = cur.execute(
                    "SELECT * FROM terms WHERE iri = ? AND ontology = ?",
                    (accession, clean_ontology),
                ).fetchone()
            else:
                if not clean_ontology and ":" in accession:
                    clean_ontology = accession.split(":")[0]
                row = cur.execute(
                    "SELECT * FROM terms WHERE curie = ? AND ontology = ?",
                    (accession, clean_ontology),
                ).fetchone()

            if not row:
                row = cur.execute(
                    "SELECT * FROM terms WHERE (curie = ? OR iri = ?) AND ontology = ?",
                    (accession, accession, clean_ontology),
                ).fetchone()

            if not row:
                return None

            term_rec = TermRecord(
                curie=row["curie"],
                iri=row["iri"],
                ontology=row["ontology"],
                label=row["label"],
            )
            return self.assemble_term_response(term_rec)
        finally:
            if should_close:
                db.close()

    # --- Use Case 2: Find a CV term with ontology and label (Exact Match) ---
    @cached_use_case
    def get_term_by_exact_label(
        self,
        ontology: str,
        label: str,
    ) -> None | TermResponse:
        """Exact match lookup by ontology and term label (case-insensitive)."""
        db, should_close = self._get_connection()
        try:
            cur = db.cursor()
            clean_ontology = ontology.strip().lower()
            row = cur.execute(
                "SELECT * FROM terms WHERE ontology = ? AND label = ?",
                (clean_ontology, label),
            ).fetchone()

            if not row:
                return None

            term_rec = TermRecord(
                curie=row["curie"],
                iri=row["iri"],
                ontology=row["ontology"],
                label=row["label"],
            )
            return self.assemble_term_response(term_rec)
        finally:
            if should_close:
                db.close()

    # --- Use Cases 3 & 4: Exact match on labels and synonyms (+ parent filter) ---
    @cached_use_case
    def search_by_label(
        self,
        label_or_synonym: str,
        ontology: None | str | list[str] = None,
        parent_curie: None | str | list[str] = None,
        search_in_synonyms: bool = True,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SearchTermSummary]:
        """Match a complete label or synonym, with optional ontology/hierarchy filters.

        Matching is case-insensitive. Descriptions and partial matches are excluded.
        Exact label matches are returned before exact synonym matches.
        """
        exact_query = label_or_synonym.strip()
        if not exact_query:
            return []

        ontologies: list[str] = []
        if ontology:
            if isinstance(ontology, str):
                ontologies = [o.strip().lower() for o in ontology.split(",") if o.strip()]
            else:
                for item in ontology:
                    ontologies.extend([o.strip().lower() for o in item.split(",") if o.strip()])

        parents: list[str] = []
        if parent_curie:
            if isinstance(parent_curie, str):
                parents = [p.strip() for p in parent_curie.split(",") if p.strip()]
            else:
                for item in parent_curie:
                    parents.extend([p.strip() for p in item.split(",") if p.strip()])

        # Find label and synonym candidates independently. Combining both
        # predicates with OR makes SQLite scan the full terms table on large
        # multi-ontology databases.
        fts_query = f'label : "{exact_query.replace(chr(34), chr(34) * 2)}"'
        sql_parts = [
            "WITH matches AS (",
            "    SELECT t.curie, t.iri, t.ontology, t.label, NULL AS matched_synonym, 0.0 AS rank",
            "    FROM (",
            "        SELECT f.curie, f.ontology FROM terms_fts f",
            "        WHERE terms_fts MATCH ?",
            "    ) label_candidates",
            "    CROSS JOIN terms t",
            "    WHERE t.curie = label_candidates.curie",
            "      AND t.ontology = label_candidates.ontology",
            "      AND t.label = ?",
        ]
        params: list[Any] = [fts_query, exact_query]
        if search_in_synonyms:
            sql_parts.extend(
                [
                    "    UNION ALL",
                    "    SELECT t.curie, t.iri, t.ontology, t.label, ",
                    "synonym_candidates.matched_synonym, 1.0 AS rank",
                    "    FROM (",
                    "        SELECT synonym.curie, synonym.ontology, synonym.tag_value ",
                    "AS matched_synonym",
                    "        FROM term_details synonym",
                    "        WHERE synonym.tag_key = 'synonym'",
                    "          AND synonym.tag_value = ?",
                    "    ) synonym_candidates",
                    "    CROSS JOIN terms t",
                    "    WHERE t.curie = synonym_candidates.curie",
                    "      AND t.ontology = synonym_candidates.ontology",
                    "      AND t.label != ?",
                ]
            )
            params.extend([exact_query, exact_query])
        sql_parts.extend(
            [
                ")",
                "SELECT curie, iri, ontology, label, matched_synonym, rank FROM matches",
            ]
        )

        if ontologies:
            placeholders = ",".join("?" for _ in ontologies)
            sql_parts.append(f"WHERE ontology IN ({placeholders})")
            params.extend(ontologies)

        if parents:
            placeholders = ",".join("?" for _ in parents)
            sql_parts.append(
                ("AND " if ontologies else "WHERE ")
                + "(curie IN ("
                + placeholders
                + ") OR EXISTS ("
                "SELECT 1 FROM term_details hierarchy "
                "WHERE hierarchy.curie = matches.curie "
                "AND hierarchy.ontology = matches.ontology "
                "AND hierarchy.tag_key = 'child-of' "
                "AND hierarchy.tag_value IN (" + placeholders + ")))"
            )
            params.extend(parents)
            params.extend(parents)

        sql_parts.extend(["ORDER BY rank, ontology, curie", "LIMIT ? OFFSET ?"])
        params.extend([limit, offset])

        db, should_close = self._get_connection()
        try:
            rows = db.execute("\n".join(sql_parts), params).fetchall()
            return [
                SearchTermSummary(
                    curie=row["curie"],
                    iri=row["iri"],
                    ontology=row["ontology"],
                    label=row["label"],
                    rank=row["rank"],
                    matched_synonym=row["matched_synonym"],
                )
                for row in rows
            ]
        finally:
            if should_close:
                db.close()

    def _fts_search(
        self,
        query: str,
        ontology: None | str | list[str] = None,
        parent_curie: None | str | list[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SearchTermSummary]:
        """Run prefix full-text search across labels, synonyms, and descriptions."""
        sanitized_q = self.sanitize_fts_query(query)
        ontologies: list[str] = []
        if ontology:
            values = [ontology] if isinstance(ontology, str) else ontology
            for item in values:
                ontologies.extend([o.strip().lower() for o in item.split(",") if o.strip()])

        parents: list[str] = []
        if parent_curie:
            values = [parent_curie] if isinstance(parent_curie, str) else parent_curie
            for item in values:
                parents.extend([p.strip() for p in item.split(",") if p.strip()])

        sql_parts: list[str] = [
            "SELECT DISTINCT t.curie, t.iri, t.ontology, t.label, f.rank",
            "FROM terms_fts f",
            "JOIN terms t ON f.curie = t.curie COLLATE NOCASE ",
            "AND f.ontology = t.ontology COLLATE NOCASE",
        ]
        params: list[Any] = []
        if parents:
            sql_parts.append("JOIN term_details d ON t.curie = d.curie AND t.ontology = d.ontology")
        sql_parts.append("WHERE terms_fts MATCH ?")
        params.append(sanitized_q)

        if ontologies:
            placeholders = ",".join("?" for _ in ontologies)
            sql_parts.append(f"AND t.ontology IN ({placeholders})")
            params.extend(ontologies)

        if parents:
            placeholders = ",".join("?" for _ in parents)
            sql_parts.append(
                f"AND ((d.tag_key = 'child-of' AND d.tag_value IN ({placeholders})) "
                f"OR t.curie IN ({placeholders}))"
            )
            params.extend(parents)
            params.extend(parents)

        sql_parts.extend(["ORDER BY f.rank", "LIMIT ? OFFSET ?"])
        params.extend([limit, offset])

        db, should_close = self._get_connection()
        try:
            rows = db.execute("\n".join(sql_parts), params).fetchall()
            seen: set[tuple[str, str]] = set()
            results: list[SearchTermSummary] = []
            for row in rows:
                identity = (row["curie"].casefold(), row["ontology"].casefold())
                if identity not in seen:
                    seen.add(identity)
                    results.append(
                        SearchTermSummary(
                            curie=row["curie"],
                            iri=row["iri"],
                            ontology=row["ontology"],
                            label=row["label"],
                            rank=row["rank"],
                        )
                    )
            return results
        finally:
            if should_close:
                db.close()

    # --- Use Case 5: Find CURIE of an IRI in an ontology ---
    @cached_use_case
    def find_curie(
        self,
        iri: str,
        ontology: None | str = None,
    ) -> None | CurieResolutionResponse:
        """Resolve a full IRI to its primary CURIE within an ontology (case-insensitive)."""
        db, should_close = self._get_connection()
        try:
            cur = db.cursor()
            ontology = ontology or ""
            clean_ontology = ontology.strip().lower()
            if ontology:
                row = cur.execute(
                    "SELECT curie FROM terms WHERE iri = ? AND ontology = ?",
                    (iri, clean_ontology),
                ).fetchone()
            else:
                row = cur.execute(
                    "SELECT curie FROM terms WHERE iri = ?",
                    (iri,),
                ).fetchone()
            if not row:
                return None
            return CurieResolutionResponse(curie=str(row["curie"]))
        finally:
            if should_close:
                db.close()

    # --- Use Case 6: Key-value tag search ---
    @cached_use_case
    def search_by_tag(
        self,
        tag_key: str,
        tag_value: str,
        ontology: None | str = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SearchTermSummary]:
        """Search ontology terms by arbitrary key-value details/tags (case-insensitive)."""
        sql = """
        SELECT DISTINCT t.curie, t.iri, t.ontology, t.label
        FROM terms t
        JOIN term_details tg ON t.curie = tg.curie AND t.ontology = tg.ontology
        WHERE tg.tag_key = ? AND tg.tag_value = ?
        """
        params: list[Any] = [tag_key, tag_value]

        if ontology:
            sql += " AND t.ontology = ?"
            params.append(ontology.strip().lower())

        sql += " LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        db, should_close = self._get_connection()
        try:
            cur = db.cursor()
            rows = cur.execute(sql, params).fetchall()
            return [
                SearchTermSummary(
                    curie=r["curie"],
                    iri=r["iri"],
                    ontology=r["ontology"],
                    label=r["label"],
                    rank=None,
                )
                for r in rows
            ]
        finally:
            if should_close:
                db.close()

    # --- Use Cases 7: Find children of a parent
    @cached_use_case
    def get_children(
        self,
        parent_curie: str,
        ontology: None | str = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SearchTermSummary]:
        """Retrieve terms that are recursive children of the specified parent CURIE."""
        return self.search_by_tag(
            tag_key="child-of",
            tag_value=parent_curie,
            ontology=ontology,
            limit=limit,
            offset=offset,
        )

    @cached_use_case
    def get_indexed_parent_terms(self) -> list[str]:
        """List distinct parent CURIEs referenced by indexed ``child-of`` tags."""
        db, should_close = self._get_connection()
        try:
            rows = db.execute(
                "SELECT DISTINCT tag_value FROM term_details "
                "WHERE tag_key = ? ORDER BY tag_value COLLATE NOCASE",
                ("child-of",),
            ).fetchall()
            return [str(row["tag_value"]) for row in rows]
        finally:
            if should_close:
                db.close()

    # --- Use Cases 8: List all ontologies
    @cached_use_case
    def list_ontologies(self) -> list[OntologyInfo]:
        """List all installed ontologies with metadata and term counts."""
        db, should_close = self._get_connection()
        try:
            cur = db.cursor()

            query_ont = """
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
                COALESCE(iri_prefix, '') as iri_prefix,
                COALESCE(num_of_terms, 0) as term_count
            FROM ontologies
            WHERE num_of_terms > 0
            ORDER BY ontology
            """
            rows = cur.execute(query_ont).fetchall()

            return [
                OntologyInfo(
                    ontology=r["ontology"],
                    name=r["name"] or None,
                    description=r["description"] or None,
                    source_uri=r["source_uri"] or None,
                    prefix=r["prefix"] or None,
                    version=r["version"] or None,
                    term_count=r["term_count"] if r["term_count"] else r["num_of_terms"],
                    num_of_terms=r["num_of_terms"] if r["num_of_terms"] else r["term_count"],
                    num_of_obsoletes=r["num_of_obsoletes"],
                    num_of_details=r["num_of_details"],
                    iri_prefix=r["iri_prefix"] or None,
                )
                for r in rows
            ]
        finally:
            if should_close:
                db.close()

    # --- Use Cases 9: get ontology info
    @cached_use_case
    def get_ontology(self, ontology: str) -> None | OntologyInfo:
        """Lookup metadata for a specific ontology short name."""
        db, should_close = self._get_connection()
        try:
            cur = db.cursor()
            clean_ont = ontology.strip().lower()
            row = cur.execute(
                """
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
                FROM ontologies WHERE LOWER(ontology) = ?
                """,
                (clean_ont,),
            ).fetchone()
            if not row:
                t_row = cur.execute(
                    "SELECT COUNT(*) as cnt FROM terms WHERE LOWER(ontology) = ?",
                    (clean_ont,),
                ).fetchone()
                if not t_row or t_row["cnt"] == 0:
                    return None
                return OntologyInfo(
                    ontology=clean_ont,
                    name=None,
                    description=None,
                    source_uri=None,
                    prefix=clean_ont.upper(),
                    version=None,
                    term_count=t_row["cnt"],
                    num_of_terms=t_row["cnt"],
                    num_of_obsoletes=0,
                    num_of_details=0,
                    iri_prefix=None,
                )
            return OntologyInfo(
                ontology=row["ontology"],
                name=row["name"] or None,
                description=row["description"] or None,
                source_uri=row["source_uri"] or None,
                prefix=row["prefix"] or None,
                version=row["version"] or None,
                term_count=row["num_of_terms"],
                num_of_terms=row["num_of_terms"],
                num_of_obsoletes=row["num_of_obsoletes"],
                num_of_details=row["num_of_details"],
                iri_prefix=row["iri_prefix"] or None,
            )
        finally:
            if should_close:
                db.close()

    # --- Use Cases 10: free-text search
    @cached_use_case
    def freetext_search(
        self,
        query: str,
        ontology: None | str | list[str] = None,
        parent_curie: None | str | list[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SearchTermSummary]:
        """Free-text search across labels, synonyms, and definitions using FTS5

        with support for single or list inputs for ontology and parent_curie.
        """
        return self._fts_search(
            query=query,
            ontology=ontology,
            parent_curie=parent_curie,
            limit=limit,
            offset=offset,
        )

    # --- Use Case 11: Find IRI of an CURIE in an ontology ---
    @cached_use_case
    def find_iri(
        self,
        curie: str,
        ontology: None | str = None,
    ) -> None | IriResolutionResponse:
        """Resolve a full IRI to its primary IRI within an ontology
        (case-insensitive)."""
        db, should_close = self._get_connection()
        try:
            cur = db.cursor()
            ontology = ontology or ""
            clean_ontology = ontology.strip().lower()
            if clean_ontology:
                row = cur.execute(
                    "SELECT iri FROM terms WHERE curie = ? AND ontology = ?",
                    (curie, clean_ontology),
                ).fetchone()
            else:
                row = cur.execute(
                    "SELECT iri FROM terms WHERE curie = ?",
                    (curie,),
                ).fetchone()
            if not row:
                return None
            return IriResolutionResponse(iri=str(row["iri"]))
        finally:
            if should_close:
                db.close()

    # --- Use Cases 15: Health check
    @cached_use_case
    def get_health(self) -> HealthResponse:
        """Health check reporting database status and term count."""
        db, should_close = self._get_connection()
        try:
            cur = db.cursor()
            row = cur.execute("SELECT COUNT(*) as cnt FROM terms").fetchone()
            count = row["cnt"] if row else 0
            return HealthResponse(
                status="ok",
                database_path=self.db_path or "default",
                term_count=count,
            )
        finally:
            if should_close:
                db.close()

    # --- Use Cases 16: get database info
    @cached_use_case
    def get_database_info(self) -> DatabaseInfo:
        """Retrieve database metadata including created_time,
        updated_time, and creators."""
        db, should_close = self._get_connection()
        try:
            cur = db.cursor()
            cols = [r[1] for r in cur.execute("PRAGMA table_info(database_info)").fetchall()]
            created_by_col = "created_by" if "created_by" in cols else "NULL as created_by"
            updated_by_col = "updated_by" if "updated_by" in cols else "NULL as updated_by"
            row = cur.execute(
                "SELECT created_time, updated_time, "
                f"{created_by_col}, {updated_by_col} "
                "FROM database_info LIMIT 1"
            ).fetchone()
            if row:
                return DatabaseInfo(
                    created_time=row["created_time"],
                    updated_time=row["updated_time"],
                    created_by=row["created_by"],
                    updated_by=row["updated_by"],
                )
            return DatabaseInfo(created_time="unknown")
        finally:
            if should_close:
                db.close()
