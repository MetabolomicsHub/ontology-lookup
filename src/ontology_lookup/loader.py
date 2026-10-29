import csv
import io
import logging
import re
import sqlite3
import time
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, Iterable, List, Optional, Self, Set, Tuple, Union
from urllib.parse import urlsplit

import ijson

from ontology_lookup.config import DEFAULT_DATABASE_CONFIG
from ontology_lookup.db import enable_wal_mode, get_write_connection
from ontology_lookup.schema import initialize_schema

logger = logging.getLogger(__name__)

DEFAULT_TARGET_PARENTS: List[str] = list(DEFAULT_DATABASE_CONFIG.parent_terms)

ALLOWED_SYNONYM_PROPERTIES = frozenset(
    {
        "http://www.geneontology.org/formats/oboInOwl#hasExactSynonym",
        "http://www.geneontology.org/formats/oboInOWL#hasExactSynonym",
        "http://www.geneontology.org/formats/oboInOwl#hasSynonym",
        "http://purl.obolibrary.org/obo/OMIABIS.owl#hasExactSynonym",
        "http://purl.obolibrary.org/obo/hasExactSynonym",
        "http://purl.obolibrary.org/obo/has_exact_synonym",
    }
)


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
    if isinstance(v, dict) and "value" in v:
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
    if isinstance(v, dict) and "value" in v:
        return extract_all_strs(v["value"])
    return []


def extract_allowed_synonyms(entity: Dict[str, Any]) -> Set[str]:
    """Keep Exact and OBO unscoped synonyms, resolving aggregated values by scope.

    A mixed aggregate cannot identify each value's origin; in that case only
    values present under an allowed source property are retained.
    """
    synonyms: Set[str] = set()
    excluded: Set[str] = set()
    for key, value in entity.items():
        if key in ALLOWED_SYNONYM_PROPERTIES:
            synonyms.update(extract_all_strs(value))
        elif "synonym" in key.lower() and key not in (
            "synonym",
            "synonymProperty",
            "synonym_property",
        ):
            excluded.update(extract_all_strs(value))

    source_properties = set(extract_all_strs(entity.get("synonymProperty")))
    if source_properties and source_properties <= ALLOWED_SYNONYM_PROPERTIES:
        # Preserve explicit allowed values even if the same text also has an
        # excluded scope. Do not reintroduce excluded values via the aggregate.
        excluded_folded = {value.casefold() for value in excluded}
        synonyms.update(
            value
            for value in extract_all_strs(entity.get("synonym"))
            if value.casefold() not in excluded_folded
        )
    return synonyms


def _searchable_synonyms(entity: Dict[str, Any]) -> Set[str]:
    """Include a distinct preferred label only when it does not bypass scope filtering."""
    synonyms = extract_allowed_synonyms(entity)
    label = extract_first_str(entity.get("label")) or extract_first_str(
        entity.get("http://www.w3.org/2000/01/rdf-schema#label")
    )
    preferred = extract_first_str(entity.get("prefLabel")) or extract_first_str(
        entity.get("http://www.w3.org/2004/02/skos/core#prefLabel")
    )
    excluded = {
        value.casefold()
        for key, raw in entity.items()
        if "synonym" in key.lower()
        and any(scope in key.lower() for scope in ("related", "broad", "narrow"))
        for value in extract_all_strs(raw)
    }
    if (
        preferred
        and preferred != label
        and (
            preferred.casefold() not in excluded
            or preferred.casefold() in {value.casefold() for value in synonyms}
        )
    ):
        synonyms.add(preferred)
    return synonyms


