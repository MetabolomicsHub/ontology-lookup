import io
import os
import shutil
import tarfile
from collections import Counter
from email.utils import parsedate_to_datetime
from http.client import IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

from ontology_lookup.manager import OntologyDatabaseManager


def archive_bytes(files: dict[str, bytes], symlink: bool = False) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in files.items():
            member = tarfile.TarInfo(name)
            if symlink:
                member.type = tarfile.SYMTYPE
                member.linkname = "../../outside"
            else:
                member.size = len(data)
            tar.addfile(member, io.BytesIO(data))
    return buffer.getvalue()


@pytest.fixture
def archive_server(monkeypatch):
    """Exercise real HEAD/GET requests against a small local OLS-like server."""
    state = {
        "/ontology_jsons.tgz": {
            "body": archive_bytes({"ontology_jsons/example.json": b'{"version": 1}'}),
            "ETag": '"json-v1"',
            "Last-Modified": "Wed, 28 Oct 2026 02:58:47 GMT",
        },
        "/sssom.tgz": {
            "body": archive_bytes({"example.sssom.tsv": b"subject_id\tpredicate_id\tobject_id\n"}),
            "ETag": '"sssom-v1"',
            "Last-Modified": "Wed, 28 Oct 2026 03:23:59 GMT",
        },
    }
    requests = Counter()

    class Handler(BaseHTTPRequestHandler):
        def do_HEAD(self):
            self.respond(False)

        def do_GET(self):
            self.respond(True)

        def respond(self, include_body):
            requests[self.command, self.path] += 1
            resource = state[self.path]
            self.send_response(resource.get(self.command + "_status", 200))
            for key in ("ETag", "Last-Modified"):
                if resource.get(key):
                    self.send_header(key, resource[key])
            self.send_header(
                "Content-Length", str(resource.get("content_length", len(resource["body"])))
            )
            self.end_headers()
            if include_body:
                self.wfile.write(resource["body"])

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    monkeypatch.setattr(
        "ontology_lookup.manager.OLS_ARCHIVE_BASE_URL",
        f"http://127.0.0.1:{server.server_port}",
    )
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield state, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_prepare_downloads_and_reuses_inputs(tmp_path: Path, archive_server):
    _, requests = archive_server
    manager = OntologyDatabaseManager(db_path=tmp_path / "unused.db")
    root = tmp_path / "inputs"
    result = manager.prepare_input_folders(root)
    assert (root / "ontology_jsons/example.json").read_bytes() == b'{"version": 1}'
    assert (root / "sssom/example.sssom.tsv").is_file()
    assert not (root / "sssom/sssom").exists()
    assert manager.default_input_dir == root / "ontology_jsons"
    assert manager.default_sssom_dir == root / "sssom"
    assert not (tmp_path / "unused.db").exists()
    for name in ("ontology_jsons", "sssom"):
        assert result[name] == {
            "archive": str(root / f"{name}.tgz"),
            "directory": str(root / name),
            "downloaded": True,
            "extracted": True,
        }
    mtime = (root / "ontology_jsons/example.json").stat().st_mtime_ns
    second = manager.prepare_input_folders(root)
    for name in ("ontology_jsons", "sssom"):
        assert second[name]["downloaded"] is False
        assert second[name]["extracted"] is False
        assert requests["HEAD", f"/{name}.tgz"] == 2
        assert requests["GET", f"/{name}.tgz"] == 1
    assert (root / "ontology_jsons/example.json").stat().st_mtime_ns == mtime


def test_prepare_replaces_changed_folder(tmp_path: Path, archive_server):
    state, requests = archive_server
    manager = OntologyDatabaseManager()
    manager.prepare_input_folders(tmp_path)
    (tmp_path / "ontology_jsons/stale.json").write_text("stale")
    state["/ontology_jsons.tgz"].update(
        body=archive_bytes({"ontology_jsons/new.json": b"new ontology"}), ETag='"json-v2"'
    )
    result = manager.prepare_input_folders(tmp_path)
    assert result["ontology_jsons"]["downloaded"] is True
    assert (tmp_path / "ontology_jsons/new.json").read_bytes() == b"new ontology"
    assert not (tmp_path / "ontology_jsons/stale.json").exists()
    assert not (tmp_path / "ontology_jsons/example.json").exists()
    assert requests["GET", "/sssom.tgz"] == 1


def test_prepare_detects_etag_change_with_same_size_and_date(tmp_path: Path, archive_server):
    state, requests = archive_server
    manager = OntologyDatabaseManager()
    manager.prepare_input_folders(tmp_path)
    state["/sssom.tgz"]["ETag"] = '"sssom-v2"'
    result = manager.prepare_input_folders(tmp_path)
    assert result["sssom"]["downloaded"] is True
    assert requests["GET", "/sssom.tgz"] == 2


def test_prepare_uses_last_modified_without_etag(tmp_path: Path, archive_server):
    state, requests = archive_server
    for resource in state.values():
        resource.pop("ETag")
    manager = OntologyDatabaseManager()
    manager.prepare_input_folders(tmp_path)
    assert manager.prepare_input_folders(tmp_path)["sssom"]["downloaded"] is False
    state["/sssom.tgz"]["Last-Modified"] = "Thu, 29 Oct 2026 03:23:59 GMT"
    assert manager.prepare_input_folders(tmp_path)["sssom"]["downloaded"] is True
    assert requests["GET", "/sssom.tgz"] == 2


