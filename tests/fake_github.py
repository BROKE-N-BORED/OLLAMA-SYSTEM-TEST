"""A fake GitHub server for the v22 update tests.

Serves the three endpoints the updater uses — the latest release,
a commit's file list, and a tag's source archive — from two small
crafted trees: a fake v22 install and a fake v23 release. The blob
checksums are computed here with hashlib, independently of the
updates module under test, so the verification checks prove the
two agree rather than the module agreeing with itself.

The server records every request path (server.requests), so a test
can prove a declined update downloaded nothing. Configure it
through start_fake_github: latest_tag ("v22" or "v23"), tamper
(serve a v23 archive with one file changed, while the file list
still names the original bytes — verification must refuse it),
no_commit (the release names no commit, so the pull must refuse
before downloading), and latest_body (the release notes).
"""

import hashlib
import io
import json
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

V22_COMMIT = "a" * 40
V23_COMMIT = "b" * 40

FAKE_V22_FILES = {
    "ollama_system_test.py": (
        b'"""Fake OST v22."""\n__version__ = "22"\n'
    ),
    "old_file.txt": b"this file is gone in v23\n",
    "templates/old.json": b'{"id": "old"}',
    "README.md": b"v22 readme\n",
    "run.sh": b"#!/bin/sh\necho v22\n",
}

FAKE_V23_FILES = {
    "ollama_system_test.py": (
        b'"""Fake OST v23."""\n__version__ = "23"\n'
    ),
    "new_file.txt": b"new in v23\n",
    "templates/old.json": b'{"id": "old"}',
    "README.md": b"v23 readme\n",
    "run.sh": b"#!/bin/sh\necho v23\n",
}

# run.sh is executable in the fake trees, like a real script file.
EXECUTABLE = {"run.sh"}


def git_blob_sha(data):
    """GitHub's checksum for a file's bytes, computed here so the
    test server does not share code with the module under test."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def build_tree(files):
    """A git/trees payload for a {path: bytes} mapping."""
    entries = []
    for path in sorted(files):
        entries.append({
            "path": path,
            "mode": "100755" if path in EXECUTABLE else "100644",
            "type": "blob",
            "sha": git_blob_sha(files[path]),
            "size": len(files[path]),
        })
    return {"sha": "tree", "tree": entries, "truncated": False}


def build_zipball(files, commit):
    """A codeload-style zipball: one top-level folder named after
    the repo and the short commit, holding the files."""
    top = "BROKE-N-BORED-OLLAMA-SYSTEM-TEST-%s" % commit[:7]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(files):
            archive.writestr("%s/%s" % (top, path), files[path])
    return buffer.getvalue()


class FakeGitHubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        server = self.server
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        server.requests.append(path)
        if path.endswith("/releases/latest"):
            tag = server.latest_tag
            commit = V23_COMMIT if tag == "v23" else V22_COMMIT
            payload = {
                "tag_name": tag,
                "html_url": (
                    "https://github.com/BROKE-N-BORED/"
                    "OLLAMA-SYSTEM-TEST/releases/tag/%s" % tag),
                "body": server.latest_body,
            }
            if not server.no_commit:
                payload["target_commitish"] = commit
            self._send_json(payload)
            return
        if "/git/trees/" in path:
            sha = path.rsplit("/git/trees/", 1)[1]
            if sha in (V23_COMMIT, "v23"):
                self._send_json(build_tree(FAKE_V23_FILES))
            elif sha in (V22_COMMIT, "v22"):
                self._send_json(build_tree(FAKE_V22_FILES))
            else:
                self._send_json({"message": "Not Found"}, status=404)
            return
        if "/zipball/refs/tags/" in path:
            tag = path.rsplit("/zipball/refs/tags/", 1)[1]
            if tag == "v23":
                files = dict(FAKE_V23_FILES)
                if server.tamper:
                    files["README.md"] = b"tampered readme\n"
                self._send_bytes(build_zipball(files, V23_COMMIT))
            elif tag == "v22":
                self._send_bytes(build_zipball(FAKE_V22_FILES, V22_COMMIT))
            else:
                self._send_json({"message": "Not Found"}, status=404)
            return
        self._send_json({"message": "Not Found"}, status=404)


class FakeGitHubServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, latest_tag="v23", tamper=False,
                 no_commit=False, latest_body="Fake v23 notes.\n"):
        super().__init__(address, FakeGitHubHandler)
        self.latest_tag = latest_tag
        self.tamper = tamper
        self.no_commit = no_commit
        self.latest_body = latest_body
        self.requests = []


def start_fake_github(latest_tag="v23", tamper=False, no_commit=False,
                      latest_body="Fake v23 notes.\n"):
    """Start the fake server on a free localhost port. Returns
    (server, port). The caller shuts it down."""
    server = FakeGitHubServer(
        ("127.0.0.1", 0), latest_tag=latest_tag, tamper=tamper,
        no_commit=no_commit, latest_body=latest_body)
    import threading
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]