class NativeSynonymIndex:
    """Disk-backed native synonym authority shared by an ontology import run."""

    def __init__(self, source_dir: Union[str, Path], batch_size: int = 5000) -> None:
        self.source_dir = Path(source_dir)
        self.batch_size = batch_size
        self._temporary: Optional[TemporaryDirectory] = None
        self._connection: Optional[sqlite3.Connection] = None

    def __enter__(self) -> Self:
        if not self.source_dir.is_dir():
            raise FileNotFoundError(f"Native ontology directory not found: {self.source_dir}")
        self._temporary = TemporaryDirectory(prefix="ontology-native-synonyms-")
        try:
            self._connection = sqlite3.connect(Path(self._temporary.name) / "synonyms.db")
            self._connection.executescript(
                "CREATE TABLE native_terms (curie TEXT PRIMARY KEY COLLATE NOCASE);"
                "CREATE TABLE native_synonyms (curie TEXT COLLATE NOCASE, "
                "synonym TEXT COLLATE NOCASE, PRIMARY KEY (curie, synonym)) WITHOUT ROWID;"
            )
            terms: List[Tuple[str]] = []
            synonyms: List[Tuple[str, str]] = []

            def flush() -> None:
                self._connection.executemany("INSERT OR IGNORE INTO native_terms VALUES (?)", terms)
                self._connection.executemany(
                    "INSERT OR IGNORE INTO native_synonyms VALUES (?, ?)", synonyms
                )
                terms.clear()
                synonyms.clear()

            with self._connection:
                for path in sorted(self.source_dir.glob("*.json")):
                    with path.open("rb") as stream:
                        metadata = extract_ontology_metadata(stream, path.stem.lower())
                        prefixes = {
                            str(metadata[key]).strip().casefold()
                            for key in ("ontology", "prefix")
                            if metadata.get(key)
                        }
                        for kind in ("classes", "individuals"):
                            count_key = (
                                "num_of_classes" if kind == "classes" else "num_of_individuals"
                            )
                            if metadata.get(count_key) == 0:
                                continue
                            stream.seek(0)
                            for entity in ijson.items(stream, f"ontologies.item.{kind}.item"):
                                if not isinstance(entity, dict):
                                    continue
                                curie = (
                                    extract_first_str(entity.get("curie"))
                                    or extract_first_str(entity.get("shortForm"))
                                    or iri_to_curie(extract_first_str(entity.get("iri")) or "")
                                )
                                if (
                                    ":" not in curie
                                    or curie.split(":", 1)[0].casefold() not in prefixes
                                ):
                                    continue
                                terms.append((curie,))
                                synonyms.extend(
                                    (curie, value) for value in _searchable_synonyms(entity)
                                )
                                if len(terms) + len(synonyms) >= self.batch_size:
                                    flush()
                flush()
            return self
        except BaseException:
            self.close()
            raise

    def resolve(self, curie: str) -> Optional[Set[str]]:
        """Return None for absent native terms, or their possibly empty synonym set."""
        rows = self._connection.execute(
            "SELECT s.synonym FROM native_terms t LEFT JOIN native_synonyms s "
            "ON s.curie = t.curie WHERE t.curie = ?",
            (curie,),
        ).fetchall()
        if not rows:
            return None
        return {row[0] for row in rows if row[0] is not None}

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None

    def __exit__(self, *args) -> None:
        self.close()


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
                    lookup_prefix, lookup_number = local.split("_", 1)
                    lookup[f"{lookup_prefix}:{lookup_number}".lower()] = canonical
        if canonical.startswith(("http://", "https://")):
            lookup[canonical.lower()] = canonical
    return lookup


