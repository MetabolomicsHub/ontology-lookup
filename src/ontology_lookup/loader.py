import io
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple, Union

import ijson

from ontology_lookup.config import DEFAULT_DATABASE_CONFIG
from ontology_lookup.db import enable_wal_mode, get_write_connection
from ontology_lookup.schema import initialize_schema

DEFAULT_TARGET_PARENTS: List[str] = list(DEFAULT_DATABASE_CONFIG.parent_terms)


def extract_first_str(v: Any) -> Optional[str]:
    """Recursively extract the first non-empty string from nested OLS value structures."""
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        return s if s else None
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        for item in v:
            res = extract_first_str(item)
            if res:
                return res
        return None
    if isinstance(v, dict):
        if "value" in v:
            return extract_first_str(v["value"])
    return None


def extract_all_strs(v: Any) -> List[str]:
    """Recursively extract all non-empty strings from nested OLS value structures."""
    if v is None:
        return []
    if isinstance(v, str):
        s = v.strip()
        return [s] if s else []
    if isinstance(v, (int, float)):
        return [str(v)]
    if isinstance(v, list):
        out: List[str] = []
        for item in v:
            out.extend(extract_all_strs(item))
        return out
    if isinstance(v, dict):
        if "value" in v:
            return extract_all_strs(v["value"])
    return []


def iri_to_curie(iri: str) -> str:
    """Derive standard CURIE notation from an IRI string."""
    if not iri:
        return ""
    if "#" in iri:
        prefix, suffix = iri.rsplit("#", 1)
        ns = prefix.split("/")[-1]
        return f"{ns}:{suffix}"
    frag = iri.rsplit("/", 1)[-1]
    if "_" in frag:
        parts = frag.split("_", 1)
        return f"{parts[0]}:{parts[1]}"
    return frag


def build_parent_lookup(parents: Iterable[str]) -> Dict[str, str]:
    """Map variants of target parent representations (CURIEs, IRIs, short forms)

    to their canonical target parent CURIE string.
    """
    lookup: Dict[str, str] = {}
    for p in parents:
        canonical = p.strip()
        if not canonical:
            continue
        lookup[canonical.lower()] = canonical
        if ":" in canonical:
            prefix, local = canonical.split(":", 1)
            # MS_1000031
            lookup[f"{prefix}_{local}".lower()] = canonical
            # OBO PURL IRI
            lookup[f"http://purl.obolibrary.org/obo/{prefix}_{local}".lower()] = canonical
            # EDAM IRI & prefix variants
            if prefix.upper() == "EDAM":
                lookup[f"http://edamontology.org/{local}".lower()] = canonical
                lookup[local.lower()] = canonical
                if "_" in local:
                    lpref, lnum = local.split("_", 1)
                    lookup[f"{lpref}:{lnum}".lower()] = canonical
        if canonical.startswith(("http://", "https://")):
            lookup[canonical.lower()] = canonical
    return lookup


def extract_ontology_metadata(
    fp: Any,
    default_ontology: str,
) -> Dict[str, str]:
    """Extract ontology short name, title, description, source URI, prefix, and version."""
    meta: Dict[str, str] = {
        "ontology": default_ontology,
        "name": "",
        "description": "",
        "source_uri": "",
        "prefix": default_ontology.upper(),
        "version": "",
    }
    version_iri = ""
    if hasattr(fp, "seekable") and fp.seekable():
        pos = fp.tell()
        try:
            parser = ijson.parse(fp)
            for prefix, event, value in parser:
                if prefix == "ontologies.item.ontologyId":
                    meta["ontology"] = str(value).lower()
                elif (
                    prefix in ("ontologies.item.title", "ontologies.item.label")
                    and event in ("string", "literal")
                    and not meta["name"]
                ):
                    meta["name"] = str(value).strip()
                elif (
                    ("title.value" in prefix or "label.value" in prefix)
                    and event in ("string", "literal")
                    and not meta["name"]
                ):
                    meta["name"] = str(value).strip()
                elif prefix.startswith("ontologies.item.description") and event in (
                    "string",
                    "literal",
                ):
                    meta["description"] = str(value).strip()
                elif (
                    prefix in ("ontologies.item.ontologyPurl", "ontologies.item.iri")
                    and not meta["source_uri"]
                ):
                    meta["source_uri"] = str(value).strip()
                elif prefix == "ontologies.item.preferredPrefix":
                    meta["prefix"] = str(value).strip()
                elif (
                    (
                        "versioninfo.value" in prefix.lower()
                        or prefix.endswith(".versionInfo")
                        or prefix == "ontologies.item.version"
                    )
                    and event in ("string", "literal")
                    and not meta["version"]
                ):
                    meta["version"] = str(value).strip()
                elif (
                    "versioniri" in prefix.lower()
                    and event in ("string", "literal")
                    and not version_iri
                ):
                    version_iri = str(value).strip()
                elif prefix == "ontologies.item.classes.item" and event == "start_map":
                    break
        except Exception:
            pass
        finally:
            fp.seek(pos)

    if not meta["version"] and version_iri:
        m = re.search(
            r"/(?:releases/)?([0-9]+(?:\.[0-9]+)+(?:-[^/]+)?|[0-9]{4}-[0-9]{2}-[0-9]{2})/",
            version_iri,
        )
        if m:
            meta["version"] = m.group(1)
        else:
            meta["version"] = version_iri

    if not meta["name"]:
        meta["name"] = default_ontology.upper()

    return meta


