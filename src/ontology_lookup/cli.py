import argparse
import json
import os
import sys
from typing import List, Optional

import uvicorn

from ontology_lookup.api import create_app
from ontology_lookup.config import DEFAULT_DATABASE_CONFIG, DatabaseCreationConfig
from ontology_lookup.loader import JsonOntologyLoader
from ontology_lookup.manager import OntologyDatabaseManager


def parse_parents_arg(parents_str: Optional[str]) -> Optional[List[str]]:
    """Parse comma-separated parents string into a list of CURIEs."""
    if parents_str is None:
        return None
    return [p.strip() for p in parents_str.split(",") if p.strip()]


def config_from_args(args: argparse.Namespace) -> DatabaseCreationConfig:
    """Resolve database creation settings, applying CLI values over .env defaults."""
    parents = parse_parents_arg(getattr(args, "parents", None))
    return DatabaseCreationConfig(
        source_directory=getattr(args, "dir", None) or DEFAULT_DATABASE_CONFIG.source_directory,
        parent_terms=tuple(parents)
        if parents is not None
        else DEFAULT_DATABASE_CONFIG.parent_terms,
        database_path=getattr(args, "db", None) or DEFAULT_DATABASE_CONFIG.database_path,
        batch_size=getattr(args, "batch_size", None) or DEFAULT_DATABASE_CONFIG.batch_size,
    )


def build_command(args: argparse.Namespace) -> None:
    """Execute single JSON file ingestion."""
    config = config_from_args(args)
    print(f"Starting ingestion from JSON source: {args.source}")
    print(f"Target database: {args.db}")

    parents = parse_parents_arg(args.parents)
    loader = JsonOntologyLoader(
        db_path=str(config.database_path),
        target_parents=parents if parents is not None else list(config.parent_terms),
        batch_size=config.batch_size,
        config=config,
    )
    terms_count, details_count = loader.load_file(
        source=args.source,
        ontology_name=args.ontology,
        target_parents=parents,
        clean_db=args.clean,
    )
    msg = (
        f"Ingestion completed successfully! Ingested {terms_count} terms "
        f"and {details_count} details."
    )
    print(msg)


def init_db_command(args: argparse.Namespace) -> None:
    """Initialize or recreate database by processing JSON files in the source directory."""
    ontologies: Optional[List[str]] = None
    if args.ontologies:
        ontologies = [o.strip() for o in args.ontologies.split(",") if o.strip()]

    parents = parse_parents_arg(args.parents)
    config = config_from_args(args)
    source_dir = args.dir or config.source_directory
    mgr = OntologyDatabaseManager(
        config=config,
        db_path=str(config.database_path),
        default_input_dir=source_dir,
        target_parents=parents if parents is not None else list(config.parent_terms),
        batch_size=config.batch_size,
    )
    print(f"Initializing database '{args.db}' from directory '{source_dir}'...")
    results = mgr.load_directory(
        source_dir=source_dir,
        ontologies=ontologies,
        target_parents=parents,
        clean=args.clean,
    )
    print(f"Database population finished across {len(results)} ontologies.")
    for ont, counts in results.items():
        print(f" - {ont}: {counts['terms']} terms, {counts['details']} details")


def update_ontology_command(args: argparse.Namespace) -> None:
    """Add or update an individual ontology within the database from JSON."""
    config = config_from_args(args)
    source_dir = args.dir or config.source_directory
    parents = parse_parents_arg(args.parents)
    mgr = OntologyDatabaseManager(
        config=config,
        db_path=str(config.database_path),
        default_input_dir=source_dir,
        target_parents=parents,
    )
    print(f"Updating ontology '{args.name}' in '{args.db}'...")
    terms, details = mgr.update_ontology(
        ontology_name=args.name,
        source_dir=source_dir,
        file_path=args.source,
        target_parents=parents,
    )
    print(f"Successfully updated '{args.name}': {terms} terms, {details} details.")