def test_prepare_adopts_existing_archives(tmp_path: Path, archive_server):
    state, requests = archive_server
    for url, resource in state.items():
        path = tmp_path / url.lstrip("/")
        path.write_bytes(resource["body"])
        timestamp = parsedate_to_datetime(resource["Last-Modified"]).timestamp()
        os.utime(path, (timestamp, timestamp))
    result = OntologyDatabaseManager().prepare_input_folders(tmp_path)
    assert all(not entry["downloaded"] and entry["extracted"] for entry in result.values())
    assert not any(method == "GET" for method, _ in requests)


def test_prepare_restores_missing_folder_from_cache(tmp_path: Path, archive_server):
    _, requests = archive_server
    manager = OntologyDatabaseManager()
    manager.prepare_input_folders(tmp_path)
    shutil.rmtree(tmp_path / "sssom")
    result = manager.prepare_input_folders(tmp_path)
    assert result["sssom"]["extracted"] is True
    assert result["sssom"]["downloaded"] is False
    assert (tmp_path / "sssom/example.sssom.tsv").is_file()
    assert requests["GET", "/sssom.tgz"] == 1


@pytest.mark.parametrize(
    "failure", ["head", "http", "truncated", "invalid_tar", "traversal", "symlink"]
)
def test_prepare_failure_preserves_archive_and_only_deletes_after_download(
    tmp_path: Path, archive_server, failure
):
    state, _ = archive_server
    manager = OntologyDatabaseManager()
    manager.prepare_input_folders(tmp_path)
    archive = tmp_path / "ontology_jsons.tgz"
    previous = archive.read_bytes()
    resource = state["/ontology_jsons.tgz"]
    resource["ETag"] = '"broken"'
    if failure == "head":
        resource["HEAD_status"] = 503
    elif failure == "http":
        resource["GET_status"] = 503
    elif failure == "truncated":
        resource["content_length"] = len(resource["body"]) + 100
    elif failure == "invalid_tar":
        resource["body"] = b"not a gzip archive"
    elif failure == "traversal":
        resource["body"] = archive_bytes({"../outside": b"unsafe"})
    else:
        resource["body"] = archive_bytes({"ontology_jsons/link": b""}, symlink=True)
    with pytest.raises((OSError, tarfile.TarError, ValueError, IncompleteRead)):
        manager.prepare_input_folders(tmp_path)
    assert archive.read_bytes() == previous
    if failure in {"head", "http", "truncated"}:
        assert (tmp_path / "ontology_jsons/example.json").read_bytes() == b'{"version": 1}'
    else:
        assert not (tmp_path / "ontology_jsons").exists()
    assert (tmp_path / "sssom/example.sssom.tsv").is_file()
    assert not (tmp_path / "outside").exists()
    assert not list(tmp_path.glob(".prepare-*"))


@pytest.mark.parametrize("name", ["ontology_jsons", "sssom"])
def test_prepare_deletes_changed_folder_before_extraction(
    tmp_path: Path, archive_server, monkeypatch, name
):
    from ontology_lookup import manager as manager_module

    state, _ = archive_server
    manager = OntologyDatabaseManager()
    manager.prepare_input_folders(tmp_path)
    destination = tmp_path / name
    (destination / "stale-file").write_text("old input")
    state[f"/{name}.tgz"]["ETag"] = '"updated"'
    original = manager_module._extract_input_archive
    extracted = []

    def checked_extract(archive, prepared, folder_name):
        assert folder_name == name
        assert not destination.exists()
        extracted.append(folder_name)
        original(archive, prepared, folder_name)

    monkeypatch.setattr(manager_module, "_extract_input_archive", checked_extract)
    result = manager.prepare_input_folders(tmp_path)
    assert extracted == [name]
    assert result[name]["downloaded"] is True
    assert destination.is_dir()
    assert not (destination / "stale-file").exists()
    other = "sssom" if name == "ontology_jsons" else "ontology_jsons"
    assert result[other]["downloaded"] is False
    assert (tmp_path / other).is_dir()


def test_prepare_redownloads_locally_modified_archive(tmp_path: Path, archive_server):
    _, requests = archive_server
    manager = OntologyDatabaseManager()
    manager.prepare_input_folders(tmp_path)
    (tmp_path / "ontology_jsons.tgz").write_bytes(b"truncated")
    assert manager.prepare_input_folders(tmp_path)["ontology_jsons"]["downloaded"] is True
    assert requests["GET", "/ontology_jsons.tgz"] == 2


def test_prepare_without_validators_cannot_assume_freshness(tmp_path: Path, archive_server):
    state, requests = archive_server
    for resource in state.values():
        resource.pop("ETag")
        resource.pop("Last-Modified")
    manager = OntologyDatabaseManager()
    manager.prepare_input_folders(tmp_path)
    assert manager.prepare_input_folders(tmp_path)["sssom"]["downloaded"] is True
    assert requests["GET", "/sssom.tgz"] == 2


def test_prepare_rejects_invalid_timeout(tmp_path: Path):
    with pytest.raises(ValueError, match="timeout must be positive"):
        OntologyDatabaseManager().prepare_input_folders(tmp_path, timeout=0)