def derive_iri_prefix(conn: sqlite3.Connection, ontology: str, preferred_prefix: str) -> str:
    """Derive common base IRI prefix (e.g. 'http://purl.obolibrary.org/obo/MS_', 'http://edamontology.org/')."""
    cur = conn.cursor()
    cur.execute(
        "SELECT iri, curie FROM terms WHERE ontology = ? AND curie LIKE ? LIMIT 50",
        (ontology, f"{preferred_prefix}:%"),
    )
    rows = cur.fetchall()
    if rows:
        prefixes: Set[str] = set()
        for iri, curie in rows:
            acc = curie.split(":", 1)[1]
            if iri.endswith(acc):
                prefixes.add(iri[: -len(acc)])
        if prefixes:
            return sorted(prefixes, key=len)[0]

    cur.execute("SELECT iri FROM terms WHERE ontology = ? LIMIT 20", (ontology,))
    rows = cur.fetchall()
    if rows:
        first_iri = rows[0][0]
        if "#" in first_iri:
            return first_iri.split("#")[0] + "#"
        elif "/" in first_iri:
            return first_iri.rsplit("/", 1)[0] + "/"
    return ""


def update_ontology_stats(
    conn: sqlite3.Connection,
    ontology: str,
    prefix: Optional[str] = None,
    version: Optional[str] = None,
    name: Optional[str] = None,
) -> None:
    """Update num_of_terms, num_of_obsoletes, num_of_details, iri_prefix, version, and name."""
    cur = conn.cursor()
    clean_ont = ontology.strip().lower()

    cur.execute("SELECT COUNT(*) FROM terms WHERE ontology = ?", (clean_ont,))
    num_terms = cur.fetchone()[0]

    cur.execute(
        """
        SELECT COUNT(DISTINCT curie)
        FROM term_details
        WHERE ontology = ? AND tag_key = 'obsolete' AND tag_value = 'true'
        """,
        (clean_ont,),
    )
    num_obsoletes = cur.fetchone()[0]

    cur.execute(
        """
        SELECT COUNT(*)
        FROM term_details
        WHERE ontology = ?
        """,
        (clean_ont,),
    )
    num_details = cur.fetchone()[0]

    if not prefix:
        cur.execute("SELECT prefix FROM ontologies WHERE ontology = ?", (clean_ont,))
        row = cur.fetchone()
        prefix = row[0] if (row and row[0]) else clean_ont.upper()

    iri_prefix = derive_iri_prefix(conn, clean_ont, prefix)

    extra_updates: List[str] = []
    params: List[Any] = [num_terms, num_obsoletes, num_details, iri_prefix]

    if version:
        extra_updates.append("version = ?")
        params.append(version)
    if name:
        extra_updates.append("name = ?")
        params.append(name)

    extra_sql = (", " + ", ".join(extra_updates)) if extra_updates else ""
    params.append(clean_ont)

    cur.execute(
        f"""
        UPDATE ontologies
        SET num_of_terms = ?,
            num_of_obsoletes = ?,
            num_of_details = ?,
            iri_prefix = ?
            {extra_sql}
        WHERE ontology = ?
        """,
        params,
    )
    conn.commit()


def recalculate_all_ontology_stats(conn: sqlite3.Connection) -> None:
    """Recalculate statistics and IRI prefixes for all ontologies present in terms table."""
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT ontology FROM terms")
    onts = [r[0] for r in cur.fetchall()]
    for ont in onts:
        cur.execute("SELECT COUNT(*) FROM ontologies WHERE ontology = ?", (ont,))
        if cur.fetchone()[0] == 0:
            cur.execute(
                "INSERT INTO ontologies (ontology, prefix) VALUES (?, ?)",
                (ont, ont.upper()),
            )
        update_ontology_stats(conn, ont)
    conn.commit()