def delete_ontology_command(args: argparse.Namespace) -> None:
    """Delete an ontology and its terms/details from the database."""
    config = config_from_args(args)
    mgr = OntologyDatabaseManager(config=config, db_path=str(config.database_path))
    print(f"Deleting ontology '{args.name}' from '{config.database_path}'...")
    deleted = mgr.delete_ontology(args.name)
    if deleted > 0:
        print(f"Successfully deleted '{args.name}' ({deleted} terms removed).")
    else:
        print(f"Ontology '{args.name}' not found or had no terms.")


def add_tag_command(args: argparse.Namespace) -> None:
    """Add a key-value tag to a term."""
    config = config_from_args(args)
    mgr = OntologyDatabaseManager(config=config, db_path=str(config.database_path))
    success = mgr.add_term_tag(
        curie=args.curie,
        tag_key=args.key,
        tag_value=args.value,
        ontology=args.ontology,
    )
    if success:
        ont_str = f" in ontology '{args.ontology}'" if args.ontology else ""
        print(f"Added tag '{args.key}={args.value}' to term '{args.curie}'{ont_str}.")
    else:
        print(f"Failed to add tag to term '{args.curie}'. Term may not exist.")


def update_tag_command(args: argparse.Namespace) -> None:
    """Update a tag value for a term."""
    config = config_from_args(args)
    mgr = OntologyDatabaseManager(config=config, db_path=str(config.database_path))
    success = mgr.update_term_tag(
        curie=args.curie,
        tag_key=args.key,
        new_value=args.value,
        old_value=args.old_value,
        ontology=args.ontology,
    )
    if success:
        ont_str = f" in ontology '{args.ontology}'" if args.ontology else ""
        print(f"Updated tag '{args.key}' for term '{args.curie}' to '{args.value}'{ont_str}.")
    else:
        print(f"No matching tag found to update for term '{args.curie}'.")


def delete_tag_command(args: argparse.Namespace) -> None:
    """Delete tag(s) from a term."""
    config = config_from_args(args)
    mgr = OntologyDatabaseManager(config=config, db_path=str(config.database_path))
    deleted = mgr.delete_term_tag(
        curie=args.curie,
        tag_key=args.key,
        tag_value=args.value,
        ontology=args.ontology,
    )
    if deleted > 0:
        ont_str = f" in ontology '{args.ontology}'" if args.ontology else ""
        print(f"Deleted {deleted} tag(s) '{args.key}' from term '{args.curie}'{ont_str}.")
    else:
        print(f"No matching tag found to delete for term '{args.curie}'.")


def list_ontologies_command(args: argparse.Namespace) -> None:
    """List installed ontologies in the database and available JSON files in directory."""
    config = config_from_args(args)
    source_dir = args.dir or config.source_directory
    mgr = OntologyDatabaseManager(
        config=config, db_path=str(config.database_path), default_input_dir=source_dir
    )

    db_info = mgr.get_database_info()
    if db_info.get("created_time"):
        creator_str = f" by {db_info['created_by']}" if db_info.get("created_by") else ""
        print(f"Database: '{args.db}' (created: {db_info['created_time']}{creator_str})")
        if db_info.get("updated_time"):
            print(f"Last updated: {db_info['updated_time']}")
        print()

    installed = mgr.list_installed_ontologies()
    if not installed:
        print(f"No ontologies found installed in database '{args.db}'.")
    else:
        print(f"Installed ontologies in '{args.db}':")
        print(
            f"{'Ontology':<10} {'Prefix':<8} {'Version':<18} {'Terms':<8} "
            f"{'Obs':<6} {'Details':<9} {'IRI Prefix':<38} {'Name':<30}"
        )
        print("-" * 130)
        for item in installed:
            name = (item.get("name") or item.get("description") or "").replace("\n", " ")
            if len(name) > 28:
                name = name[:25] + "..."
            prefix = item.get("prefix") or item["ontology"].upper()
            version = item.get("version") or "-"
            iri_prefix = item.get("iri_prefix") or "-"
            num_terms = item.get("num_of_terms", item.get("term_count", 0))
            num_obs = item.get("num_of_obsoletes", 0)
            num_details = item.get("num_of_details", item.get("detail_count", 0))
            print(
                f"{item['ontology']:<10} {prefix:<8} {version:<18} {num_terms:<8} "
                f"{num_obs:<6} {num_details:<9} {iri_prefix:<38} {name:<30}"
            )

    if args.available:
        available = mgr.list_available_ontologies(source_dir=source_dir)
        print(f"\nAvailable JSON ontologies in '{source_dir}': ({len(available)} total)")
        print(f"{'Ontology':<15} {'File':<25} {'Size (MB)':<10}")
        print("-" * 52)
        for item in available[:30]:
            print(f"{item['ontology']:<15} {item['filename']:<25} {item['size_mb']:<10.2f}")
        if len(available) > 30:
            print(f"... and {len(available) - 30} more.")


