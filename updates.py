"""Silent update check + verified pull (v22) — Qt-free, stdlib-only.

OLLAMA SYSTEM TEST checks GitHub for a newer release when it opens,
silently: up to date or unreachable means nothing happens at all. When
a newer release is out, the window asks; only on the user's tap does
the app pull it. The pull is never a blind download: the release's
file list comes from GitHub's API, the source archive is downloaded,
and every extracted file's git blob checksum is compared against that
list before anything is installed. A download that does not match
installs nothing.

The shape follows Pixel Forge v0.69's updates.py (one-call check,
three outcomes, never an exception for a network problem), extended
with the pull half Pixel Forge does not have: download, verify,
install, restart.

check_for_update is the one-call API the GUI worker uses. pull_update
is the one-call API the pull worker uses. Both take urlopen as a test
seam — pass a stub and no network is touched — and the base URLs are
module constants the suite points at a local fake server.
"""

import hashlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile

REPO_OWNER = "BROKE-N-BORED"
REPO_NAME = "OLLAMA-SYSTEM-TEST"
API_BASE = "https://api.github.com"
CODELOAD_BASE = "https://codeload.github.com"
CHECK_TIMEOUT_S = 8
TREE_TIMEOUT_S = 8
DOWNLOAD_TIMEOUT_S = 120
DOWNLOAD_CHUNK = 65536

# Top-level names in the install folder that an update never touches:
# the user's results and logs, their saved providers and capability
# cache, and the private folder holding PySide6.
PRESERVE_NAMES = (
    "results", "logs", ".venv", "providers.json", "capabilities.json",
)

# A file the install folder must hold before anything is installed
# into it — the guard that stops an update landing in the wrong
# folder.
APP_ENTRY = "ollama_system_test.py"


class UpdateError(Exception):
    """A plain-words reason an update step could not run."""