class JsonOntologyLoader:
    """Streams and ingests ontology JSON files into SQLite database."""

    def __init__(
        self,
        db_path: str,
        target_parents: Optional[List[str]] = None,
        batch_size: Optional[int] = None,
        config=None,
    ) -> None:
        config = config or DEFAULT_DATABASE_CONFIG
        self.db_path = str(db_path or config.database_path)
        self.target_parents: List[str] = (
            list(target_parents) if target_parents is not None else list(config.parent_terms)
        )
        self.batch_size = batch_size or config.batch_size

    def load_file(
        self,
        source: Union[str, Path, io.BytesIO, io.BufferedReader],
        ontology_name: Optional[str] = None,
        target_parents: Optional[List[str]] = None,
        delete_existing: bool = True,
        clean_db: bool = False,
    ) -> Tuple[int, int]:
        """Ingest ontology classes from a JSON file into SQLite.

        Args:
            source: File path (str/Path) or binary stream (BytesIO/BufferedReader).
            ontology_name: Ontology short name (e.g., 'ms', 'edam'). If not supplied,
                           inferred from the filename stem.
            target_parents: Optional list of target parent CURIEs for recursive child-of links.
                            Defaults to self.target_parents.
            delete_existing: If True, deletes existing terms for this ontology before ingesting.
            clean_db: If True, resets the database file and initializes fresh schema.

        Returns:
            Tuple of (terms_count, details_count).
        """
        if clean_db and os.path.exists(self.db_path):
            os.remove(self.db_path)

        # Infer ontology name from file name if not provided
        inferred_name = ontology_name
        if not inferred_name:
            if isinstance(source, (str, Path)):
                inferred_name = Path(source).stem.lower()
            else:
                inferred_name = "ontology"

        target_ontology = inferred_name.lower()

        # Build parent lookup for recursive hierarchy filter
        active_parents = target_parents if target_parents is not None else self.target_parents
        parent_lookup = build_parent_lookup(active_parents)

        # Determine stream
        should_close = False
        fp: Union[io.BytesIO, io.BufferedReader]
        if isinstance(source, (str, Path)):
            src_path = Path(source).resolve()
            if not src_path.exists():
                raise FileNotFoundError(f"Ontology JSON file not found: {src_path}")
            fp = open(src_path, "rb")
            should_close = True
        else:
            fp = source

        # Extract ontology metadata (short name, description, source URI, prefix)
        meta = extract_ontology_metadata(fp, target_ontology)
        final_ont = meta.get("ontology") or target_ontology

        conn = get_write_connection(self.db_path)
        initialize_schema(conn)
        cur = conn.cursor()

        if delete_existing:
            cur.execute("DELETE FROM terms_fts WHERE ontology = ?", (final_ont,))
            cur.execute("DELETE FROM term_details WHERE ontology = ?", (final_ont,))
            cur.execute("DELETE FROM terms WHERE ontology = ?", (final_ont,))
            cur.execute("DELETE FROM ontologies WHERE ontology = ?", (final_ont,))
            conn.commit()

        # Insert metadata into ontologies table and update database_info
        cur.execute(
            """
            INSERT OR REPLACE INTO ontologies
            (ontology, name, description, source_uri, prefix, version)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                final_ont,
                meta.get("name", final_ont.upper()),
                meta.get("description", ""),
                meta.get("source_uri", ""),
                meta.get("prefix", final_ont.upper()),
                meta.get("version", ""),
            ),
        )
        cur.execute("UPDATE database_info SET updated_time = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')")
        conn.commit()

        terms_batch: List[Tuple[str, str, str, str]] = []
        details_batch: List[Tuple[str, str, str, str]] = []
        fts_batch: List[Tuple[str, str, str, str, str]] = []

        total_terms = 0
        total_details = 0

        saw_child_flags = False
        curies_in_file: List[str] = []
        parent_refs: Set[str] = set()

        def flush_batches() -> None:
            nonlocal total_terms, total_details
            if terms_batch:
                cur.executemany(
                    "INSERT OR REPLACE INTO terms "
                    "(curie, iri, ontology, label) VALUES (?, ?, ?, ?)",
                    terms_batch,
                )
                total_terms += len(terms_batch)
                terms_batch.clear()

            if details_batch:
                cur.executemany(
                    "INSERT OR REPLACE INTO term_details "
                    "(curie, ontology, tag_key, tag_value) VALUES (?, ?, ?, ?)",
                    details_batch,
                )
                total_details += len(details_batch)
                details_batch.clear()

            if fts_batch:
                cur.executemany(
                    "INSERT INTO terms_fts (curie, ontology, label, synonyms, description) "
                    "VALUES (?, ?, ?, ?, ?)",
                    fts_batch,
                )
                fts_batch.clear()

            conn.commit()

        try:
            # Stream classes directly using ijson
            classes_iter = ijson.items(fp, "ontologies.item.classes.item")
            for cls in classes_iter:
                if not isinstance(cls, dict):
                    continue

                iri = extract_first_str(cls.get("iri")) or ""
                curie = (
                    extract_first_str(cls.get("curie"))
                    or extract_first_str(cls.get("shortForm"))
                    or iri_to_curie(iri)
                )
                if not curie:
                    continue

                label = (
                    extract_first_str(cls.get("label"))
                    or extract_first_str(cls.get("http://www.w3.org/2000/01/rdf-schema#label"))
                    or curie
                )

                terms_batch.append((curie, iri, target_ontology, label))

                # Synonyms
                synonyms_set: Set[str] = set(extract_all_strs(cls.get("synonym")))
                for k, v in cls.items():
                    if "synonym" in k.lower() and k not in (
                        "synonym",
                        "synonymProperty",
                    ):
                        synonyms_set.update(extract_all_strs(v))

                for syn in synonyms_set:
                    details_batch.append((curie, target_ontology, "synonym", syn))

                # Definitions
                defs = set(extract_all_strs(cls.get("definition")))
                defs.update(extract_all_strs(cls.get("http://purl.obolibrary.org/obo/IAO_0000115")))
                defs.update(
                    extract_all_strs(
                        cls.get("http://www.geneontology.org/formats/oboInOwl#hasDefinition")
                    )
                )
                for d in defs:
                    details_batch.append((curie, target_ontology, "definition", d))

                # Obsolete status
                is_obsolete = cls.get("isObsolete")
                if is_obsolete is True:
                    details_batch.append((curie, target_ontology, "obsolete", "true"))
                elif is_obsolete is False:
                    details_batch.append((curie, target_ontology, "obsolete", "false"))

                # Leaf status: tag_key is_leaf with value '1' if term has no child
                has_child_flag = (
                    cls.get("hasDirectChildren") is True
                    or cls.get("hasHierarchicalChildren") is True
                    or cls.get("hasChildren") is True
                )
                no_child_flag = (
                    cls.get("hasDirectChildren") is False
                    and cls.get("hasHierarchicalChildren") is False
                ) or cls.get("hasChildren") is False
                if has_child_flag or no_child_flag:
                    saw_child_flags = True
                    if no_child_flag:
                        details_batch.append((curie, target_ontology, "is_leaf", "1"))
                else:
                    curies_in_file.append(curie)
                    ancestors_raw = (
                        extract_all_strs(cls.get("hierarchicalAncestor"))
                        + extract_all_strs(cls.get("directAncestor"))
                        + extract_all_strs(cls.get("hierarchicalParent"))
                        + extract_all_strs(cls.get("directParent"))
                    )
                    for a_raw in ancestors_raw:
                        clean_a = a_raw.strip().lower()
                        parent_refs.add(clean_a)
                        parent_refs.add(iri_to_curie(clean_a).strip().lower())

                # Recursive Hierarchy: only add child-of links for the specified target parents
                if parent_lookup:
                    ancestors = (
                        extract_all_strs(cls.get("hierarchicalAncestor"))
                        or extract_all_strs(cls.get("directAncestor"))
                        or extract_all_strs(cls.get("hierarchicalParent"))
                        or extract_all_strs(cls.get("directParent"))
                    )
                    matched_parents: Set[str] = set()
                    for anc in ancestors:
                        clean_anc = anc.strip().lower()
                        if clean_anc in parent_lookup:
                            matched_parents.add(parent_lookup[clean_anc])

                    for mp in matched_parents:
                        if mp.lower() != curie.lower():
                            details_batch.append((curie, target_ontology, "child-of", mp))

                # FTS entry
                syns_str = " ".join(synonyms_set)
                defs_str = " ".join(defs)
                fts_batch.append((curie, target_ontology, label, syns_str, defs_str))

                if len(terms_batch) >= self.batch_size:
                    flush_batches()

            # Handle non-flagged files (fallback to parents graph check)
            if not saw_child_flags and curies_in_file:
                for c in curies_in_file:
                    if c.strip().lower() not in parent_refs:
                        details_batch.append((c, target_ontology, "is_leaf", "1"))

            flush_batches()
            update_ontology_stats(
                conn,
                final_ont,
                prefix=meta.get("prefix", final_ont.upper()),
                version=meta.get("version", ""),
                name=meta.get("name", final_ont.upper()),
            )

        finally:
            if should_close:
                fp.close()

        cur.execute("PRAGMA optimize;")
        conn.commit()
        conn.close()

        enable_wal_mode(self.db_path)

        return total_terms, total_details