def extract_ontology_metadata(
    fp: Any,
    default_ontology: str,
) -> Dict[str, Any]:
    """Extract ontology short name, title, description, source URI, prefix, and version."""
    meta: Dict[str, Any] = {
        "ontology": default_ontology,
        "name": "",
        "description": "",
        "source_uri": "",
        "prefix": default_ontology.upper(),
        "version": "",
        "num_of_classes": None,
        "num_of_individuals": None,
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
                ) or (
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
                elif "numberofclasses" in prefix.lower() and event in (
                    "number",
                    "string",
                    "literal",
                ):
                    try:
                        meta["num_of_classes"] = int(value)
                    except ValueError:
                        pass
                    except TypeError:
                        pass
                elif "numberofindividuals" in prefix.lower() and event in (
                    "number",
                    "string",
                    "literal",
                ):
                    try:
                        meta["num_of_individuals"] = int(value)
                    except ValueError:
                        pass
                    except TypeError:
                        pass
                elif (
                    prefix
                    in (
                        "ontologies.item.classes.item",
                        "ontologies.item.individuals.item",
                    )
                    and event == "start_map"
                ):
                    break
        except Exception as ex:  # noqa: BLE001
            logger.warning("Error: %s", ex)
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

    version = meta["version"]
    if version.lower().startswith(("http://", "https://")):
        path_parts = [part for part in urlsplit(version).path.split("/") if part]
        if path_parts:
            meta["version"] = path_parts[-1]

    if not meta["name"]:
        meta["name"] = default_ontology.upper()

    return meta


def derive_iri_prefix(conn: sqlite3.Connection, ontology: str, preferred_prefix: str) -> str:
    """Derive common base IRI prefix (e.g. 'http://purl.obolibrary.org/obo/MS_', 'https://ror.org/')."""
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
            return min(prefixes, key=len)

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
    ontologies = [r[0] for r in cur.fetchall()]
    for ont in ontologies:
        cur.execute("SELECT COUNT(*) FROM ontologies WHERE ontology = ?", (ont,))
        if cur.fetchone()[0] == 0:
            cur.execute(
                "INSERT INTO ontologies (ontology, prefix) VALUES (?, ?)",
                (ont, ont.upper()),
            )
        update_ontology_stats(conn, ont)
    conn.commit()


class SssomOntologyLoader:
    """Stream SSSOM TSV mappings into xref tags for existing ontology terms."""

    def __init__(self, db_path: str, batch_size: Optional[int] = None) -> None:
        self.db_path = str(db_path)
        self.batch_size = (
            batch_size if batch_size is not None else DEFAULT_DATABASE_CONFIG.batch_size
        )
        if self.batch_size <= 0:
            raise ValueError("batch_size must be positive")

    @staticmethod
    def _ontology(curie: str) -> Optional[str]:
        """Use the CURIE prefix, rather than the source filename, as ontology."""
        prefix, separator, local_id = curie.partition(":")
        if (
            not separator
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*", prefix)
            or not local_id
            or local_id.startswith("//")
            or any(char.isspace() for char in local_id)
        ):
            return None
        return prefix.lower()

    def load_directory(self, source_dir: Union[str, Path] = ".cache/sssom") -> int:
        """Add bidirectional xrefs from all *.tsv files; return newly inserted tags.

        Accept owl:equivalentClass (and the rdfs:equivalentClass alias).
        oboInOwl:hasDbXref also requires a nonblank object_label and an installed
        object ontology. Each direction is inserted only if its owning term exists
        under the lowercased CURIE prefix. Missing terms are never manufactured.
        Repeated mappings/imports are ignored. Import after loading ontology JSONs;
        re-run after replacing ontologies to restore their xref tags.
        """
        directory = Path(source_dir)
        if not directory.is_dir():
            raise FileNotFoundError(f"SSSOM directory not found: {directory}")
        if not Path(self.db_path).is_file():
            raise FileNotFoundError(f"Ontology database not found: {self.db_path}")

        files = sorted(directory.glob("*.tsv"))
        conn = get_write_connection(self.db_path)
        total_inserted = 0
        affected_ontologies: Set[str] = set()
        batch: List[Tuple[str, str, str]] = []
        try:
            installed = {
                row[0].lower() for row in conn.execute("SELECT DISTINCT ontology FROM terms")
            }
            conn.execute(
                "CREATE TEMP TABLE sssom_xrefs (curie TEXT, ontology TEXT, tag_value TEXT)"
            )

            def flush() -> None:
                nonlocal total_inserted
                if not batch:
                    return
                conn.executemany("INSERT INTO sssom_xrefs VALUES (?, ?, ?)", batch)
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO term_details (curie, ontology, tag_key, tag_value) "
                    "SELECT t.curie, x.ontology, 'xref', x.tag_value FROM sssom_xrefs x "
                    "JOIN terms t ON t.curie = x.curie AND t.ontology = x.ontology"
                )
                total_inserted += cursor.rowcount
                conn.execute("DELETE FROM sssom_xrefs")
                batch.clear()

            with conn:
                for path in files:
                    with path.open(encoding="utf-8-sig", newline="") as stream:
                        reader = csv.DictReader(
                            (line for line in stream if not line.startswith("#")),
                            delimiter="\t",
                        )
                        required = {"subject_id", "predicate_id", "object_id"}
                        if not required.issubset(reader.fieldnames or []):
                            raise ValueError(f"Missing required SSSOM columns in {path}")
                        for row in reader:
                            predicate = (row.get("predicate_id") or "").strip()
                            if predicate not in {
                                "owl:equivalentClass",
                                "rdfs:equivalentClass",
                                "oboInOwl:hasDbXref",
                            }:
                                continue
                            if (
                                predicate == "oboInOwl:hasDbXref"
                                and not (row.get("object_label") or "").strip()
                            ):
                                continue
                            subject = (row.get("subject_id") or "").strip()
                            obj = (row.get("object_id") or "").strip()
                            subject_ontology = self._ontology(subject)
                            object_ontology = self._ontology(obj)
                            if not subject_ontology or not object_ontology:
                                continue
                            if predicate == "oboInOwl:hasDbXref" and (
                                object_ontology not in installed
                            ):
                                continue
                            batch.extend(
                                [(subject, subject_ontology, obj), (obj, object_ontology, subject)]
                            )
                            affected_ontologies.update((subject_ontology, object_ontology))
                            if len(batch) >= self.batch_size:
                                flush()
                flush()
                if total_inserted:
                    for ontology in sorted(affected_ontologies & installed):
                        conn.execute(
                            "UPDATE ontologies SET num_of_details = "
                            "(SELECT COUNT(*) FROM term_details WHERE ontology = ?) "
                            "WHERE ontology = ?",
                            (ontology, ontology),
                        )
                    conn.execute(
                        "UPDATE database_info SET "
                        "updated_time = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
                    )
            logger.info("Imported %d xref tags from %d SSSOM files", total_inserted, len(files))
            return total_inserted
        finally:
            conn.close()