_VERSION_RE = re.compile(r"^v?(\d+(?:\.\d+)*)$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def parse_version(text):
    """A version string as a tuple of ints — "22" becomes (22,),
    "v22" the same — or None when it is not a version at all.
    Anything but an optional "v" followed by numbers and dots is
    refused, so a tag like "v22-beta" reads as no usable version
    rather than a wrong one."""
    if not isinstance(text, str):
        return None
    match = _VERSION_RE.match(text.strip())
    if match is None:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def is_newer(latest, current):
    """True when the latest version is newer than the running one.
    Either side unparsable means no — an unknown version is never
    reported as an update. Shorter tuples are padded with zeros,
    so 22 and 22.0 compare equal."""
    latest_parsed = parse_version(latest)
    current_parsed = parse_version(current)
    if latest_parsed is None or current_parsed is None:
        return False
    width = max(len(latest_parsed), len(current_parsed))
    latest_padded = latest_parsed + (0,) * (width - len(latest_parsed))
    current_padded = current_parsed + (0,) * (width - len(current_parsed))
    return latest_padded > current_padded


def latest_release_url():
    return (
        f"{API_BASE}/repos/{REPO_OWNER}/{REPO_NAME}/releases/latest"
    )


def tree_url(commit):
    return (
        f"{API_BASE}/repos/{REPO_OWNER}/{REPO_NAME}/git/trees/"
        f"{commit}?recursive=1"
    )


def zipball_url(tag):
    return (
        f"{CODELOAD_BASE}/{REPO_OWNER}/{REPO_NAME}/zipball/"
        f"refs/tags/{tag}"
    )


def _read_json(url, timeout, urlopen):
    """GET a URL and parse the JSON answer. Raises UpdateError
    with a plain reason on any network or parsing problem."""
    opener = urlopen or urllib.request.urlopen
    try:
        with opener(url, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, TimeoutError):
            raise UpdateError("the update check timed out")
        raise UpdateError("could not reach GitHub: %s" % reason)
    except TimeoutError:
        raise UpdateError("the update check timed out")
    except (ValueError, UnicodeDecodeError):
        raise UpdateError(
            "GitHub did not answer in a form this app understands")
    if not isinstance(payload, dict):
        raise UpdateError(
            "GitHub did not answer in a form this app understands")
    return payload


def _first_notes_line(body):
    """The first non-empty line of the release notes — what the
    popup shows. Empty when the release carries no notes."""
    if not isinstance(body, str):
        return ""
    for line in body.splitlines():
        if line.strip():
            return line.strip()
    return ""


def fetch_latest_release(urlopen=None):
    """The latest release from GitHub's public endpoint, as
    {"version", "tag", "url", "commit", "notes"}. "commit" is the
    exact code the release holds (its target commit); it may be
    None when GitHub does not name one, and the pull path refuses
    to run without it. Raises UpdateError with a plain reason
    when the network fails, the answer is not JSON, or it
    carries no usable tag. urlopen is a test seam."""
    payload = _read_json(
        latest_release_url(), CHECK_TIMEOUT_S, urlopen)
    tag = payload.get("tag_name")
    if not isinstance(tag, str) or not tag:
        raise UpdateError("the releases page did not name a version")
    version = parse_version(tag)
    if version is None:
        raise UpdateError(
            "the latest release tag did not look like a version: %s"
            % tag)
    commit = payload.get("target_commitish")
    if not isinstance(commit, str) or not commit:
        commit = None
    url = payload.get("html_url") or (
        f"https://github.com/{REPO_OWNER}/{REPO_NAME}/releases")
    return {
        "version": ".".join(str(part) for part in version),
        "tag": tag,
        "url": url,
        "commit": commit,
        "notes": _first_notes_line(payload.get("body")),
    }


def check_for_update(current_version, urlopen=None):
    """One call, three outcomes, never an exception for a network
    or parsing problem:

    {"state": "newer", "latest": "23", "tag": "v23",
     "url": "<release page>", "commit": "<sha>",
     "notes": "<first notes line>",
     "version": "23"} — a newer release is out.
    {"state": "current", ...} — the running version is the
    newest (or newer than the latest release, a dev tree).
    {"state": "unknown", "latest": None, "url": None,
     "reason": "<plain words>"} — the check could not run.
    "version" always carries the latest version when one was
    found, so the pull worker can name it without caring which
    state the check returned.
    """
    try:
        release = fetch_latest_release(urlopen=urlopen)
    except UpdateError as exc:
        return {"state": "unknown", "latest": None, "url": None,
                "tag": None, "commit": None, "notes": "",
                "version": None, "reason": str(exc)}
    if is_newer(release["version"], current_version):
        return {"state": "newer", "latest": release["version"],
                "tag": release["tag"], "url": release["url"],
                "commit": release["commit"],
                "notes": release["notes"],
                "version": release["version"]}
    return {"state": "current", "latest": release["version"],
            "tag": release["tag"], "url": release["url"],
            "commit": release["commit"],
            "notes": release["notes"],
            "version": release["version"]}


def fetch_tree(commit, urlopen=None):
    """The release's file list from GitHub's API, as
    {path: (blob_sha, mode)} for files only. Raises UpdateError
    when the list cannot be fetched, is truncated (too long to
    check fully — verification would be a half-check), or has an
    entry without a name or checksum."""
    payload = _read_json(tree_url(commit), TREE_TIMEOUT_S, urlopen)
    if payload.get("truncated"):
        raise UpdateError(
            "the release's file list was too long to check fully")
    entries = payload.get("tree")
    if not isinstance(entries, list):
        raise UpdateError(
            "GitHub did not answer with a file list")
    tree = {}
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("type") != "blob":
            continue
        path = entry.get("path")
        sha = entry.get("sha")
        if not isinstance(path, str) or not path:
            raise UpdateError(
                "the file list had an entry without a name")
        if not isinstance(sha, str) or not sha:
            raise UpdateError(
                "the file list had an entry without a checksum")
        tree[path] = (sha, entry.get("mode", "100644"))
    return tree


def git_blob_sha(data):
    """The checksum GitHub lists for a file's bytes: SHA-1 of
    "blob <length>\\0" followed by the bytes. Comparing this
    against the API's file list proves the downloaded bytes are
    exactly the release's bytes."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def download_archive(tag, dest_path, urlopen=None, progress=None,
                     cancelled=None):
    """Download the release's source archive to dest_path.
    progress(done, total) is called per chunk; total is None
    when the server does not say how big the file is.
    cancelled() is checked between chunks — when it returns
    True the download stops with UpdateError and the partial
    file is removed. Raises UpdateError on any network problem."""
    opener = urlopen or urllib.request.urlopen
    try:
        response = opener(zipball_url(tag),
                          timeout=DOWNLOAD_TIMEOUT_S)
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, TimeoutError):
            raise UpdateError("the download timed out")
        raise UpdateError("could not reach GitHub: %s" % reason)
    except TimeoutError:
        raise UpdateError("the download timed out")
    total = None
    headers = getattr(response, "headers", None)
    if headers is not None:
        length = headers.get("Content-Length")
        if length is not None:
            try:
                total = int(length)
            except ValueError:
                total = None
    done = 0
    try:
        with response:
            with open(dest_path, "wb") as handle:
                while True:
                    if cancelled is not None and cancelled():
                        raise UpdateError("the download was stopped")
                    try:
                        chunk = response.read(DOWNLOAD_CHUNK)
                    except (urllib.error.URLError, TimeoutError) as exc:
                        raise UpdateError(
                            "the download stopped part-way: %s" % exc)
                    if not chunk:
                        break
                    handle.write(chunk)
                    done += len(chunk)
                    if progress is not None:
                        progress(done, total)
    except UpdateError:
        try:
            os.remove(dest_path)
        except OSError:
            pass
        raise
    except OSError as exc:
        try:
            os.remove(dest_path)
        except OSError:
            pass
        raise UpdateError("could not save the download: %s" % exc)


def extract_archive(archive_path, dest_dir):
    """Unpack a source archive into dest_dir and return the
    folder holding the repo files — a zipball carries one
    top-level folder named after the repo and the commit.
    Raises UpdateError on a bad zip or a layout that is not
    one top-level folder."""
    try:
        archive = zipfile.ZipFile(archive_path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise UpdateError("the download is not a valid archive: %s" % exc)
    with archive:
        names = [name for name in archive.namelist() if name.strip()]
        if not names:
            raise UpdateError("the download is empty")
        tops = {name.split("/")[0] for name in names if "/" in name}
        if len(tops) != 1:
            raise UpdateError(
                "the download does not hold one folder of files")
        top = tops.pop()
        archive.extractall(dest_dir)
    return os.path.join(dest_dir, top)


def verify_extracted(root, tree):
    """Compare every file under root against the release's file
    list. Returns a list of problems — empty means the download
    is exactly the release: no file changed, none missing, none
    extra. A file whose bytes differ from the listed checksum
    is a problem, whatever the cause."""
    problems = []
    seen = set()
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            seen.add(rel)
            if rel not in tree:
                problems.append(
                    "the download has an extra file: %s" % rel)
                continue
            try:
                with open(full, "rb") as handle:
                    data = handle.read()
            except OSError as exc:
                problems.append(
                    "the download's %s could not be read: %s"
                    % (rel, exc))
                continue
            if git_blob_sha(data) != tree[rel][0]:
                problems.append(
                    "the download's %s does not match the release"
                    % rel)
    for rel in sorted(tree):
        if rel not in seen:
            problems.append("the download is missing: %s" % rel)
    return problems


def install_update(root, new_tree, old_tree, install_dir):
    """Copy the verified files into the install folder. Files in
    the new release land (creating folders as needed); files the
    old release had and the new one does not are removed, unless
    they are user data; anything else in the folder — user files
    that belong to neither release — is left alone. Stale
    bytecode folders go, so the new code is compiled fresh.
    old_tree may be None (the old file list could not be
    fetched): then nothing is removed, only added and replaced.
    Returns the installed repo paths. Raises UpdateError on any
    failure, and refuses a folder that does not hold the app."""
    if not os.path.isfile(os.path.join(install_dir, APP_ENTRY)):
        raise UpdateError(
            "this folder does not hold OLLAMA SYSTEM TEST")
    installed = []
    try:
        for rel in sorted(new_tree):
            src = os.path.join(root, *rel.split("/"))
            dst = os.path.join(install_dir, *rel.split("/"))
            parent = os.path.dirname(dst)
            if parent:
                os.makedirs(parent, exist_ok=True)
            shutil.copyfile(src, dst)
            if new_tree[rel][1] == "100755":
                os.chmod(dst, 0o755)
            installed.append(rel)
        if old_tree is not None:
            for rel in sorted(set(old_tree) - set(new_tree)):
                if rel in PRESERVE_NAMES:
                    continue
                if rel.split("/")[0] in PRESERVE_NAMES:
                    continue
                dst = os.path.join(install_dir, *rel.split("/"))
                if os.path.isfile(dst) or os.path.islink(dst):
                    os.remove(dst)
        for dirpath, dirnames, _filenames in os.walk(install_dir):
            if "__pycache__" in dirnames:
                shutil.rmtree(os.path.join(dirpath, "__pycache__"),
                              ignore_errors=True)
                dirnames.remove("__pycache__")
            for skip in ("results", "logs", ".venv"):
                if skip in dirnames:
                    dirnames.remove(skip)
    except OSError as exc:
        raise UpdateError("could not install the update: %s" % exc)
    return installed


def pull_update(release, current_version, install_dir, urlopen=None,
                progress=None, cancelled=None):
    """Download, verify, and install a release the user asked
    for. release is the dict from check_for_update (state
    "newer"). progress(stage, done, total) reports the stages
    "downloading", "verifying", and "installing". Returns
    {"version": ...} on success. Raises UpdateError — and
    installs nothing — when the release does not name the exact
    code it holds, the file list cannot be fetched, the download
    fails, or the download does not match the file list."""
    tag = release.get("tag")
    commit = release.get("commit")
    if not isinstance(tag, str) or not tag:
        raise UpdateError("the release did not name a version")
    if not isinstance(commit, str) or not _SHA_RE.match(commit):
        raise UpdateError(
            "the release did not name the exact code it holds, "
            "so the download cannot be checked")
    new_tree = fetch_tree(commit, urlopen=urlopen)
    old_tree = None
    try:
        old_tree = fetch_tree("v%s" % current_version,
                              urlopen=urlopen)
    except UpdateError:
        old_tree = None
    tmp = tempfile.mkdtemp(prefix="ost-update-")
    try:
        archive = os.path.join(tmp, "update.zip")
        if progress is not None:
            progress("downloading", 0, None)
        download_archive(
            tag, archive, urlopen=urlopen,
            progress=(lambda done, total: progress(
                "downloading", done, total))
            if progress is not None else None,
            cancelled=cancelled)
        if progress is not None:
            progress("verifying", 0, None)
        root = extract_archive(archive, tmp)
        problems = verify_extracted(root, new_tree)
        if problems:
            raise UpdateError(
                "the download did not match the release: %s"
                % "; ".join(problems[:3]))
        if progress is not None:
            progress("installing", 0, None)
        install_update(root, new_tree, old_tree, install_dir)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {"version": release.get("version")}


def restart_command(app_path, argv):
    """The command that relaunches the app: the same Python that
    is running now, the app file, and the launch arguments after
    the program name."""
    return [sys.executable, app_path] + list(argv[1:])


def restart_app(app_path, argv=None, _execv=os.execv):
    """Replace this process with a fresh copy of the app — the
    new code on disk runs. _execv is a test seam: pass a stub
    and the restart is recorded instead of performed."""
    args = restart_command(
        app_path, argv if argv is not None else sys.argv)
    _execv(args[0], args)
