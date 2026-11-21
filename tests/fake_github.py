"""A fake GitHub server for the v22 update tests.

Serves the three endpoints the updater uses — the latest release,
a commit's file list, and a tag's source archive — from small
crafted trees: a fake v22 install and a fake v23 release for the
updater unit tests, plus fake v28/v29 trees so the window-level
tests can run the same story one version up (the app itself is
v28 now, so its "newer release" is v29). The blob checksums are
computed here with hashlib, independently of the updates module
under test, so the verification checks prove the two agree
rather than the module agreeing with itself.

The server records every request path (server.requests), so a test
can prove a declined update downloaded nothing. Configure it
through start_fake_github: latest_tag ("v22", "v23", "v24",
"v25", "v26", "v27", "v28" or "v29"),
tamper (serve the latest archive with one file changed, while
the file list still names the original bytes — verification must
refuse it), no_commit (the release names no commit, so the pull
must refuse before downloading), and latest_body (the release
notes).
"""

import hashlib
import io
import json
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

V22_COMMIT = "a" * 40
V23_COMMIT = "b" * 40
V24_COMMIT = "c" * 40
V25_COMMIT = "d" * 40
V26_COMMIT = "e" * 40
V27_COMMIT = "f" * 40
V28_COMMIT = "1" * 40
V29_COMMIT = "2" * 40

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

FAKE_V24_FILES = {
    "ollama_system_test.py": (
        b'"""Fake OST v24."""\n__version__ = "24"\n'
    ),
    "new_file.txt": b"new in v24\n",
    "templates/old.json": b'{"id": "old"}',
    "README.md": b"v24 readme\n",
    "run.sh": b"#!/bin/sh\necho v24\n",
}

FAKE_V25_FILES = {
    "ollama_system_test.py": (
        b'"""Fake OST v25."""\n__version__ = "25"\n'
    ),
    "new_file.txt": b"new in v25\n",
    "templates/old.json": b'{"id": "old"}',
    "README.md": b"v25 readme\n",
    "run.sh": b"#!/bin/sh\necho v25\n",
}

FAKE_V26_FILES = {
    "ollama_system_test.py": (
        b'"""Fake OST v26."""\n__version__ = "26"\n'
    ),
    "new_file.txt": b"new in v26\n",
    "templates/old.json": b'{"id": "old"}',
    "README.md": b"v26 readme\n",
    "run.sh": b"#!/bin/sh\necho v26\n",
}

FAKE_V27_FILES = {
    "ollama_system_test.py": (
        b'"""Fake OST v27."""\n__version__ = "27"\n'
    ),
    "new_file.txt": b"new in v27\n",
    "templates/old.json": b'{"id": "old"}',
    "README.md": b"v27 readme\n",
    "run.sh": b"#!/bin/sh\necho v27\n",
}

FAKE_V28_FILES = {
    "ollama_system_test.py": (
        b'"""Fake OST v28."""\n__version__ = "28"\n'
    ),
    "new_file.txt": b"new in v28\n",
    "templates/old.json": b'{"id": "old"}',
    "README.md": b"v28 readme\n",
    "run.sh": b"#!/bin/sh\necho v28\n",
}

FAKE_V29_FILES = {
    "ollama_system_test.py": (
        b'"""Fake OST v29."""\n__version__ = "29"\n'
    ),
    "new_file.txt": b"new in v29\n",
    "templates/old.json": b'{"id": "old"}',
    "README.md": b"v29 readme\n",
    "run.sh": b"#!/bin/sh\necho v29\n",
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
            commit = {"v29": V29_COMMIT, "v28": V28_COMMIT,
                      "v27": V27_COMMIT,
                      "v26": V26_COMMIT,
                      "v25": V25_COMMIT,
                      "v24": V24_COMMIT, "v23": V23_COMMIT}.get(
                tag, V22_COMMIT)
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
            if sha in (V29_COMMIT, "v29"):
                self._send_json(build_tree(FAKE_V29_FILES))
            elif sha in (V28_COMMIT, "v28"):
                self._send_json(build_tree(FAKE_V28_FILES))
            elif sha in (V27_COMMIT, "v27"):
                self._send_json(build_tree(FAKE_V27_FILES))
            elif sha in (V26_COMMIT, "v26"):
                self._send_json(build_tree(FAKE_V26_FILES))
            elif sha in (V25_COMMIT, "v25"):
                self._send_json(build_tree(FAKE_V25_FILES))
            elif sha in (V24_COMMIT, "v24"):
                self._send_json(build_tree(FAKE_V24_FILES))
            elif sha in (V23_COMMIT, "v23"):
                self._send_json(build_tree(FAKE_V23_FILES))
            elif sha in (V22_COMMIT, "v22"):
                self._send_json(build_tree(FAKE_V22_FILES))
            else:
                self._send_json({"message": "Not Found"}, status=404)
            return
        if "/zipball/refs/tags/" in path:
            tag = path.rsplit("/zipball/refs/tags/", 1)[1]
            if tag == "v29":
                files = dict(FAKE_V29_FILES)
                if server.tamper:
                    files["README.md"] = b"tampered readme\n"
                self._send_bytes(build_zipball(files, V29_COMMIT))
            elif tag == "v28":
                files = dict(FAKE_V28_FILES)
                if server.tamper:
                    files["README.md"] = b"tampered readme\n"
                self._send_bytes(build_zipball(files, V28_COMMIT))
            elif tag == "v27":
                files = dict(FAKE_V27_FILES)
                if server.tamper:
                    files["README.md"] = b"tampered readme\n"
                self._send_bytes(build_zipball(files, V27_COMMIT))
            elif tag == "v26":
                files = dict(FAKE_V26_FILES)
                if server.tamper:
                    files["README.md"] = b"tampered readme\n"
                self._send_bytes(build_zipball(files, V26_COMMIT))
            elif tag == "v25":
                files = dict(FAKE_V25_FILES)
                if server.tamper:
                    files["README.md"] = b"tampered readme\n"
                self._send_bytes(build_zipball(files, V25_COMMIT))
            elif tag == "v24":
                files = dict(FAKE_V24_FILES)
                if server.tamper:
                    files["README.md"] = b"tampered readme\n"
                self._send_bytes(build_zipball(files, V24_COMMIT))
            elif tag == "v23":
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