class JsonOntologyLoader:
    """Streams and ingests ontology JSON files into SQLite database."""

    def __init__(
        self,
        db_path: str,
        target_parents: Optional[List[str]] = None,
        batch_size: Optional[int] = None,
        config: Any = None,
        native_synonym_index: Optional[NativeSynonymIndex] = None,
    ) -> None:
        config = config or DEFAULT_DATABASE_CONFIG
        self.db_path = str(db_path or config.database_path)
        self.target_parents: List[str] = (
            list(target_parents) if target_parents is not None else list(config.parent_terms)
        )
        self.batch_size = batch_size or config.batch_size
        self.native_synonym_index = native_synonym_index
        self.last_import_duration: Optional[float] = None

    def load_file(
        self,
        source: Optional[Union[str, Path, io.BytesIO, io.BufferedReader]],
        ontology_name: Optional[str] = None,
        target_parents: Optional[List[str]] = None,
        delete_existing: bool = True,
        clean_db: bool = False,
        sssom_dir: Optional[Union[str, Path]] = None,
        native_source_dir: Optional[Union[str, Path]] = None,
    ) -> Tuple[int, int]:
        """Import JSON using native synonym authority, logging elapsed seconds.

        native_source_dir defaults to the parent folder for file inputs. Streams
        without an explicit directory use source-local filtering. A shared index
        supplied to the loader is reused unless native_source_dir overrides it.
        Counts and schema are unchanged; optional SSSOM loading is included in timing.
        """
        started = time.perf_counter()
        name = ontology_name or (
            Path(source).stem if isinstance(source, (str, Path)) else "ontology"
        )
        try:
            with ExitStack() as stack:
                native_index = self.native_synonym_index
                if native_index is None or native_source_dir is not None:
                    directory = native_source_dir
                    if directory is None and isinstance(source, (str, Path)):
                        directory = Path(source).resolve().parent
                    if directory is not None:
                        native_index = stack.enter_context(
                            NativeSynonymIndex(directory, self.batch_size)
                        )
                counts = self._load_file(
                    source,
                    ontology_name,
                    target_parents,
                    delete_existing,
                    clean_db,
                    sssom_dir,
                    native_index,
                )
        except Exception:
            self.last_import_duration = time.perf_counter() - started
            logger.error(
                "[%s] Ontology import failed after %.3f seconds", name, self.last_import_duration
            )
            raise
        self.last_import_duration = time.perf_counter() - started
        logger.info(
            "[%s] Imported %d terms and %d details in %.3f seconds",
            name,
            *counts,
            self.last_import_duration,
        )
        return counts

    def _load_file(
        self,
        source: Optional[Union[str, Path, io.BytesIO, io.BufferedReader]],
        ontology_name: Optional[str] = None,
        target_parents: Optional[List[str]] = None,
        delete_existing: bool = True,
        clean_db: bool = False,
        sssom_dir: Optional[Union[str, Path]] = None,
        native_synonym_index: Optional[NativeSynonymIndex] = None,
    ) -> Tuple[int, int]:
        """Ingest ontology classes and individuals from a JSON file into SQLite.

        Args:
            source: File path (str/Path) or binary stream (BytesIO/BufferedReader).
            ontology_name: Ontology short name (e.g., 'ms', 'edam', 'ror'). If not supplied,
                           inferred from the filename stem.
            target_parents: Optional list of target parent CURIEs for recursive child-of links.
                            Defaults to self.target_parents.
            delete_existing: If True, deletes existing terms for this ontology before ingesting.
            clean_db: If True, resets the database file and initializes fresh schema.
            sssom_dir: Optional folder to re-import xrefs from after loading terms.

        Returns:
            Tuple of (terms_count, details_count).
        """
        if sssom_dir is not None and not Path(sssom_dir).is_dir():
            raise FileNotFoundError(f"SSSOM directory not found: {sssom_dir}")

        if clean_db and Path(self.db_path).exists():
            Path(self.db_path).unlink()

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
        fp: Optional[Union[io.BytesIO, io.BufferedReader]]
        if isinstance(source, (str, Path)):
            src_path = Path(source).resolve()
            if not src_path.exists():
                raise FileNotFoundError(f"Ontology JSON file not found: {src_path}")
            fp = src_path.open("rb")
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
        seen_curies: Set[str] = set()

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
            # Determine which entity arrays to stream (classes and/or individuals)
            entity_paths: List[Tuple[str, str]] = []
            if meta.get("num_of_classes") != 0:
                entity_paths.append(("class", "ontologies.item.classes.item"))
            if meta.get("num_of_individuals") != 0:
                entity_paths.append(("individual", "ontologies.item.individuals.item"))
            if not entity_paths:
                entity_paths = [
                    ("class", "ontologies.item.classes.item"),
                    ("individual", "ontologies.item.individuals.item"),
                ]

            for path_idx, (entity_kind, item_path) in enumerate(entity_paths):
                if path_idx > 0:
                    if hasattr(fp, "seekable") and fp.seekable():
                        fp.seek(0)
                    else:
                        logger.warning(
                            "Stream is not seekable; skipping additional entity path '%s'",
                            item_path,
                        )
                        break

                for entity in ijson.items(fp, item_path):
                    if not isinstance(entity, dict):
                        continue

                    iri = extract_first_str(entity.get("iri")) or ""
                    curie = (
                        extract_first_str(entity.get("curie"))
                        or extract_first_str(entity.get("shortForm"))
                        or iri_to_curie(iri)
                    )
                    if not curie:
                        continue

                    if curie in seen_curies:
                        continue
                    seen_curies.add(curie)

                    label = (
                        extract_first_str(entity.get("label"))
                        or extract_first_str(
                            entity.get("http://www.w3.org/2000/01/rdf-schema#label")
                        )
                        or curie
                    )

                    terms_batch.append((curie, iri, target_ontology, label))

                    if entity_kind == "individual":
                        details_batch.append((curie, target_ontology, "entity_type", "individual"))

                    # Synonyms
                    synonyms_set = (
                        native_synonym_index.resolve(curie)
                        if native_synonym_index is not None
                        else None
                    )
                    if synonyms_set is None:
                        synonyms_set = _searchable_synonyms(entity)
                    for syn in synonyms_set:
                        details_batch.append((curie, target_ontology, "synonym", syn))

                    # Definitions and descriptions
                    defs: Set[str] = set(extract_all_strs(entity.get("definition")))
                    defs.update(
                        extract_all_strs(entity.get("http://purl.obolibrary.org/obo/IAO_0000115"))
                    )
                    defs.update(
                        extract_all_strs(
                            entity.get("http://www.geneontology.org/formats/oboInOwl#hasDefinition")
                        )
                    )
                    defs.update(
                        extract_all_strs(entity.get("http://purl.org/dc/terms/description"))
                    )
                    defs.update(extract_all_strs(entity.get("description")))
                    for d in defs:
                        details_batch.append((curie, target_ontology, "definition", d))

                    # Obsolete status
                    is_obsolete = entity.get("isObsolete")
                    if is_obsolete is True:
                        details_batch.append((curie, target_ontology, "obsolete", "true"))
                    elif is_obsolete is False:
                        details_batch.append((curie, target_ontology, "obsolete", "false"))

                    # Leaf status: tag_key is_leaf with value '1' if term has no child
                    has_child_flag = (
                        entity.get("hasDirectChildren") is True
                        or entity.get("hasHierarchicalChildren") is True
                        or entity.get("hasChildren") is True
                    )
                    no_child_flag = (
                        entity.get("hasDirectChildren") is False
                        and entity.get("hasHierarchicalChildren") is False
                    ) or entity.get("hasChildren") is False
                    if has_child_flag or no_child_flag:
                        saw_child_flags = True
                        if no_child_flag:
                            details_batch.append((curie, target_ontology, "is_leaf", "1"))
                    else:
                        curies_in_file.append(curie)
                        ancestors_raw = (
                            extract_all_strs(entity.get("hierarchicalAncestor"))
                            + extract_all_strs(entity.get("directAncestor"))
                            + extract_all_strs(entity.get("hierarchicalParent"))
                            + extract_all_strs(entity.get("directParent"))
                        )
                        for a_raw in ancestors_raw:
                            clean_a = a_raw.strip().lower()
                            parent_refs.add(clean_a)
                            parent_refs.add(iri_to_curie(clean_a).strip().lower())

                    # Recursive Hierarchy: only add child-of links for the specified target parents
                    if parent_lookup:
                        ancestors = (
                            extract_all_strs(entity.get("hierarchicalAncestor"))
                            or extract_all_strs(entity.get("directAncestor"))
                            or extract_all_strs(entity.get("hierarchicalParent"))
                            or extract_all_strs(entity.get("directParent"))
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
                    synonyms_str = " ".join(synonyms_set)
                    defs_str = " ".join(defs)
                    fts_batch.append((curie, target_ontology, label, synonyms_str, defs_str))

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

        except Exception:
            conn.close()
            raise
        finally:
            if should_close:
                fp.close()

        cur.execute("PRAGMA optimize;")
        conn.commit()
        conn.close()

        enable_wal_mode(self.db_path)

        if sssom_dir is not None:
            SssomOntologyLoader(self.db_path, self.batch_size).load_directory(sssom_dir)
            conn = get_write_connection(self.db_path)
            try:
                total_details = conn.execute(
                    "SELECT COUNT(*) FROM term_details WHERE ontology = ?", (final_ont,)
                ).fetchone()[0]
            finally:
                conn.close()

        return total_terms, total_details