def freetext_search_command(args: argparse.Namespace) -> None:
    """Execute freetext search across ontology labels, synonyms, and definitions."""
    from ontology_lookup.service import OntologyLookupService

    ontologies = [o.strip() for o in args.ontology.split(",")] if args.ontology else None
    parents = parse_parents_arg(args.parent)
    with OntologyLookupService(db_path=args.db) as svc:
        results = svc.freetext_search(
            query=args.query,
            ontology=ontologies,
            parent_curie=parents,
            limit=args.limit,
            offset=args.offset,
        )
        if not results:
            print(f"No results found for query: '{args.query}'")
        else:
            print(f"Found {len(results)} match(es) for '{args.query}':")
            print(f"{'CURIE':<15} {'Ontology':<10} {'Label':<40} {'Rank':<8}")
            print("-" * 75)
            for r in results:
                rank_str = f"{r.rank:.2f}" if r.rank is not None else "N/A"
                print(f"{r.curie:<15} {r.ontology:<10} {r.label:<40} {rank_str:<8}")


def lookup_command(args: argparse.Namespace) -> None:
    """Run a read-only OntologyLookupService use case and print JSON."""
    from ontology_lookup.service import OntologyLookupService

    with OntologyLookupService(db_path=args.db) as svc:
        if args.command == "get-term-by-accession":
            result = svc.get_term_by_accession(args.ontology, args.accession)
        elif args.command in ("get-term-by-label"):
            result = svc.get_term_by_exact_label(args.ontology, args.label)
        elif args.command in ("freetext-search"):
            result = svc.freetext_search(
                args.query,
                args.ontology,
                parent_curie=parse_parents_arg(args.parent),
                limit=args.limit,
                offset=args.offset,
            )
        elif args.command in ("search-by-label",):
            result = svc.search_by_label(
                label_or_synonym=args.label_or_synonym,
                ontology=args.ontology,
                parent_curie=parse_parents_arg(args.parent),
                search_in_synonyms=args.search_in_synonyms,
                limit=args.limit,
                offset=args.offset,
            )
        elif args.command == "curie-to-iri":
            result = svc.find_iri(args.curie, args.ontology)
        elif args.command in ("iri-to-curie",):
            result = svc.find_curie(args.iri, args.ontology)
        elif args.command == "search-by-tag":
            result = svc.search_by_tag(
                args.tag_key, args.tag_value, args.ontology, args.limit, args.offset
            )
        elif args.command == "get-children":
            result = svc.get_children(args.parent, args.ontology, args.limit, args.offset)
        elif args.command == "list-ontologies":
            result = svc.list_ontologies()
        elif args.command == "list-parent-terms":
            result = svc.get_indexed_parent_terms()
        elif args.command == "get-ontology":
            result = svc.get_ontology(args.ontology)
        elif args.command == "health":
            result = svc.get_health()
        else:
            result = svc.get_database_info()

    if result is None:
        print("No result found.")
    elif isinstance(result, list):
        if result and hasattr(result[0], "model_dump"):
            result = [item.model_dump() for item in result]
        print(json.dumps(result, indent=2))
    else:
        print(result.model_dump_json(indent=2))


def serve_command(args: argparse.Namespace) -> None:
    """Start the FastAPI server using Uvicorn."""
    os.environ["ONTOLOGY_DB_PATH"] = args.db
    print(f"Serving Ontology Lookup Service with database '{args.db}' on {args.host}:{args.port}")
    app = create_app(db_path=args.db)
    uvicorn.run(app, host=args.host, port=args.port)


def create_parser() -> argparse.ArgumentParser:
    """Build the command-line argument parser."""
    parser = argparse.ArgumentParser(
        prog="ontology-lookup",
        description="Local ontology lookup engine and multi-ontology manager.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    default_parents_str = ",".join(DEFAULT_DATABASE_CONFIG.parent_terms)
    admin_parser = subparsers.add_parser("admin", help="Create or update the database")
    admin_subparsers = admin_parser.add_subparsers(dest="admin_command", required=True)

    # 1. build (single source JSON)
    build_parser = admin_subparsers.add_parser(
        "build", help="Build SQLite database from ontology JSON"
    )
    build_parser.add_argument(
        "--source",
        "-s",
        required=True,
        help="Ontology JSON file path",
    )
    build_parser.add_argument(
        "--db",
        "-d",
        default=str(DEFAULT_DATABASE_CONFIG.database_path),
        help=f"Target SQLite database path (default: {DEFAULT_DATABASE_CONFIG.database_path})",
    )
    build_parser.add_argument(
        "--ontology",
        "-o",
        default=None,
        help="Ontology identifier override (default: inferred from filename stem)",
    )
    build_parser.add_argument(
        "--parents",
        "-p",
        default=default_parents_str,
        help=f"Target parent CURIEs for recursive child-of links (default: {default_parents_str})",
    )
    build_parser.add_argument(
        "--batch-size",
        "-b",
        type=int,
        default=DEFAULT_DATABASE_CONFIG.batch_size,
        help=f"Batch size for database inserts (default: {DEFAULT_DATABASE_CONFIG.batch_size})",
    )
    build_parser.add_argument(
        "--clean",
        action="store_true",
        help="Remove existing database file before building",
    )

    # 2. init-db (directory-based multi-ontology build)
    init_parser = admin_subparsers.add_parser(
        "init-db",
        help="Initialize database from directory of JSON files",
    )
    init_parser.add_argument(
        "--dir",
        "-i",
        default=str(DEFAULT_DATABASE_CONFIG.source_directory),
        help=(
            "Directory containing ontology JSON files "
            f"(default: {DEFAULT_DATABASE_CONFIG.source_directory})"
        ),
    )
    init_parser.add_argument(
        "--db",
        "-d",
        default=str(DEFAULT_DATABASE_CONFIG.database_path),
        help=f"Target SQLite database path (default: {DEFAULT_DATABASE_CONFIG.database_path})",
    )
    init_parser.add_argument(
        "--ontologies",
        "-o",
        default=None,
        help="Comma-separated list of ontology short names to ingest (e.g. 'ms,edam,obi')",
    )
    init_parser.add_argument(
        "--parents",
        "-p",
        default=default_parents_str,
        help=f"Target parent CURIEs for recursive child-of links (default: {default_parents_str})",
    )
    init_parser.add_argument(
        "--clean",
        action="store_true",
        help="Clean/recreate database from scratch",
    )
    init_parser.add_argument(
        "--batch-size",
        "-b",
        type=int,
        default=DEFAULT_DATABASE_CONFIG.batch_size,
        help="Batch size for database inserts",
    )

    # 3. update-ontology
    upd_parser = admin_subparsers.add_parser(
        "update-ontology", help="Add or update a single ontology from JSON"
    )
    upd_parser.add_argument(
        "--name",
        "-n",
        required=True,
        help="Ontology short name (e.g., 'ms', 'edam', 'obi')",
    )
    upd_parser.add_argument(
        "--dir",
        "-i",
        default=str(DEFAULT_DATABASE_CONFIG.source_directory),
        help=(
            "Source directory containing JSON files "
            f"(default: {DEFAULT_DATABASE_CONFIG.source_directory})"
        ),
    )
    upd_parser.add_argument(
        "--source",
        "-s",
        default=None,
        help="Optional explicit JSON file path override",
    )
    upd_parser.add_argument(
        "--parents",
        "-p",
        default=default_parents_str,
        help="Optional target parent CURIEs for recursive child-of links",
    )
    upd_parser.add_argument(
        "--db",
        "-d",
        default=str(DEFAULT_DATABASE_CONFIG.database_path),
        help="Target SQLite database path",
    )

    # 4. list-ontologies
    list_parser = subparsers.add_parser(
        "list-ontologies", help="List installed ontologies in the database"
    )
    list_parser.add_argument(
        "--db",
        "-d",
        default=str(DEFAULT_DATABASE_CONFIG.database_path),
        help="Target SQLite database path",
    )
    list_parser.add_argument(
        "--dir",
        "-i",
        default=str(DEFAULT_DATABASE_CONFIG.source_directory),
        help="Source directory containing JSON files",
    )
    list_parser.add_argument(
        "--available",
        "-a",
        action="store_true",
        help="Also list available JSON files in the source directory",
    )

    # 5. freetext-search
    fts_parser = subparsers.add_parser(
        "freetext-search",
        help="Search across labels, synonyms, and definitions",
    )
    fts_parser.add_argument(
        "--query",
        "-q",
        required=True,
        help="Search query string",
    )
    fts_parser.add_argument(
        "--ontology",
        "-o",
        default=None,
        help="Optional comma-separated ontology filter",
    )
    fts_parser.add_argument(
        "--parent",
        "-p",
        default=None,
        help="Optional parent CURIE filter",
    )
    fts_parser.add_argument(
        "--limit",
        "-l",
        type=int,
        default=50,
        help="Max results to return (default: 50)",
    )
    fts_parser.add_argument("--offset", type=int, default=0, help="Number of results to skip")
    fts_parser.add_argument(
        "--db",
        "-d",
        default=str(DEFAULT_DATABASE_CONFIG.database_path),
        help="Target SQLite database path",
    )
    fts_parser.set_defaults(command="freetext-search")

    # Direct read-only commands for OntologyLookupService use cases.
    def add_db_argument(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument(
            "--db",
            "-d",
            default=str(DEFAULT_DATABASE_CONFIG.database_path),
            help="SQLite database path",
        )

    term_parser = subparsers.add_parser(
        "get-term-by-accession",
        help="Look up a term by ontology and CURIE or IRI",
    )
    term_parser.add_argument("--ontology", "-o", required=True, help="Ontology short name")
    term_parser.add_argument("--accession", "-a", required=True, help="Term CURIE or full IRI")
    add_db_argument(term_parser)
    term_parser.set_defaults(command="get-term-by-accession")

    label_parser = subparsers.add_parser(
        "get-term-by-label",
        help="Look up a term by exact label",
    )
    label_parser.add_argument("--ontology", "-o", required=True, help="Ontology short name")
    label_parser.add_argument("--label", "-l", required=True, help="Exact term label")
    add_db_argument(label_parser)
    label_parser.set_defaults(command="get-term-by-label")

    search_parser = subparsers.add_parser(
        "search-by-label",
        help="Exact match on labels and synonyms",
    )
    search_parser.add_argument(
        "--label-or-synonym",
        required=True,
        help="Complete label or synonym to match",
    )
    search_parser.add_argument(
        "--ontology", "-o", default=None, help="Ontology filter (comma-separated)"
    )
    search_parser.add_argument(
        "--parent", "-p", default=None, help="Parent CURIE filter (comma-separated)"
    )
    search_parser.add_argument(
        "--search-in-synonyms",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Include exact synonym matches (default: enabled)",
    )
    search_parser.add_argument("--limit", "-l", type=int, default=50, help="Maximum results")
    search_parser.add_argument("--offset", type=int, default=0, help="Result offset")
    add_db_argument(search_parser)

    curie_parser = subparsers.add_parser(
        "iri-to-curie",
        help="Resolve an IRI to its CURIE",
    )
    curie_parser.add_argument("--iri", required=True, help="Full term IRI")
    curie_parser.add_argument("--ontology", "-o", help="Ontology short name")
    add_db_argument(curie_parser)
    curie_parser.set_defaults(command="iri-to-curie")

    iri_parser = subparsers.add_parser("curie-to-iri", help="Resolve a CURIE to its IRI")
    iri_parser.add_argument("--curie", required=True, help="Term CURIE")
    iri_parser.add_argument("--ontology", "-o", default=None, help="Optional ontology filter")
    add_db_argument(iri_parser)
    iri_parser.set_defaults(command="curie-to-iri")

    tag_parser = subparsers.add_parser("search-by-tag", help="Search terms by a tag key and value")
    tag_parser.add_argument("--tag-key", required=True, help="Tag key")
    tag_parser.add_argument("--tag-value", required=True, help="Tag value")
    tag_parser.add_argument("--ontology", "-o", default=None, help="Optional ontology filter")
    tag_parser.add_argument("--limit", "-l", type=int, default=50, help="Maximum results")
    tag_parser.add_argument("--offset", type=int, default=0, help="Result offset")
    add_db_argument(tag_parser)

    children_parser = subparsers.add_parser(
        "get-children", help="Find descendants of a parent CURIE"
    )
    children_parser.add_argument("--parent", "-p", required=True, help="Parent CURIE")
    children_parser.add_argument("--ontology", "-o", default=None, help="Optional ontology filter")
    children_parser.add_argument("--limit", "-l", type=int, default=50, help="Maximum results")
    children_parser.add_argument("--offset", type=int, default=0, help="Result offset")
    add_db_argument(children_parser)

    parent_terms_parser = subparsers.add_parser(
        "list-parent-terms", help="List indexed hierarchy parent CURIEs"
    )
    add_db_argument(parent_terms_parser)

    ontology_parser = subparsers.add_parser("get-ontology", help="Show metadata for one ontology")
    ontology_parser.add_argument("--ontology", "-o", required=True, help="Ontology short name")
    add_db_argument(ontology_parser)

    health_parser = subparsers.add_parser("health", help="Report database health and term count")
    add_db_argument(health_parser)
    info_parser = subparsers.add_parser(
        "database-info", help="Show database creation and update metadata"
    )
    add_db_argument(info_parser)

    # 6. serve
    serve_parser = subparsers.add_parser("serve", help="Run the FastAPI lookup server")
    serve_parser.add_argument(
        "--db",
        "-d",
        default=str(DEFAULT_DATABASE_CONFIG.database_path),
        help=f"SQLite database path (default: {DEFAULT_DATABASE_CONFIG.database_path})",
    )
    serve_parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host address to bind (default: 0.0.0.0)",
    )
    serve_parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port to listen on (default: 8000)",
    )

    # 7. delete-ontology
    del_ont_parser = admin_subparsers.add_parser(
        "delete-ontology", help="Completely remove an ontology and all its terms"
    )
    del_ont_parser.add_argument(
        "--name",
        "-n",
        required=True,
        help="Ontology short name to delete (e.g. 'ms', 'edam')",
    )
    del_ont_parser.add_argument(
        "--db",
        "-d",
        default=str(DEFAULT_DATABASE_CONFIG.database_path),
        help="Target SQLite database path",
    )

    # 8. add-tag
    add_tag_p = admin_subparsers.add_parser("add-tag", help="Add a key-value tag to a term")
    add_tag_p.add_argument("--curie", "-c", required=True, help="Term CURIE (e.g. 'MS:1000031')")
    add_tag_p.add_argument("--key", "-k", required=True, help="Tag key (e.g. 'custom_group')")
    add_tag_p.add_argument("--value", "-v", required=True, help="Tag value")
    add_tag_p.add_argument(
        "--ontology",
        "-o",
        default=None,
        help="Optional ontology short name to scope the tag operation",
    )
    add_tag_p.add_argument(
        "--db",
        "-d",
        default=str(DEFAULT_DATABASE_CONFIG.database_path),
        help="Target SQLite database path",
    )

    # 9. update-tag
    upd_tag_p = admin_subparsers.add_parser("update-tag", help="Update a tag value for a term")
    upd_tag_p.add_argument("--curie", "-c", required=True, help="Term CURIE (e.g. 'MS:1000031')")
    upd_tag_p.add_argument("--key", "-k", required=True, help="Tag key to update")
    upd_tag_p.add_argument("--value", "-v", required=True, help="New tag value")
    upd_tag_p.add_argument(
        "--old-value",
        default=None,
        help="Optional existing tag value to target specifically",
    )
    upd_tag_p.add_argument(
        "--ontology",
        "-o",
        default=None,
        help="Optional ontology short name to scope the tag operation",
    )
    upd_tag_p.add_argument(
        "--db",
        "-d",
        default=str(DEFAULT_DATABASE_CONFIG.database_path),
        help="Target SQLite database path",
    )

    # 10. delete-tag
    del_tag_p = admin_subparsers.add_parser("delete-tag", help="Delete tag(s) from a term")
    del_tag_p.add_argument("--curie", "-c", required=True, help="Term CURIE (e.g. 'MS:1000031')")
    del_tag_p.add_argument("--key", "-k", required=True, help="Tag key to delete")
    del_tag_p.add_argument(
        "--value", "-v", default=None, help="Optional specific tag value to delete"
    )
    del_tag_p.add_argument(
        "--ontology",
        "-o",
        default=None,
        help="Optional ontology short name to scope the tag operation",
    )
    del_tag_p.add_argument(
        "--db",
        "-d",
        default=str(DEFAULT_DATABASE_CONFIG.database_path),
        help="Target SQLite database path",
    )

    # Keep both help listings stable and easy to scan regardless of where each
    # command's arguments are defined above.
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            ordered_names = sorted(
                action.choices,
                key=lambda name: (name != "admin", name),
            )
            action.choices = {name: action.choices[name] for name in ordered_names}
            action._choices_actions.sort(
                key=lambda choice_action: (choice_action.dest != "admin", choice_action.dest)
            )
            for child_parser in action.choices.values():
                for child_action in child_parser._actions:
                    if isinstance(child_action, argparse._SubParsersAction):
                        child_names = sorted(child_action.choices)
                        child_action.choices = {
                            name: child_action.choices[name] for name in child_names
                        }
                        child_action._choices_actions.sort(key=lambda item: item.dest)

    return parser


def main(argv: Optional[List[str]] = None) -> None:
    """Entry point for CLI execution."""
    parser = create_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    if args.command == "admin":
        if args.admin_command == "build":
            build_command(args)
        elif args.admin_command == "init-db":
            init_db_command(args)
        elif args.admin_command == "update-ontology":
            update_ontology_command(args)
        elif args.admin_command == "delete-ontology":
            delete_ontology_command(args)
        elif args.admin_command == "add-tag":
            add_tag_command(args)
        elif args.admin_command == "update-tag":
            update_tag_command(args)
        elif args.admin_command == "delete-tag":
            delete_tag_command(args)
    elif args.command == "list-ontologies":
        list_ontologies_command(args)
    elif args.command == "freetext-search":
        freetext_search_command(args)
    elif args.command in {
        "get-term-by-accession",
        "get-term-by-label",
        "search-by-label",
        "iri-to-curie",
        "curie-to-iri",
        "search-by-tag",
        "get-children",
        "get-ontology",
        "health",
        "database-info",
        "list-parent-terms",
    }:
        lookup_command(args)
    elif args.command == "serve":
        serve_command(args)


if __name__ == "__main__":
    main()
