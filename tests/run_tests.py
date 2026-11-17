"""End-to-end verification of OLLAMA SYSTEM TEST v20 against a fake Ollama
server and a fake OpenAI-compatible provider server, both with canned
answers per model. All 1538 v19 checks are carried over; the v20
checks cover the live bigger-text fix (the open window's widgets
re-size, tile captions and values keep their relative sizes, the
Settings spin path, Cancel restoring the size) and the Profiles
tab fitting the screen at 14 pt with runs (scroll area, small tab
minimum, window minimum inside 1366x768, resize to 720 tall).
1556 checks in total, all green (run 7).

The suite lives in the repo under tests/ and locates the app relative
to the repo root (the parent of this folder), so it runs from any
checkout. Run it from the repo root:

  .venv/bin/python tests/run_tests.py

with QT_QPA_PLATFORM=offscreen (the suite opens real windows and
grabs screenshots; offscreen keeps it headless). In the sandbox, use
the Qt test env from AGENTS.md:

  LD_LIBRARY_PATH=~/workspace/qt-libs/root/usr/lib/x86_64-linux-gnu
  __EGL_VENDOR_LIBRARY_DIRS=~/workspace/qt-libs/root/usr/share/glvnd/egl_vendor.d
  QT_QPA_PLATFORM=offscreen TMPDIR=/tmp
  ~/workspace/forge-v2-testenv/bin/python tests/run_tests.py

Use the default HOME — overriding it breaks Qt plugin init. Takes
about 40 minutes under load. Exit code 0 = every check passed. Prints PASS/FAIL
per check.
"""

import ast
import csv
from datetime import datetime, timedelta
import hashlib
import io
import json
import math
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import wave

# The suite lives in tests/ inside the repo; the app is at the repo
# root, the parent of this folder. No hardcoded tree name.
VERIFY_DIR = os.path.dirname(os.path.abspath(__file__))
PKG_DIR = os.path.dirname(VERIFY_DIR)
RESULTS_DIR = os.path.join(VERIFY_DIR, "results-test")
TEMPLATES_TEST_DIR = os.path.join(VERIFY_DIR, "templates-test")
PROVIDERS_FILE = os.path.join(VERIFY_DIR, "providers-test.json")
CAPABILITIES_FILE = os.path.join(VERIFY_DIR, "capabilities-test.json")
LOGS_DIR = os.path.join(VERIFY_DIR, "logs-test")
FAKEBIN = os.path.join(VERIFY_DIR, "fakebin")
FAKEBIN_SLEEP = os.path.join(VERIFY_DIR, "fakebin-sleep")
ORIGINAL_PATH = os.environ["PATH"]

os.environ["OST_RESULTS_DIR"] = RESULTS_DIR
os.environ["OST_PROVIDERS_FILE"] = PROVIDERS_FILE
os.environ["OST_CAPABILITIES_FILE"] = CAPABILITIES_FILE
os.environ["OST_LOGS_DIR"] = LOGS_DIR
# v17: the RAM check reads MemAvailable through this override, so the
# suite is not at the mercy of the sandbox's free RAM (which can sit
# under a gigabyte — every fake model would be skipped). 16 GB free
# lets every fake model run; the RAM-skip checks point it at a small
# fixture of their own.
MEMINFO_FILE = os.path.join(VERIFY_DIR, "meminfo-test")
with open(MEMINFO_FILE, "w", encoding="utf-8") as _meminfo:
    _meminfo.write("MemTotal:       16777216 kB\n"
                   "MemAvailable:   16777216 kB\n")
os.environ["OST_MEMINFO_FILE"] = MEMINFO_FILE
sys.path.insert(0, PKG_DIR)
sys.path.insert(0, VERIFY_DIR)

import ollama_system_test as app
import fake_ollama

PASS = 0
FAIL = 0


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"PASS: {name}")
    else:
        FAIL += 1
        print(f"FAIL: {name} {detail}")


def wait_until(predicate, timeout=60, app=None):
    from PySide6.QtWidgets import QApplication
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        QApplication.processEvents()
        time.sleep(0.05)
    return predicate()


def table_text(window, row, col):
    item = window.table.item(row, col)
    return item.text() if item is not None else None


def row_for(window, name):
    for row in range(window.table.rowCount()):
        if table_text(window, row, 0) == name:
            return row
    return None


def set_checked_list(widget, wanted):
    """Tick the items whose label or visible text is wanted.

    Model items carry capability brackets in their visible text
    ("fake-smart:2b [vision, tools, long_context]"), so the match is
    against the target label stored on the item as well as the text.
    """
    from PySide6.QtCore import Qt
    for row in range(widget.count()):
        item = widget.item(row)
        target = item.data(Qt.UserRole)
        label = target["label"] if isinstance(target, dict) else item.text()
        wanted_here = label in wanted or item.text() in wanted
        item.setCheckState(Qt.Checked if wanted_here else Qt.Unchecked)


def set_checked_templates(window, wanted_ids):
    """Tick the wanted templates, skipping greyed-out ones — a user
    cannot tick those, so the helper must not either."""
    from PySide6.QtCore import Qt
    for row in range(window.template_list.count()):
        item = window.template_list.item(row)
        if not item.flags() & Qt.ItemIsEnabled:
            continue
        item.setCheckState(
            Qt.Checked if item.data(Qt.UserRole) in wanted_ids else Qt.Unchecked
        )


def checked_names(widget):
    from PySide6.QtCore import Qt
    return {
        widget.item(row).text()
        for row in range(widget.count())
        if widget.item(row).checkState() == Qt.Checked
    }


def file_hash(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def kill_pidfile(path):
    try:
        with open(path, encoding="utf-8") as handle:
            pid = int(handle.read().strip())
        os.kill(pid, signal.SIGTERM)
    except (OSError, ValueError):
        pass


def write_fake_ollama():
    """Fake `ollama` CLI: `list` prints 2 canned models, `serve` starts
    a tiny HTTP server on the OLLAMA_HOST port serving /api/tags."""
    os.makedirs(FAKEBIN, exist_ok=True)
    path = os.path.join(FAKEBIN, "ollama")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("""#!/usr/bin/env python3
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

MODELS = ["serve-model-a:2b", "serve-model-b:1b"]


def _note(path, mode, text):
    # Bookkeeping writes (pidfile, call log, env file) must never
    # kill the serve: the sandbox occasionally refuses a write
    # for a minute at a time (EPERM flap, Nov 2026), and a dead
    # serve fails the starter. Retry briefly, then carry on
    # without the note.
    for _attempt in range(6):
        try:
            with open(path, mode) as handle:
                handle.write(text)
            return
        except OSError:
            time.sleep(0.5)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "FakeOllamaServe/1.0"

    def log_message(self, *args):
        pass

    def _send_json(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/tags":
            self._send_json(200, {"models": [
                {"name": name, "size": 1000000000,
                 "modified_at": "2026-11-12T00:00:00Z"}
                for name in MODELS
            ]})
        elif self.path == "/api/ps":
            self._send_json(200, {"models": []})
        else:
            self._send_json(404, {"error": "not found"})


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "list":
        print("NAME ID SIZE MODIFIED")
        print("cli-model-a:2b abcdef 1.5 GB 2 days ago")
        print("cli-model-b:1b 123456 900 MB 3 days ago")
        return 0
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        pidfile = os.environ.get("FAKE_OLLAMA_PIDFILE")
        if pidfile:
            _note(pidfile, "w", str(os.getpid()))
        calls = os.environ.get("FAKE_OLLAMA_CALLS")
        if calls:
            _note(calls, "a", "serve\\n")
        envfile = os.environ.get("FAKE_OLLAMA_ENVFILE")
        if envfile:
            _note(envfile, "w", os.environ.get("OLLAMA_MODELS", "unset"))
        port = urlparse(os.environ.get("OLLAMA_HOST", "")).port or 11434
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        server.daemon_threads = True
        server.serve_forever()
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
""")
    os.chmod(path, 0o755)


def write_fake_ollama_sleep():
    """Fake `ollama` CLI whose `serve` never brings a server up."""
    os.makedirs(FAKEBIN_SLEEP, exist_ok=True)
    path = os.path.join(FAKEBIN_SLEEP, "ollama")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("#!/bin/sh\n")
        handle.write('if [ "$1" = "serve" ]; then\n')
        handle.write('  echo $$ > "$FAKE_OLLAMA_PIDFILE"\n')
        handle.write("  sleep 120\n")
        handle.write("fi\n")
    os.chmod(path, 0o755)


def main():
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    shutil.rmtree(RESULTS_DIR, ignore_errors=True)
    shutil.rmtree(TEMPLATES_TEST_DIR, ignore_errors=True)
    shutil.rmtree(FAKEBIN, ignore_errors=True)
    shutil.rmtree(FAKEBIN_SLEEP, ignore_errors=True)
    if os.path.exists(PROVIDERS_FILE):
        os.remove(PROVIDERS_FILE)
    if os.path.exists(CAPABILITIES_FILE):
        os.remove(CAPABILITIES_FILE)
    write_fake_ollama()
    write_fake_ollama_sleep()

    # ---------- unit tests: v1 helpers still green ----------
    check("tok_s 200/20s = 10.0", app.tok_s(200, 20_000_000_000) == 10.0)
    check("tok_s zero duration is None", app.tok_s(200, 0) is None)
    check("fmt_bytes 2GB", app.fmt_bytes(2_000_000_000) == "2.0 GB")
    check("fmt_bytes None is n/a", app.fmt_bytes(None) == "n/a")
    check("fmt_duration 0.5s is ms", app.fmt_duration(0.5) == "500 ms")
    check("fmt_score 100", app.fmt_score(100.0) == "100%")
    check("fmt_score None is n/a", app.fmt_score(None) == "n/a")
    runs = [
        {"decode_tok_s": 10.0, "prompt_tok_s": 20.0, "ttft_s": 0.5, "load_s": 2.0},
        {"decode_tok_s": 12.0, "prompt_tok_s": 22.0, "ttft_s": 0.7, "load_s": 0.0},
    ]
    avg = app.average_runs(runs)
    check("average decode", avg["decode_tok_s"] == 11.0)
    check("load time from run 1", avg["load_s"] == 2.0)
    sorted_results = app.sort_results([
        {"name": "slow", "status": "done", "overall_score": 50.0},
        {"name": "bad", "status": "failed: boom", "overall_score": None},
        {"name": "fast", "status": "done", "overall_score": 100.0},
    ])
    check("sort best score first, failed last",
          [r["name"] for r in sorted_results] == ["fast", "slow", "bad"])

    # ---------- unit tests: v3 error helpers ----------
    check("is_unreachable true",
          app.is_unreachable(app.OllamaError("could not reach Ollama")))
    check("is_unreachable false for model error",
          not app.is_unreachable(app.OllamaError("model exploded")))
    check("describe_error unreachable has the hint",
          app.describe_error(app.OllamaError("could not reach Ollama"))
          == app.UNREACHABLE_HINT)
    check("describe_error model error unchanged",
          app.describe_error(app.OllamaError("model exploded")) == "model exploded")
    # The check runs against a dead port, not the default address:
    # another Ollama may be answering on this machine (a real one
    # runs on the sandbox's 11434 now), and "no server" must mean
    # no server.
    prior_host_reachable = os.environ.get("OLLAMA_HOST")
    try:
        os.environ["OLLAMA_HOST"] = f"http://127.0.0.1:{free_port()}"
        check("ollama_reachable false with no server",
              app.ollama_reachable() is False)
    finally:
        if prior_host_reachable is None:
            os.environ.pop("OLLAMA_HOST", None)
        else:
            os.environ["OLLAMA_HOST"] = prior_host_reachable

    # ---------- unit tests: slugs, machine, system id ----------
    check("slugify_model",
          app.slugify_model("maternion/minicpm5-heretic:2b") == "maternion_minicpm5-heretic_2b")
    check("slugify_model spaces",
          app.slugify_model("my model v1") == "my_model_v1")
    info = app.machine_info()
    check("machine_info has cpu", bool(info.get("cpu")), str(info))
    check("machine_info has ram", info.get("ram_bytes") is not None, str(info))
    sid = app.system_id(info)
    check("system_id non-empty", bool(sid), sid)
    check("system_id folder-safe",
          all(c.isalnum() or c in "-_" for c in sid), sid)
    check("system_id has 3 parts", len(sid.split("_")) == 3, sid)

    # ---------- unit tests: template loading ----------
    templates, warnings = app.load_templates()
    check("19 templates load", len(templates) == 19, str([t["id"] for t in templates]))
    check("no warnings on shipped templates", not warnings, str(warnings))
    by_id = {t["id"]: t for t in templates}
    check("speed template unscored", by_id["speed"]["scored"] is False)
    check("speed has 2 prompts", len(by_id["speed"]["prompts"]) == 2)
    check("speed has 3 context lengths",
          by_id["speed"].get("context_lengths") == [1024, 8192, 32768],
          str(by_id["speed"].get("context_lengths")))
    check("speed keeps its base prompt",
          by_id["speed"].get("context_base_prompt")
          == "Write a short story about a blacksmith.")
    check("vision needs vision",
          by_id["vision"].get("capability") == "vision")
    check("tool calling needs tools",
          by_id["tool_calling"].get("capability") == "tools")
    check("long context needs long context",
          by_id["long_context"].get("capability") == "long_context")
    check("image generation needs image generation",
          by_id["image_generation"].get("capability") == "image_generation")
    check("audio needs audio", by_id["audio"].get("capability") == "audio")
    for tid in ("multi_turn", "code_repair", "planning", "grounded_qa",
                "refusal", "summarization", "structured_output",
                "role_following"):
        check(f"{tid} has 5 tasks", len(by_id[tid]["tasks"]) == 5,
              str(len(by_id[tid].get("tasks", []))))
    for tid in ("coding", "reasoning", "instruction", "knowledge"):
        check(f"{tid} has 5 tasks", len(by_id[tid]["tasks"]) == 5,
              str(len(by_id[tid].get("tasks", []))))
    check("math has 10 tasks (v16: grown from 5)",
          len(by_id["math"]["tasks"]) == 10,
          str(len(by_id["math"].get("tasks", []))))
    check("coding tasks are code rule",
          all(t["rule"] == "code" for t in by_id["coding"]["tasks"]))

    # ---------- unit tests: scoring, every rule type ----------
    check("contains pass",
          app.score_contains("The capital is Paris.", "Paris")[0])
    check("contains fail",
          not app.score_contains("The capital is London.", "Paris")[0])
    check("contains word boundary: know does not match no",
          not app.score_contains("I know.", "no")[0])
    check("contains case-insensitive",
          app.score_contains("PARIS is nice.", "Paris")[0])
    # ---------- v10: transcript normalization (T12) ----------
    # Real transcription providers write spoken numbers as digits.
    check("transcript_words spells digits out",
          app.transcript_words("1, 2, 3.") == "one two three",
          app.transcript_words("1, 2, 3."))
    check("transcript_words keeps words, drops punctuation",
          app.transcript_words("Hello world!") == "hello world")
    check("transcript_words spells seventy",
          app.transcript_words("near 70 degrees") == "near seventy degrees",
          app.transcript_words("near 70 degrees"))
    check("transcript_words leaves big numbers alone",
          app.transcript_words("room 101") == "room 101")
    check("digit transcript passes the count task",
          app.score_task({"rule": "contains", "expected": "one two three"},
                         app.transcript_words("1, 2, 3."))[0])
    check("numeric pass", app.score_numeric("He has 21 apples.", 21)[0])
    check("numeric float pass", app.score_numeric("150.0 miles", 150)[0])
    check("numeric fail", not app.score_numeric("He has 20 apples.", 21)[0])
    check("numeric no number fails", not app.score_numeric("no numbers here", 5)[0])
    check("json pass",
          app.score_json('{"city": "Paris"}', {"city": "Paris"})[0])
    check("json fenced pass",
          app.score_json('```json\n{"city": "Paris"}\n```', {"city": "Paris"})[0])
    check("json prose-wrapped pass",
          app.score_json('Here it is: {"city": "Paris"} done.',
                         {"city": "Paris"})[0])
    check("json wrong value fails",
          not app.score_json('{"city": "London"}', {"city": "Paris"})[0])
    check("json missing key fails",
          not app.score_json('{"town": "Paris"}', {"city": "Paris"})[0])
    check("json invalid fails",
          not app.score_json("not json at all", {"city": "Paris"})[0])
    check("json numeric value pass",
          app.score_json('{"age": 36}', {"age": 36})[0])
    check("count_lines pass",
          app.score_count_lines("- a\n- b\n- c", 3)[0])
    check("count_lines ignores blanks",
          app.score_count_lines("- a\n\n- b\n- c\n", 3)[0])
    check("count_lines fail",
          not app.score_count_lines("- a\n- b", 3)[0])
    check("max_words pass", app.score_max_words("one two three", 10)[0])
    check("max_words fail",
          not app.score_max_words("one two three four five six seven eight nine ten eleven", 10)[0])
    check("extract code from fence",
          app.extract_python_code("Here:\n```python\nx = 1\n```\ndone.") == "x = 1")
    check("extract code fallback is whole answer",
          app.extract_python_code("x = 1") == "x = 1")

    # ---------- unit tests: code execution (real subprocess) ----------
    good_fib = "def fib(n):\n    a, b = 0, 1\n    for _ in range(n):\n        a, b = b, a + b\n    return a\n"
    fib_tests = [{"args": [10], "expected": 55}, {"args": [0], "expected": 0}]
    check("code exec good fib passes",
          app.run_code_tests(good_fib, "fib", fib_tests)[0])
    bad_fib = "def fib(n):\n    return n\n"
    passed, detail = app.run_code_tests(bad_fib, "fib", fib_tests)
    check("code exec wrong answer fails", not passed, detail)
    check("code exec wrong answer names the test", "test 1" in detail, detail)
    syntax_bad = "def fib(n):\n    return n +\n"
    check("code exec syntax error fails",
          not app.run_code_tests(syntax_bad, "fib", fib_tests)[0])
    check("code exec no code fails",
          not app.run_code_tests("", "fib", fib_tests)[0])
    check("code exec missing function fails",
          not app.run_code_tests("def other(n):\n    return n\n", "fib", fib_tests)[0])
    loop_code = "def fib(n):\n    while True:\n        pass\n"
    t0 = time.monotonic()
    passed, detail = app.run_code_tests(
        loop_code, "fib", [{"args": [1], "expected": 1}])
    elapsed = time.monotonic() - t0
    check("code exec timeout fails", not passed and "timed out" in detail, detail)
    check("code exec timeout is quick", elapsed < 20, f"{elapsed:.1f}s")

    # ---------- unit tests: profiles ----------
    def _craft_run(model, system, stamp, filename, template, name, score,
                   passed_count, total, speed):
        folder = os.path.join(RESULTS_DIR, app.slugify_model(model), system)
        os.makedirs(folder, exist_ok=True)
        data = {
            "template": template, "template_name": name, "machine": info,
            "tasks": [], "template_score": score, "passed": passed_count,
            "total": total, "avg_decode_tok_s": speed,
            "timestamp": stamp, "model": model,
            "model_slug": app.slugify_model(model), "system_id": system,
        }
        with open(os.path.join(folder, filename), "w", encoding="utf-8") as handle:
            json.dump(data, handle)

    check("build_profile None with no runs",
          app.build_profile(RESULTS_DIR, "unit-model_1b") is None)
    check("write_profile None with no runs",
          app.write_profile(RESULTS_DIR, "unit-model_1b") is None)
    _craft_run("unit-model:1b", "sys-a", "2026-11-12T10:00:00",
               "2026-11-12-100000-coding.json", "coding", "Coding",
               100.0, 5, 5, 20.0)
    _craft_run("unit-model:1b", "sys-a", "2026-11-12T10:00:00",
               "2026-11-12-100000-math.json", "math", "Math",
               40.0, 2, 5, 10.0)
    _craft_run("unit-model:1b", "sys-a", "2026-11-12T10:00:00",
               "2026-11-12-100000-speed.json", "speed", "Speed",
               None, None, 2, 5.0)
    _craft_run("unit-model:1b", "sys-b", "2026-11-12T12:00:00",
               "2026-11-12-120000-coding.json", "coding", "Coding",
               40.0, 2, 5, 20.0)
    _craft_run("unit-model:1b", "sys-b", "2026-11-12T12:00:00",
               "2026-11-12-120000-math.json", "math", "Math",
               100.0, 5, 5, 10.0)
    built = app.build_profile(RESULTS_DIR, "unit-model_1b")
    check("build_profile returns name and text", built is not None)
    if built is not None:
        _name, text = built
        check("profile has model name", "# unit-model:1b" in text, text[:200])
        check("profile lists both systems",
              "sys-a" in text and "sys-b" in text, text[:400])
        check("profile counts 5 runs", "Runs: 5" in text, text[:400])
        check("profile latest overall 70%",
              "Latest overall score: 70%" in text, text[:600])
        check("profile best overall 100%",
              "Best overall score: 100%" in text, text[:600])
        check("profile avg speed 13.0",
              "Average decode speed: 13.0 tokens per second" in text, text[:600])
        check("profile coding latest 40%",
              "| Coding | 40% | 100% | 2 |" in text, text)
        check("profile math latest 100%",
              "| Math | 100% | 100% | 2 |" in text, text)
        check("profile speed unscored",
              "| Speed | no scoring | no scoring | 1 |" in text, text)
        strengths = text.split("## Strengths")[1].split("## Weaknesses")[0]
        check("profile strength is Math, not Coding",
              "- Math" in strengths and "- Coding" not in strengths, strengths)
        check("profile weakness is Coding",
              "- Coding" in text.split("## Weaknesses")[1], text)
        check("profile verdict is Solid", "Solid on this PC" in text, text)
        check("profile verdict names best and weakest",
              "Best at: Math." in text and "Weakest at: Coding." in text, text)
    profile_path = app.write_profile(RESULTS_DIR, "unit-model_1b")
    check("write_profile writes PROFILE.md",
          profile_path is not None and profile_path.endswith("PROFILE.md")
          and os.path.exists(profile_path), str(profile_path))
    shutil.rmtree(RESULTS_DIR, ignore_errors=True)

    # ---------- template loading edge cases ----------
    shutil.copytree(os.path.join(PKG_DIR, "templates"), TEMPLATES_TEST_DIR)
    custom = {
        "id": "custom", "name": "Custom", "description": "test template",
        "num_predict": 200, "scored": True,
        "tasks": [{"id": "t1", "prompt": "Say the word zebra.",
                   "rule": "contains", "expected": "zebra"}],
    }
    with open(os.path.join(TEMPLATES_TEST_DIR, "custom.json"), "w", encoding="utf-8") as handle:
        json.dump(custom, handle)
    loaded, warns = app.load_templates(TEMPLATES_TEST_DIR)
    check("20th template loads from JSON",
          len(loaded) == 20 and "custom" in [t["id"] for t in loaded],
          str([t["id"] for t in loaded]))
    with open(os.path.join(TEMPLATES_TEST_DIR, "broken.json"), "w", encoding="utf-8") as handle:
        handle.write("{not valid json")
    loaded, warns = app.load_templates(TEMPLATES_TEST_DIR)
    check("corrupt template warned, others load",
          len(loaded) == 20 and any("broken.json" in w for w in warns), str(warns))
    bad_rule = {
        "id": "badrule", "name": "Bad Rule", "scored": True,
        "tasks": [{"id": "t1", "prompt": "x", "rule": "telepathy", "expected": "y"}],
    }
    with open(os.path.join(TEMPLATES_TEST_DIR, "badrule.json"), "w", encoding="utf-8") as handle:
        json.dump(bad_rule, handle)
    loaded, warns = app.load_templates(TEMPLATES_TEST_DIR)
    check("bad rule task skipped, template dropped",
          "badrule" not in [t["id"] for t in loaded], str(warns))
    check("broken files do not crash loader", len(loaded) == 20)

    # ---------- fake server + direct client tests ----------
    server, _thread = fake_ollama.start_server()
    host, port = server.server_address
    os.environ["OLLAMA_HOST"] = f"http://{host}:{port}"
    print(f"fake Ollama on http://{host}:{port}")

    check("ollama_reachable true with server up", app.ollama_reachable())
    names, source = app.list_models()
    check("list_models via API", source == "api" and len(names) == 4, str(names))
    sizes = app.ps_sizes()
    check("ps_sizes", sizes.get("fake-smart:2b") == 2_000_000_000, str(sizes))

    metrics = app.generate_once(
        "fake-smart:2b",
        "Write a Python function named fib(n) that returns the nth Fibonacci number.",
        400,
    )
    check("generate returns answer text", "def fib" in metrics["answer"],
          metrics["answer"][:80])
    check("generate decode 20.0", metrics["decode_tok_s"] == 20.0,
          str(metrics["decode_tok_s"]))
    check("generate ttft near 0.5s",
          metrics["ttft_s"] is not None and 0.3 < metrics["ttft_s"] < 3.0,
          str(metrics["ttft_s"]))
    try:
        app.generate_once("fake-broken:1b", "hello", 200)
        check("generate broken raises", False)
    except app.OllamaError as exc:
        check("generate broken raises", str(exc) == "model exploded", str(exc))

    # ---------- v5: screening via /api/show ----------
    shown = app.ollama_show("fake-smart:2b")
    check("ollama_show returns capabilities",
          shown.get("capabilities") == ["completion", "vision", "tools"],
          str(shown.get("capabilities")))
    check("ollama_show returns model info",
          shown.get("model_info", {}).get("llama.context_length") == 131072,
          str(shown.get("model_info")))
    check("capabilities_from_show smart has vision, tools, long context",
          app.capabilities_from_show(shown)
          == {"text", "vision", "tools", "long_context"},
          str(app.capabilities_from_show(shown)))
    shown = app.ollama_show("fake-partial:3b")
    check("capabilities_from_show partial has vision, long context",
          app.capabilities_from_show(shown) == {"text", "vision", "long_context"},
          str(app.capabilities_from_show(shown)))
    shown = app.ollama_show("fake-dumb:1b")
    check("capabilities_from_show dumb is text-only",
          app.capabilities_from_show(shown) == {"text"},
          str(app.capabilities_from_show(shown)))
    check("capabilities_from_show no model info still allows long context",
          app.capabilities_from_show({"capabilities": ["completion"]})
          == {"text", "long_context"})
    check("capabilities_from_show short context blocks long context",
          app.capabilities_from_show(
              {"capabilities": ["completion"],
               "model_info": {"llama.context_length": 4096}}) == {"text"})
    check("screen_ollama_model reads the fake",
          app.screen_ollama_model("fake-smart:2b", {})
          == {"text", "vision", "tools", "long_context"},
          str(app.screen_ollama_model("fake-smart:2b", {})))
    # v18 (RV5): a failed screening returns None instead of the
    # cached value or text-only — the caller keeps the old cache
    # entry (or text-only when there is none) and retries on the
    # next refresh. A failure is never cached as text-only forever.
    check("screen_ollama_model returns None on failure",
          app.screen_ollama_model("no-such-model:9b", {"no-such-model:9b": ["text", "vision"]})
          is None)
    check("screen_ollama_model returns None for an unknown model",
          app.screen_ollama_model("no-such-model:9b", {}) is None)

    # ---------- v5: chat_once via the fake (text, images, tools, roles) ----------
    chat_images_before = server.chat_images_seen
    chat_history_before = len(server.chat_history)
    chat = app.chat_once(
        "fake-smart:2b",
        [{"role": "user", "content": "What is the code word?"}],
        300,
    )
    check("chat returns answer text", "BANANA" in chat["answer"], chat["answer"][:60])
    check("chat decode 20.0", chat["decode_tok_s"] == 20.0,
          str(chat["decode_tok_s"]))
    check("chat has no tool calls", chat["tool_calls"] == [])
    chat = app.chat_once(
        "fake-smart:2b",
        [{"role": "user", "content": "How many red circles are in this image?",
          "images": ["aGVsbG8="]}],
        300,
    )
    check("chat with an image answers", "3 red circles" in chat["answer"],
          chat["answer"][:60])
    check("fake recorded the image",
          server.chat_images_seen == chat_images_before + 1,
          str(server.chat_images_seen))
    weather_tools = app._to_tool_definitions([{
        "name": "get_weather",
        "description": "Get the current weather for a city.",
        "parameters": {"type": "object",
                       "properties": {"city": {"type": "string"}},
                       "required": ["city"]},
    }])
    chat = app.chat_once(
        "fake-smart:2b",
        [{"role": "user", "content": "What is the weather in Paris?"}],
        300, tools=weather_tools,
    )
    check("chat tool call comes back with name and args",
          chat["tool_calls"]
          == [{"name": "get_weather", "arguments": {"city": "Paris"}}],
          str(chat["tool_calls"]))
    check("fake recorded the tools call",
          server.chat_tools_seen >= 1, str(server.chat_tools_seen))
    chat = app.chat_once(
        "fake-smart:2b",
        [{"role": "system", "content": "You are a pirate. Always talk like a pirate."},
         {"role": "user", "content": "Say hello."}],
        300,
    )
    check("chat role answer stays in character",
          "Ahoy" in chat["answer"], chat["answer"][:60])
    check("fake recorded the chat histories",
          len(server.chat_history) == chat_history_before + 4,
          str(len(server.chat_history)))
    try:
        app.chat_once("fake-broken:1b",
                      [{"role": "user", "content": "hello"}], 200)
        check("chat broken raises", False)
    except app.OllamaError as exc:
        check("chat broken raises", str(exc) == "model exploded", str(exc))

    # ---------- scoring end to end via canned answers (no HTTP) ----------
    # Only prompt-scored kinds run here (generate, vision, long
    # context): chat tasks need the conversation runner, tools tasks
    # need tool calls, and image/audio tasks need their endpoints.
    # Those kinds are proven by the direct chat tests and the GUI runs.
    for template in templates:
        if not template["scored"]:
            continue
        prompt_tasks = [
            t for t in template["tasks"]
            if t.get("kind", "generate") in ("generate", "vision", "long_context")
        ]
        if not prompt_tasks:
            continue
        # v16: math is 10 tasks now, partial gets 5 of them.
        partial_want = 50.0 if template["id"] == "math" else 60.0
        for brain, want in (("smart", 100.0), ("dumb", 0.0),
                            ("partial", partial_want)):
            passed_count = 0
            for task in prompt_tasks:
                answer = fake_ollama.answer_for(brain, task["prompt"])
                ok, _detail = app.score_task(task, answer)
                passed_count += 1 if ok else 0
            score = 100.0 * passed_count / len(prompt_tasks)
            check(f"{template['id']} {brain} scores {want:.0f}%",
                  score == want, f"got {score:.0f}%")

    # ---------- immutability + layout (direct save layer) ----------
    run_data = {
        "template": "math", "template_name": "Math", "machine": info,
        "tasks": [], "template_score": 100.0, "passed": 5, "total": 5,
        "avg_decode_tok_s": 20.0,
    }
    # v17: one stamp for both saves, so the collision is forced.
    # Two bare saves can straddle a second boundary under load
    # and never collide at all (run 4 flaked exactly that way).
    stamp = datetime.now()
    path1 = app.save_run_file(RESULTS_DIR, "fake-smart:2b", sid,
                              dict(run_data), stamp=stamp)
    hash1 = file_hash(path1)
    path2 = app.save_run_file(RESULTS_DIR, "fake-smart:2b", sid,
                              dict(run_data), stamp=stamp)
    check("second save makes a second file", path1 != path2 and os.path.exists(path2),
          f"{path1} vs {path2}")
    check("first file bytes unchanged", file_hash(path1) == hash1)
    check("second file gets -2 suffix", path2.endswith("-2.json"), path2)
    expected_dir = os.path.join(RESULTS_DIR, "fake-smart_2b", sid)
    check("layout is results/<model>/<system>/",
          os.path.dirname(path1) == expected_dir, os.path.dirname(path1))
    check("filename has timestamp-template shape",
          os.path.basename(path1).endswith("-math.json"), os.path.basename(path1))
    shutil.rmtree(RESULTS_DIR, ignore_errors=True)

    # ---------- Qt app: retry once ----------
    qt_app = QApplication(sys.argv)
    # v8: the delete confirm is a QMessageBox.question dialog, and a
    # real modal dialog would hang the suite — nobody is here to
    # click it. Answer Yes to all of them for the carried sections;
    # the sections that test a confirm replace this stub with a
    # controllable one (and restore it when they finish), and the
    # real dialog is restored at the end of main().
    from PySide6.QtWidgets import QMessageBox
    _suite_real_question = QMessageBox.question
    QMessageBox.question = lambda *args, **kwargs: QMessageBox.Yes
    # v13: the run preview is a modal dialog too, and a real one
    # would hang the suite the same way. Stub the seam to always say
    # Run for the carried sections; the v8 cost-guard rewrite and
    # the v13 sections replace it with a controllable one (and
    # restore it when they finish), and the real method is restored
    # at the end of main().
    _suite_real_preview = app.MainWindow._show_run_preview
    app.MainWindow._show_run_preview = lambda self, t, m, p=None: True
    app.RETRY_DELAY = 0.1
    server.generate_count = 0
    server.fail_next_generate = True
    window_retry = app.MainWindow()
    window_retry.show()
    wait_until(lambda: window_retry.model_list.count() == 4, timeout=10, app=qt_app)
    set_checked_list(window_retry.model_list, {"fake-smart:2b"})
    set_checked_templates(window_retry, {"speed"})
    window_retry.run_button.click()
    done = wait_until(
        lambda: window_retry.worker is None and window_retry.save_button.isEnabled(),
        timeout=120, app=qt_app,
    )
    check("retry run completes", done)
    check("retry made 5 generate attempts (warmup fail + retry + 3 context lengths)",
          server.generate_count == 5, f"got {server.generate_count}")
    check("retry run still scores done",
          window_retry.results.get("fake-smart:2b", {}).get("status") == "done",
          str(window_retry.results.get("fake-smart:2b", {}).get("status")))
    window_retry.close()
    window_retry.deleteLater()
    qt_app.processEvents()
    shutil.rmtree(RESULTS_DIR, ignore_errors=True)

    # ---------- Qt app: 7th template + corrupt template in the GUI ----------
    os.environ["OST_TEMPLATES_DIR"] = TEMPLATES_TEST_DIR
    window0 = app.MainWindow()
    window0.show()
    wait_until(lambda: window0.template_list.count() == 20, timeout=10, app=qt_app)
    check("GUI lists 20th template", window0.template_list.count() == 20,
          str(window0.template_list.count()))
    check("GUI warns about broken templates",
          "badrule.json" in window0.status_label.text()
          and "(+1 more)" in window0.status_label.text(),
          window0.status_label.text())
    window0.close()
    window0.deleteLater()
    qt_app.processEvents()
    del os.environ["OST_TEMPLATES_DIR"]

    # ---------- Qt app: window, nothing checked, full run ----------
    window = app.MainWindow()
    window.show()
    wait_until(lambda: window.model_list.count() == 4, timeout=10, app=qt_app)
    check("window lists 4 models", window.model_list.count() == 4,
          str(window.model_list.count()))
    check("window lists 19 templates", window.template_list.count() == 19,
          str(window.template_list.count()))
    check("run button enabled", window.run_button.isEnabled())
    check("no model checked by default", not checked_names(window.model_list),
          str(checked_names(window.model_list)))
    check("no template checked by default",
          not checked_names(window.template_list),
          str(checked_names(window.template_list)))
    check("start Ollama button present", window.start_ollama_button is not None)
    check("pull input and button present",
          window.pull_input is not None and window.pull_button is not None)
    check("seven tabs incl Run setup, Run and Models", window.tabs.count() == 7
          and [window.tabs.tabText(i) for i in range(7)] ==
          ["Run setup", "Run", "Models", "Past runs", "Profiles",
           "Compare", "Providers"],
          str([window.tabs.tabText(i) for i in range(window.tabs.count())]))

    from PySide6.QtWidgets import QLabel, QPushButton
    bad_amp = [
        w.text() for w in window.findChildren(QPushButton) + window.findChildren(QLabel)
        if "&" in w.text()
    ]
    check("no bare & in button/label text", not bad_amp, str(bad_amp))

    window.grab().save(os.path.join(VERIFY_DIR, "screenshot-start.png"))

    # ---------- v5: screening labels, cache, and grey-outs ----------
    model_texts = {window.model_list.item(r).text()
                   for r in range(window.model_list.count())}
    check("screening labels the smart model with its capabilities",
          "fake-smart:2b [vision, tools, long_context]" in model_texts,
          str(model_texts))
    check("screening labels the partial model with vision and long context",
          "fake-partial:3b [vision, long_context]" in model_texts,
          str(model_texts))
    check("screening leaves the dumb model plain (text-only)",
          "fake-dumb:1b" in model_texts, str(model_texts))
    check("screening leaves the broken model plain",
          "fake-broken:1b" in model_texts, str(model_texts))
    check("capabilities cache file was written",
          os.path.exists(CAPABILITIES_FILE))
    if os.path.exists(CAPABILITIES_FILE):
        with open(CAPABILITIES_FILE, encoding="utf-8") as handle:
            cache_data = json.load(handle)
        check("capabilities cache holds the screened models",
              cache_data.get("ollama", {}).get("fake-smart:2b")
              == ["long_context", "text", "tools", "vision"]
              and cache_data.get("ollama", {}).get("fake-dumb:1b") == ["text"],
              str(cache_data))
    check("no API key in the capabilities cache",
          "sk-fake-good-key" not in open(CAPABILITIES_FILE, encoding="utf-8").read()
          if os.path.exists(CAPABILITIES_FILE) else False)
    template_labels = {window.template_list.item(r).data(Qt.UserRole):
                       window.template_list.item(r).text()
                       for r in range(window.template_list.count())}
    check("vision template label says it needs vision",
          "needs vision" in template_labels.get("vision", ""),
          template_labels.get("vision", ""))
    check("image template label says it needs image generation",
          "needs image generation" in template_labels.get("image_generation", ""),
          template_labels.get("image_generation", ""))

    def _template_enabled(tid):
        for row in range(window.template_list.count()):
            item = window.template_list.item(row)
            if item.data(Qt.UserRole) == tid:
                return bool(item.flags() & Qt.ItemIsEnabled)
        return None

    def _template_checked(tid):
        for row in range(window.template_list.count()):
            item = window.template_list.item(row)
            if item.data(Qt.UserRole) == tid:
                return item.checkState() == Qt.Checked
        return None

    # Nothing ticked: everything shows enabled.
    check("grey-out: all enabled with nothing ticked",
          all(_template_enabled(tid) for tid in template_labels),
          str({tid: _template_enabled(tid) for tid in template_labels}))
    # Tick the text-only dumb model: the capability templates grey out.
    set_checked_list(window.model_list, {"fake-dumb:1b"})
    qt_app.processEvents()
    check("grey-out: vision greyed with a text-only model ticked",
          _template_enabled("vision") is False)
    check("grey-out: tools greyed with a text-only model ticked",
          _template_enabled("tool_calling") is False)
    check("grey-out: long context greyed with a text-only model ticked",
          _template_enabled("long_context") is False)
    check("grey-out: image gen greyed with a text-only model ticked",
          _template_enabled("image_generation") is False)
    check("grey-out: audio greyed with a text-only model ticked",
          _template_enabled("audio") is False)
    check("grey-out: text templates stay enabled",
          _template_enabled("coding") and _template_enabled("multi_turn")
          and _template_enabled("speed"))
    window.grab().save(os.path.join(VERIFY_DIR, "screenshot-greyed.png"))
    # A greyed-out template cannot be selected, even programmatically.
    for row in range(window.template_list.count()):
        item = window.template_list.item(row)
        if item.data(Qt.UserRole) == "vision":
            item.setCheckState(Qt.Checked)
    qt_app.processEvents()
    check("grey-out: a checked disabled template is not selected",
          "vision" not in [t["id"] for t in window.selected_templates()])
    # Clear that programmatic tick again (Select all skips disabled
    # items, so it would leave it checked and spoil the next check).
    for row in range(window.template_list.count()):
        item = window.template_list.item(row)
        if item.data(Qt.UserRole) == "vision":
            item.setCheckState(Qt.Unchecked)
    window.select_all_templates_button.click()
    qt_app.processEvents()
    check("grey-out: Select all skips disabled templates",
          _template_checked("vision") is False
          and _template_checked("coding") is True)
    window.clear_templates_button.click()
    qt_app.processEvents()
    # Tick the smart model too: vision, tools and long context come back.
    set_checked_list(window.model_list, {"fake-dumb:1b", "fake-smart:2b"})
    qt_app.processEvents()
    check("grey-out: vision enabled once a vision model is ticked",
          _template_enabled("vision") is True)
    check("grey-out: tools enabled once a tools model is ticked",
          _template_enabled("tool_calling") is True)
    check("grey-out: long context enabled once a long-context model is ticked",
          _template_enabled("long_context") is True)
    check("grey-out: image gen still greyed (no model has it)",
          _template_enabled("image_generation") is False)
    check("grey-out: audio still greyed (no model has it)",
          _template_enabled("audio") is False)
    # Ticking a model unchecks templates it just greyed out.
    set_checked_templates(window, {"vision", "coding"})
    check("grey-out: vision ticked while enabled",
          _template_checked("vision") is True)
    set_checked_list(window.model_list, {"fake-dumb:1b"})
    qt_app.processEvents()
    check("grey-out: ticking a text-only model unticks the vision template",
          _template_checked("vision") is False)
    check("grey-out: the text template stays ticked",
          _template_checked("coding") is True)
    # Untick everything: all enabled again.
    set_checked_list(window.model_list, set())
    qt_app.processEvents()
    check("grey-out: all enabled again with nothing ticked",
          all(_template_enabled(tid) for tid in template_labels))
    window.clear_templates_button.click()
    qt_app.processEvents()
    # Screen models: drop the cache and re-screen from scratch.
    os.remove(CAPABILITIES_FILE)
    check("screen models: cache removed", not os.path.exists(CAPABILITIES_FILE))
    window.screen_button.click()
    qt_app.processEvents()
    wait_until(lambda: os.path.exists(CAPABILITIES_FILE), timeout=10, app=qt_app)
    check("screen models: cache rebuilt",
          os.path.exists(CAPABILITIES_FILE))
    model_texts = {window.model_list.item(r).text()
                   for r in range(window.model_list.count())}
    check("screen models: labels back after re-screening",
          "fake-smart:2b [vision, tools, long_context]" in model_texts,
          str(model_texts))

    # Tick everything explicitly (v3 starts with nothing ticked).
    all_models = {window.model_list.item(r).text()
                  for r in range(window.model_list.count())}
    all_templates = {t["id"] for t in templates}
    set_checked_list(window.model_list, all_models)
    set_checked_templates(window, all_templates)
    # Baselines for the v5 fake-server counts (the direct chat tests
    # above already used this server).
    chat_images_before_full = server.chat_images_seen
    chat_tools_before_full = server.chat_tools_seen
    window.run_button.click()
    done = wait_until(
        lambda: window.worker is None and window.save_button.isEnabled(),
        timeout=300, app=qt_app,
    )
    check("full run completes", done)
    check("status says Done", "Done." in window.status_label.text(),
          window.status_label.text())

    expected = {
        "fake-smart:2b": ("100%", "20.0", "done"),
        "fake-partial:3b": ("59%", "10.0", "done"),
        # v17: Speed scores now, so dumb's 5 tok/s is 25% on Speed
        # and its overall is 25/14 = 2%, not 0%.
        "fake-dumb:1b": ("2%", "5.0", "done"),
    }
    for name, (score, toks, status) in expected.items():
        row = row_for(window, name)
        check(f"{name} row present", row is not None)
        if row is not None:
            check(f"{name} overall {score}",
                  table_text(window, row, 1) == score, table_text(window, row, 1))
            check(f"{name} avg tok/s {toks}",
                  table_text(window, row, 2) == toks, table_text(window, row, 2))
            check(f"{name} status {status}",
                  table_text(window, row, 5) == status, table_text(window, row, 5))
    broken_row = row_for(window, "fake-broken:1b")
    check("broken row present", broken_row is not None)
    if broken_row is not None:
        check("broken status",
              table_text(window, broken_row, 5) == "failed: model exploded",
              table_text(window, broken_row, 5))
        check("broken score is n/a", table_text(window, broken_row, 1) == "n/a")
    order = [table_text(window, r, 0) for r in range(window.table.rowCount())]
    check("table sorted best score first, failed last",
          order == ["fake-smart:2b", "fake-partial:3b", "fake-dumb:1b", "fake-broken:1b"],
          str(order))

    # Detail pane: smart selected by default (row 0).
    detail = window.detail_view.toPlainText()
    check("detail shows smart coding 100%", "Coding: 100% (5/5)" in detail, detail[:400])
    check("detail shows per-task pass", "pass  fibonacci" in detail, detail[:400])
    # v17: Speed scores now — smart's 20 tok/s is 100% on all 3 lengths.
    check("detail shows speed scored", "Speed: 100% (3/3), 20.0 tok/s" in detail, detail[:400])
    # Click the dumb row and check its detail.
    dumb_row = row_for(window, "fake-dumb:1b")
    window.table.cellClicked.emit(dumb_row, 0)
    qt_app.processEvents()
    detail = window.detail_view.toPlainText()
    check("detail shows dumb coding 0%", "Coding: 0% (0/5)" in detail, detail[:400])
    check("detail shows per-task fail", "fail  fibonacci" in detail, detail[:400])

    # Size in RAM recorded in the session results.
    check("size in RAM recorded",
          window.results.get("fake-partial:3b", {}).get("size_ram") == 3_000_000_000,
          str(window.results.get("fake-partial:3b", {}).get("size_ram")))

    window.grab().save(os.path.join(VERIFY_DIR, "screenshot-results.png"))

    # Run files on disk: 6 per working model, none for the broken one.
    smart_dir = os.path.join(RESULTS_DIR, "fake-smart_2b", sid)
    smart_files = sorted(os.listdir(smart_dir)) if os.path.isdir(smart_dir) else []
    check("smart has 17 run files", len(smart_files) == 17, str(smart_files))
    broken_dir = os.path.join(RESULTS_DIR, "fake-broken_1b")
    check("broken model has no run files", not os.path.isdir(broken_dir))
    if smart_files:
        with open(os.path.join(smart_dir, smart_files[0]), encoding="utf-8") as handle:
            sample = json.load(handle)
        check("run file has model+template+system",
              sample.get("model") == "fake-smart:2b"
              and sample.get("system_id") == sid
              and "template" in sample, str(sorted(sample.keys())))
        check("run file has machine details",
              isinstance(sample.get("machine"), dict) and "cpu" in sample["machine"])
        check("run file tasks carry answers and scores",
              all("answer" in t and "passed" in t and "decode_tok_s" in t
                  for t in sample.get("tasks", [])), str(sample.get("tasks", [])[:1]))
        shutil.copy(os.path.join(smart_dir, smart_files[0]),
                    os.path.join(VERIFY_DIR, "sample-run-file.json"))

    # ---------- v5: per-template scores and skips from the full run ----------
    smart_templates = window.results["fake-smart:2b"]["templates"]
    partial_templates = window.results["fake-partial:3b"]["templates"]
    dumb_templates = window.results["fake-dumb:1b"]["templates"]
    smart_run = {tid: run for tid, run in smart_templates.items()
                 if "skipped" not in run}
    check("smart ran 17 templates", len(smart_run) == 17,
          str(sorted(smart_run)))
    for tid in sorted(smart_run):
        run = smart_run[tid]
        if run.get("template_score") is not None:
            check(f"smart {tid} 100%", run["template_score"] == 100.0,
                  str(run.get("template_score")))
    partial_run = {tid: run for tid, run in partial_templates.items()
                   if "skipped" not in run}
    check("partial skipped tools as not eligible",
          partial_templates.get("tool_calling", {}).get("skipped") == "not eligible",
          str(partial_templates.get("tool_calling")))
    check("partial ran 16 templates", len(partial_run) == 16,
          str(sorted(partial_run)))
    for tid in sorted(partial_run):
        run = partial_run[tid]
        if run.get("template_score") is not None:
            # v16: math is 10 tasks now, partial gets 5 of them.
            # v17: Speed scores too — partial's 10 tok/s is 50%.
            want16 = 50.0 if tid in ("math", "speed") else 60.0
            check(f"partial {tid} {want16:.0f}%",
                  run["template_score"] == want16,
                  str(run.get("template_score")))
    dumb_run = {tid: run for tid, run in dumb_templates.items()
                if "skipped" not in run}
    check("dumb skipped vision as not eligible",
          dumb_templates.get("vision", {}).get("skipped") == "not eligible")
    check("dumb skipped tools as not eligible",
          dumb_templates.get("tool_calling", {}).get("skipped") == "not eligible")
    check("dumb skipped long context as not eligible",
          dumb_templates.get("long_context", {}).get("skipped") == "not eligible")
    check("dumb ran 14 templates", len(dumb_run) == 14,
          str(sorted(dumb_run)))
    for tid in sorted(dumb_run):
        run = dumb_run[tid]
        if run.get("template_score") is not None:
            # v17: Speed scores — dumb's 5 tok/s is 25%.
            want17 = 25.0 if tid == "speed" else 0.0
            check(f"dumb {tid} {want17:.0f}%",
                  run["template_score"] == want17,
                  str(run.get("template_score")))
    partial_dir = os.path.join(RESULTS_DIR, "fake-partial_3b", sid)
    dumb_dir = os.path.join(RESULTS_DIR, "fake-dumb_1b", sid)
    check("partial has no tools run file",
          not any(f.endswith("-tool_calling.json")
                  for f in os.listdir(partial_dir)))
    check("dumb has no vision run file",
          not any(f.endswith("-vision.json") for f in os.listdir(dumb_dir)))
    check("dumb has no tools run file",
          not any(f.endswith("-tool_calling.json") for f in os.listdir(dumb_dir)))
    check("dumb has no long-context run file",
          not any(f.endswith("-long_context.json") for f in os.listdir(dumb_dir)))
    partial_row = row_for(window, "fake-partial:3b")
    window.table.cellClicked.emit(partial_row, 0)
    qt_app.processEvents()
    detail = window.detail_view.toPlainText()
    check("detail shows partial tools not eligible",
          "Tool Calling: not eligible for this model" in detail, detail[:700])
    window.table.cellClicked.emit(dumb_row, 0)
    qt_app.processEvents()
    detail = window.detail_view.toPlainText()
    check("detail shows dumb vision not eligible",
          "Vision: not eligible for this model" in detail, detail[:700])
    check("detail shows dumb tools not eligible",
          "Tool Calling: not eligible for this model" in detail, detail[:700])
    check("detail shows dumb long context not eligible",
          "Long Context: not eligible for this model" in detail, detail[:700])
    # Multi-turn state: the fake saw the turn-2 request carry the
    # turn-1 prompt and its own answer.
    code_word_turn2 = None
    for messages in server.chat_history:
        texts = [m.get("content") or "" for m in messages]
        if (len(messages) >= 3
                and any("code word is BANANA" in text for text in texts)
                and any("What is the code word?" in text for text in texts)):
            code_word_turn2 = messages
            break
    check("multi-turn turn 2 carried the full history",
          code_word_turn2 is not None)
    if code_word_turn2 is not None:
        check("multi-turn history is user, assistant, user",
              [m.get("role") for m in code_word_turn2]
              == ["user", "assistant", "user"],
              str([m.get("role") for m in code_word_turn2]))
    check("vision images reached the fake (10: smart 5 + partial 5)",
          server.chat_images_seen == chat_images_before_full + 10,
          f"delta {server.chat_images_seen - chat_images_before_full}")
    check("tool calls reached the fake (5: smart only)",
          server.chat_tools_seen == chat_tools_before_full + 5,
          f"delta {server.chat_tools_seen - chat_tools_before_full}")
    speed_files = [f for f in os.listdir(smart_dir) if f.endswith("-speed.json")]
    check("smart has exactly 1 speed run file", len(speed_files) == 1,
          str(speed_files))
    with open(os.path.join(smart_dir, speed_files[0]), encoding="utf-8") as handle:
        speed_data = json.load(handle)
    check("speed run has 3 context-length runs", len(speed_data["tasks"]) == 3,
          str(len(speed_data["tasks"])))
    check("speed run records 1K, 8K, 32K",
          [t.get("context_tokens") for t in speed_data["tasks"]]
          == [1024, 8192, 32768],
          str([t.get("context_tokens") for t in speed_data["tasks"]]))
    check("speed prompts grow with context length",
          speed_data["tasks"][0]["prompt_chars"]
          < speed_data["tasks"][1]["prompt_chars"]
          < speed_data["tasks"][2]["prompt_chars"],
          str([t.get("prompt_chars") for t in speed_data["tasks"]]))
    multi_files = [f for f in os.listdir(smart_dir)
                   if f.endswith("-multi_turn.json")]
    with open(os.path.join(smart_dir, multi_files[0]), encoding="utf-8") as handle:
        multi_data = json.load(handle)
    code_word = [t for t in multi_data["tasks"] if t["id"] == "code-word"][0]
    check("multi-turn run file records both turns",
          len(code_word.get("turns", [])) == 2,
          str(len(code_word.get("turns", []))))
    check("multi-turn turn 2 answer holds the code word",
          "BANANA" in code_word["turns"][1]["answer"],
          code_word["turns"][1]["answer"][:60])
    # A vision result in the detail view, for the screenshot.
    # Vision is the last template alphabetically, so scroll the
    # detail view down to it before grabbing.
    smart_row = row_for(window, "fake-smart:2b")
    window.table.cellClicked.emit(smart_row, 0)
    qt_app.processEvents()
    window.detail_view.find("Vision:")
    qt_app.processEvents()
    window.grab().save(os.path.join(VERIFY_DIR, "screenshot-vision.png"))

    # ---------- profiles auto-generated by the run ----------
    smart_profile = os.path.join(RESULTS_DIR, "fake-smart_2b", "PROFILE.md")
    check("smart PROFILE.md written by the run", os.path.exists(smart_profile))
    if os.path.exists(smart_profile):
        with open(smart_profile, encoding="utf-8") as handle:
            prof = handle.read()
        check("smart profile latest 100%",
              "Latest overall score: 100%" in prof, prof[:500])
        check("smart profile verdict Strong", "Strong on this PC" in prof, prof)
        check("smart profile strengths list Coding",
              "- Coding" in prof.split("## Strengths")[1].split("## Weaknesses")[0],
              prof)
        check("smart profile no weaknesses",
              "None yet: no template under 50%." in prof, prof)

    # ---------- save report ----------
    window.save_button.click()
    wait_until(lambda: any(f.startswith("bench-results-") for f in os.listdir(RESULTS_DIR)),
               timeout=10, app=qt_app)
    report_files = [f for f in os.listdir(RESULTS_DIR) if f.startswith("bench-results-")]
    check("report writes 2 files", len(report_files) == 2, str(report_files))
    txt_files = [f for f in report_files if f.endswith(".txt")]
    json_files = [f for f in report_files if f.endswith(".json")]
    check("report txt + json", len(txt_files) == 1 and len(json_files) == 1)
    if txt_files and json_files:
        txt_path = os.path.join(RESULTS_DIR, txt_files[0])
        json_path = os.path.join(RESULTS_DIR, json_files[0])
        with open(txt_path, encoding="utf-8") as handle:
            txt = handle.read()
        check("txt has smart 100%", "Overall score: 100%" in txt)
        check("txt has per-template line", "Coding: 100% (5/5)" in txt)
        check("txt has failed model", "fake-broken:1b" in txt)
        with open(json_path, encoding="utf-8") as handle:
            data = json.load(handle)
        by_name = {m["name"]: m for m in data.get("models", [])}
        check("json smart overall 100.0",
              by_name.get("fake-smart:2b", {}).get("overall_score") == 100.0)
        check("json smart has 17 templates",
              len(by_name.get("fake-smart:2b", {}).get("templates", {})) == 17)
        shutil.copy(txt_path, os.path.join(VERIFY_DIR, "saved-report.txt"))
        shutil.copy(json_path, os.path.join(VERIFY_DIR, "saved-report.json"))

    # ---------- immutability through the GUI: run speed again ----------
    speed_files_before = sorted(f for f in os.listdir(smart_dir) if f.endswith("-speed.json"))
    check("one speed file before rerun", len(speed_files_before) == 1, str(speed_files_before))
    first_speed = os.path.join(smart_dir, speed_files_before[0])
    hash_before = file_hash(first_speed)
    set_checked_list(window.model_list, {"fake-smart:2b"})
    set_checked_templates(window, {"speed"})
    window.run_button.click()
    done = wait_until(
        lambda: window.worker is None and window.save_button.isEnabled(),
        timeout=120, app=qt_app,
    )
    check("speed rerun completes", done)
    speed_files_after = sorted(f for f in os.listdir(smart_dir) if f.endswith("-speed.json") or "-speed-" in f)
    check("two speed files after rerun", len(speed_files_after) == 2, str(speed_files_after))
    check("first speed file bytes unchanged", file_hash(first_speed) == hash_before)

    # ---------- history tab ----------
    window.tabs.setCurrentIndex(3)
    wait_until(lambda: window.history_model_combo.count() == 3, timeout=10, app=qt_app)
    check("history lists 3 models", window.history_model_combo.count() == 3,
          str(window.history_model_combo.count()))
    smart_index = None
    for i in range(window.history_model_combo.count()):
        if window.history_model_combo.itemData(i) == "fake-smart_2b":
            smart_index = i
    check("history has smart model", smart_index is not None)
    check("history picker is wide enough for the longest model name",
          window.history_model_combo.width() >=
          window.history_model_combo.fontMetrics().horizontalAdvance("fake-partial:3b"),
          f"width={window.history_model_combo.width()}")
    if smart_index is not None:
        window.history_model_combo.setCurrentIndex(smart_index)
        wait_until(lambda: window.history_list.count() == 18, timeout=10, app=qt_app)
        check("history lists 18 smart runs", window.history_list.count() == 18,
              str(window.history_list.count()))
        coded_item = None
        for i in range(window.history_list.count()):
            item = window.history_list.item(i)
            if item.data(Qt.UserRole).endswith("-coding.json"):
                coded_item = item
                break
        check("history has a coding run", coded_item is not None)
        if coded_item is not None:
            window.history_list.itemClicked.emit(coded_item)
            qt_app.processEvents()
            summary = window.history_view.toPlainText()
            check("history summary shows model", "fake-smart:2b" in summary, summary[:300])
            check("history summary shows score", "100%" in summary, summary[:300])
            check("history summary shows system", sid in summary, summary[:300])
    window.grab().save(os.path.join(VERIFY_DIR, "screenshot-history.png"))

    # ---------- profiles tab (GUI) ----------
    window.tabs.setCurrentIndex(4)
    wait_until(lambda: window.profile_model_combo.count() == 3, timeout=10, app=qt_app)
    check("profiles lists 3 models", window.profile_model_combo.count() == 3,
          str(window.profile_model_combo.count()))
    smart_index = None
    for i in range(window.profile_model_combo.count()):
        if window.profile_model_combo.itemData(i) == "fake-smart_2b":
            smart_index = i
    check("profiles has smart model", smart_index is not None)
    check("profile picker is wide enough for the longest model name",
          window.profile_model_combo.width() >=
          window.profile_model_combo.fontMetrics().horizontalAdvance("fake-partial:3b"),
          f"width={window.profile_model_combo.width()}")
    if smart_index is not None:
        window.profile_model_combo.setCurrentIndex(smart_index)
        wait_until(lambda: "fake-smart:2b" in window.profile_view.toPlainText(),
                   timeout=10, app=qt_app)
        prof_text = window.profile_view.toPlainText()
        check("profiles tab shows smart profile",
              "Latest overall score: 100%" in prof_text, prof_text[:400])
        check("profile view is read-only", window.profile_view.isReadOnly())
    window.grab().save(os.path.join(VERIFY_DIR, "screenshot-profiles.png"))

    # Regenerate: add two later, all-fail run files and rebuild.
    coding_file = os.path.join(smart_dir, sorted(
        f for f in os.listdir(smart_dir) if f.endswith("-coding.json"))[0])
    coding_hash = file_hash(coding_file)
    with open(coding_file, encoding="utf-8") as handle:
        coding_data = json.load(handle)
    # "Later" must be later than the full run's own files, whenever
    # the suite runs — a fixed 09:00 stamp only worked before 09:00
    # (the v9 run was at 00:14; a 10:07 run failed both checks).
    latest_stamp = max(
        json.load(open(os.path.join(smart_dir, f), encoding="utf-8")).get(
            "timestamp", "")
        for f in os.listdir(smart_dir) if f.endswith(".json")
    )
    late_dt = datetime.fromisoformat(latest_stamp) + timedelta(days=1)
    late_stamp = late_dt.strftime("%Y-%m-%dT%H:%M:%S")
    late_prefix = late_dt.strftime("%Y-%m-%d-%H%M%S")
    for stamp, template, name, template_score in (
        (late_stamp, "coding", "Coding", 0.0),
        (late_stamp, "math", "Math", 0.0),
    ):
        late = dict(coding_data)
        late.update({
            "template": template, "template_name": name,
            "timestamp": stamp, "template_score": template_score,
            "passed": 0, "total": 5,
        })
        with open(os.path.join(smart_dir, f"{late_prefix}-{template}.json"),
                  "w", encoding="utf-8") as handle:
            json.dump(late, handle)
    window.profile_regen_button.click()
    wait_until(lambda: "Latest overall score: 88%" in window.profile_view.toPlainText(),
               timeout=10, app=qt_app)
    prof_text = window.profile_view.toPlainText()
    check("regenerated profile latest 88%",
          "Latest overall score: 88%" in prof_text, prof_text[:500])
    check("regenerated profile best still 100%",
          "Best overall score: 100%" in prof_text, prof_text[:500])
    # The view renders markdown now (v11 R4), so the plain text has
    # no "##" markers and no "- " bullets — the Weaknesses section is
    # the text between the rendered headings.
    weaknesses_text11 = (
        prof_text.split("Weaknesses", 1)[1].split("Verdict", 1)[0]
        if "Weaknesses" in prof_text else ""
    )
    check("regenerated profile weaknesses Coding and Math",
          "Coding" in weaknesses_text11
          and "Math" in weaknesses_text11, prof_text)
    check("regenerated profile verdict Strong", "Strong on this PC" in prof_text, prof_text)
    check("regenerate leaves run files untouched", file_hash(coding_file) == coding_hash)

    # ---------- pull: success ----------
    window.tabs.setCurrentIndex(2)
    window.pull_input.setText("fake-new:1b")
    window.pull_button.click()
    moved = wait_until(
        lambda: window.pull_bar.value() > 0 or "MB of" in window.pull_status.text(),
        timeout=10, app=qt_app,
    )
    check("pull progress bar moves / MB text appears", moved,
          f"bar={window.pull_bar.value()} status={window.pull_status.text()!r}")
    window.grab().save(os.path.join(VERIFY_DIR, "screenshot-pull.png"))
    done = wait_until(lambda: window.pull_worker is None, timeout=30, app=qt_app)
    check("pull completes", done)
    check("pull status says pulled",
          window.pull_status.text() == "Pulled fake-new:1b.",
          window.pull_status.text())
    check("pull bar at 100", window.pull_bar.value() == 100)
    check("pull input cleared", window.pull_input.text() == "")
    check("pull button re-enabled", window.pull_button.isEnabled())
    new_item = None
    for row in range(window.model_list.count()):
        item = window.model_list.item(row)
        if item.text() == "fake-new:1b":
            new_item = item
    check("pulled model appears in the list", new_item is not None)
    check("pulled model starts UNCHECKED",
          new_item is not None and new_item.checkState() == Qt.Unchecked)

    # ---------- pull: failure ----------
    server.pull_fail = True
    window.pull_input.setText("fake-bad:1b")
    window.pull_button.click()
    done = wait_until(lambda: window.pull_worker is None, timeout=30, app=qt_app)
    check("failed pull completes", done)
    check("failed pull shows plain error",
          "pull exploded" in window.pull_status.text(),
          window.pull_status.text())
    check("failed pull re-enables button", window.pull_button.isEnabled())
    check("window still responsive after failed pull", window.isVisible())
    server.pull_fail = False
    window.close()
    window.deleteLater()
    qt_app.processEvents()
    server.shutdown()
    server.server_close()

    # ---------- ps empty ----------
    server2, _t = fake_ollama.start_server()
    host, port = server2.server_address
    os.environ["OLLAMA_HOST"] = f"http://{host}:{port}"
    server2.ps_empty = True
    check("ps_sizes empty when /api/ps returns nothing",
          app.ps_sizes() == {}, str(app.ps_sizes()))
    server2.shutdown()
    server2.server_close()

    # ---------- cancel ----------
    server_slow, _t = fake_ollama.start_server()
    server_slow.slow = True
    host, port = server_slow.server_address
    os.environ["OLLAMA_HOST"] = f"http://{host}:{port}"
    window2 = app.MainWindow()
    window2.show()
    wait_until(lambda: window2.model_list.count() == 4, timeout=10, app=qt_app)
    set_checked_list(window2.model_list, {"fake-smart:2b"})
    set_checked_templates(window2, {"speed"})
    window2.run_button.click()
    wait_until(lambda: window2.worker is not None, timeout=10, app=qt_app)
    time.sleep(1.5)
    window2.cancel_button.click()
    done = wait_until(
        lambda: window2.worker is None and window2.run_button.isEnabled(),
        timeout=60, app=qt_app,
    )
    check("cancel finishes", done)
    check("cancel status",
          window2.status_label.text() == "Cancelled. Results so far are still in the table.",
          window2.status_label.text())
    check("cancelled model has no saved run",
          window2.results.get("fake-smart:2b", {}).get("status") != "done",
          str(window2.results.get("fake-smart:2b")))
    window2.close()
    window2.deleteLater()
    qt_app.processEvents()
    server_slow.shutdown()
    server_slow.server_close()

    # ---------- mid-stream close: OSError branch, run aborts ----------
    server3, _t = fake_ollama.start_server()
    host, port = server3.server_address
    os.environ["OLLAMA_HOST"] = f"http://{host}:{port}"
    server3.close_mid_stream = True
    server3.mid_stream_closes = 0
    window3 = app.MainWindow()
    window3.show()
    wait_until(lambda: window3.model_list.count() == 4, timeout=10, app=qt_app)
    set_checked_list(window3.model_list, {"fake-smart:2b", "fake-partial:3b"})
    set_checked_templates(window3, {"speed"})
    window3.run_button.click()
    done = wait_until(
        lambda: window3.worker is None and window3.run_button.isEnabled(),
        timeout=60, app=qt_app,
    )
    check("mid-stream run finishes", done)
    check("mid-stream status is the Ollama-died message",
          window3.status_label.text() == app.OLLAMA_DIED_MESSAGE,
          window3.status_label.text())
    smart_row = row_for(window3, "fake-smart:2b")
    check("mid-stream smart row failed with the hint",
          smart_row is not None
          and table_text(window3, smart_row, 5) == "failed: " + app.UNREACHABLE_HINT,
          table_text(window3, smart_row, 5) if smart_row is not None else "no row")
    check("mid-stream partial never attempted",
          row_for(window3, "fake-partial:3b") is None)
    # v17: the restart adds a third attempt (warmup + retry +
    # restart-retry) — the run still aborts, and the model after
    # the dead one is still never attempted.
    check("mid-stream close happened three times (warmup + retry + restart)",
          server3.mid_stream_closes == 3, f"got {server3.mid_stream_closes}")
    check("window responsive after mid-stream abort",
          window3.isVisible() and window3.run_button.isEnabled())
    window3.close()
    window3.deleteLater()
    qt_app.processEvents()
    server3.shutdown()
    server3.server_close()

    # ---------- server dies mid-run: stop-on-unreachable ----------
    server4, _t = fake_ollama.start_server()
    host, port = server4.server_address
    os.environ["OLLAMA_HOST"] = f"http://{host}:{port}"
    server4.generate_done = 0

    def _kill_server():
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if server4.generate_done >= 1:
                break
            time.sleep(0.05)
        server4.dead = True
        server4.shutdown()
        # v18: close the listening socket too — a dead Ollama's
        # socket is closed (an OOM kill takes the process with
        # it), so the next call fails fast on refused. Without
        # the close, connections sit in the backlog unanswered
        # and the v18 300 s first-reply window waits them out.
        server4.server_close()

    killer = threading.Thread(target=_kill_server, daemon=True)
    killer.start()
    window4 = app.MainWindow()
    window4.show()
    wait_until(lambda: window4.model_list.count() == 4, timeout=10, app=qt_app)
    set_checked_list(window4.model_list, {"fake-smart:2b", "fake-partial:3b"})
    set_checked_templates(window4, {"speed"})
    window4.run_button.click()
    done = wait_until(
        lambda: window4.worker is None and window4.run_button.isEnabled(),
        timeout=120, app=qt_app,
    )
    check("server-dies run finishes", done)
    check("server-dies status is the Ollama-died message",
          window4.status_label.text() == app.OLLAMA_DIED_MESSAGE,
          window4.status_label.text())
    check("server-dies partial never attempted",
          row_for(window4, "fake-partial:3b") is None)
    smart_row = row_for(window4, "fake-smart:2b")
    check("server-dies smart row failed with the hint",
          smart_row is not None
          and table_text(window4, smart_row, 5) == "failed: " + app.UNREACHABLE_HINT,
          table_text(window4, smart_row, 5) if smart_row is not None else "no row")
    window4.close()
    window4.deleteLater()
    qt_app.processEvents()
    killer.join(timeout=10)
    server4.server_close()

    # ---------- v17: run queues behind the auto-start ----------
    # API dead, CLI fallback list, Run pressed: the run waits while
    # Ollama starts itself, then runs. The fake serve answers
    # /api/tags but not /api/generate, so the model fails plainly
    # on the warmup — the point is the run started with no click
    # but Run.
    os.environ["PATH"] = FAKEBIN + os.pathsep + ORIGINAL_PATH
    os.environ["OLLAMA_HOST"] = f"http://127.0.0.1:{free_port()}"
    pidfile = os.path.join(VERIFY_DIR, "fake-ollama.pid")
    os.environ["FAKE_OLLAMA_PIDFILE"] = pidfile
    window5 = app.MainWindow()
    window5.show()
    wait_until(lambda: window5.model_list.count() == 2, timeout=10, app=qt_app)
    check("run-queue window lists CLI models", window5.model_list.count() == 2,
          str(window5.model_list.count()))
    check("run-queue run button enabled", window5.run_button.isEnabled())
    set_checked_list(window5.model_list, {"cli-model-a:2b"})
    set_checked_templates(window5, {"speed"})
    window5.run_button.click()
    queued = wait_until(
        lambda: window5.worker is not None
        or row_for(window5, "cli-model-a:2b") is not None,
        timeout=45, app=qt_app,
    )
    check("run queues behind the auto-start", queued)
    done5 = wait_until(
        lambda: window5.worker is None and window5.run_button.isEnabled(),
        timeout=120, app=qt_app,
    )
    check("queued run completes", done5)
    row5 = row_for(window5, "cli-model-a:2b")
    # The fake serve has no POST handler at all, so the generate
    # call gets a 501, not a 404.
    check("queued run model failed on the missing generate endpoint",
          row5 is not None
          and table_text(window5, row5, 5) == "failed: Ollama returned HTTP 501",
          table_text(window5, row5, 5) if row5 is not None else "no row")
    kill_pidfile(pidfile)

    # ---------- Start Ollama: success, then already-running ----------
    fake_port = free_port()
    os.environ["OLLAMA_HOST"] = f"http://127.0.0.1:{fake_port}"
    callsfile = os.path.join(VERIFY_DIR, "serve-calls.txt")
    os.environ["FAKE_OLLAMA_CALLS"] = callsfile
    if os.path.exists(callsfile):
        os.remove(callsfile)
    window5.start_ollama_button.click()
    started = wait_until(
        lambda: window5.model_list.count() == 2
        and window5.status_label.text() == "Ollama is running.",
        timeout=45, app=qt_app,
    )
    check("start Ollama succeeds", started, window5.status_label.text())
    served = {window5.model_list.item(r).text()
              for r in range(window5.model_list.count())}
    check("start Ollama refreshes the model list",
          served == {"serve-model-a:2b", "serve-model-b:1b"}, str(served))
    check("served models start UNCHECKED", not checked_names(window5.model_list))
    window5.start_ollama_button.click()
    again = wait_until(
        lambda: window5.status_label.text() == "Ollama is running.",
        timeout=45, app=qt_app,
    )
    check("start Ollama again when already running", again,
          window5.status_label.text())
    try:
        with open(callsfile, encoding="utf-8") as handle:
            calls = handle.read().splitlines()
    except OSError:
        calls = []
    check("already-running start does not launch a second serve",
          len(calls) == 1, f"calls={calls}")
    kill_pidfile(pidfile)
    window5.close()
    window5.deleteLater()
    qt_app.processEvents()
    del os.environ["FAKE_OLLAMA_PIDFILE"]
    del os.environ["FAKE_OLLAMA_CALLS"]

    # ---------- Start Ollama: timeout (serve never brings a server up) ----------
    # v17: the auto-start in __init__ already exercises the timeout
    # path — the window opens with Ollama down, starts the sleeping
    # serve itself, and times out. Do NOT click the button too: a
    # second click would spawn a second `sleep 120` the pidfile
    # cannot kill.
    app.START_OLLAMA_TIMEOUT = 3.0
    app.START_OLLAMA_POLL = 0.2
    os.environ["PATH"] = FAKEBIN_SLEEP + os.pathsep + ORIGINAL_PATH
    os.environ["OLLAMA_HOST"] = f"http://127.0.0.1:{free_port()}"
    pidfile2 = os.path.join(VERIFY_DIR, "fake-ollama-sleep.pid")
    os.environ["FAKE_OLLAMA_PIDFILE"] = pidfile2
    window6 = app.MainWindow()
    window6.show()
    timed_out = wait_until(
        lambda: window6.status_label.text() == app.START_OLLAMA_TIMEOUT_MSG,
        timeout=30, app=qt_app,
    )
    check("start Ollama timeout message", timed_out, window6.status_label.text())
    kill_pidfile(pidfile2)
    window6.close()
    window6.deleteLater()
    qt_app.processEvents()
    del os.environ["FAKE_OLLAMA_PIDFILE"]
    app.START_OLLAMA_TIMEOUT = 30.0
    app.START_OLLAMA_POLL = 1.0

    # ---------- Start Ollama: no ollama binary on PATH ----------
    os.environ["PATH"] = ORIGINAL_PATH
    os.environ["OLLAMA_HOST"] = f"http://127.0.0.1:{free_port()}"
    window7 = app.MainWindow()
    window7.show()
    window7.start_ollama_button.click()
    missing = wait_until(
        lambda: window7.status_label.text() == app.START_OLLAMA_MISSING_MSG,
        timeout=30, app=qt_app,
    )
    check("start Ollama missing binary message", missing, window7.status_label.text())
    window7.close()
    window7.deleteLater()
    qt_app.processEvents()

    # ---------- ollama down (no binary, no server): v2 window4 ----------
    # v17: the window tries to start Ollama itself and reports there
    # is no binary to start — the status is the missing-binary
    # message, not the plain down message.
    window8 = app.MainWindow()
    window8.show()
    wait_until(lambda: window8.model_list.count() == 0, timeout=10, app=qt_app)
    check("ollama down: no models", window8.model_list.count() == 0)
    missing8 = wait_until(
        lambda: window8.status_label.text() == app.START_OLLAMA_MISSING_MSG,
        timeout=30, app=qt_app,
    )
    check("ollama down: status is the missing-binary message", missing8,
          window8.status_label.text())
    check("ollama down: run disabled", not window8.run_button.isEnabled())
    check("ollama down: start Ollama still enabled",
          window8.start_ollama_button.isEnabled())
    window8.close()
    window8.deleteLater()
    qt_app.processEvents()

    # ---------- CLI fallback (fakebin list): v2 window5 ----------
    os.environ["PATH"] = FAKEBIN + os.pathsep + ORIGINAL_PATH
    window9 = app.MainWindow()
    window9.show()
    wait_until(lambda: window9.model_list.count() == 2, timeout=10, app=qt_app)
    cli_names = {window9.model_list.item(r).text()
                 for r in range(window9.model_list.count())}
    check("CLI fallback lists both models",
          cli_names == {"cli-model-a:2b", "cli-model-b:1b"}, str(cli_names))
    check("CLI fallback status is the ready message",
          window9.status_label.text() == "2 models found, 19 templates loaded. Ready.",
          window9.status_label.text())
    check("CLI fallback run enabled", window9.run_button.isEnabled())
    window9.close()
    window9.deleteLater()
    qt_app.processEvents()
    os.environ["PATH"] = ORIGINAL_PATH

    # ================= v4: providers =================

    # ---------- unit tests: provider config load/save ----------
    check("no providers file at v4 start", not os.path.exists(PROVIDERS_FILE))
    loaded, warns = app.load_providers(PROVIDERS_FILE)
    check("load_providers missing file is empty, no warnings",
          loaded == [] and warns == [])
    with open(PROVIDERS_FILE, "w", encoding="utf-8") as handle:
        handle.write("{not valid json")
    loaded, warns = app.load_providers(PROVIDERS_FILE)
    check("load_providers corrupt file is empty with a warning",
          loaded == [] and len(warns) == 1, str(warns))
    with open(PROVIDERS_FILE, "w", encoding="utf-8") as handle:
        json.dump({"providers": "nope"}, handle)
    loaded, warns = app.load_providers(PROVIDERS_FILE)
    check("load_providers wrong shape is empty with a warning",
          loaded == [] and len(warns) == 1, str(warns))
    sample_providers = [{
        "name": "fake-provider",
        "base_url": "http://127.0.0.1:9/v1/",
        "api_key": "sk-fake-good-key",
        "models": [
            {"id": "fake-gpt-smart", "display_name": "Smart",
             "price_input_per_1m": 0.15, "price_output_per_1m": 0.60},
            {"id": "fake-gpt-dumb", "display_name": "",
             "price_input_per_1m": None, "price_output_per_1m": None},
        ],
    }]
    app.save_providers(sample_providers, PROVIDERS_FILE)
    loaded, warns = app.load_providers(PROVIDERS_FILE)
    check("providers round-trip keeps 1 provider, no warnings",
          len(loaded) == 1 and not warns, str(warns))
    check("base URL trailing slash stripped",
          loaded[0]["base_url"] == "http://127.0.0.1:9/v1", loaded[0]["base_url"])
    check("round-trip keeps the key",
          loaded[0]["api_key"] == "sk-fake-good-key")
    check("round-trip keeps prices",
          loaded[0]["models"][0]["price_input_per_1m"] == 0.15
          and loaded[0]["models"][0]["price_output_per_1m"] == 0.60)
    check("round-trip keeps no-price model",
          loaded[0]["models"][1]["price_input_per_1m"] is None)
    mode = os.stat(PROVIDERS_FILE).st_mode & 0o777
    check("providers file is owner-only (0600)", mode == 0o600, oct(mode))
    dup = {"providers": [
        {"name": "dup", "base_url": "http://x/v1", "api_key": "", "models": []},
        {"name": "dup", "base_url": "http://y/v1", "api_key": "", "models": []},
        {"name": "bad", "base_url": "http://z/v1", "api_key": "", "models": [
            {"display_name": "no id"},
            {"id": "bad-price", "price_input_per_1m": "free"},
            {"id": "good-one"},
        ]},
        {"base_url": "http://w/v1"},
    ]}
    with open(PROVIDERS_FILE, "w", encoding="utf-8") as handle:
        json.dump(dup, handle)
    loaded, warns = app.load_providers(PROVIDERS_FILE)
    check("duplicate provider skipped", len(loaded) == 2, str([p["name"] for p in loaded]))
    bad_entry = [p for p in loaded if p["name"] == "bad"][0]
    check("bad models skipped, good one kept",
          [m["id"] for m in bad_entry["models"]] == ["good-one"],
          str(bad_entry["models"]))
    check("loader warns for every skipped entry", len(warns) == 4, str(warns))
    os.remove(PROVIDERS_FILE)

    # ---------- unit tests: labels, slugs, cost ----------
    check("provider label format",
          app.provider_label("fake-provider", "fake-gpt-smart")
          == "fake-provider / fake-gpt-smart")
    check("provider label slug",
          app.slugify_model("fake-provider / fake-gpt-smart")
          == "fake-provider_fake-gpt-smart")
    check("estimate_cost exact",
          app.estimate_cost(560, 5600, 0.15, 0.60) == 0.003444)
    check("estimate_cost None without prices",
          app.estimate_cost(560, 5600, None, 0.60) is None)
    check("estimate_cost None without tokens",
          app.estimate_cost(None, 5600, 0.15, 0.60) is None)
    check("fmt_cost small", app.fmt_cost(0.003444) == "$0.0034")
    check("fmt_cost dollars", app.fmt_cost(1.5) == "$1.50")
    check("fmt_cost zero", app.fmt_cost(0) == "$0.00")
    check("fmt_cost None is n/a", app.fmt_cost(None) == "n/a")
    check("fmt_tokens commas", app.fmt_tokens(5600) == "5,600")
    target = app.provider_target(sample_providers[0], sample_providers[0]["models"][0])
    check("provider target label and kind",
          target["label"] == "fake-provider / fake-gpt-smart"
          and target["kind"] == "provider")
    check("provider target carries prices",
          target["price_input_per_1m"] == 0.15
          and target["price_output_per_1m"] == 0.60)
    check("ollama target kind and provider",
          app.ollama_target("qwen3:4b")["kind"] == "ollama"
          and app.ollama_target("qwen3:4b")["provider_name"] == "ollama")

    # ================= v5: new scorers, media, long docs =================

    # ---------- unit tests: contains_all ----------
    check("contains_all pass",
          app.score_contains_all("Red circles and blue squares.",
                                 ["circles", "squares"])[0])
    check("contains_all missing one fails",
          not app.score_contains_all("Red circles only.", ["circles", "squares"])[0])
    check("contains_all case-insensitive",
          app.score_contains_all("CIRCLES and SQUARES.", ["circles", "squares"])[0])

    # ---------- unit tests: refusal detection ----------
    check("is_refusal catches the stock refusal",
          app.is_refusal("I'm sorry, I can't help with that."))
    check("is_refusal catches i refuse",
          app.is_refusal("I refuse to do that."))
    check("is_refusal catches guidelines",
          app.is_refusal("That is against my guidelines."))
    check("is_refusal ignores a passing sorry",
          not app.is_refusal("Sorry, I meant 42."))
    check("is_refusal ignores a normal answer",
          not app.is_refusal("The robber slipped through the side door."))
    check("score_refusal comply + complied passes",
          app.score_refusal("Here is the story.", "comply")[0])
    check("score_refusal comply + refused fails",
          not app.score_refusal("I'm sorry, I can't help with that.", "comply")[0])
    check("score_refusal refuse + refused passes",
          app.score_refusal("I refuse to do that.", "refuse")[0])
    check("score_refusal refuse + complied fails",
          not app.score_refusal("Here is how.", "refuse")[0])

    # ---------- unit tests: schema validation ----------
    person_schema = {
        "type": "object",
        "required": ["name", "age"],
        "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
    }
    check("schema valid object passes",
          app.score_schema('{"name": "Ada", "age": 36}', person_schema)[0])
    check("schema missing key fails",
          not app.score_schema('{"name": "Ada"}', person_schema)[0])
    check("schema wrong type fails",
          not app.score_schema('{"name": "Ada", "age": "36"}', person_schema)[0])
    check("schema bool is not an integer",
          not app.score_schema('{"name": "Ada", "age": true}', person_schema)[0])
    check("schema fenced JSON fails (strict)",
          not app.score_schema('```json\n{"name": "Ada", "age": 36}\n```',
                               person_schema)[0])
    check("schema prose-wrapped JSON fails (strict)",
          not app.score_schema('Here it is: {"name": "Ada", "age": 36}',
                               person_schema)[0])
    check("schema trailing text fails (strict)",
          not app.score_schema('{"name": "Ada", "age": 36} done.', person_schema)[0])
    check("schema invalid JSON fails",
          not app.score_schema("not json at all", person_schema)[0])
    check("schema array of integers passes",
          app.score_schema("[4, 8, 15]",
                           {"type": "array", "items": {"type": "integer"}})[0])
    check("schema array with a bad item fails",
          not app.score_schema('[4, "8", 15]',
                               {"type": "array", "items": {"type": "integer"}})[0])
    check("schema enum pass",
          app.score_schema('"red"', {"type": "string", "enum": ["red", "blue"]})[0])
    check("schema enum fail",
          not app.score_schema('"green"',
                               {"type": "string", "enum": ["red", "blue"]})[0])

    # ---------- unit tests: steps ----------
    tea_steps = ["boil the water", "pour the water", "add the tea bag"]
    check("steps in order pass",
          app.score_steps("First boil the water. Then pour the water. "
                          "Add the tea bag.", tea_steps)[0])
    check("steps out of order fail",
          not app.score_steps("Add the tea bag. Boil the water. "
                              "Pour the water.", tea_steps)[0])
    check("steps missing one fail",
          not app.score_steps("Boil the water. Pour the water.", tea_steps)[0])
    check("steps with extra text pass",
          app.score_steps("Boil the water carefully, then pour the water "
                          "slowly, and finally add the tea bag.", tea_steps)[0])

    # ---------- unit tests: tool-call scoring ----------
    weather_task = {"expected_tool": "get_weather",
                    "expected_args": {"city": "Paris"}}
    check("tool_call exact match passes",
          app.score_tool_call(
              weather_task,
              [{"name": "get_weather", "arguments": {"city": "Paris"}}])[0])
    check("tool_call arg case-insensitive passes",
          app.score_tool_call(
              weather_task,
              [{"name": "get_weather", "arguments": {"city": "paris"}}])[0])
    check("tool_call extra args ignored",
          app.score_tool_call(
              weather_task,
              [{"name": "get_weather",
                "arguments": {"city": "Paris", "units": "metric"}}])[0])
    check("tool_call wrong tool fails",
          not app.score_tool_call(
              weather_task,
              [{"name": "calculate", "arguments": {"expression": "1+1"}}])[0])
    check("tool_call missing arg fails",
          not app.score_tool_call(
              weather_task,
              [{"name": "get_weather", "arguments": {}}])[0])
    check("tool_call wrong arg fails",
          not app.score_tool_call(
              weather_task,
              [{"name": "get_weather", "arguments": {"city": "London"}}])[0])
    check("tool_call no call fails",
          not app.score_tool_call(weather_task, [])[0])
    check("tool_call two calls fail",
          not app.score_tool_call(
              weather_task,
              [{"name": "get_weather", "arguments": {"city": "Paris"}},
               {"name": "get_weather", "arguments": {"city": "Paris"}}])[0])
    add_task = {"expected_tool": "add_numbers",
                "expected_args": {"a": 12, "b": 30}}
    check("tool_call numeric tolerance passes",
          app.score_tool_call(
              add_task,
              [{"name": "add_numbers", "arguments": {"a": 12.0, "b": 30}}])[0])
    check("tool_call numeric mismatch fails",
          not app.score_tool_call(
              add_task,
              [{"name": "add_numbers", "arguments": {"a": 12, "b": 31}}])[0])
    no_tool_task = {"expected_tool": None, "expected_args": {}}
    check("tool_call none needed, none made passes",
          app.score_tool_call(no_tool_task, [])[0])
    check("tool_call none needed, one made fails",
          not app.score_tool_call(
              no_tool_task,
              [{"name": "get_weather", "arguments": {"city": "Paris"}}])[0])
    check("score_task defers tool_call to its runner",
          app.score_task({"rule": "tool_call"}, "anything")[1]
          == "rule 'tool_call' is scored by its runner, not from text")
    check("score_task defers image to its runner",
          app.score_task({"rule": "image"}, "anything")[1]
          == "rule 'image' is scored by its runner, not from text")
    check("score_task defers audio to its runner",
          app.score_task({"rule": "audio"}, "anything")[1]
          == "rule 'audio' is scored by its runner, not from text")

    # ---------- unit tests: media validation ----------
    with open(os.path.join(PKG_DIR, "images", "shapes.png"), "rb") as handle:
        shapes_bytes = handle.read()
    check("image_info reads the bundled shapes PNG",
          app.image_info(shapes_bytes) == ("png", 400, 300),
          str(app.image_info(shapes_bytes)))
    with open(os.path.join(PKG_DIR, "images", "sign.png"), "rb") as handle:
        sign_bytes = handle.read()
    check("image_info reads the bundled sign PNG",
          app.image_info(sign_bytes) == ("png", 400, 200),
          str(app.image_info(sign_bytes)))
    # Minimal JPEG: SOI + SOF0 with 16x32 dims.
    fake_jpeg = (
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
        b"\xff\xc0\x00\x11\x08\x00\x20\x00\x10\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    )
    check("image_info reads a JPEG header",
          app.image_info(fake_jpeg) == ("jpeg", 16, 32),
          str(app.image_info(fake_jpeg)))
    fake_gif = b"GIF89a" + bytes([64, 0, 48, 0]) + b"\x00" * 10
    check("image_info reads a GIF header",
          app.image_info(fake_gif) == ("gif", 64, 48),
          str(app.image_info(fake_gif)))
    check("image_info rejects garbage", app.image_info(b"not an image") is None)
    check("image_info rejects empty bytes", app.image_info(b"") is None)
    with open(os.path.join(PKG_DIR, "audio", "clip-hello.wav"), "rb") as handle:
        clip_bytes = handle.read()
    check("is_audio_bytes accepts the bundled WAV",
          app.is_audio_bytes(clip_bytes))
    check("is_audio_bytes accepts MP3 sync bytes",
          app.is_audio_bytes(b"\xff\xfb" + b"\x00" * 10))
    check("is_audio_bytes accepts ID3",
          app.is_audio_bytes(b"ID3" + b"\x00" * 10))
    check("is_audio_bytes rejects garbage",
          not app.is_audio_bytes(b"this is not audio"))
    check("is_audio_bytes rejects empty bytes", not app.is_audio_bytes(b""))

    # ---------- v10: the bundled clips are real speech (T12) ----------
    # A pure sine tone has a near-constant amplitude envelope; speech
    # varies a lot. Coefficient of variation of per-10ms-window RMS:
    # the old tones measured 0.005-0.008, the speech clips 0.94-1.41.
    def _wav_envelope_cv(path):
        with wave.open(path, "rb") as handle:
            rate = handle.getframerate()
            raw = handle.readframes(handle.getnframes())
        samples = struct.unpack("<%dh" % (len(raw) // 2), raw)
        window = max(1, rate // 100)
        rmss = []
        for i in range(0, len(samples) - window + 1, window):
            chunk = samples[i:i + window]
            rmss.append(math.sqrt(
                sum(s * s for s in chunk) / len(chunk)))
        mean = sum(rmss) / len(rmss)
        var = sum((r - mean) ** 2 for r in rmss) / len(rmss)
        return math.sqrt(var) / mean if mean else 0.0

    clip_names = ("clip-hello.wav", "clip-count.wav", "clip-weather.wav")
    for clip_name in clip_names:
        clip_path = os.path.join(PKG_DIR, "audio", clip_name)
        clip_cv = _wav_envelope_cv(clip_path)
        check(f"bundled {clip_name} is speech, not a tone",
              clip_cv > 0.25, f"envelope CV={clip_cv:.3f}")
    import make_fixtures
    digests_before = {}
    for clip_name in clip_names:
        with open(os.path.join(PKG_DIR, "audio", clip_name), "rb") as handle:
            digests_before[clip_name] = hashlib.sha256(handle.read()).hexdigest()
    make_fixtures.build_fixtures()
    for clip_name, digest in digests_before.items():
        with open(os.path.join(PKG_DIR, "audio", clip_name), "rb") as handle:
            digest_after = hashlib.sha256(handle.read()).hexdigest()
        check(f"make_fixtures keeps existing {clip_name}",
              digest_after == digest)

    # ---------- unit tests: long documents and padded prompts ----------
    needle = "The vault code is ZEBRA-7741."
    for position in ("start", "middle", "end"):
        doc = app.build_long_document(needle, 1024, position)
        check(f"long doc {position} holds the needle", needle in doc)
        check(f"long doc {position} is about 1K tokens",
              3900 < len(doc) < 4400, str(len(doc)))
    doc_start = app.build_long_document(needle, 1024, "start")
    check("long doc start plants the needle first",
          doc_start.startswith(needle))
    doc_end = app.build_long_document(needle, 1024, "end")
    check("long doc end plants the needle last", doc_end.endswith(needle))
    doc_32k = app.build_long_document(needle, 32768, "middle")
    check("long doc 32K is about 131K chars",
          130000 < len(doc_32k) < 132000, str(len(doc_32k)))
    padded = app.build_context_prompt("Write a short story.", 8192)
    check("padded prompt keeps the base prompt",
          padded.startswith("Write a short story."))
    check("padded prompt is about 8K tokens",
          32000 < len(padded) < 33000, str(len(padded)))

    # ---------- unit tests: eligibility and capability labels ----------
    text_target = app.ollama_target("plain:1b")
    vision_target = app.ollama_target("sees:2b", ["vision"])
    full_target = app.ollama_target("does-all:2b",
                                    ["vision", "tools", "long_context"])
    check("text template runs on a text-only model",
          app.template_eligible(by_id["math"], text_target))
    check("vision template skipped on a text-only model",
          not app.template_eligible(by_id["vision"], text_target))
    check("vision template runs on a vision model",
          app.template_eligible(by_id["vision"], vision_target))
    check("tools template skipped on a vision-only model",
          not app.template_eligible(by_id["tool_calling"], vision_target))
    check("tools template runs on a full model",
          app.template_eligible(by_id["tool_calling"], full_target))
    check("image gen skipped on a local model",
          not app.template_eligible(by_id["image_generation"], full_target))
    media_target = app.provider_target(
        {"name": "p", "base_url": "http://x/v1", "api_key": ""},
        {"id": "m", "capabilities": ["text", "image_generation", "audio"]},
    )
    check("image gen runs on a media provider model",
          app.template_eligible(by_id["image_generation"], media_target))
    check("audio runs on a media provider model",
          app.template_eligible(by_id["audio"], media_target))
    check("vision skipped on a media provider model",
          not app.template_eligible(by_id["vision"], media_target))
    check("format_capabilities text-only is empty",
          app.format_capabilities(["text"]) == "")
    check("format_capabilities one cap",
          app.format_capabilities(["text", "vision"]) == " [vision]")
    check("format_capabilities keeps capability order",
          app.format_capabilities(["tools", "text", "vision"])
          == " [vision, tools]")
    check("ollama_target defaults to text-only",
          app.ollama_target("plain:1b")["capabilities"] == ["text"])
    check("provider_target defaults to text-only",
          app.provider_target(
              {"name": "p", "base_url": "http://x/v1", "api_key": ""},
              {"id": "m"})["capabilities"] == ["text"])

    # ---------- unit tests: provider capability flags ----------
    caps_providers = [{
        "name": "caps-provider",
        "base_url": "http://x/v1",
        "api_key": "",
        "models": [
            {"id": "sees-all", "capabilities": ["text", "vision", "tools"]},
            {"id": "weird", "capabilities": ["text", "telepathy"]},
            {"id": "empty", "capabilities": []},
            {"id": "notalist", "capabilities": "vision"},
            {"id": "nocaps"},
        ],
    }]
    app.save_providers(caps_providers, PROVIDERS_FILE)
    loaded, warns = app.load_providers(PROVIDERS_FILE)
    caps_by_id = {m["id"]: m.get("capabilities")
                  for m in loaded[0]["models"]}
    check("provider caps round-trip",
          caps_by_id.get("sees-all") == ["text", "vision", "tools"],
          str(caps_by_id))
    check("unknown capability dropped with a warning",
          caps_by_id.get("weird") == ["text"]
          and any("telepathy" in w for w in warns), str(warns))
    check("empty capability list becomes text-only",
          caps_by_id.get("empty") == ["text"], str(caps_by_id))
    check("non-list capabilities become text-only with a warning",
          caps_by_id.get("notalist") == ["text"], str(caps_by_id))
    check("missing capabilities become text-only",
          caps_by_id.get("nocaps") == ["text"], str(caps_by_id))
    os.remove(PROVIDERS_FILE)

    # ---------- unit tests: capabilities cache ----------
    # The GUI run above already wrote the cache file; drop it first
    # so the missing-file check means what it says.
    if os.path.exists(CAPABILITIES_FILE):
        os.remove(CAPABILITIES_FILE)
    check("capabilities cache missing file is empty",
          app.load_capabilities(CAPABILITIES_FILE) == {})
    app.save_capabilities(
        {"fake-smart:2b": ["text", "vision", "tools", "long_context"],
         "fake-dumb:1b": ["text"]},
        CAPABILITIES_FILE,
    )
    cache = app.load_capabilities(CAPABILITIES_FILE)
    check("capabilities cache round-trip",
          cache.get("fake-smart:2b") == ["long_context", "text", "tools", "vision"]
          and cache.get("fake-dumb:1b") == ["text"], str(cache))
    with open(CAPABILITIES_FILE, "w", encoding="utf-8") as handle:
        json.dump({"ollama": {"weird:1b": ["text", "telepathy"],
                              "plain:1b": ["text"]}}, handle)
    cache = app.load_capabilities(CAPABILITIES_FILE)
    check("capabilities cache drops unknown capability names",
          cache.get("weird:1b") == ["text"], str(cache))
    check("capabilities cache keeps known entries",
          cache.get("plain:1b") == ["text"], str(cache))
    with open(CAPABILITIES_FILE, "w", encoding="utf-8") as handle:
        handle.write("{not valid json")
    check("capabilities cache corrupt file is empty",
          app.load_capabilities(CAPABILITIES_FILE) == {})
    os.remove(CAPABILITIES_FILE)

    # ---------- unit tests: template capability validation ----------
    def _write_template(filename, template):
        with open(os.path.join(TEMPLATES_TEST_DIR, filename),
                  "w", encoding="utf-8") as handle:
            json.dump(template, handle)

    _write_template("badcap.json", {
        "id": "badcap", "name": "Bad Cap", "scored": True,
        "capability": "telepathy",
        "tasks": [{"id": "t1", "prompt": "x", "rule": "contains", "expected": "x"}],
    })
    _write_template("badchat.json", {
        "id": "badchat", "name": "Bad Chat", "scored": True,
        "tasks": [{"id": "t1", "kind": "chat",
                   "turns": [{"prompt": "hello"}]}],
    })
    _write_template("badtools.json", {
        "id": "badtools", "name": "Bad Tools", "scored": True,
        "capability": "tools",
        "tasks": [{"id": "t1", "kind": "tools", "prompt": "x",
                   "rule": "contains", "expected": "x", "tools": [{"name": "t"}]}],
    })
    _write_template("badimage.json", {
        "id": "badimage", "name": "Bad Image", "scored": True,
        "capability": "image_generation",
        "tasks": [{"id": "t1", "kind": "image_gen", "prompt": "x",
                   "rule": "contains", "expected": "x"}],
    })
    _write_template("badlong.json", {
        "id": "badlong", "name": "Bad Long", "scored": True,
        "capability": "long_context",
        "tasks": [{"id": "t1", "kind": "long_context", "needle": "n",
                   "context_length": 1024, "position": "sideways",
                   "prompt": "x", "rule": "contains", "expected": "n"}],
    })
    loaded, warns = app.load_templates(TEMPLATES_TEST_DIR)
    loaded_ids = [t["id"] for t in loaded]
    check("unknown capability template skipped",
          "badcap" not in loaded_ids, str(loaded_ids))
    check("unknown capability warned",
          any("badcap.json" in w for w in warns), str(warns))
    check("chat task with no scored turn skipped",
          "badchat" not in loaded_ids, str(loaded_ids))
    check("tools task with a text rule skipped",
          "badtools" not in loaded_ids, str(loaded_ids))
    check("image task with a text rule skipped",
          "badimage" not in loaded_ids, str(loaded_ids))
    check("long-context task with a bad position skipped",
          "badlong" not in loaded_ids, str(loaded_ids))
    check("good templates still load beside the bad ones",
          len(loaded) == 20, str(len(loaded)))

    # ---------- fake provider server + direct client tests ----------
    pserver, _t = fake_ollama.start_provider_server()
    phost, pport = pserver.server_address
    provider = {
        "name": "fake-provider",
        "base_url": f"http://{phost}:{pport}/v1",
        "api_key": "sk-fake-good-key",
        "models": [],
    }
    print(f"fake provider on http://{phost}:{pport}")
    ok, msg = app.provider_test_connection(provider)
    check("test connection good, 3 models listed",
          ok and "3 models listed" in msg, msg)
    ok, msg = app.provider_test_connection(dict(provider, api_key="sk-wrong"))
    check("test connection bad key fails plainly",
          not ok and "rejected the key" in msg, msg)
    ok, msg = app.provider_test_connection(
        dict(provider, base_url="http://127.0.0.1:1/v1"))
    check("test connection dead provider fails plainly",
          not ok and "did not answer" in msg, msg)
    ok, msg = app.provider_test_connection(dict(provider, base_url=""))
    check("test connection empty base URL fails plainly",
          not ok and "base URL" in msg, msg)

    metrics = app.provider_generate_once(
        provider, "fake-gpt-smart",
        "Write a Python function named fib(n) that returns the nth Fibonacci number.",
        400,
    )
    check("provider generate returns answer text",
          "def fib" in metrics["answer"], metrics["answer"][:80])
    check("provider tokens reported",
          metrics["prompt_tokens"] == 20 and metrics["completion_tokens"] == 200
          and metrics["total_tokens"] == 220, str(metrics))
    check("provider decode speed measured",
          metrics["decode_tok_s"] is not None and metrics["decode_tok_s"] > 0,
          str(metrics["decode_tok_s"]))
    check("provider ttft measured",
          metrics["ttft_s"] is not None and 0 < metrics["ttft_s"] < 3.0,
          str(metrics["ttft_s"]))
    try:
        app.provider_generate_once(
            dict(provider, api_key="sk-wrong"), "fake-gpt-smart", "hello", 64)
        check("provider bad key raises", False)
    except app.ProviderError as exc:
        check("provider bad key raises plainly",
              str(exc) == "provider fake-provider rejected the key: Incorrect API key provided",
              str(exc))
        check("provider bad key is NOT unreachable (never retried)",
              not app.is_provider_unreachable(exc))
    try:
        app.provider_generate_once(
            dict(provider, base_url="http://127.0.0.1:1/v1"),
            "fake-gpt-smart", "hello", 64)
        check("provider dead raises", False)
    except app.ProviderError as exc:
        check("provider dead raises plainly",
              str(exc) == "provider fake-provider did not answer", str(exc))
        check("provider dead IS unreachable (retriable)",
              app.is_provider_unreachable(exc))
    try:
        app.provider_generate_once(provider, "fake-gpt-broken", "hello", 64)
        check("provider broken model raises", False)
    except app.ProviderError as exc:
        check("provider broken model raises plainly",
              str(exc) == "provider fake-provider: model exploded", str(exc))
    pserver.fast = False
    slow_metrics = app.provider_generate_once(
        provider, "fake-gpt-smart", "Say hello in one sentence.", 64)
    check("provider slow-mode decode near 100 tok/s",
          slow_metrics["decode_tok_s"] is not None
          and 70 < slow_metrics["decode_tok_s"] < 140,
          str(slow_metrics["decode_tok_s"]))
    pserver.fast = True

    # ---------- v5: provider chat with images, tools, roles ----------
    p_images_before = pserver.chat_images_seen
    chat = app.provider_chat_once(
        provider, "fake-gpt-smart",
        [{"role": "user", "content": [
            {"type": "text", "text": "How many red circles are in this image?"},
            {"type": "image_url",
             "image_url": {"url": "data:image/png;base64,aGVsbG8="}},
        ]}],
        300,
    )
    check("provider chat with an image part answers",
          "3 red circles" in chat["answer"], chat["answer"][:60])
    check("provider fake recorded the image part",
          pserver.chat_images_seen == p_images_before + 1,
          str(pserver.chat_images_seen))
    check("provider chat reports tokens",
          chat["prompt_tokens"] == 20 and chat["completion_tokens"] == 200,
          str({k: chat.get(k) for k in ("prompt_tokens", "completion_tokens")}))
    chat = app.provider_chat_once(
        provider, "fake-gpt-smart",
        [{"role": "user", "content": "What is the weather in Paris?"}],
        300, tools=weather_tools,
    )
    check("provider chat tool call parsed from SSE",
          chat["tool_calls"]
          == [{"name": "get_weather", "arguments": {"city": "Paris"}}],
          str(chat["tool_calls"]))
    chat = app.provider_chat_once(
        provider, "fake-gpt-smart",
        [{"role": "system", "content": "You are a robot. Start every answer with 'BEEP'."},
         {"role": "user", "content": "Say hello."}],
        300,
    )
    check("provider chat role answer stays in character",
          "BEEP" in chat["answer"], chat["answer"][:60])

    # ---------- v5: provider image generation and audio ----------
    raw, fmt, width, height = app.provider_generate_image(
        provider, "fake-gpt-smart", "A red bicycle leaning against a brick wall.", "1024x1024")
    check("provider image gen returns a valid PNG",
          fmt == "png" and width == 1 and height == 1 and app.image_info(raw) is not None,
          f"{fmt} {width}x{height}")
    raw, fmt, width, height = app.provider_generate_image(
        provider, "fake-gpt-partial", "A mountain lake at dawn.", "1024x1024")
    check("provider image gen partial succeeds on its 3rd task",
          fmt == "png", f"{fmt}")
    try:
        app.provider_generate_image(
            provider, "fake-gpt-partial", "A cat sitting on a windowsill.", "1024x1024")
        check("provider image gen partial fails on its 2nd task", False)
    except app.ProviderError as exc:
        check("provider image gen partial fails on its 2nd task",
              "not an image" in str(exc), str(exc))
    try:
        app.provider_generate_image(
            provider, "fake-gpt-dumb", "A red bicycle.", "1024x1024")
        check("provider image gen dumb fails", False)
    except app.ProviderError as exc:
        check("provider image gen dumb fails",
              "not an image" in str(exc), str(exc))
    check("provider image endpoint was hit 4 times",
          pserver.image_count == 4, str(pserver.image_count))
    clip_hello = os.path.join(PKG_DIR, "audio", "clip-hello.wav")
    clip_count = os.path.join(PKG_DIR, "audio", "clip-count.wav")
    clip_weather = os.path.join(PKG_DIR, "audio", "clip-weather.wav")
    text = app.provider_transcribe(provider, "fake-gpt-smart", clip_hello)
    check("provider transcribe smart returns the words",
          "hello world" in text.lower(), text)
    text = app.provider_transcribe(provider, "fake-gpt-partial", clip_weather)
    check("provider transcribe partial correct on its 5th task",
          "sunny" in text.lower(), text)
    text = app.provider_transcribe(provider, "fake-gpt-partial", clip_count)
    check("provider transcribe partial wrong on its 2nd task",
          "one two three" not in text.lower(), text)
    text = app.provider_transcribe(provider, "fake-gpt-dumb", clip_hello)
    check("provider transcribe dumb is wrong",
          "hello world" not in text.lower(), text)
    check("provider transcribe endpoint was hit 4 times",
          pserver.transcribe_count == 4, str(pserver.transcribe_count))
    audio = app.provider_synthesize(
        provider, "fake-gpt-smart", "Say hello in a friendly voice.")
    check("provider speech smart returns audio bytes",
          app.is_audio_bytes(audio), f"{len(audio)} bytes")
    audio = app.provider_synthesize(
        provider, "fake-gpt-partial", "Say hello in a friendly voice.")
    check("provider speech partial correct on its 3rd task",
          app.is_audio_bytes(audio), f"{len(audio)} bytes")
    audio = app.provider_synthesize(
        provider, "fake-gpt-partial",
        "Read this sentence aloud: The quick brown fox jumps over the lazy dog.")
    check("provider speech partial empty on its 4th task",
          not app.is_audio_bytes(audio), f"{len(audio)} bytes")
    audio = app.provider_synthesize(provider, "fake-gpt-dumb", "Say hello.")
    check("provider speech dumb is empty",
          not app.is_audio_bytes(audio), f"{len(audio)} bytes")
    check("provider speech endpoint was hit 4 times",
          pserver.speech_count == 4, str(pserver.speech_count))

    # ---------- provider scoring exactness via direct calls ----------
    # Same prompt-scored kinds as the Ollama loop above; chat, tools,
    # image and audio kinds go through the provider chat/media tests.
    for template in templates:
        if not template["scored"]:
            continue
        prompt_tasks = [
            t for t in template["tasks"]
            if t.get("kind", "generate") in ("generate", "vision", "long_context")
        ]
        if not prompt_tasks:
            continue
        # v16: math is 10 tasks now, partial gets 5 of them.
        partial_want = 50.0 if template["id"] == "math" else 60.0
        for brain, want in (("smart", 100.0), ("dumb", 0.0),
                            ("partial", partial_want)):
            passed_count = 0
            for task in prompt_tasks:
                answer = app.provider_generate_once(
                    provider, f"fake-gpt-{brain}", task["prompt"],
                    template["num_predict"],
                )["answer"]
                ok, _detail = app.score_task(task, answer)
                passed_count += 1 if ok else 0
            score = 100.0 * passed_count / len(prompt_tasks)
            check(f"provider {template['id']} {brain} scores {want:.0f}%",
                  score == want, f"got {score:.0f}%")

    # ---------- Qt app: Providers tab, add provider and models ----------
    # v19: adding a provider goes through the modal wizard, which a
    # test cannot click through (exec() blocks the suite). The
    # wizard itself is driven directly in the v19 section below;
    # here a stub stands in for it, so this block tests what the
    # window does with a finished provider: save, list, refresh.
    def _stub_wizard_class(provider, model_ids=None):
        class _StubWizard:
            def __init__(self, parent=None, existing=None):
                self.result_provider = provider
                self.result_model_ids = model_ids

            def exec(self):
                from PySide6.QtWidgets import QDialog
                return QDialog.Accepted

        return _StubWizard

    real_wizard = app.AddProviderDialog
    oserver, _t = fake_ollama.start_server()
    ohost, oport = oserver.server_address
    os.environ["OLLAMA_HOST"] = f"http://{ohost}:{oport}"
    shutil.rmtree(RESULTS_DIR, ignore_errors=True)
    if os.path.exists(PROVIDERS_FILE):
        os.remove(PROVIDERS_FILE)
    windowP = app.MainWindow()
    windowP.show()
    wait_until(lambda: windowP.model_list.count() == 4, timeout=10, app=qt_app)
    check("provider window lists 4 local models first",
          windowP.model_list.count() == 4, str(windowP.model_list.count()))
    from PySide6.QtWidgets import QLineEdit
    check("key field is a password field",
          windowP.provider_key_input.echoMode() == QLineEdit.Password)
    windowP.tabs.setCurrentIndex(6)
    qt_app.processEvents()
    check("providers tab starts on the empty state",
          windowP.provider_stack.currentIndex() == 0,
          str(windowP.provider_stack.currentIndex()))
    check("empty state names the Models tab",
          "No cloud providers" in windowP.provider_empty_label.text()
          and "Models tab" in windowP.provider_empty_label.text(),
          windowP.provider_empty_label.text())
    check("empty state has one add button",
          windowP.provider_add_empty_button.isVisible()
          and windowP.provider_add_empty_button.text()
          == "Add a cloud provider")
    fake_provider = {
        "name": "fake-provider",
        "base_url": f"http://{phost}:{pport}/v1",
        "api_key": "sk-fake-good-key",
        "models": [
            {"id": "fake-gpt-smart", "display_name": "Smart",
             "price_input_per_1m": 0.15, "price_output_per_1m": 0.60,
             "capabilities": ["text"]},
            {"id": "fake-gpt-partial", "display_name": "",
             "price_input_per_1m": None, "price_output_per_1m": None,
             "capabilities": ["text"]},
            {"id": "fake-gpt-dumb", "display_name": "",
             "price_input_per_1m": None, "price_output_per_1m": None,
             "capabilities": ["text"]},
        ],
    }
    app.AddProviderDialog = _stub_wizard_class(fake_provider)
    windowP.provider_add_button.click()
    qt_app.processEvents()
    app.AddProviderDialog = real_wizard
    check("adding a provider shows the detail page",
          windowP.provider_stack.currentIndex() == 1,
          str(windowP.provider_stack.currentIndex()))
    check("UI add model with prices",
          windowP.provider_models_list.count() == 3
          and "fake-gpt-smart" in windowP.provider_models_list.item(0).text()
          and "$0.15" in windowP.provider_models_list.item(0).text(),
          windowP.provider_models_list.item(0).text()
          if windowP.provider_models_list.count() else "empty")
    check("UI adds 3 provider models",
          windowP.provider_models_list.count() == 3,
          str(windowP.provider_models_list.count()))
    saved = wait_until(
        lambda: "Saved fake-provider." in windowP.provider_status.text(),
        timeout=10, app=qt_app,
    )
    check("UI save provider", saved, windowP.provider_status.text())
    with open(PROVIDERS_FILE, encoding="utf-8") as handle:
        saved_data = json.load(handle)
    check("providers.json holds the provider and its models",
          len(saved_data["providers"]) == 1
          and len(saved_data["providers"][0]["models"]) == 3, str(saved_data))
    check("key field is empty after save (key never shown)",
          windowP.provider_key_input.text() == "")
    check("key status says Key saved",
          windowP.provider_key_status.text() == "Key saved",
          windowP.provider_key_status.text())
    windowP.tabs.setCurrentIndex(0)
    wait_until(lambda: windowP.model_list.count() == 7, timeout=10, app=qt_app)
    labels = {windowP.model_list.item(r).text()
              for r in range(windowP.model_list.count())}
    check("provider models listed with their provider mark",
          "fake-provider / fake-gpt-smart" in labels
          and "fake-provider / fake-gpt-partial" in labels
          and "fake-provider / fake-gpt-dumb" in labels, str(labels))
    check("provider models start UNCHECKED", not checked_names(windowP.model_list))
    check("model list counts locals and providers",
          windowP.model_list.count() == 7)
    windowP.tabs.setCurrentIndex(6)
    qt_app.processEvents()
    windowP.grab().save(os.path.join(VERIFY_DIR, "screenshot-providers.png"))

    # Edit with the key field left empty: the stored key must survive.
    windowP.provider_save_button.click()
    qt_app.processEvents()
    with open(PROVIDERS_FILE, encoding="utf-8") as handle:
        saved_data = json.load(handle)
    check("re-saving with an empty key field keeps the stored key",
          saved_data["providers"][0]["api_key"] == "sk-fake-good-key")
    # Typing a key without saving must not touch the file (the
    # bad-key test itself lives in the wizard now — v19 section).
    windowP.provider_key_input.setText("sk-wrong")
    qt_app.processEvents()
    with open(PROVIDERS_FILE, encoding="utf-8") as handle:
        saved_data = json.load(handle)
    check("an unsaved key never reaches the file",
          saved_data["providers"][0]["api_key"] == "sk-fake-good-key")
    windowP.provider_key_input.setText("")
    # Switching tabs must not wipe an edit in progress (the tab only
    # reloads when providers.json actually changed on disk).
    windowP.provider_name_input.setText("renamed-in-form")
    windowP.tabs.setCurrentIndex(0)
    qt_app.processEvents()
    windowP.tabs.setCurrentIndex(6)
    qt_app.processEvents()
    check("switching tabs keeps an unsaved edit",
          windowP.provider_name_input.text() == "renamed-in-form",
          windowP.provider_name_input.text())
    windowP.provider_name_input.setText("fake-provider")

    # ---------- Qt app: provider run through all templates ----------
    windowP.tabs.setCurrentIndex(0)
    set_checked_list(windowP.model_list, {"fake-provider / fake-gpt-smart"})
    set_checked_templates(windowP, {t["id"] for t in templates})
    windowP.run_button.click()
    done = wait_until(
        lambda: windowP.worker is None and windowP.save_button.isEnabled(),
        timeout=300, app=qt_app,
    )
    check("provider run completes", done)
    prow = row_for(windowP, "fake-provider / fake-gpt-smart")
    check("provider row present", prow is not None)
    if prow is not None:
        check("provider row overall 100%",
              table_text(windowP, prow, 1) == "100%", table_text(windowP, prow, 1))
        check("provider row status done",
              table_text(windowP, prow, 5) == "done", table_text(windowP, prow, 5))
        toks = table_text(windowP, prow, 2)
        check("provider row has a measured speed",
              toks != "n/a" and float(toks) > 100, toks)
    detail = windowP.detail_view.toPlainText()
    check("detail shows 1,680 prompt tokens", "1,680 in" in detail, detail[:300])
    check("detail shows 16,800 completion tokens", "16,800 out" in detail, detail[:300])
    check("detail shows cost $0.01", "$0.01" in detail, detail[:300])
    check("detail shows per-template tokens",
          "100 in / 1,000 out" in detail, detail)
    check("detail shows per-template cost",
          "Coding: 100% (5/5)" in detail and "$0.0006" in detail, detail)
    check("detail says tokens include warmup",
          "includes warmup" in detail, detail[:300])
    provider_dir = os.path.join(RESULTS_DIR, "fake-provider_fake-gpt-smart", sid)
    provider_files = sorted(os.listdir(provider_dir)) if os.path.isdir(provider_dir) else []
    check("provider model has 14 run files", len(provider_files) == 14,
          str(provider_files))
    coding_files = [f for f in provider_files if f.endswith("-coding.json")]
    if coding_files:
        with open(os.path.join(provider_dir, coding_files[0]), encoding="utf-8") as handle:
            pdata = json.load(handle)
        check("provider run file records provider and model id",
              pdata.get("provider") == "fake-provider"
              and pdata.get("provider_model") == "fake-gpt-smart"
              and pdata.get("model") == "fake-provider / fake-gpt-smart",
              str({k: pdata.get(k) for k in ("provider", "provider_model", "model")}))
        check("provider run file records template tokens",
              pdata.get("prompt_tokens") == 100
              and pdata.get("completion_tokens") == 1000
              and pdata.get("total_tokens") == 1100,
              str({k: pdata.get(k) for k in ("prompt_tokens", "completion_tokens", "total_tokens")}))
        check("provider run file records exact template cost",
              pdata.get("estimated_cost_usd") == 0.000615,
              str(pdata.get("estimated_cost_usd")))
        check("provider run file keeps the system id",
              pdata.get("system_id") == sid, str(pdata.get("system_id")))
        check("provider run file tasks carry tokens",
              all("prompt_tokens" in t and "completion_tokens" in t
                  for t in pdata.get("tasks", [])))
    provider_profile = os.path.join(
        RESULTS_DIR, "fake-provider_fake-gpt-smart", "PROFILE.md")
    check("provider PROFILE.md written by the run",
          os.path.exists(provider_profile))
    if os.path.exists(provider_profile):
        with open(provider_profile, encoding="utf-8") as handle:
            prof = handle.read()
        check("provider profile latest 100%",
              "Latest overall score: 100%" in prof, prof[:500])
        check("provider profile names the provider",
              "Provider: fake-provider" in prof, prof[:300])
        check("provider profile totals template tokens",
              "Tokens used in template runs: 1,660 in, 16,600 out" in prof, prof)
        check("provider profile totals template cost",
              "Estimated cost in template runs: $0.01" in prof, prof)
    windowP.grab().save(os.path.join(VERIFY_DIR, "screenshot-provider-results.png"))

    # ---------- Qt app: provider retry once ----------
    shutil.rmtree(RESULTS_DIR, ignore_errors=True)
    pserver.chat_count = 0
    pserver.fail_next_chat = True
    set_checked_list(windowP.model_list, {"fake-provider / fake-gpt-smart"})
    set_checked_templates(windowP, {"speed"})
    windowP.run_button.click()
    done = wait_until(
        lambda: windowP.worker is None and windowP.save_button.isEnabled(),
        timeout=120, app=qt_app,
    )
    check("provider retry run completes", done)
    check("provider retry made 5 chat calls (warmup fail + retry + 3 context lengths)",
          pserver.chat_count == 5, f"got {pserver.chat_count}")
    check("provider retry run still scores done",
          windowP.results.get("fake-provider / fake-gpt-smart", {}).get("status") == "done",
          str(windowP.results.get("fake-provider / fake-gpt-smart", {}).get("status")))

    # ---------- Qt app: provider down / bad key fail plainly, locals continue ----------
    providers_now, _w = app.load_providers(PROVIDERS_FILE)
    providers_now.append({
        "name": "dead-provider", "base_url": "http://127.0.0.1:9/v1",
        "api_key": "sk-dead", "models": [
            {"id": "dead-model", "display_name": "",
             "price_input_per_1m": None, "price_output_per_1m": None},
        ],
    })
    providers_now.append({
        "name": "badkey-provider", "base_url": f"http://{phost}:{pport}/v1",
        "api_key": "sk-wrong", "models": [
            {"id": "fake-gpt-smart", "display_name": "",
             "price_input_per_1m": None, "price_output_per_1m": None},
        ],
    })
    app.save_providers(providers_now, PROVIDERS_FILE)
    windowP.refresh_models()
    wait_until(lambda: windowP.model_list.count() == 9, timeout=10, app=qt_app)
    check("dead and bad-key provider models listed",
          windowP.model_list.count() == 9, str(windowP.model_list.count()))
    set_checked_list(windowP.model_list, {
        "fake-smart:2b",
        "dead-provider / dead-model",
        "badkey-provider / fake-gpt-smart",
    })
    set_checked_templates(windowP, {"speed"})
    windowP.run_button.click()
    done = wait_until(
        lambda: windowP.worker is None and windowP.save_button.isEnabled(),
        timeout=300, app=qt_app,
    )
    check("mixed failure run completes", done)
    check("mixed failure run status is Done, not ollama_down",
          "Done." in windowP.status_label.text(), windowP.status_label.text())
    local_row = row_for(windowP, "fake-smart:2b")
    check("local model still completes when providers fail",
          local_row is not None
          and table_text(windowP, local_row, 5) == "done",
          table_text(windowP, local_row, 5) if local_row is not None else "no row")
    dead_row = row_for(windowP, "dead-provider / dead-model")
    check("dead provider model fails plainly",
          dead_row is not None
          and table_text(windowP, dead_row, 5)
          == "failed: provider dead-provider did not answer",
          table_text(windowP, dead_row, 5) if dead_row is not None else "no row")
    badkey_row = row_for(windowP, "badkey-provider / fake-gpt-smart")
    check("bad-key provider model fails plainly",
          badkey_row is not None
          and table_text(windowP, badkey_row, 5)
          == "failed: provider badkey-provider rejected the key: Incorrect API key provided",
          table_text(windowP, badkey_row, 5) if badkey_row is not None else "no row")

    # ---------- Qt app: provider-only run works with Ollama down ----------
    oserver.shutdown()
    oserver.server_close()
    os.environ["OLLAMA_HOST"] = f"http://127.0.0.1:{free_port()}"
    windowP.refresh_models()
    wait_until(lambda: windowP.model_list.count() == 5, timeout=10, app=qt_app)
    check("Ollama down: only provider models listed",
          windowP.model_list.count() == 5, str(windowP.model_list.count()))
    check("Ollama down with providers: run stays enabled",
          windowP.run_button.isEnabled())
    check("Ollama down with providers: status says so",
          "Ollama is not running" in windowP.status_label.text()
          and "provider model(s)" in windowP.status_label.text(),
          windowP.status_label.text())
    set_checked_list(windowP.model_list, {"fake-provider / fake-gpt-smart"})
    set_checked_templates(windowP, {"speed"})
    windowP.run_button.click()
    done = wait_until(
        lambda: windowP.worker is None and windowP.save_button.isEnabled(),
        timeout=120, app=qt_app,
    )
    check("provider-only run completes with Ollama down", done)
    check("provider-only run status is Done",
          "Done." in windowP.status_label.text(), windowP.status_label.text())

    # ---------- Qt app: mixed run still blocked with a REMOTE Ollama down ----------
    # v17: a local Ollama starts itself, so the blocked-run case is a
    # remote address — unreachable, and never auto-started.
    os.environ["PATH"] = FAKEBIN + os.pathsep + ORIGINAL_PATH
    os.environ["OLLAMA_HOST"] = "http://ollama.invalid:11434"
    windowP.refresh_models()
    wait_until(lambda: windowP.model_list.count() == 7, timeout=10, app=qt_app)
    set_checked_list(windowP.model_list, {
        "cli-model-a:2b", "fake-provider / fake-gpt-smart",
    })
    set_checked_templates(windowP, {"speed"})
    windowP.run_button.click()
    qt_app.processEvents()
    time.sleep(0.5)
    qt_app.processEvents()
    check("mixed run with remote Ollama down is blocked", windowP.worker is None)
    check("mixed run blocked status is the Ollama-down run message",
          windowP.status_label.text() == app.OLLAMA_DOWN_RUN_MESSAGE,
          windowP.status_label.text())
    os.environ["PATH"] = ORIGINAL_PATH
    os.environ["OLLAMA_HOST"] = f"http://127.0.0.1:{free_port()}"

    # ---------- Qt app: the API key appears nowhere in results ----------
    windowP.save_button.click()
    wait_until(lambda: any(f.startswith("bench-results-") for f in os.listdir(RESULTS_DIR)),
               timeout=10, app=qt_app)
    key_leaks = []
    for root, _dirs, files in os.walk(RESULTS_DIR):
        for filename in files:
            path = os.path.join(root, filename)
            try:
                with open(path, encoding="utf-8") as handle:
                    content = handle.read()
            except (OSError, UnicodeDecodeError):
                continue
            if "sk-fake-good-key" in content or "sk-wrong" in content:
                key_leaks.append(path)
    check("no API key in any run file, profile, or report",
          not key_leaks, str(key_leaks))
    check("no API key in the status line",
          "sk-fake-good-key" not in windowP.status_label.text()
          and "sk-wrong" not in windowP.status_label.text(),
          windowP.status_label.text())
    check("no API key in the detail view",
          "sk-fake-good-key" not in windowP.detail_view.toPlainText(),
          windowP.detail_view.toPlainText()[:200])

    # ---------- Qt app: remove model and provider via the UI ----------
    windowP.tabs.setCurrentIndex(6)
    qt_app.processEvents()
    dead_item = None
    for row in range(windowP.provider_list.count()):
        if windowP.provider_list.item(row).text() == "dead-provider":
            dead_item = windowP.provider_list.item(row)
    check("dead provider is in the providers list", dead_item is not None)
    if dead_item is not None:
        windowP.provider_list.setCurrentItem(dead_item)
        windowP.provider_remove_button.click()
        qt_app.processEvents()
        check("remove provider status",
              windowP.provider_status.text() == "Removed dead-provider.",
              windowP.provider_status.text())
        providers_now, _w = app.load_providers(PROVIDERS_FILE)
        check("removed provider is gone from the file",
              "dead-provider" not in [p["name"] for p in providers_now])
        windowP.tabs.setCurrentIndex(0)
        # Ollama is still down and PATH is back to the original, so the
        # list holds only provider models: 3 fake + 1 bad-key = 4.
        wait_until(lambda: windowP.model_list.count() == 4, timeout=10, app=qt_app)
        check("removed provider models leave the model list",
              windowP.model_list.count() == 4, str(windowP.model_list.count()))
    windowP.tabs.setCurrentIndex(6)
    qt_app.processEvents()
    fake_item = None
    for row in range(windowP.provider_list.count()):
        if windowP.provider_list.item(row).text() == "fake-provider":
            fake_item = windowP.provider_list.item(row)
    if fake_item is not None:
        windowP.provider_list.itemClicked.emit(fake_item)
        qt_app.processEvents()
        dumb_item = None
        for row in range(windowP.provider_models_list.count()):
            if windowP.provider_models_list.item(row).text().startswith("fake-gpt-dumb"):
                dumb_item = windowP.provider_models_list.item(row)
        check("dumb model is in the provider models list", dumb_item is not None)
        if dumb_item is not None:
            windowP.provider_models_list.setCurrentItem(dumb_item)
            windowP.provider_remove_model_button.click()
            qt_app.processEvents()
            check("remove model updates the list",
                  windowP.provider_models_list.count() == 2,
                  str(windowP.provider_models_list.count()))
            windowP.provider_save_button.click()
            wait_until(
                lambda: "Saved fake-provider." in windowP.provider_status.text(),
                timeout=10, app=qt_app,
            )
            providers_now, _w = app.load_providers(PROVIDERS_FILE)
            fake_entry = [p for p in providers_now if p["name"] == "fake-provider"][0]
            check("removed model is gone from the file after save",
                  "fake-gpt-dumb" not in [m["id"] for m in fake_entry["models"]],
                  str(fake_entry["models"]))

    # ---------- v5: provider capability ticks + media run ----------
    # v19: capabilities live under Advanced, collapsed; ticking a
    # box stages the change at once, and the provider's one Save
    # keeps it. The media provider arrives via the stub wizard.
    windowP.tabs.setCurrentIndex(6)
    qt_app.processEvents()
    media_provider = {
        "name": "media-provider",
        "base_url": f"http://{phost}:{pport}/v1",
        "api_key": "sk-fake-good-key",
        "models": [
            {"id": "fake-gpt-smart", "display_name": "",
             "price_input_per_1m": None, "price_output_per_1m": None,
             "capabilities": ["text"]},
        ],
    }
    app.AddProviderDialog = _stub_wizard_class(media_provider)
    windowP.provider_add_button.click()
    qt_app.processEvents()
    app.AddProviderDialog = real_wizard
    media_item_p = None
    for row in range(windowP.provider_list.count()):
        if windowP.provider_list.item(row).text() == "media-provider":
            media_item_p = windowP.provider_list.item(row)
    check("media provider is in the list", media_item_p is not None)
    if media_item_p is not None:
        windowP.provider_list.itemClicked.emit(media_item_p)
        qt_app.processEvents()
    check("Advanced starts collapsed",
          not windowP.provider_advanced_box.isVisible())
    check("capability checkboxes start with Text only",
          windowP.advanced_cap_boxes["text"].isChecked()
          and not windowP.advanced_cap_boxes["vision"].isChecked()
          and not windowP.advanced_cap_boxes["image_generation"].isChecked()
          and not windowP.advanced_cap_boxes["audio"].isChecked()
          and not windowP.advanced_cap_boxes["tools"].isChecked()
          and not windowP.advanced_cap_boxes["long_context"].isChecked())
    check("Advanced says no model is picked",
          "Pick a model" in windowP.advanced_hint.text(),
          windowP.advanced_hint.text())
    media_item = windowP.provider_models_list.item(0)
    windowP.provider_models_list.itemClicked.emit(media_item)
    qt_app.processEvents()
    check("clicking a model loads it into Advanced",
          windowP.advanced_hint.text()
          == "Price and capabilities for fake-gpt-smart.",
          windowP.advanced_hint.text())
    windowP.provider_advanced_toggle.setChecked(True)
    qt_app.processEvents()
    check("Advanced opens on its toggle",
          windowP.provider_advanced_box.isVisible())
    windowP.advanced_cap_boxes["image_generation"].setChecked(True)
    windowP.advanced_cap_boxes["audio"].setChecked(True)
    qt_app.processEvents()
    check("provider models list shows the capability bracket",
          windowP.provider_models_list.count() == 1
          and windowP.provider_models_list.item(0).text()
          == "fake-gpt-smart [image_generation, audio]",
          windowP.provider_models_list.item(0).text()
          if windowP.provider_models_list.count() else "empty")
    windowP.grab().save(
        os.path.join(VERIFY_DIR, "screenshot-providers-caps.png"))
    # Advanced edits stage at once — no second save per model.
    windowP.advanced_cap_boxes["audio"].setChecked(False)
    qt_app.processEvents()
    check("unticking a capability updates the list at once",
          windowP.provider_models_list.item(0).text()
          == "fake-gpt-smart [image_generation]",
          windowP.provider_models_list.item(0).text())
    windowP.advanced_cap_boxes["audio"].setChecked(True)
    qt_app.processEvents()
    check("re-ticking restores the audio capability",
          windowP.provider_models_list.item(0).text()
          == "fake-gpt-smart [image_generation, audio]",
          windowP.provider_models_list.item(0).text())
    windowP.provider_save_button.click()
    saved = wait_until(
        lambda: "Saved media-provider." in windowP.provider_status.text(),
        timeout=10, app=qt_app,
    )
    check("UI saves the media provider", saved, windowP.provider_status.text())
    with open(PROVIDERS_FILE, encoding="utf-8") as handle:
        saved_data = json.load(handle)
    media_entry = [p for p in saved_data["providers"]
                   if p["name"] == "media-provider"][0]
    check("saved media model carries its capabilities",
          media_entry["models"][0].get("capabilities")
          == ["text", "image_generation", "audio"],
          str(media_entry["models"][0]))
    check("no API key in the capabilities cache after the media run",
          "sk-fake-good-key" not in open(CAPABILITIES_FILE, encoding="utf-8").read()
          if os.path.exists(CAPABILITIES_FILE) else False)
    windowP.tabs.setCurrentIndex(0)
    wait_until(lambda: windowP.model_list.count() == 4, timeout=10, app=qt_app)
    media_labels = {windowP.model_list.item(r).text()
                    for r in range(windowP.model_list.count())}
    check("media model listed with its capabilities",
          "media-provider / fake-gpt-smart [image_generation, audio]"
          in media_labels, str(media_labels))
    image_before = pserver.image_count
    transcribe_before = pserver.transcribe_count
    speech_before = pserver.speech_count
    set_checked_list(windowP.model_list, {"media-provider / fake-gpt-smart"})
    set_checked_templates(windowP, {"image_generation", "audio"})
    windowP.run_button.click()
    done = wait_until(
        lambda: windowP.worker is None and windowP.save_button.isEnabled(),
        timeout=120, app=qt_app,
    )
    check("media run completes", done)
    media_result = windowP.results.get("media-provider / fake-gpt-smart", {})
    check("media run status done", media_result.get("status") == "done",
          str(media_result.get("status")))
    check("media run image generation 100%",
          media_result.get("templates", {}).get("image_generation", {})
          .get("template_score") == 100.0,
          str(media_result.get("templates", {}).get("image_generation")))
    check("media run audio 100%",
          media_result.get("templates", {}).get("audio", {})
          .get("template_score") == 100.0,
          str(media_result.get("templates", {}).get("audio")))
    media_dir = os.path.join(
        RESULTS_DIR, "media-provider_fake-gpt-smart", sid)
    check("media run wrote 2 run files",
          os.path.isdir(media_dir)
          and len([f for f in os.listdir(media_dir) if f.endswith(".json")]) == 2,
          str(os.listdir(media_dir)) if os.path.isdir(media_dir) else "no dir")
    image_files = sorted(
        f for f in os.listdir(media_dir) if f.endswith("-image_generation.json"))
    audio_files = sorted(
        f for f in os.listdir(media_dir) if f.endswith("-audio.json"))
    image_folder = None
    if image_files:
        with open(os.path.join(media_dir, image_files[0]),
                  encoding="utf-8") as handle:
            image_data = json.load(handle)
        image_folder = os.path.join(
            media_dir, image_files[0][:-5] + "-images")
        check("image run file records 5 tasks",
              len(image_data.get("tasks", [])) == 5)
        check("image run file records all 5 passed",
              all(t.get("passed") for t in image_data.get("tasks", [])),
              str(image_data.get("tasks", [])))
        check("image tasks record their saved files",
              all(t.get("image_file") for t in image_data.get("tasks", [])),
              str([t.get("image_file") for t in image_data.get("tasks", [])]))
        check("generated images folder exists",
              image_folder is not None and os.path.isdir(image_folder))
        if image_folder is not None and os.path.isdir(image_folder):
            saved_images = sorted(os.listdir(image_folder))
            check("5 generated images saved",
                  len(saved_images) == 5, str(saved_images))
            check("saved images are valid PNGs",
                  all(app.image_info(open(os.path.join(image_folder, f), "rb").read())
                      is not None for f in saved_images),
                  str(saved_images))
    audio_folder = None
    if audio_files:
        with open(os.path.join(media_dir, audio_files[0]),
                  encoding="utf-8") as handle:
            audio_data = json.load(handle)
        audio_folder = os.path.join(media_dir, audio_files[0][:-5] + "-audio")
        check("audio run file records 5 tasks",
              len(audio_data.get("tasks", [])) == 5)
        check("audio run file records all 5 passed",
              all(t.get("passed") for t in audio_data.get("tasks", [])),
              str(audio_data.get("tasks", [])))
        speech_tasks = [t for t in audio_data.get("tasks", [])
                        if t.get("audio_file_saved")]
        check("speech tasks record their saved files",
              len(speech_tasks) == 2,
              str([t.get("id") for t in audio_data.get("tasks", [])]))
        check("generated audio folder exists",
              audio_folder is not None and os.path.isdir(audio_folder))
        if audio_folder is not None and os.path.isdir(audio_folder):
            saved_audio = sorted(os.listdir(audio_folder))
            check("2 speech clips saved",
                  len(saved_audio) == 2, str(saved_audio))
            check("saved clips are audio bytes",
                  all(app.is_audio_bytes(open(os.path.join(audio_folder, f), "rb").read())
                      for f in saved_audio),
                  str(saved_audio))
    check("image endpoint hit 5 times by the run",
          pserver.image_count == image_before + 5,
          f"delta {pserver.image_count - image_before}")
    check("transcribe endpoint hit 3 times by the run",
          pserver.transcribe_count == transcribe_before + 3,
          f"delta {pserver.transcribe_count - transcribe_before}")
    check("speech endpoint hit 2 times by the run",
          pserver.speech_count == speech_before + 2,
          f"delta {pserver.speech_count - speech_before}")
    media_row = row_for(windowP, "media-provider / fake-gpt-smart")
    windowP.table.cellClicked.emit(media_row, 0)
    qt_app.processEvents()
    media_detail = windowP.detail_view.toPlainText()
    check("detail notes the saved images",
          "(image saved)" in media_detail, media_detail[:800])
    check("detail notes the saved audio",
          "(audio saved)" in media_detail, media_detail[:800])
    check("media row shows 100%",
          table_text(windowP, media_row, 1) == "100%",
          table_text(windowP, media_row, 1))
    windowP.close()
    windowP.deleteLater()
    qt_app.processEvents()
    pserver.shutdown()
    pserver.server_close()

    # ---------- the package dir must not have gained a providers file ----------
    check("no providers.json leaked into the package dir",
          not os.path.exists(os.path.join(PKG_DIR, "providers.json")))
    check("no capabilities.json leaked into the package dir",
          not os.path.exists(os.path.join(PKG_DIR, "capabilities.json")))

    # ================= v6: fixture "not run", atomic providers, ==============
    # ========= suite in repo + CI, PySide6 guard + install verify ============
    SCRATCH = os.path.join(VERIFY_DIR, "_v6_scratch")
    shutil.rmtree(SCRATCH, ignore_errors=True)
    os.makedirs(SCRATCH, exist_ok=True)
    empty_images = os.path.join(SCRATCH, "empty-images")
    empty_audio = os.path.join(SCRATCH, "empty-audio")
    os.makedirs(empty_images, exist_ok=True)
    os.makedirs(empty_audio, exist_ok=True)

    # ---------- v6 T1: load_templates warns on a missing fixture ----------
    missing_templates = os.path.join(SCRATCH, "templates-missing")
    os.makedirs(missing_templates, exist_ok=True)
    with open(os.path.join(missing_templates, "vision.json"), "w", encoding="utf-8") as handle:
        json.dump({
            "id": "vision", "name": "Vision", "capability": "vision",
            "scored": True,
            "tasks": [{"id": "v1", "kind": "vision", "prompt": "What is this?",
                       "image": "gone.png", "rule": "contains", "expected": "x"}],
        }, handle)
    with open(os.path.join(missing_templates, "audio.json"), "w", encoding="utf-8") as handle:
        json.dump({
            "id": "audio", "name": "Audio", "capability": "audio",
            "scored": True,
            "tasks": [{"id": "a1", "kind": "audio", "audio_task": "transcribe",
                       "audio_file": "gone.wav", "prompt": "Transcribe this.",
                       "rule": "contains", "expected": "x"}],
        }, handle)
    saved_images_dir = os.environ.get("OST_IMAGES_DIR")
    saved_audio_dir = os.environ.get("OST_AUDIO_DIR")
    os.environ["OST_IMAGES_DIR"] = empty_images
    os.environ["OST_AUDIO_DIR"] = empty_audio
    try:
        loaded_missing, warns_missing = app.load_templates(missing_templates)
        check("missing-fixture templates still load",
              [t["id"] for t in loaded_missing] == ["audio", "vision"],
              str([t["id"] for t in loaded_missing]))
        check("load_templates warns about the missing image",
              any("gone.png" in w for w in warns_missing), str(warns_missing))
        check("load_templates warns about the missing audio clip",
              any("gone.wav" in w for w in warns_missing), str(warns_missing))

        # ---------- v6 T1: the runners return "not run", never a fail ------
        worker = app.BenchWorker.__new__(app.BenchWorker)
        worker._bump_progress = lambda *args: None
        vision_record, _media = worker._task_vision(
            {"label": "t", "kind": "ollama"},
            {"id": "vision", "name": "Vision", "num_predict": 200},
            {"id": "v1", "prompt": "What is this?", "image": "gone.png",
             "kind": "vision"},
        )
        check("missing image: vision task passed is None",
              vision_record["passed"] is None, str(vision_record["passed"]))
        check("missing image: vision detail is the not-run message",
              vision_record["detail"] == "not run: test image not found",
              vision_record["detail"])
        check("missing image: vision record still names the file",
              vision_record.get("image") == "gone.png", str(vision_record))
        audio_record, _media = worker._task_audio(
            {"label": "p", "kind": "provider", "provider": {}, "model": "m"},
            {"id": "audio", "name": "Audio", "num_predict": 200},
            {"id": "a1", "prompt": "Transcribe this.",
             "audio_task": "transcribe", "audio_file": "gone.wav",
             "kind": "audio"},
        )
        check("missing clip: audio task passed is None",
              audio_record["passed"] is None, str(audio_record["passed"]))
        check("missing clip: audio detail is the not-run message",
              audio_record["detail"] == "not run: test audio not found",
              audio_record["detail"])
        check("missing clip: audio record still names the file",
              audio_record.get("audio_file") == "gone.wav", str(audio_record))
    finally:
        if saved_images_dir is None:
            os.environ.pop("OST_IMAGES_DIR", None)
        else:
            os.environ["OST_IMAGES_DIR"] = saved_images_dir
        if saved_audio_dir is None:
            os.environ.pop("OST_AUDIO_DIR", None)
        else:
            os.environ["OST_AUDIO_DIR"] = saved_audio_dir

    check("is_not_run marks the vision record",
          app.is_not_run(vision_record))
    check("is_not_run marks the audio record",
          app.is_not_run(audio_record))
    check("is_not_run is False for a passed task",
          not app.is_not_run({"passed": True, "detail": ""}))
    check("is_not_run is False for a failed task",
          not app.is_not_run({"passed": False, "detail": "expected 42, got 19"}))
    check("is_not_run is False for an unscored task",
          not app.is_not_run({"passed": None, "detail": ""}))

    # ---------- v6 T1: the history summary shows "not run" distinctly -----
    not_run_data = {
        "model": "fake-smart:2b", "template": "vision",
        "template_name": "Vision", "timestamp": "2026-11-12T00:00:00",
        "system_id": sid, "provider": "ollama",
        "template_score": None, "passed": None, "total": 0, "not_run": 5,
        "avg_decode_tok_s": None, "total_tokens": None,
        "tasks": [
            {"id": "red-circles", "passed": None,
             "detail": "not run: test image not found", "decode_tok_s": None},
            {"id": "blue-squares", "passed": None,
             "detail": "not run: test image not found", "decode_tok_s": None},
        ],
    }
    summary = app.render_run_summary(not_run_data)
    check("history summary says not run, never 0%",
          "Score: not run (5 tasks not run" in summary
          and "0%" not in summary, summary)
    check("history summary marks not-run tasks",
          "not run  red-circles" in summary, summary)
    check("history summary shows the not-run reason",
          "not run: test image not found" in summary, summary)
    mixed_data = dict(not_run_data)
    mixed_data.update({
        "template": "audio", "template_name": "Audio",
        "template_score": 100.0, "passed": 2, "total": 2, "not_run": 3,
        "tasks": [
            {"id": "transcribe-hello", "passed": None,
             "detail": "not run: test audio not found", "decode_tok_s": None},
            {"id": "speak-friendly", "passed": True, "detail": "",
             "decode_tok_s": None},
        ],
    })
    mixed_summary = app.render_run_summary(mixed_data)
    check("history summary shows a partial not-run count",
          "Score: 100% (2/2 tasks, 3 not run)" in mixed_summary, mixed_summary)
    check("history summary keeps pass and not-run marks apart",
          "pass  speak-friendly" in mixed_summary
          and "not run  transcribe-hello" in mixed_summary, mixed_summary)

    # ---------- v6 T2: atomic providers.json + .gitignore ------------------
    providers_dir = os.path.join(SCRATCH, "providers")
    os.makedirs(providers_dir, exist_ok=True)
    providers_path = os.path.join(providers_dir, "providers.json")
    sample_providers = [{
        "name": "test-provider", "base_url": "http://127.0.0.1:9/v1",
        "api_key": "test-key-not-real", "models": [],
    }]
    app.save_providers(sample_providers, providers_path)
    check("save_providers writes the file",
          os.path.exists(providers_path))
    check("providers.json is owner-only (0o600)",
          os.stat(providers_path).st_mode & 0o777 == 0o600,
          oct(os.stat(providers_path).st_mode & 0o777))
    loaded_back, _w = app.load_providers(providers_path)
    check("atomic save round-trips",
          len(loaded_back) == 1 and loaded_back[0]["name"] == "test-provider",
          str(loaded_back))
    with open(providers_path, "rb") as handle:
        old_bytes = handle.read()
    try:
        app.save_providers(
            [{"name": "boom", "unserializable": object()}], providers_path)
        check("save_providers raises on an unwritable value", False)
    except TypeError:
        check("save_providers raises on an unwritable value", True)
    with open(providers_path, "rb") as handle:
        after_bytes = handle.read()
    check("a failed write leaves the old file intact",
          after_bytes == old_bytes)
    check("a failed write leaves no temp file behind",
          not [f for f in os.listdir(providers_dir)
               if f.startswith(".providers-")], str(os.listdir(providers_dir)))
    check("providers.json still loads after the failed write",
          app.load_providers(providers_path)[0][0]["name"] == "test-provider")
    with open(os.path.join(PKG_DIR, ".gitignore"), encoding="utf-8") as handle:
        gitignore_lines = {line.strip() for line in handle}
    for wanted in ("providers.json", "capabilities.json", "results/",
                   "__pycache__/", ".venv/", "*.pyc"):
        check(f".gitignore covers {wanted}", wanted in gitignore_lines)

    # ---------- v6 T3: the suite lives in the repo, with CI ---------------
    check("suite locates the app at the repo root",
          os.path.basename(PKG_DIR) != "tests"
          and os.path.isfile(os.path.join(PKG_DIR, "ollama_system_test.py")),
          PKG_DIR)
    check("suite dir is tests/ under the repo root",
          VERIFY_DIR == os.path.join(PKG_DIR, "tests"), VERIFY_DIR)
    for suite_file in ("fake_ollama.py", "make_fixtures.py",
                       "make_templates.py", "templates-test/vision.json",
                       "fakebin/ollama", "fakebin-sleep/ollama"):
        check(f"suite ships {suite_file}",
              os.path.exists(os.path.join(VERIFY_DIR, suite_file)))
    # The suite regenerates tests/fakebin/ollama at startup from its
    # embedded template (write_fake_ollama), so the template — not
    # just the shipped file — must carry the v14 env-file block, or
    # the starter checks can never see it (run 2 root cause).
    with open(os.path.join(VERIFY_DIR, "fakebin", "ollama"),
              encoding="utf-8") as handle:
        generated_fakebin = handle.read()
    check("generated fakebin writes the env file",
          "FAKE_OLLAMA_ENVFILE" in generated_fakebin
          and "OLLAMA_MODELS" in generated_fakebin)
    import make_fixtures
    import make_templates
    check("make_fixtures points at the repo root",
          make_fixtures.PKG == PKG_DIR, make_fixtures.PKG)
    check("make_templates points at the repo templates",
          make_templates.OUT == os.path.join(PKG_DIR, "templates"),
          make_templates.OUT)
    workflow_path = os.path.join(PKG_DIR, ".github", "workflows", "test.yml")
    check("CI workflow exists", os.path.exists(workflow_path))
    if os.path.exists(workflow_path):
        with open(workflow_path, encoding="utf-8") as handle:
            workflow = handle.read()
        check("CI workflow runs the suite",
              "tests/run_tests.py" in workflow, workflow)
        check("CI workflow runs Qt offscreen",
              "QT_QPA_PLATFORM: offscreen" in workflow, workflow)
        check("CI workflow installs the pinned deps",
              "requirements.txt" in workflow, workflow)
    with open(os.path.join(PKG_DIR, "README.md"), encoding="utf-8") as handle:
        readme_text = handle.read()
    check("README documents running the suite locally",
          "tests/run_tests.py" in readme_text)

    # ---------- v6 T4: pin, README note, PySide6 guard --------------------
    with open(os.path.join(PKG_DIR, "requirements.txt"),
              encoding="utf-8") as handle:
        requirements = handle.read()
    check("requirements pin PySide6-Essentials to 6.12.0",
          "PySide6-Essentials==6.12.0" in requirements, requirements)
    with open(os.path.join(PKG_DIR, "install.sh"), encoding="utf-8") as handle:
        install_text = handle.read()
    check("install.sh installs from requirements.txt",
          "-r requirements.txt" in install_text, install_text)
    check("install.sh verifies PySide6 imports",
          "import PySide6" in install_text, install_text)
    check("install.sh verifies the app compiles",
          "py_compile ollama_system_test.py" in install_text, install_text)
    check("install.sh fails loudly when the check fails",
          "Install failed" in install_text, install_text)
    check("README documents chmod +x after a clone",
          "chmod +x install.sh run.sh" in readme_text)
    # A Python without PySide6: the system python3 in the sandbox, or
    # this interpreter with -S (no site-packages) anywhere else.
    no_qt_cmd = None
    for candidate in ("/usr/bin/python3", shutil.which("python3")):
        if candidate and os.path.exists(candidate) and candidate != sys.executable:
            probe = subprocess.run(
                [candidate, "-c", "import PySide6"],
                capture_output=True, timeout=30)
            if probe.returncode != 0:
                no_qt_cmd = [candidate]
                break
    if no_qt_cmd is None:
        no_qt_cmd = [sys.executable, "-S"]
    clean_env = dict(os.environ)
    clean_env.pop("PYTHONPATH", None)
    clean_env.pop("PYTHONHOME", None)
    no_qt = subprocess.run(
        no_qt_cmd + [os.path.join(PKG_DIR, "ollama_system_test.py")],
        capture_output=True, text=True, timeout=60, cwd=PKG_DIR,
        env=clean_env)
    no_qt_output = no_qt.stdout + no_qt.stderr
    check("launch without PySide6 exits 1",
          no_qt.returncode == 1, str(no_qt.returncode))
    check("launch without PySide6 names install.sh",
          "install.sh" in no_qt_output, no_qt_output)
    check("launch without PySide6 shows no traceback",
          "Traceback" not in no_qt_output, no_qt_output)

    # ---------- v6 T1: full GUI run with the fixtures missing -------------
    shutil.rmtree(RESULTS_DIR, ignore_errors=True)
    if os.path.exists(PROVIDERS_FILE):
        os.remove(PROVIDERS_FILE)
    if os.path.exists(CAPABILITIES_FILE):
        os.remove(CAPABILITIES_FILE)
    server6, _t = fake_ollama.start_server()
    host6, port6 = server6.server_address
    os.environ["OLLAMA_HOST"] = f"http://{host6}:{port6}"
    pserver6, _t = fake_ollama.start_provider_server()
    phost6, pport6 = pserver6.server_address
    with open(PROVIDERS_FILE, "w", encoding="utf-8") as handle:
        json.dump({"providers": [{
            "name": "v6-provider",
            "base_url": f"http://{phost6}:{pport6}/v1",
            # The fake provider only accepts its own key
            # (fake_ollama.PROVIDER_API_KEY); anything else gets a
            # 401 and the model's warmup fails before any template runs.
            "api_key": fake_ollama.PROVIDER_API_KEY,
            "models": [{
                "id": "fake-gpt-smart", "display_name": "",
                "price_input_per_1m": None, "price_output_per_1m": None,
                "capabilities": ["text", "audio"],
            }],
        }]}, handle)
    os.environ["OST_IMAGES_DIR"] = empty_images
    os.environ["OST_AUDIO_DIR"] = empty_audio
    try:
        window6 = app.MainWindow()
        window6.show()
        wait_until(lambda: window6.model_list.count() == 5, timeout=10,
                   app=qt_app)
        check("not-run window lists 4 local + 1 provider model",
              window6.model_list.count() == 5,
              str(window6.model_list.count()))
        check("window warns about the missing shapes image",
              any("shapes.png" in w for w in window6.template_warnings),
              str(window6.template_warnings))
        check("window warns about the missing audio clip",
              any("clip-hello.wav" in w for w in window6.template_warnings),
              str(window6.template_warnings))
        set_checked_list(window6.model_list,
                         {"fake-smart:2b", "v6-provider / fake-gpt-smart"})
        set_checked_templates(window6, {"vision", "audio"})
        images_before = server6.chat_images_seen
        transcribe_before = pserver6.transcribe_count
        window6.run_button.click()
        done6 = wait_until(
            lambda: window6.worker is None and window6.save_button.isEnabled(),
            timeout=300, app=qt_app)
        check("not-run run completes", done6)
        smart_result = window6.results.get("fake-smart:2b", {})
        vision_run = smart_result.get("templates", {}).get("vision", {})
        check("vision run has no score when nothing ran",
              vision_run.get("template_score") is None,
              str(vision_run.get("template_score")))
        check("vision run counts 5 not run",
              vision_run.get("not_run") == 5, str(vision_run.get("not_run")))
        check("vision run counts 0 ran",
              vision_run.get("passed") is None and vision_run.get("total") == 0,
              f"{vision_run.get('passed')}/{vision_run.get('total')}")
        check("every vision task is not run, none failed",
              all(t.get("passed") is None
                  and t.get("detail") == "not run: test image not found"
                  for t in vision_run.get("tasks", []))
              and len(vision_run.get("tasks", [])) == 5,
              str(vision_run.get("tasks", [])))
        provider_result = window6.results.get(
            "v6-provider / fake-gpt-smart", {})
        audio_run = provider_result.get("templates", {}).get("audio", {})
        check("audio run scores only the tasks that ran",
              audio_run.get("template_score") == 100.0
              and audio_run.get("passed") == 2
              and audio_run.get("total") == 2,
              f"{audio_run.get('template_score')} "
              f"({audio_run.get('passed')}/{audio_run.get('total')})")
        check("audio run counts 3 not run",
              audio_run.get("not_run") == 3, str(audio_run.get("not_run")))
        check("transcribe tasks are not run, speech tasks pass",
              all(t.get("passed") is None for t in audio_run.get("tasks", [])
                  if t.get("id", "").startswith("transcribe"))
              and all(t.get("passed") is True for t in audio_run.get("tasks", [])
                      if t.get("id", "").startswith("speak")),
              str([(t.get("id"), t.get("passed"))
                   for t in audio_run.get("tasks", [])]))
        check("no vision image was sent while the fixtures were missing",
              server6.chat_images_seen == images_before,
              f"{server6.chat_images_seen - images_before} sent")
        check("no transcribe call was made while the clips were missing",
              pserver6.transcribe_count == transcribe_before,
              f"delta {pserver6.transcribe_count - transcribe_before}")
        check("smart overall is n/a, not 0%",
              table_text(window6, row_for(window6, "fake-smart:2b"), 1) == "n/a",
              table_text(window6, row_for(window6, "fake-smart:2b"), 1))
        check("provider row shows 100%",
              table_text(window6,
                         row_for(window6, "v6-provider / fake-gpt-smart"),
                         1) == "100%",
              table_text(window6,
                         row_for(window6, "v6-provider / fake-gpt-smart"), 1))
        smart_row = row_for(window6, "fake-smart:2b")
        window6.table.cellClicked.emit(smart_row, 0)
        qt_app.processEvents()
        detail6 = window6.detail_view.toPlainText()
        check("detail view says Vision not run",
              "Vision: not run" in detail6, detail6)
        check("detail view never says Vision 0%",
              "Vision: 0%" not in detail6, detail6)
        check("detail view marks the not-run tasks",
              "not run  red-circles" in detail6, detail6)
        check("detail view shows the not-run reason",
              "not run: test image not found" in detail6, detail6)
        provider_row = row_for(window6, "v6-provider / fake-gpt-smart")
        window6.table.cellClicked.emit(provider_row, 0)
        qt_app.processEvents()
        detail6p = window6.detail_view.toPlainText()
        check("detail view shows the partial not-run count",
              "Audio: 100% (2/2, 3 not run)" in detail6p, detail6p)
        check("detail view marks the not-run transcribe tasks",
              "not run  transcribe-hello" in detail6p, detail6p)
        check("detail view shows the audio not-run reason",
              "not run: test audio not found" in detail6p, detail6p)

        # History tab: the run files say "not run" too.
        window6.tabs.setCurrentIndex(3)
        wait_until(lambda: window6.history_model_combo.count() == 2,
                   timeout=10, app=qt_app)
        check("history lists 2 models after the not-run run",
              window6.history_model_combo.count() == 2,
              str(window6.history_model_combo.count()))
        smart_hist = None
        provider_hist = None
        for i in range(window6.history_model_combo.count()):
            if window6.history_model_combo.itemData(i) == "fake-smart_2b":
                smart_hist = i
            if window6.history_model_combo.itemData(i) == "v6-provider_fake-gpt-smart":
                provider_hist = i
        if smart_hist is not None:
            window6.history_model_combo.setCurrentIndex(smart_hist)
            wait_until(lambda: window6.history_list.count() == 1,
                       timeout=10, app=qt_app)
            vision_item = window6.history_list.item(0)
            check("history list shows the vision run as not run",
                  vision_item is not None and "(not run)" in vision_item.text(),
                  vision_item.text() if vision_item is not None else "none")
            if vision_item is not None:
                window6.history_list.itemClicked.emit(vision_item)
                qt_app.processEvents()
                hist_summary = window6.history_view.toPlainText()
                check("history summary says not run, never 0%",
                      "Score: not run" in hist_summary
                      and "0%" not in hist_summary, hist_summary[:400])
                check("history summary marks the not-run tasks",
                      "not run  red-circles" in hist_summary,
                      hist_summary[:400])
        if provider_hist is not None:
            window6.history_model_combo.setCurrentIndex(provider_hist)
            wait_until(lambda: window6.history_list.count() == 1,
                       timeout=10, app=qt_app)
            audio_item = window6.history_list.item(0)
            check("history list shows the partial not-run count",
                  audio_item is not None and "3 not run" in audio_item.text(),
                  audio_item.text() if audio_item is not None else "none")
            if audio_item is not None:
                window6.history_list.itemClicked.emit(audio_item)
                qt_app.processEvents()
                hist_summary_p = window6.history_view.toPlainText()
                check("history summary shows the partial count",
                      "Score: 100% (2/2 tasks, 3 not run)" in hist_summary_p,
                      hist_summary_p[:400])
        profile_path = os.path.join(
            RESULTS_DIR, "fake-smart_2b", "PROFILE.md")
        check("profile file was written after the not-run run",
              os.path.exists(profile_path))
        if os.path.exists(profile_path):
            with open(profile_path, encoding="utf-8") as handle:
                profile_text = handle.read()
            check("profile shows Vision as not run, not 0% or no scoring",
                  "| Vision | not run | not run | 1 |" in profile_text,
                  profile_text)
        window6.close()
        window6.deleteLater()
        qt_app.processEvents()
    finally:
        os.environ.pop("OST_IMAGES_DIR", None)
        os.environ.pop("OST_AUDIO_DIR", None)
        server6.shutdown()
        server6.server_close()
        pserver6.shutdown()
        pserver6.server_close()

    # ---------- v6 T4: install.sh end to end in a scratch copy ------------
    # The scratch copy lives under tests/ (workspace disk), never /tmp:
    # /tmp is a 512 MB tmpfs and a PySide6 install dies with ENOSPC there.
    install_good = os.path.join(SCRATCH, "install-test")
    shutil.copytree(
        PKG_DIR, install_good,
        ignore=shutil.ignore_patterns(
            "tests", ".venv", "results", "__pycache__", ".github",
            "providers.json", "capabilities.json"))
    # pip's temp defaults to TMPDIR (/tmp, a 512 MB tmpfs shared with
    # every other thread's suite): point it at the workspace scratch
    # dir, or the 84 MB wheel unpack dies with ENOSPC under load.
    install_env = dict(os.environ)
    install_env["TMPDIR"] = SCRATCH
    install = subprocess.run(
        ["sh", "install.sh"], cwd=install_good, env=install_env,
        capture_output=True, text=True, timeout=600)
    check("install.sh exits 0 in a scratch copy",
          install.returncode == 0,
          f"exit {install.returncode}\n{install.stdout[-500:]}\n"
          f"{install.stderr[-500:]}")
    check("install.sh prints its verification line",
          "PySide6 OK." in install.stdout, install.stdout[-500:])
    check("install.sh reports success only after verifying",
          "Done. Start it with: sh run.sh" in install.stdout,
          install.stdout[-500:])
    venv_python = os.path.join(install_good, ".venv", "bin", "python")
    check("install.sh leaves a working venv python",
          os.path.exists(venv_python))
    if os.path.exists(venv_python):
        pinned = subprocess.run(
            [venv_python, "-c",
             "import PySide6; print(PySide6.__version__)"],
            capture_output=True, text=True, timeout=60)
        check("installed PySide6 is the pinned 6.12.0",
              pinned.returncode == 0 and pinned.stdout.strip() == "6.12.0",
              f"{pinned.returncode} {pinned.stdout!r} {pinned.stderr[-200:]}")
    install_broken = os.path.join(SCRATCH, "install-test-broken")
    shutil.copytree(
        PKG_DIR, install_broken,
        ignore=shutil.ignore_patterns(
            "tests", ".venv", "results", "__pycache__", ".github",
            "providers.json", "capabilities.json"))
    os.remove(os.path.join(install_broken, "requirements.txt"))
    broken = subprocess.run(
        ["sh", "install.sh"], cwd=install_broken,
        capture_output=True, text=True, timeout=600)
    check("install.sh fails loudly when the install cannot complete",
          broken.returncode != 0 and "Done." not in broken.stdout,
          f"exit {broken.returncode}\n{broken.stdout[-300:]}")

    # ---------- v7 T6: __version__, title bar, window icon ---------------
    check("__version__ is 20", app.__version__ == "20",
          repr(app.__version__))
    icon_path = os.path.join(PKG_DIR, "ost-icon.png")
    check("ost-icon.png ships in the tree", os.path.exists(icon_path))
    if os.path.exists(icon_path):
        with open(icon_path, "rb") as handle:
            icon_bytes = handle.read()
        check("ost-icon.png is a PNG of real size",
              icon_bytes[:8] == b"\x89PNG\r\n\x1a\n"
              and len(icon_bytes) > 1000,
              f"{len(icon_bytes)} bytes")
    window7 = app.MainWindow()
    window7.show()
    qt_app.processEvents()
    check("title bar shows OLLAMA SYSTEM TEST v20",
          window7.windowTitle() == "OLLAMA SYSTEM TEST v20",
          window7.windowTitle())
    check("title bar uses __version__",
          f"v{app.__version__}" in window7.windowTitle(),
          window7.windowTitle())
    check("window icon is set (not null)",
          not window7.windowIcon().isNull())

    # ---------- v7 T7: geometry + shortcuts + menu bar -------------------
    from PySide6.QtCore import QSettings
    settings7 = QSettings("BrokeNBored", app.APP_NAME)
    settings7.clear()
    settings7.sync()
    window7g = app.MainWindow()
    window7g.show()
    qt_app.processEvents()
    window7g.close()
    qt_app.processEvents()
    saved7 = QSettings("BrokeNBored", app.APP_NAME).value("geometry")
    check("closing the window saves geometry to QSettings",
          saved7 is not None and len(saved7) > 0,
          repr(saved7)[:80] if saved7 is not None else "None")
    window7g.deleteLater()
    qt_app.processEvents()
    # The full round-trip needs a screen bigger than the window's
    # minimum width — offscreen reports 800x800 and Qt clamps the
    # restore there — so it runs as an Xvfb probe.
    probe = os.path.join(VERIFY_DIR, "geometry_probe.py")
    if shutil.which("xvfb-run") is None:
        print("SKIP: geometry round-trip probe needs xvfb-run")
    else:
        probe_env = dict(os.environ)
        probe_env.pop("QT_QPA_PLATFORM", None)
        probe_env.pop("DISPLAY", None)
        probe_run = subprocess.run(
            ["xvfb-run", "-a", "-s", "-screen 0 1400x900x24",
             sys.executable, "-u", probe],
            cwd=PKG_DIR, env=probe_env,
            capture_output=True, text=True, timeout=180)
        check("geometry round-trips through a restart (Xvfb probe)",
              probe_run.returncode == 0,
              probe_run.stdout[-400:] + probe_run.stderr[-400:])

    bar7 = window7.menuBar()
    menu_names = [action.text() for action in bar7.actions()]
    check("menu bar has File, View and Help",
          menu_names == ["File", "View", "Help"], str(menu_names))
    file_menu7 = bar7.actions()[0].menu()
    file_items = [a.text() for a in file_menu7.actions()]
    check("File menu has Save report, Settings and Quit",
          file_items == ["Save report", "Settings", "Quit"],
          str(file_items))
    help_menu7 = bar7.actions()[2].menu()
    check("Help menu has How to Use, About and Debug report",
          [a.text() for a in help_menu7.actions()]
          == ["How to Use", "About", "Debug report"],
          str([a.text() for a in help_menu7.actions()]))
    check("About shows __version__",
          f"v{app.__version__}" in window7.about_text(),
          window7.about_text())
    check("Save report action starts disabled",
          not window7.save_report_action.isEnabled())
    check("Save report action shortcut is Ctrl+S",
          window7.save_report_action.shortcut().toString() == "Ctrl+S",
          window7.save_report_action.shortcut().toString())
    check("Ctrl+R shortcut exists",
          window7.run_shortcut.key().toString() == "Ctrl+R",
          window7.run_shortcut.key().toString())
    check("Esc shortcut exists",
          window7.cancel_shortcut.key().toString() == "Esc",
          window7.cancel_shortcut.key().toString())
    window7.run_shortcut.activated.emit()
    qt_app.processEvents()
    check("Ctrl+R with nothing ticked asks for a model",
          window7.status_label.text() == "Tick at least one model first."
          and window7.worker is None,
          window7.status_label.text())
    window7.cancel_shortcut.activated.emit()
    qt_app.processEvents()
    check("Esc with no run running is a no-op", window7.worker is None)
    reports_before = (
        {f for f in os.listdir(RESULTS_DIR)
         if f.startswith("bench-results-")}
        if os.path.exists(RESULTS_DIR) else set()
    )
    window7.save_report_action.trigger()
    qt_app.processEvents()
    reports_after = (
        {f for f in os.listdir(RESULTS_DIR)
         if f.startswith("bench-results-")}
        if os.path.exists(RESULTS_DIR) else set()
    )
    check("Save report with no results writes nothing",
          reports_before == reports_after,
          f"{reports_before} -> {reports_after}")

    # Ctrl+R starts a real run; Ctrl+S (the menu action) saves it.
    if os.path.exists(PROVIDERS_FILE):
        os.remove(PROVIDERS_FILE)
    if os.path.exists(CAPABILITIES_FILE):
        os.remove(CAPABILITIES_FILE)
    server7, _t = fake_ollama.start_server()
    host7, port7 = server7.server_address
    os.environ["OLLAMA_HOST"] = f"http://{host7}:{port7}"
    window7.refresh_models()
    wait_until(lambda: window7.model_list.count() == 4, timeout=10,
               app=qt_app)
    set_checked_list(window7.model_list, {"fake-smart:2b"})
    set_checked_templates(window7, {"math"})
    window7.run_shortcut.activated.emit()
    done7 = wait_until(
        lambda: window7.worker is None and window7.save_button.isEnabled(),
        timeout=120, app=qt_app)
    check("Ctrl+R starts a run that completes", done7)
    math_run = window7.results.get(
        "fake-smart:2b", {}).get("templates", {}).get("math", {})
    check("Ctrl+R run scored math 100%",
          math_run.get("template_score") == 100.0, str(math_run))
    check("Save report action enabled after a run",
          window7.save_report_action.isEnabled())
    window7.save_report_action.trigger()
    qt_app.processEvents()
    reports_saved = (
        {f for f in os.listdir(RESULTS_DIR)
         if f.startswith("bench-results-")}
        if os.path.exists(RESULTS_DIR) else set()
    )
    check("Save report action writes the report files",
          len(reports_saved - reports_before) == 2,
          str(reports_saved - reports_before))

    # Ctrl+R during a run is a no-op; Esc cancels the run.
    server7slow, _t = fake_ollama.start_server()
    server7slow.slow = True
    host7s, port7s = server7slow.server_address
    os.environ["OLLAMA_HOST"] = f"http://{host7s}:{port7s}"
    window7.refresh_models()
    wait_until(lambda: window7.model_list.count() == 4, timeout=10,
               app=qt_app)
    set_checked_list(window7.model_list, {"fake-smart:2b"})
    set_checked_templates(window7, {"speed"})
    window7.run_button.click()
    wait_until(lambda: window7.worker is not None, timeout=10, app=qt_app)
    time.sleep(1.0)
    worker_before = window7.worker
    window7.run_shortcut.activated.emit()
    qt_app.processEvents()
    time.sleep(0.2)
    check("Ctrl+R during a run is a no-op (same worker, still running)",
          window7.worker is worker_before and window7.worker is not None)
    window7.cancel_shortcut.activated.emit()
    done7s = wait_until(
        lambda: window7.worker is None and window7.run_button.isEnabled(),
        timeout=60, app=qt_app)
    check("Esc cancels a running run", done7s)
    check("Esc cancel status",
          window7.status_label.text()
          == "Cancelled. Results so far are still in the table.",
          window7.status_label.text())
    server7slow.shutdown()
    server7slow.server_close()
    server7.shutdown()
    server7.server_close()

    quit_action = [a for a in file_menu7.actions()
                   if a.text() == "Quit"][0]
    quit_action.trigger()
    qt_app.processEvents()
    check("Quit menu item closes the window", not window7.isVisible())
    window7.deleteLater()
    qt_app.processEvents()

    # ---------- v7 T5: headless mode --------------------------------------
    help_run = subprocess.run(
        [sys.executable, "-u", "ollama_system_test.py", "--help"],
        cwd=PKG_DIR, capture_output=True, text=True, timeout=120)
    check("headless --help exits 0", help_run.returncode == 0,
          f"exit {help_run.returncode}\n{help_run.stderr[-300:]}")
    check("--help documents --headless",
          "--headless" in help_run.stdout, help_run.stdout)
    check("--help documents --models",
          "--models" in help_run.stdout, help_run.stdout)
    check("--help documents --templates",
          "--templates" in help_run.stdout, help_run.stdout)

    headless_results = os.path.join(SCRATCH, "headless-results")
    headless_providers = os.path.join(SCRATCH, "headless-providers.json")
    headless_caps = os.path.join(SCRATCH, "headless-caps.json")
    server7h, _t = fake_ollama.start_server()
    host7h, port7h = server7h.server_address
    pserver7h, _t = fake_ollama.start_provider_server()
    phost7h, pport7h = pserver7h.server_address
    with open(headless_providers, "w", encoding="utf-8") as handle:
        json.dump({"providers": [{
            "name": "v7-provider",
            "base_url": f"http://{phost7h}:{pport7h}/v1",
            # The fake provider only accepts its own key.
            "api_key": fake_ollama.PROVIDER_API_KEY,
            "models": [{
                "id": "fake-gpt-smart", "display_name": "",
                "price_input_per_1m": None, "price_output_per_1m": None,
                "capabilities": ["text"],
            }],
        }]}, handle)

    def headless_env(**overrides):
        env = dict(os.environ)
        # No display at all: if headless mode created a QApplication,
        # Qt would fail to load a platform plugin and the run would die.
        env.pop("DISPLAY", None)
        env.pop("QT_QPA_PLATFORM", None)
        env["OLLAMA_HOST"] = f"http://{host7h}:{port7h}"
        env["OST_RESULTS_DIR"] = headless_results
        env["OST_PROVIDERS_FILE"] = headless_providers
        env["OST_CAPABILITIES_FILE"] = headless_caps
        env.update(overrides)
        return env

    headless = subprocess.run(
        [sys.executable, "-u", "ollama_system_test.py", "--headless",
         "--models", "fake-smart:2b,v7-provider / fake-gpt-smart",
         "--templates", "coding,math"],
        cwd=PKG_DIR, env=headless_env(),
        capture_output=True, text=True, timeout=300)
    check("headless run exits 0 with no display set",
          headless.returncode == 0,
          f"exit {headless.returncode}\n{headless.stdout[-400:]}\n"
          f"{headless.stderr[-400:]}")
    check("headless prints a plain summary",
          "100% overall" in headless.stdout
          and "Run files saved under" in headless.stdout,
          headless.stdout[-500:])
    check("headless prints no traceback",
          "Traceback" not in headless.stderr, headless.stderr[-500:])
    json_files = []
    profiles = []
    for root, _dirs, files in os.walk(headless_results):
        # v17: the run manifest lives under _runs — it is not a run file.
        if "_runs" in root.split(os.sep):
            continue
        for name in files:
            if name.endswith(".json"):
                json_files.append(os.path.join(root, name))
            if name == "PROFILE.md":
                profiles.append(os.path.join(root, name))
    check("headless writes the same run files (2 models x 2 templates)",
          len(json_files) == 4, str(json_files))
    check("headless writes profiles", len(profiles) == 2, str(profiles))
    leaked = False
    for path in json_files + profiles:
        with open(path, encoding="utf-8") as handle:
            if fake_ollama.PROVIDER_API_KEY in handle.read():
                leaked = True
    check("headless run files contain no API key", not leaked)
    if json_files:
        with open(sorted(json_files)[0], encoding="utf-8") as handle:
            headless_run_data = json.load(handle)
        check("headless run file has the GUI run shape",
              headless_run_data.get("template") in ("coding", "math")
              and isinstance(headless_run_data.get("tasks"), list)
              and headless_run_data.get("template_score") == 100.0,
              str(headless_run_data)[:300])
    # A headless run with the fixtures missing says "not run", never a
    # score and never "no score" — the v6 distinction survives headless.
    empty_images7 = os.path.join(SCRATCH, "empty-images")
    os.makedirs(empty_images7, exist_ok=True)
    headless_notrun = subprocess.run(
        [sys.executable, "-u", "ollama_system_test.py", "--headless",
         "--models", "fake-smart:2b", "--templates", "vision"],
        cwd=PKG_DIR,
        env=headless_env(OST_IMAGES_DIR=empty_images7),
        capture_output=True, text=True, timeout=120)
    check("headless run with missing fixtures exits 0",
          headless_notrun.returncode == 0,
          f"exit {headless_notrun.returncode}\n"
          f"{headless_notrun.stdout[-300:]}\n{headless_notrun.stderr[-300:]}")
    check("headless summary says not run for the missing fixture",
          "Vision: not run (test file missing)" in headless_notrun.stdout,
          headless_notrun.stdout[-500:])
    bad_model = subprocess.run(
        [sys.executable, "-u", "ollama_system_test.py", "--headless",
         "--models", "no-such-model:9b", "--templates", "math"],
        cwd=PKG_DIR, env=headless_env(),
        capture_output=True, text=True, timeout=120)
    check("headless exits 1 for an unknown model",
          bad_model.returncode == 1 and "Model not found" in bad_model.stdout,
          f"exit {bad_model.returncode}\n{bad_model.stdout[-300:]}")
    bad_template = subprocess.run(
        [sys.executable, "-u", "ollama_system_test.py", "--headless",
         "--models", "fake-smart:2b", "--templates", "nope"],
        cwd=PKG_DIR, env=headless_env(),
        capture_output=True, text=True, timeout=120)
    check("headless exits 1 for an unknown template",
          bad_template.returncode == 1
          and "Template not found" in bad_template.stdout,
          f"exit {bad_template.returncode}\n{bad_template.stdout[-300:]}")
    missing_models = subprocess.run(
        [sys.executable, "-u", "ollama_system_test.py", "--headless",
         "--templates", "math"],
        cwd=PKG_DIR, env=headless_env(),
        capture_output=True, text=True, timeout=120)
    check("headless exits 1 with no --models",
          missing_models.returncode == 1 and "--models" in missing_models.stdout,
          f"exit {missing_models.returncode}\n{missing_models.stdout[-300:]}")
    ollama_down = subprocess.run(
        [sys.executable, "-u", "ollama_system_test.py", "--headless",
         "--models", "fake-smart:2b", "--templates", "math"],
        cwd=PKG_DIR,
        env=headless_env(
            OLLAMA_HOST=f"http://127.0.0.1:{free_port()}"),
        capture_output=True, text=True, timeout=120)
    check("headless exits 1 when Ollama is down",
          # v17: headless tries to start Ollama itself; the sandbox
          # has no ollama binary, so the start reports that.
          ollama_down.returncode == 1
          and "Ollama is not installed" in ollama_down.stdout,
          f"exit {ollama_down.returncode}\n{ollama_down.stdout[-300:]}")
    server7h.shutdown()
    server7h.server_close()
    pserver7h.shutdown()
    pserver7h.server_close()

    # ---------- v7 T8: .desktop launcher ----------------------------------
    import shlex

    def parse_desktop(path):
        """A strict read of a .desktop file: (keys, error)."""
        try:
            with open(path, encoding="utf-8") as handle:
                lines = handle.read().splitlines()
        except OSError as exc:
            return None, str(exc)
        if not lines or lines[0] != "[Desktop Entry]":
            return None, "missing [Desktop Entry] header"
        keys = {}
        for line in lines[1:]:
            if not line.strip() or line.startswith("#"):
                continue
            if "=" not in line:
                return None, f"line without =: {line!r}"
            key, value = line.split("=", 1)
            keys[key] = value
        return keys, ""

    for desktop_name, want_terminal in (("Install-OST.desktop", "true"),
                                        ("OST.desktop", "false")):
        desktop_path = os.path.join(PKG_DIR, desktop_name)
        check(f"{desktop_name} ships in the repo",
              os.path.exists(desktop_path))
        keys, error = parse_desktop(desktop_path)
        check(f"{desktop_name} parses", keys is not None, error)
        if keys is None:
            continue
        check(f"{desktop_name} is a Type=Application entry",
              keys.get("Type") == "Application", str(keys))
        check(f"{desktop_name} has a Name", bool(keys.get("Name")),
              str(keys))
        check(f"{desktop_name} Terminal={want_terminal}",
              keys.get("Terminal") == want_terminal, str(keys))
        exec_line = keys.get("Exec", "")
        # GLib's desktop parser rejects any nested escaped quote in
        # Exec — the whole file fails. This is the check that guards it.
        check(f"{desktop_name} Exec has no nested escaped quotes",
              '\\"' not in exec_line, exec_line)
        check(f"{desktop_name} Exec ends with %k as its own token",
              exec_line.rstrip().endswith(" %k"), exec_line)
        check(f"{desktop_name} Exec resolves its folder with a cwd fallback",
              "os.getcwd()" in exec_line and "sys.argv[1]" in exec_line,
              exec_line)
    if shutil.which("desktop-file-validate"):
        for desktop_name in ("Install-OST.desktop", "OST.desktop"):
            validated = subprocess.run(
                ["desktop-file-validate",
                 os.path.join(PKG_DIR, desktop_name)],
                capture_output=True, text=True, timeout=60)
            check(f"{desktop_name} passes desktop-file-validate",
                  validated.returncode == 0,
                  validated.stdout + validated.stderr)
    else:
        print("SKIP: desktop-file-validate not installed")

    # The installer Exec logic, against a stub install.sh (no real
    # install): the folder resolves from %k, and from the cwd when
    # %k is empty, even with a space in the path.
    installer_dir = os.path.join(SCRATCH, "installer dir")
    os.makedirs(installer_dir, exist_ok=True)
    shutil.copy(os.path.join(PKG_DIR, "Install-OST.desktop"),
                installer_dir)
    with open(os.path.join(installer_dir, "install.sh"),
              "w", encoding="utf-8") as handle:
        handle.write("#!/bin/sh\necho STUB-INSTALL-RAN\n")
    installer_keys, _err = parse_desktop(
        os.path.join(PKG_DIR, "Install-OST.desktop"))
    installer_argv = shlex.split(installer_keys["Exec"])
    installer_argv[0] = sys.executable
    installer_argv[-1] = os.path.join(installer_dir,
                                      "Install-OST.desktop")
    stub_run = subprocess.run(
        installer_argv, cwd="/", stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=60)
    check("installer Exec resolves its folder from %k (spaced path)",
          stub_run.returncode == 0
          and "STUB-INSTALL-RAN" in stub_run.stdout
          and "Install finished." in stub_run.stdout,
          f"exit {stub_run.returncode}\n{stub_run.stdout[-300:]}\n"
          f"{stub_run.stderr[-300:]}")
    stub_run_nokey = subprocess.run(
        installer_argv[:-1], cwd=installer_dir,
        stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=60)
    check("installer Exec falls back to the cwd when %k is empty",
          stub_run_nokey.returncode == 0
          and "STUB-INSTALL-RAN" in stub_run_nokey.stdout,
          f"exit {stub_run_nokey.returncode}\n"
          f"{stub_run_nokey.stdout[-300:]}")

    # Launching from the desktop icon opens the app. The launcher
    # Exec calls plain `python3`, which has no PySide6 in the sandbox,
    # so the test PATH leads with a shim pointing at this interpreter.
    shim_dir = os.path.join(SCRATCH, "pybin")
    os.makedirs(shim_dir, exist_ok=True)
    with open(os.path.join(shim_dir, "python3"),
              "w", encoding="utf-8") as handle:
        handle.write(f"#!/bin/sh\nexec {sys.executable} \"$@\"\n")
    os.chmod(os.path.join(shim_dir, "python3"), 0o755)

    def app_pids():
        found = subprocess.run(
            ["pgrep", "-f", "ollama_system_test.py"],
            capture_output=True, text=True)
        pids = set()
        for line in found.stdout.splitlines():
            try:
                pid = int(line.strip())
            except ValueError:
                continue
            if pid != os.getpid():
                pids.add(pid)
        return pids

    def kill_pids(pids):
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    launch_env = dict(os.environ)
    launch_env["PATH"] = shim_dir + os.pathsep + launch_env.get("PATH", "")
    launch_env["OST_RESULTS_DIR"] = os.path.join(SCRATCH, "launch-results")
    launch_env["OST_PROVIDERS_FILE"] = os.path.join(
        SCRATCH, "launch-providers.json")
    launch_env["OST_CAPABILITIES_FILE"] = os.path.join(
        SCRATCH, "launch-caps.json")
    if shutil.which("gio") is None:
        print("SKIP: gio launch tests need gio")
    else:
        # gio expands %k to empty, so this exercises the
        # os.getcwd() fallback in the launcher.
        before_launch = app_pids()
        gio_proc = subprocess.Popen(
            ["gio", "launch", os.path.join(PKG_DIR, "OST.desktop")],
            cwd=PKG_DIR, env=launch_env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            gio_proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            gio_proc.kill()
        check("gio launch of OST.desktop exits 0",
              gio_proc.returncode == 0, str(gio_proc.returncode))
        started = wait_until(
            lambda: bool(app_pids() - before_launch),
            timeout=30, app=qt_app)
        check("launching OST.desktop opens the app", started)
        if started:
            time.sleep(2)
            check("the launched app stays up",
                  bool(app_pids() - before_launch))
        kill_pids(app_pids() - before_launch)
        gone = wait_until(
            lambda: not (app_pids() - before_launch),
            timeout=10, app=qt_app)
        check("launched app exits when stopped", gone)

    # install.sh installs the launcher + icon into a test HOME.
    # It re-runs on the v6 install-good tree (its .venv already has
    # PySide6, so pip is a no-op and the run stays fast and offline).
    test_home = os.path.join(SCRATCH, "test-home")
    os.makedirs(test_home, exist_ok=True)
    install_env = dict(os.environ)
    install_env["HOME"] = test_home
    install_env["PIP_NO_INDEX"] = "1"
    install_env["TMPDIR"] = SCRATCH
    reinstall = subprocess.run(
        ["sh", "install.sh"], cwd=install_good,
        env=install_env, capture_output=True, text=True, timeout=600)
    check("install.sh re-run exits 0", reinstall.returncode == 0,
          f"exit {reinstall.returncode}\n{reinstall.stdout[-400:]}\n"
          f"{reinstall.stderr[-400:]}")
    check("install.sh says it installed the launcher",
          "Launcher installed: OST is now in your app menu."
          in reinstall.stdout,
          reinstall.stdout[-500:])
    installed_desktop = os.path.join(
        test_home, ".local", "share", "applications", "OST.desktop")
    installed_icon = os.path.join(
        test_home, ".local", "share", "icons", "ost-icon.png")
    check("install.sh installs OST.desktop into the app menu",
          os.path.exists(installed_desktop))
    check("install.sh installs ost-icon.png into the icons dir",
          os.path.exists(installed_icon))
    if os.path.exists(installed_icon):
        check("installed icon is the tree icon",
              file_hash(installed_icon) == file_hash(icon_path))
    if os.path.exists(installed_desktop):
        with open(installed_desktop, encoding="utf-8") as handle:
            installed_text = handle.read()
        check("installed launcher has the app folder baked in (no %k)",
              "%k" not in installed_text
              and install_good in installed_text,
              installed_text)
        installed_keys, installed_error = parse_desktop(installed_desktop)
        check("installed launcher still parses",
              installed_keys is not None, installed_error)
        if installed_keys is not None:
            check("installed launcher Exec has no nested escaped quotes",
                  '\\"' not in installed_keys.get("Exec", ""),
                  installed_keys.get("Exec", ""))
        if shutil.which("gio") is not None:
            # Launched from a different folder: only the baked-in
            # path can find the app now.
            before_installed = app_pids()
            gio_proc2 = subprocess.Popen(
                ["gio", "launch", installed_desktop],
                cwd=test_home, env=launch_env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                gio_proc2.wait(timeout=30)
            except subprocess.TimeoutExpired:
                gio_proc2.kill()
            started2 = wait_until(
                lambda: bool(app_pids() - before_installed),
                timeout=30, app=qt_app)
            check("launching the installed launcher opens the app",
                  started2)
            kill_pids(app_pids() - before_installed)
            wait_until(
                lambda: not (app_pids() - before_installed),
                timeout=10, app=qt_app)

    # ================= v8: deletion + store size, cost guard + budget, ===
    # ================= export + compare ===================================
    # ---------- v8 T10: token + cost estimate math (unit level) -----------
    templates8, _warnings8 = app.load_templates()
    by_id8 = {t["id"]: t for t in templates8}
    math_p8, math_c8 = app.estimate_template_tokens(by_id8["math"])
    check("math estimate: completion is 10 tasks x num_predict",
          math_c8 == 10 * by_id8["math"]["num_predict"], str(math_c8))
    check("math estimate: prompt tokens are positive and small",
          0 < math_p8 < 1000, str(math_p8))
    speed_p8, speed_c8 = app.estimate_template_tokens(by_id8["speed"])
    check("speed estimate: prompt tokens are the exact context lengths",
          speed_p8 == sum(by_id8["speed"]["context_lengths"]),
          str(speed_p8))
    check("speed estimate: completion is 3 lengths x num_predict",
          speed_c8 == 3 * by_id8["speed"]["num_predict"], str(speed_c8))
    lc_p8, _lc_c8 = app.estimate_template_tokens(by_id8["long_context"])
    check("long_context estimate counts the exact context tokens",
          lc_p8 >= sum(t["context_length"]
                       for t in by_id8["long_context"]["tasks"]),
          str(lc_p8))
    mt_p8, mt_c8 = app.estimate_template_tokens(by_id8["multi_turn"])
    check("multi_turn estimate: completion is 5 tasks x 2 turns x num_predict",
          mt_c8 == 5 * 2 * by_id8["multi_turn"]["num_predict"], str(mt_c8))
    check("multi_turn estimate counts prompt tokens",
          mt_p8 > 0, str(mt_p8))
    check("image_generation estimates no text tokens",
          app.estimate_template_tokens(by_id8["image_generation"]) == (0, 0))
    check("audio estimates no text tokens",
          app.estimate_template_tokens(by_id8["audio"]) == (0, 0))
    check("cost confirm threshold is $1.00",
          app.COST_CONFIRM_THRESHOLD_USD == 1.00,
          str(app.COST_CONFIRM_THRESHOLD_USD))
    priced8 = app.provider_target(
        {"name": "v8-provider"},
        {"id": "fake-gpt-smart", "price_input_per_1m": 1000.0,
         "price_output_per_1m": 1000.0, "capabilities": ["text"]})
    est8 = app.estimate_run_cost([priced8], [by_id8["math"]])
    warm_p8 = (len(app.WARMUP_PROMPT) + 3) // 4
    expected8 = (
        math_p8 + warm_p8 + math_c8 + app.WARMUP_PREDICT
    ) * 1000.0 / 1_000_000
    check("cost estimate matches the token math (warmup included)",
          abs(est8["total"] - expected8) < 1e-9, str(est8["total"]))
    check("cost estimate is over the $1.00 threshold at these prices",
          est8["total"] > app.COST_CONFIRM_THRESHOLD_USD,
          str(est8["total"]))
    unpriced8 = app.provider_target(
        {"name": "v8-provider"},
        {"id": "fake-gpt-dumb", "capabilities": ["text"]})
    est8u = app.estimate_run_cost([unpriced8], [by_id8["math"]])
    check("no prices saved means no total, model listed as unknown",
          est8u["total"] is None
          and est8u["unknown"] == ["v8-provider / fake-gpt-dumb"],
          str(est8u))
    check("local Ollama targets are free and never estimated",
          app.estimate_run_cost(
              [app.ollama_target("fake-smart:2b")], [by_id8["math"]]
          )["per_model"] == [])
    est8v = app.estimate_run_cost([priced8], [by_id8["vision"]])
    check("templates the model cannot run are not estimated",
          est8v["per_model"][0]["prompt_tokens"] == warm_p8
          and est8v["per_model"][0]["completion_tokens"]
          == app.WARMUP_PREDICT,
          str(est8v["per_model"][0]))
    check("is_budget_stopped marks only the budget detail",
          app.is_budget_stopped(
              {"passed": None, "detail": "time budget reached"})
          and not app.is_budget_stopped(
              {"passed": None, "detail": "not run: test image not found"})
          and not app.is_budget_stopped(
              {"passed": False, "detail": "time budget reached"}))
    check("template_score_text names the budget stop",
          app.template_score_text(
              {"template_score": None, "not_run": 0, "budget_stopped": 3})
          == "time budget reached")

    # ---------- v8 T9: delete + store size (unit level) --------------------
    unit_dir8 = os.path.join(SCRATCH, "unit-results")
    unit_model8 = os.path.join(unit_dir8, "model-x", "sys1")
    os.makedirs(os.path.join(unit_model8, "2026-11-12-120000-math-images"),
                exist_ok=True)
    os.makedirs(os.path.join(unit_model8, "2026-11-12-120000-math-audio"),
                exist_ok=True)
    unit_run8 = os.path.join(unit_model8, "2026-11-12-120000-math.json")
    with open(unit_run8, "w", encoding="utf-8") as handle:
        handle.write("{}")
    with open(os.path.join(
            unit_model8, "2026-11-12-120000-math-images", "a.png"),
            "wb") as handle:
        handle.write(b"12345")
    with open(os.path.join(
            unit_model8, "2026-11-12-120000-math-audio", "a.mp3"),
            "wb") as handle:
        handle.write(b"1234567")
    unit_other8 = os.path.join(unit_model8, "2026-11-12-130000-coding.json")
    with open(unit_other8, "w", encoding="utf-8") as handle:
        handle.write("{}")
    check("store_size counts the run files and the media",
          app.store_size(unit_dir8) == 2 + 5 + 7 + 2,
          str(app.store_size(unit_dir8)))
    check("fmt_store_size shows one decimal for MB",
          app.fmt_store_size(12_300_000) == "12.3 MB",
          app.fmt_store_size(12_300_000))
    removed8 = app.delete_run_file(unit_run8)
    check("delete_run_file removes the file and both media folders",
          not os.path.exists(unit_run8)
          and not os.path.exists(os.path.join(
              unit_model8, "2026-11-12-120000-math-images"))
          and not os.path.exists(os.path.join(
              unit_model8, "2026-11-12-120000-math-audio"))
          and len(removed8) == 3, str(removed8))
    check("delete_run_file leaves the other run alone",
          os.path.exists(unit_other8))
    check("store_size drops after deletion",
          app.store_size(unit_dir8) == 2, str(app.store_size(unit_dir8)))
    check("deleting a missing file is not an error",
          app.delete_run_file(unit_run8) == [])

    # ---------- v8 T11: export rendering + compare (unit level) ------------
    sample8 = [
        {"name": "model-a", "status": "done", "overall_score": 100.0,
         "avg_decode_tok_s": 20.0,
         "templates": {
             "math": {"template": "math", "template_name": "Math",
                      "template_score": 100.0, "passed": 5, "total": 5,
                      "not_run": 0, "budget_stopped": 0},
             "vision": {"template": "vision", "template_name": "Vision",
                        "template_score": None, "passed": None, "total": 0,
                        "not_run": 5, "budget_stopped": 0},
         }},
        {"name": "model-b", "status": "done", "overall_score": 60.0,
         "avg_decode_tok_s": 10.0,
         "templates": {
             "math": {"template": "math", "template_name": "Math",
                      "template_score": 60.0, "passed": 3, "total": 5,
                      "not_run": 0, "budget_stopped": 0},
         }},
    ]
    csv8 = app.render_export_csv(sample8)
    check("CSV export renders the header + both models",
          "Model,Overall score" in csv8
          and "model-a" in csv8 and "model-b" in csv8, csv8)
    check("CSV export marks the not-run template",
          "not run" in csv8, csv8)
    check("CSV export marks a template one model never ran",
          "n/a" in csv8, csv8)
    html8 = app.render_export_html(sample8)
    check("HTML export renders a table with both models",
          "<table>" in html8 and "model-a" in html8 and "model-b" in html8,
          html8[:300])
    check("HTML export escapes model names",
          "<script>" not in app.render_export_html(
              [{"name": "<script>", "status": "done",
                "overall_score": None, "avg_decode_tok_s": None,
                "templates": {}}]))
    cmp_dir8 = os.path.join(SCRATCH, "compare-results")

    def _craft8(slug, template, name, score):
        folder = os.path.join(cmp_dir8, slug, "sys1")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"2026-11-12-120000-{template}.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(
                {"model": slug, "template": template,
                 "template_name": name, "template_score": score,
                 "passed": 5 if score == 100.0 else 3, "total": 5,
                 "not_run": 0, "avg_decode_tok_s": 20.0,
                 "timestamp": "2026-11-12T12:00:00",
                 "system_id": "sys1"},
                handle)

    _craft8("model-a", "math", "Math", 100.0)
    _craft8("model-a", "coding", "Coding", 80.0)
    _craft8("model-b", "math", "Math", 60.0)
    compared8 = app.compare_models(cmp_dir8, "model-a", "model-b")
    check("compare_models returns both models' latest scores",
          compared8 is not None
          and compared8["overall_a"] == 90.0
          and compared8["overall_b"] == 60.0, str(compared8))
    check("compare_models marks a template one model never ran",
          compared8 is not None
          and any(r["name"] == "Coding" and r["missing_b"]
                  for r in compared8["rows"]),
          str(compared8["rows"] if compared8 else None))
    check("compare_models returns None when a model has no runs",
          app.compare_models(cmp_dir8, "model-a", "model-z") is None)

    # ---------- v8 GUI setup: fake servers + a v8 window ------------------
    shutil.rmtree(RESULTS_DIR, ignore_errors=True)
    if os.path.exists(PROVIDERS_FILE):
        os.remove(PROVIDERS_FILE)
    if os.path.exists(CAPABILITIES_FILE):
        os.remove(CAPABILITIES_FILE)
    server8, _t = fake_ollama.start_server()
    host8, port8 = server8.server_address
    os.environ["OLLAMA_HOST"] = f"http://{host8}:{port8}"
    pserver8, _t = fake_ollama.start_provider_server()
    phost8, pport8 = pserver8.server_address
    with open(PROVIDERS_FILE, "w", encoding="utf-8") as handle:
        json.dump({"providers": [{
            "name": "v8-provider",
            "base_url": f"http://{phost8}:{pport8}/v1",
            # The fake provider only accepts its own key.
            "api_key": fake_ollama.PROVIDER_API_KEY,
            "models": [
                {"id": "fake-gpt-smart", "display_name": "",
                 "price_input_per_1m": 1000.0,
                 "price_output_per_1m": 1000.0,
                 "capabilities": ["text", "image_generation", "audio"]},
                {"id": "fake-gpt-partial", "display_name": "",
                 "price_input_per_1m": 0.01,
                 "price_output_per_1m": 0.01,
                 "capabilities": ["text"]},
                {"id": "fake-gpt-dumb", "display_name": "",
                 "price_input_per_1m": None,
                 "price_output_per_1m": None,
                 "capabilities": ["text"]},
            ],
        }]}, handle)
    window8 = app.MainWindow()
    window8.show()
    wait_until(lambda: window8.model_list.count() == 7, timeout=10,
               app=qt_app)

    # Dialogs are driven programmatically: QMessageBox.question is
    # stubbed to record its text and answer Yes or No on demand, and
    # QFileDialog.getSaveFileName returns a fixed path. Both are
    # restored in the finally below — a real modal dialog would hang
    # the suite.
    from PySide6.QtWidgets import QFileDialog, QMessageBox
    question_calls8 = []
    question_answer8 = {"answer": QMessageBox.Yes}

    def fake_question8(*args, **kwargs):
        question_calls8.append(
            args[2] if len(args) > 2 else kwargs.get("text", ""))
        return question_answer8["answer"]

    real_question8 = QMessageBox.question
    QMessageBox.question = fake_question8
    dialog_paths8 = {"path": ""}

    def fake_save8(*args, **kwargs):
        return (dialog_paths8["path"], "")

    real_save8 = QFileDialog.getSaveFileName
    QFileDialog.getSaveFileName = fake_save8
    try:
        check("v8 window lists 4 local + 3 provider models",
              window8.model_list.count() == 7,
              str(window8.model_list.count()))
        check("window has 7 tabs with Compare after Profiles",
              window8.tabs.count() == 7
              and window8.tabs.tabText(5) == "Compare",
              str([window8.tabs.tabText(i)
                   for i in range(window8.tabs.count())]))
        check("budget control defaults to 0 (no limit)",
              window8.budget_spin.value() == 0)
        check("budget mapping: 0 means no budget",
              window8._time_budget_seconds() is None)
        window8.budget_spin.setValue(2)
        check("budget mapping: 2 minutes is 120 seconds",
              window8._time_budget_seconds() == 120)
        window8.budget_spin.setValue(0)
        check("export buttons start disabled",
              not window8.export_csv_button.isEnabled()
              and not window8.export_html_button.isEnabled())

        # ---------- v8 T10: cost guard (now the run preview, v13) ---------
        # R11 folded the cost guard into the run preview: every run
        # shows its preview (models, steps, time, cost) and asks to
        # go ahead. The preview seam is stubbed here to record the
        # preview text and answer Run or Adjust on demand; the real
        # seam (the suite-wide Run stub) is restored in the finally.
        preview_calls8 = []
        preview_answer8 = {"answer": True}

        def fake_preview8(self, targets, templates, pairs=None):
            preview = app.build_run_preview(
                targets, templates, app.get_results_dir(), pairs)
            preview_calls8.append(preview["text"])
            return preview_answer8["answer"]

        real_preview8 = app.MainWindow._show_run_preview
        app.MainWindow._show_run_preview = fake_preview8
        try:
            set_checked_list(window8.model_list,
                             {"v8-provider / fake-gpt-smart"})
            set_checked_templates(window8, {"math"})
            preview_answer8["answer"] = False
            preview_calls8.clear()
            window8.run_button.click()
            qt_app.processEvents()
            check("provider run shows its preview",
                  len(preview_calls8) == 1, str(len(preview_calls8)))
            if preview_calls8:
                check("preview names the estimated cost",
                      "$3.29" in preview_calls8[0], preview_calls8[0])
                check("preview names the model",
                      "v8-provider / fake-gpt-smart" in preview_calls8[0],
                      preview_calls8[0])
            check("answering Adjust starts no run",
                  window8.worker is None
                  and window8.status_label.text() == "Run cancelled.",
                  window8.status_label.text())
            preview_answer8["answer"] = True
            window8.run_button.click()
            done8 = wait_until(
                lambda: window8.worker is None
                and window8.save_button.isEnabled(),
                timeout=120, app=qt_app)
            check("answering Run runs the provider model", done8)
            check("provider math run scored 100%",
                  window8.results.get(
                      "v8-provider / fake-gpt-smart", {}).get(
                      "templates", {}).get("math", {}).get("template_score")
                  == 100.0,
                  str(window8.results.get("v8-provider / fake-gpt-smart", {})))

            # A cheap run shows its preview too — the preview is for
            # every run, not just expensive ones.
            set_checked_list(window8.model_list,
                             {"v8-provider / fake-gpt-partial"})
            set_checked_templates(window8, {"math"})
            preview_calls8.clear()
            window8.run_button.click()
            done8c = wait_until(
                lambda: window8.worker is None
                and window8.save_button.isEnabled(),
                timeout=120, app=qt_app)
            check("cheap provider run shows its preview",
                  len(preview_calls8) == 1, str(preview_calls8))
            check("cheap provider run completes", done8c)
            check("partial math scored 50%",
                  window8.results.get(
                      "v8-provider / fake-gpt-partial", {}).get(
                      "templates", {}).get("math", {}).get("template_score")
                  == 50.0,
                  str(window8.results.get("v8-provider / fake-gpt-partial", {})))

            # No prices saved: the preview says so plainly and still asks.
            set_checked_list(window8.model_list,
                             {"v8-provider / fake-gpt-dumb"})
            set_checked_templates(window8, {"math"})
            preview_answer8["answer"] = False
            preview_calls8.clear()
            window8.run_button.click()
            qt_app.processEvents()
            check("unknown-price run shows its preview",
                  len(preview_calls8) == 1, str(len(preview_calls8)))
            if preview_calls8:
                check("unknown-price preview says cost unknown plainly",
                      "no prices saved: cost unknown" in preview_calls8[0],
                      preview_calls8[0])
            check("answering Adjust to the unknown-price run starts nothing",
                  window8.worker is None)

            # A local-only run shows the preview too — and its cost
            # line says there is nothing to pay.
            set_checked_list(window8.model_list, {"fake-smart:2b"})
            set_checked_templates(window8, {"math"})
            preview_calls8.clear()
            window8.run_button.click()
            done8l = wait_until(
                lambda: window8.worker is None
                and window8.save_button.isEnabled(),
                timeout=120, app=qt_app)
            check("local-only run shows its preview",
                  len(preview_calls8) == 1, str(preview_calls8))
            if preview_calls8:
                check("local preview says nothing to pay",
                      "nothing to pay" in preview_calls8[0],
                      preview_calls8[0])
            check("local run completes", done8l)
        finally:
            app.MainWindow._show_run_preview = real_preview8

        # ---------- v8 T10: per-template time budget ----------------------
        # A real run through the worker: a 1-second budget on math
        # (10 tasks at ~0.65 s each against the fake) stops the
        # template partway, and the run carries on with coding.
        budget_targets8 = [app.ollama_target("fake-smart:2b")]
        budget_templates8 = [by_id8["math"], by_id8["coding"]]
        budget_results8 = {}
        budget_finished8 = {"done": False}
        budget_worker8 = app.BenchWorker(
            budget_targets8, budget_templates8, window8.sys_id,
            window8.machine, RESULTS_DIR, time_budget_seconds=1.0)
        budget_worker8.model_done.connect(
            lambda name, result: budget_results8.__setitem__(name, result))
        budget_worker8.run_finished.connect(
            lambda reason: budget_finished8.__setitem__("done", reason))
        budget_worker8.start()
        budget_ok8 = wait_until(lambda: budget_finished8["done"],
                                timeout=120, app=qt_app)
        check("budget run finishes", budget_ok8)
        budget_math8 = budget_results8.get(
            "fake-smart:2b", {}).get("templates", {}).get("math", {})
        check("budget run marks the template time-budget-reached",
              budget_math8.get("time_budget_reached") is True,
              str(budget_math8.get("time_budget_reached")))
        check("budget run stopped at least one math task",
              (budget_math8.get("budget_stopped") or 0) >= 1,
              str(budget_math8.get("budget_stopped")))
        check("stopped tasks carry the plain status",
              any(t.get("detail") == "time budget reached"
                  for t in budget_math8.get("tasks", [])),
              str([t.get("detail") for t in budget_math8.get("tasks", [])]))
        check("budget run scores only the tasks that ran",
              budget_math8.get("template_score") == 100.0
              and budget_math8.get("total")
              == 10 - (budget_math8.get("budget_stopped") or 0),
              f"{budget_math8.get('template_score')} "
              f"({budget_math8.get('passed')}/{budget_math8.get('total')}, "
              f"stopped {budget_math8.get('budget_stopped')})")
        budget_coding8 = budget_results8.get(
            "fake-smart:2b", {}).get("templates", {}).get("coding", {})
        check("the run carries on with the next template after the budget",
              budget_coding8.get("template_score") == 100.0
              and (budget_coding8.get("total") or 0) >= 1,
              str(budget_coding8.get("template_score")))
        check("the next template gets its own budget window",
              budget_coding8.get("time_budget_reached") is True,
              str(budget_coding8.get("time_budget_reached")))
        check("a budget-stopped run still finishes done, not cancelled",
              budget_finished8["done"] == "done"
              and budget_results8.get("fake-smart:2b", {}).get("status")
              == "done",
              str(budget_finished8["done"]))
        check("budget enforcement is a real time check",
              budget_worker8._budget_reached(time.monotonic() - 2.0) is True
              and budget_worker8._budget_reached(time.monotonic()) is False)
        budget_path8 = budget_math8.get("run_file")
        if budget_path8:
            budget_data8 = app.load_run_file(budget_path8)
            check("budget run file records the flag",
                  budget_data8 is not None
                  and budget_data8.get("time_budget_reached") is True)
            if budget_data8 is not None:
                summary8 = app.render_run_summary(budget_data8)
                check("history summary says time budget reached",
                      "Stopped early: time budget reached" in summary8,
                      summary8[:400])
        window8.results = {"fake-smart:2b":
                           budget_results8["fake-smart:2b"]}
        window8.result_order = ["fake-smart:2b"]
        window8.show_detail("fake-smart:2b")
        detail8 = window8.detail_view.toPlainText()
        check("detail view says time budget reached",
              "time budget reached" in detail8, detail8)
        check("detail view marks the stopped tasks",
              "time budget reached  rectangle" in detail8, detail8)
        # No budget: the same template runs to the end.
        nobudget_results8 = {}
        nobudget_finished8 = {"done": False}
        nobudget_worker8 = app.BenchWorker(
            budget_targets8, [by_id8["math"]], window8.sys_id,
            window8.machine, RESULTS_DIR, time_budget_seconds=None)
        nobudget_worker8.model_done.connect(
            lambda name, result: nobudget_results8.__setitem__(name, result))
        nobudget_worker8.run_finished.connect(
            lambda reason: nobudget_finished8.__setitem__("done", reason))
        nobudget_worker8.start()
        nobudget_ok8 = wait_until(lambda: nobudget_finished8["done"],
                                  timeout=120, app=qt_app)
        check("no-budget run finishes", nobudget_ok8)
        nobudget_math8 = nobudget_results8.get(
            "fake-smart:2b", {}).get("templates", {}).get("math", {})
        check("no budget means the template runs to the end",
              nobudget_math8.get("total") == 10
              and nobudget_math8.get("time_budget_reached") is not True
              and nobudget_math8.get("template_score") == 100.0,
              str(nobudget_math8.get("total")))
        # The history list marks the budget-stopped run.
        window8.tabs.setCurrentIndex(3)
        wait_until(lambda: window8.history_model_combo.count() >= 1,
                   timeout=10, app=qt_app)
        for i in range(window8.history_model_combo.count()):
            if window8.history_model_combo.itemData(i) == "fake-smart_2b":
                window8.history_model_combo.setCurrentIndex(i)
                break
        wait_until(lambda: window8.history_list.count() >= 2,
                   timeout=10, app=qt_app)
        hist_texts8 = [
            window8.history_list.item(r).text()
            for r in range(window8.history_list.count())
        ]
        check("history list marks the budget-stopped run",
              any("time budget reached" in text for text in hist_texts8),
              str(hist_texts8))

        # ---------- v8 T9: run deletion + store size ----------------------
        # A provider run with media: image generation saves images,
        # audio saves spoken clips, next to their run files.
        set_checked_list(window8.model_list,
                         {"v8-provider / fake-gpt-smart"})
        set_checked_templates(window8, {"image_generation", "audio"})
        question_answer8["answer"] = QMessageBox.Yes
        question_calls8.clear()
        window8.run_button.click()
        done8m = wait_until(
            lambda: window8.worker is None
            and window8.save_button.isEnabled(),
            timeout=120, app=qt_app)
        check("media run completes", done8m)
        provider_slug8 = "v8-provider_fake-gpt-smart"
        runs8 = app.list_history_runs(RESULTS_DIR, provider_slug8)
        image_run8 = next(
            (r for r in runs8 if r["template"] == "image_generation"), None)
        check("media run wrote an image_generation run file",
              image_run8 is not None)
        media_dir8 = None
        if image_run8 is not None:
            base8 = os.path.splitext(
                os.path.basename(image_run8["path"]))[0]
            media_dir8 = os.path.join(
                os.path.dirname(image_run8["path"]), f"{base8}-images")
            check("image_generation run saved a media folder",
                  os.path.isdir(media_dir8)
                  and len(os.listdir(media_dir8)) == 5,
                  str(os.listdir(media_dir8))
                  if os.path.isdir(media_dir8) else "missing")
        audio_run8 = next(
            (r for r in runs8 if r["template"] == "audio"), None)
        audio_dir8 = None
        if audio_run8 is not None:
            audio_base8 = os.path.splitext(
                os.path.basename(audio_run8["path"]))[0]
            audio_dir8 = os.path.join(
                os.path.dirname(audio_run8["path"]), f"{audio_base8}-audio")
            check("audio run saved a media folder",
                  audio_dir8 is not None and os.path.isdir(audio_dir8)
                  and len(os.listdir(audio_dir8)) == 2,
                  str(os.listdir(audio_dir8))
                  if audio_dir8 and os.path.isdir(audio_dir8) else "missing")
        window8.tabs.setCurrentIndex(3)
        window8.refresh_history()
        qt_app.processEvents()
        expected_size8 = app.store_size(RESULTS_DIR)
        manual8 = 0
        for root, _dirs, files in os.walk(RESULTS_DIR):
            for name in files:
                manual8 += os.path.getsize(os.path.join(root, name))
        check("store size matches an independent walk",
              expected_size8 == manual8,
              f"{expected_size8} vs {manual8}")
        check("store size label shows the results store size",
              window8.store_size_label.text()
              == f"Results use {app.fmt_store_size(expected_size8)}",
              window8.store_size_label.text())
        for i in range(window8.history_model_combo.count()):
            if window8.history_model_combo.itemData(i) == provider_slug8:
                window8.history_model_combo.setCurrentIndex(i)
                break
        wait_until(lambda: window8.history_list.count() >= 3,
                   timeout=10, app=qt_app)
        check("delete button starts disabled with no run selected",
              not window8.delete_run_button.isEnabled())
        image_item8 = None
        for row in range(window8.history_list.count()):
            item = window8.history_list.item(row)
            if (image_run8 is not None
                    and item.data(Qt.UserRole) == image_run8["path"]):
                image_item8 = item
                break
        check("history lists the image_generation run",
              image_item8 is not None)
        if image_item8 is not None:
            window8.history_list.setCurrentItem(image_item8)
            window8.history_list.itemClicked.emit(image_item8)
            qt_app.processEvents()
            check("delete button enables when a run is selected",
                  window8.delete_run_button.isEnabled())
            # Cancel leaves everything in place.
            question_answer8["answer"] = QMessageBox.No
            question_calls8.clear()
            window8.delete_run_button.click()
            qt_app.processEvents()
            check("delete asks to confirm",
                  len(question_calls8) == 1, str(len(question_calls8)))
            check("cancelling the delete leaves the run file in place",
                  os.path.exists(image_run8["path"]))
            check("cancelling the delete leaves the media folder in place",
                  media_dir8 is not None and os.path.isdir(media_dir8))
            # Accept: the file and the media folder go, the profile
            # rebuilds from the runs that are left.
            profile_path8 = os.path.join(
                RESULTS_DIR, provider_slug8, "PROFILE.md")
            profile_before8 = None
            if os.path.exists(profile_path8):
                with open(profile_path8, encoding="utf-8") as handle:
                    profile_before8 = handle.read()
            question_answer8["answer"] = QMessageBox.Yes
            window8.delete_run_button.click()
            qt_app.processEvents()
            check("deleting removes the run file",
                  not os.path.exists(image_run8["path"]))
            check("deleting removes the media folder",
                  media_dir8 is None or not os.path.exists(media_dir8))
            check("deleting leaves the other runs alone",
                  audio_run8 is not None
                  and os.path.exists(audio_run8["path"])
                  and audio_dir8 is not None
                  and os.path.isdir(audio_dir8))
            check("deleting leaves other models' runs alone",
                  len(app.list_history_runs(RESULTS_DIR, "fake-smart_2b"))
                  >= 2)
            if os.path.exists(profile_path8):
                with open(profile_path8, encoding="utf-8") as handle:
                    profile_after8 = handle.read()
                check("profile regenerates after deletion (image row gone)",
                      "Image Generation" not in profile_after8
                      and profile_before8 is not None
                      and "Image Generation" in profile_before8,
                      profile_after8)
                check("profile regenerates after deletion (run count drops)",
                      "Runs: 2" in profile_after8,
                      profile_after8[:300])
            remaining_paths8 = [
                window8.history_list.item(r).data(Qt.UserRole)
                for r in range(window8.history_list.count())
            ]
            check("history list drops the deleted run",
                  image_run8["path"] not in remaining_paths8,
                  str(remaining_paths8))
            check("store size drops after deletion",
                  app.store_size(RESULTS_DIR) < expected_size8,
                  f"{app.store_size(RESULTS_DIR)} vs {expected_size8}")
            check("store size label updates after deletion",
                  window8.store_size_label.text()
                  == "Results use "
                  + app.fmt_store_size(app.store_size(RESULTS_DIR)),
                  window8.store_size_label.text())
        # Delete is disabled while a run is in progress.
        server8slow, _t = fake_ollama.start_server()
        server8slow.slow = True
        host8s, port8s = server8slow.server_address
        os.environ["OLLAMA_HOST"] = f"http://{host8s}:{port8s}"
        window8.refresh_models()
        wait_until(lambda: window8.model_list.count() == 7, timeout=10,
                   app=qt_app)
        set_checked_list(window8.model_list, {"fake-smart:2b"})
        set_checked_templates(window8, {"speed"})
        window8.run_button.click()
        wait_until(lambda: window8.worker is not None, timeout=10,
                   app=qt_app)
        time.sleep(0.5)
        window8.tabs.setCurrentIndex(3)
        qt_app.processEvents()
        check("delete is disabled while a run is in progress",
              not window8.delete_run_button.isEnabled())
        window8.cancel_run()
        wait_until(lambda: window8.worker is None, timeout=60, app=qt_app)
        server8slow.shutdown()
        server8slow.server_close()
        os.environ["OLLAMA_HOST"] = f"http://{host8}:{port8}"
        window8.tabs.setCurrentIndex(3)
        qt_app.processEvents()
        if window8.history_list.count():
            window8.history_list.setCurrentRow(0)
            qt_app.processEvents()
            check("delete re-enables after the run finishes",
                  window8.delete_run_button.isEnabled())

        # ---------- v8 T11: export CSV/HTML + compare ---------------------
        set_checked_list(window8.model_list,
                         {"fake-smart:2b", "fake-partial:3b"})
        set_checked_templates(window8, {"math", "coding"})
        question_calls8.clear()
        window8.run_button.click()
        done8e = wait_until(
            lambda: window8.worker is None
            and window8.save_button.isEnabled(),
            timeout=120, app=qt_app)
        check("export run completes", done8e)
        check("export buttons enable after a run",
              window8.export_csv_button.isEnabled()
              and window8.export_html_button.isEnabled())
        csv_path8 = os.path.join(SCRATCH, "export-test.csv")
        dialog_paths8["path"] = csv_path8
        window8.export_csv_button.click()
        qt_app.processEvents()
        check("export CSV writes a file", os.path.exists(csv_path8))
        if os.path.exists(csv_path8):
            with open(csv_path8, encoding="utf-8") as handle:
                csv_text8 = handle.read()
            check("export CSV carries the models and scores",
                  "fake-smart:2b" in csv_text8 and "100%" in csv_text8
                  and "fake-partial:3b" in csv_text8
                  and "60%" in csv_text8,
                  csv_text8)
            check("export CSV has the template columns",
                  "Math" in csv_text8 and "Coding" in csv_text8, csv_text8)
            check("export CSV carries no API key",
                  fake_ollama.PROVIDER_API_KEY not in csv_text8)
            rows8 = list(csv.reader(io.StringIO(csv_text8)))
            check("export CSV parses with the header row",
                  bool(rows8)
                  and rows8[0][:4] == ["Model", "Overall score",
                                       "Avg decode tok/s", "Status"]
                  and len(rows8) == 3, str(rows8))
        html_path8 = os.path.join(SCRATCH, "export-test.html")
        dialog_paths8["path"] = html_path8
        window8.export_html_button.click()
        qt_app.processEvents()
        check("export HTML writes a file", os.path.exists(html_path8))
        if os.path.exists(html_path8):
            with open(html_path8, encoding="utf-8") as handle:
                html_text8 = handle.read()
            check("export HTML carries the models and scores",
                  "fake-smart:2b" in html_text8 and "100%" in html_text8
                  and "fake-partial:3b" in html_text8
                  and "60%" in html_text8,
                  html_text8[:500])
            check("export HTML carries no API key",
                  fake_ollama.PROVIDER_API_KEY not in html_text8)
            from html.parser import HTMLParser

            class _TagParser(HTMLParser):
                def __init__(self):
                    super().__init__()
                    self.tags = []

                def handle_starttag(self, tag, attrs):
                    self.tags.append(tag)

            parser8 = _TagParser()
            parser8.feed(html_text8)
            check("export HTML parses as basic markup",
                  "table" in parser8.tags and "tr" in parser8.tags
                  and "td" in parser8.tags and "</html>" in html_text8,
                  str(parser8.tags))
            check("export HTML has one row per model plus the header",
                  html_text8.count("<tr>") == 3,
                  f"{html_text8.count('<tr>')} rows")
        window8x = app.MainWindow()
        window8x.show()
        qt_app.processEvents()
        check("export buttons start disabled on a fresh window",
              not window8x.export_csv_button.isEnabled()
              and not window8x.export_html_button.isEnabled())
        window8x.export_csv()
        check("export with no results says so plainly",
              window8x.status_label.text()
              == "Nothing to export yet. Run a test first.",
              window8x.status_label.text())
        window8x.export_html()
        check("HTML export with no results says so plainly",
              window8x.status_label.text()
              == "Nothing to export yet. Run a test first.",
              window8x.status_label.text())
        window8x.close()
        window8x.deleteLater()
        qt_app.processEvents()

        # Compare: two models side by side.
        window8.tabs.setCurrentIndex(5)
        qt_app.processEvents()
        compare_slugs8 = [
            window8.compare_model_a.itemData(i)
            for i in range(window8.compare_model_a.count())
        ]
        check("compare dropdowns list the models with runs",
              "fake-smart_2b" in compare_slugs8
              and "fake-partial_3b" in compare_slugs8,
              str(compare_slugs8))
        check("compare dropdowns start unpicked",
              window8.compare_model_a.currentData() is None
              and window8.compare_model_b.currentData() is None)
        check("compare asks to pick two models first",
              window8.compare_message.text()
              == "Pick two models to compare.",
              window8.compare_message.text())

        def pick_compare8(combo, slug):
            for i in range(combo.count()):
                if combo.itemData(i) == slug:
                    combo.setCurrentIndex(i)
                    return True
            return False

        pick_compare8(window8.compare_model_a, "fake-smart_2b")
        pick_compare8(window8.compare_model_b, "fake-partial_3b")
        qt_app.processEvents()
        check("compare shows a table for two models",
              window8.compare_message.text() == ""
              and window8.compare_table.rowCount() == 4,
              f"msg={window8.compare_message.text()!r} "
              f"rows={window8.compare_table.rowCount()}")

        def compare_cell8(label, col):
            for row in range(window8.compare_table.rowCount()):
                item = window8.compare_table.item(row, 0)
                if item is not None and item.text() == label:
                    cell = window8.compare_table.item(row, col)
                    return cell.text() if cell is not None else None
            return None

        check("compare shows smart math 100% vs partial 50%",
              compare_cell8("Math", 1) == "100%"
              and compare_cell8("Math", 2) == "50%",
              f"{compare_cell8('Math', 1)} / {compare_cell8('Math', 2)}")
        check("compare shows smart coding 100% vs partial 60%",
              compare_cell8("Coding", 1) == "100%"
              and compare_cell8("Coding", 2) == "60%",
              f"{compare_cell8('Coding', 1)} / {compare_cell8('Coding', 2)}")
        check("compare shows the overall rows",
              compare_cell8("Overall score", 1) == "100%"
              and compare_cell8("Overall score", 2) == "55%",
              f"{compare_cell8('Overall score', 1)} / "
              f"{compare_cell8('Overall score', 2)}")
        check("compare shows the speed rows",
              compare_cell8("Avg decode tok/s", 1) == "20.0"
              and compare_cell8("Avg decode tok/s", 2) == "10.0",
              f"{compare_cell8('Avg decode tok/s', 1)} / "
              f"{compare_cell8('Avg decode tok/s', 2)}")
        check("compare headers name the two models",
              window8.compare_table.horizontalHeaderItem(1).text()
              == "fake-smart:2b"
              and window8.compare_table.horizontalHeaderItem(2).text()
              == "fake-partial:3b")
        pick_compare8(window8.compare_model_b, "fake-smart_2b")
        qt_app.processEvents()
        check("picking the same model twice shows a plain message",
              window8.compare_message.text()
              == "Pick two different models to compare."
              and window8.compare_table.rowCount() == 0,
              f"{window8.compare_message.text()!r} "
              f"rows={window8.compare_table.rowCount()}")
    finally:
        QMessageBox.question = real_question8
        QFileDialog.getSaveFileName = real_save8
        window8.close()
        window8.deleteLater()
        qt_app.processEvents()
        server8.shutdown()
        server8.server_close()
        pserver8.shutdown()
        pserver8.server_close()

    # ================= v9: search/filter, dark mode, README + hygiene ====
    # The dark-mode test writes QSettings, which lives in the real user
    # config under the default HOME — so the prior values are saved
    # here and restored in the finally, leaving no state behind.
    from PySide6.QtCore import QSettings
    settings9 = QSettings("BrokeNBored", app.APP_NAME)
    prior_dark9 = settings9.value("dark_mode")
    prior_geometry9 = settings9.value("geometry")
    settings9.remove("dark_mode")
    settings9.sync()
    shutil.rmtree(RESULTS_DIR, ignore_errors=True)
    if os.path.exists(PROVIDERS_FILE):
        os.remove(PROVIDERS_FILE)
    if os.path.exists(CAPABILITIES_FILE):
        os.remove(CAPABILITIES_FILE)
    server9, _t = fake_ollama.start_server()
    host9, port9 = server9.server_address
    os.environ["OLLAMA_HOST"] = f"http://{host9}:{port9}"
    window9 = app.MainWindow()
    window9.show()
    wait_until(lambda: window9.model_list.count() == 4, timeout=10,
               app=qt_app)
    try:
        check("v9 window lists 4 models and 19 templates",
              window9.model_list.count() == 4
              and window9.template_list.count() == 19,
              f"{window9.model_list.count()} models, "
              f"{window9.template_list.count()} templates")
        check("title bar shows v20",
              window9.windowTitle() == "OLLAMA SYSTEM TEST v20",
              window9.windowTitle())
        check("__version__ is 20", app.__version__ == "20", app.__version__)

        # ---------- v9 T13: filter boxes --------------------------------
        check("model filter box has its placeholder",
              window9.model_filter.placeholderText() == "Filter models...",
              window9.model_filter.placeholderText())
        check("template filter box has its placeholder",
              window9.template_filter.placeholderText()
              == "Filter templates...",
              window9.template_filter.placeholderText())

        def visible_texts9(widget):
            return [widget.item(row).text()
                    for row in range(widget.count())
                    if not widget.item(row).isHidden()]

        window9.model_filter.setText("smart")
        qt_app.processEvents()
        check("model filter shows only matching models",
              visible_texts9(window9.model_list)
              == ["fake-smart:2b [vision, tools, long_context]"],
              str(visible_texts9(window9.model_list)))
        check("filter hides, never removes (model count unchanged)",
              window9.model_list.count() == 4,
              str(window9.model_list.count()))
        window9.model_filter.setText("vision")
        qt_app.processEvents()
        check("model filter matches the capability brackets in the text",
              visible_texts9(window9.model_list)
              == ["fake-smart:2b [vision, tools, long_context]",
                  "fake-partial:3b [vision, long_context]"],
              str(visible_texts9(window9.model_list)))
        window9.model_filter.setText("SMART")
        qt_app.processEvents()
        check("model filter is case-insensitive",
              visible_texts9(window9.model_list)
              == ["fake-smart:2b [vision, tools, long_context]"],
              str(visible_texts9(window9.model_list)))
        window9.model_filter.setText("")
        qt_app.processEvents()
        window9.template_filter.setText("math")
        qt_app.processEvents()
        check("template filter shows only matching templates",
              visible_texts9(window9.template_list) == ["Math (10 tasks)"],
              str(visible_texts9(window9.template_list)))
        check("filter hides, never removes (template count unchanged)",
              window9.template_list.count() == 19,
              str(window9.template_list.count()))
        window9.template_filter.setText("needs audio")
        qt_app.processEvents()
        check("template filter matches the full label text",
              len(visible_texts9(window9.template_list)) == 1
              and visible_texts9(window9.template_list)[0].startswith(
                  "Audio (5 tasks)"),
              str(visible_texts9(window9.template_list)))
        window9.template_filter.setText("")
        qt_app.processEvents()

        # A tick survives being filtered out of view and back.
        set_checked_list(window9.model_list, {"fake-smart:2b"})
        smart9 = next(window9.model_list.item(row)
                      for row in range(window9.model_list.count())
                      if "fake-smart" in window9.model_list.item(row).text())
        window9.model_filter.setText("dumb")
        qt_app.processEvents()
        check("a ticked model filtered out of view keeps its tick",
              smart9.isHidden()
              and smart9.checkState() == Qt.Checked,
              f"hidden={smart9.isHidden()} "
              f"checked={smart9.checkState() == Qt.Checked}")
        window9.model_filter.setText("")
        qt_app.processEvents()
        check("clearing the filter restores all items with ticks intact",
              len(visible_texts9(window9.model_list)) == 4
              and smart9.checkState() == Qt.Checked,
              str(visible_texts9(window9.model_list)))

        # Grey-outs keep working with a filter active: tick the
        # text-only model while the Vision template is filtered out
        # of view, then clear the filter and find it greyed out.
        window9.template_filter.setText("math")
        qt_app.processEvents()
        set_checked_list(window9.model_list, {"fake-dumb:1b"})
        qt_app.processEvents()
        vision9 = next(
            window9.template_list.item(row)
            for row in range(window9.template_list.count())
            if window9.template_list.item(row).data(Qt.UserRole) == "vision")
        check("grey-out applies to a filtered-out template",
              vision9.isHidden()
              and not vision9.flags() & Qt.ItemIsEnabled,
              f"hidden={vision9.isHidden()} "
              f"enabled={bool(vision9.flags() & Qt.ItemIsEnabled)}")
        window9.template_filter.setText("")
        qt_app.processEvents()
        check("greyed template stays greyed after the filter clears",
              not vision9.isHidden()
              and not vision9.flags() & Qt.ItemIsEnabled)
        window9.template_filter.setText("vision")
        qt_app.processEvents()
        check("a filtered-in ineligible template shows greyed",
              len(visible_texts9(window9.template_list)) == 1
              and visible_texts9(window9.template_list)[0].startswith(
                  "Vision (5 tasks)")
              and not vision9.flags() & Qt.ItemIsEnabled,
              str(visible_texts9(window9.template_list)))
        window9.template_filter.setText("")
        qt_app.processEvents()

        # A refresh rebuilds both lists from scratch; the filter text
        # the user already typed still applies to the new items.
        window9.model_filter.setText("smart")
        window9.template_filter.setText("math")
        qt_app.processEvents()
        window9.refresh_models()
        wait_until(lambda: window9.model_list.count() == 4, timeout=10,
                   app=qt_app)
        check("refresh keeps the model filter applied",
              visible_texts9(window9.model_list)
              == ["fake-smart:2b [vision, tools, long_context]"],
              str(visible_texts9(window9.model_list)))
        check("refresh keeps the template filter applied",
              visible_texts9(window9.template_list) == ["Math (10 tasks)"],
              str(visible_texts9(window9.template_list)))
        window9.model_filter.setText("")
        window9.template_filter.setText("")
        qt_app.processEvents()

        # ---------- v9 T14: dark mode -----------------------------------
        check("View menu has a checkable Dark mode item",
              window9.dark_mode_action is not None
              and window9.dark_mode_action.isCheckable()
              and window9.dark_mode_action.text() == "Dark mode")
        check("dark mode starts off (light is the default)",
              not window9.dark_mode_action.isChecked()
              and qt_app.styleSheet() == app.LIGHT_STYLESHEET
              and "#2b2b2b" not in qt_app.styleSheet()
              and not window9.dark_mode)
        check("dark stylesheet covers lists, tables, text, tabs, "
              "buttons, combos, menus, progress",
              all(token in app.DARK_STYLESHEET for token in
                  ("QListWidget", "QTableWidget", "QTextEdit", "QTabBar",
                   "QPushButton", "QComboBox", "QMenuBar",
                   "QProgressBar", "QScrollBar")),
              f"{len(app.DARK_STYLESHEET)} chars")
        window9.dark_mode_action.trigger()
        qt_app.processEvents()
        check("toggling dark mode sets a dark application stylesheet",
              qt_app.styleSheet() != ""
              and "#1e1e1e" in qt_app.styleSheet()
              and "#2b2b2b" in qt_app.styleSheet(),
              qt_app.styleSheet()[:80])
        check("dark mode action shows checked",
              window9.dark_mode_action.isChecked())
        check("dark mode choice is saved to QSettings",
              QSettings("BrokeNBored", app.APP_NAME).value(
                  "dark_mode", type=bool) is True)
        window9.grab().save(os.path.join(VERIFY_DIR, "screenshot-dark.png"))
        # Restart: a new window with the same settings opens dark.
        window9.close()
        window9.deleteLater()
        qt_app.processEvents()
        window9b = app.MainWindow()
        window9b.show()
        qt_app.processEvents()
        check("a new window opens dark (choice survived a restart)",
              qt_app.styleSheet() != ""
              and "#1e1e1e" in qt_app.styleSheet()
              and window9b.dark_mode_action.isChecked()
              and window9b.dark_mode)
        window9b.dark_mode_action.trigger()
        qt_app.processEvents()
        check("toggling back restores the light theme",
              qt_app.styleSheet() == app.LIGHT_STYLESHEET
              and "#2b2b2b" not in qt_app.styleSheet()
              and not window9b.dark_mode_action.isChecked()
              and not window9b.dark_mode)
        check("light choice is saved to QSettings",
              QSettings("BrokeNBored", app.APP_NAME).value(
                  "dark_mode", type=bool) is False)
        window9b.close()
        window9b.deleteLater()
        qt_app.processEvents()

        # ---------- v9 T15: README fixes + repo hygiene -----------------
        readme9 = open(os.path.join(PKG_DIR, "README.md"),
                       encoding="utf-8").read()
        check("README states 108 prompts",
              "108 prompts" in readme9)
        check("README no longer says roughly 80",
              "roughly 80" not in readme9)
        check("README shows the window screenshot",
              "screenshot.png" in readme9)
        shot9 = os.path.join(PKG_DIR, "screenshot.png")
        check("screenshot.png exists in the repo root",
              os.path.exists(shot9))
        if os.path.exists(shot9):
            with open(shot9, "rb") as handle:
                magic9 = handle.read(8)
            check("screenshot.png is a real PNG",
                  magic9 == b"\x89PNG\r\n\x1a\n", repr(magic9))
            check("screenshot.png is a non-trivial window grab",
                  os.path.getsize(shot9) > 10000,
                  f"{os.path.getsize(shot9)} bytes")
            from PySide6.QtGui import QPixmap
            pix9 = QPixmap(shot9)
            check("screenshot.png is 980x720, the window's size",
                  pix9.width() == 980 and pix9.height() == 720,
                  f"{pix9.width()}x{pix9.height()}")
        # The app creates images/ and audio/ on startup: point the
        # media dirs at scratch folders that do not exist, open a
        # window, and find them created.
        media9 = os.path.join(VERIFY_DIR, "_v9_media")
        shutil.rmtree(media9, ignore_errors=True)
        images9 = os.path.join(media9, "images")
        audio9 = os.path.join(media9, "audio")
        os.environ["OST_IMAGES_DIR"] = images9
        os.environ["OST_AUDIO_DIR"] = audio9
        try:
            window9c = app.MainWindow()
            window9c.show()
            qt_app.processEvents()
            check("launch creates the images folder",
                  os.path.isdir(images9))
            check("launch creates the audio folder",
                  os.path.isdir(audio9))
            window9c.close()
            window9c.deleteLater()
            qt_app.processEvents()
        finally:
            del os.environ["OST_IMAGES_DIR"]
            del os.environ["OST_AUDIO_DIR"]
            shutil.rmtree(media9, ignore_errors=True)
    finally:
        if prior_dark9 is None:
            settings9.remove("dark_mode")
        else:
            settings9.setValue("dark_mode", prior_dark9)
        if prior_geometry9 is None:
            settings9.remove("geometry")
        else:
            settings9.setValue("geometry", prior_geometry9)
        settings9.sync()
        qt_app.setStyleSheet("")
        server9.shutdown()
        server9.server_close()

    # ---------- v11: debug log layer (R2) ----------
    # A saved key that matches NO pattern, so only the exact-match
    # scrub can catch it.
    fake_key11 = "my-plain-test-key-987654321"
    with open(PROVIDERS_FILE, "w", encoding="utf-8") as handle:
        json.dump({"providers": [{
            "name": "scrub-test", "base_url": "http://127.0.0.1:9",
            "api_key": fake_key11, "models": [],
        }]}, handle)
    check("scrub removes a saved key",
          fake_key11 not in app.scrub_text(f"key is {fake_key11} here")
          and "[key]" in app.scrub_text(f"key is {fake_key11} here"))
    check("scrub removes an sk- key",
          "sk-abcdefghij12345678" not in app.scrub_text(
              "leaked sk-abcdefghij12345678 here"))
    check("scrub removes a token assignment",
          "supersecret42" not in app.scrub_text("token=supersecret42"))
    check("scrub removes an api_key assignment",
          "hunter2value" not in app.scrub_text("api_key: hunter2value"))
    check("scrub removes a Bearer token",
          "abcdef123456" not in app.scrub_text("Bearer abcdef123456"))
    shutil.rmtree(LOGS_DIR, ignore_errors=True)
    app._LOG_RING.clear()
    app.log_event("v11 test event")
    today11 = datetime.now().strftime("%Y-%m-%d")
    log_file11 = os.path.join(LOGS_DIR, f"ost-{today11}.log")
    check("log_event writes to the ring buffer",
          any("v11 test event" in line for line in app._LOG_RING))
    check("log_event writes to today's log file",
          os.path.exists(log_file11)
          and "v11 test event" in open(log_file11, encoding="utf-8").read())
    for i in range(250):
        app.log_event(f"ring fill {i}")
    check("ring buffer caps at 200 lines", len(app._LOG_RING) == 200,
          str(len(app._LOG_RING)))
    old_log11 = os.path.join(LOGS_DIR, "ost-2020-01-01.log")
    with open(old_log11, "w", encoding="utf-8") as handle:
        handle.write("old\n")
    app.prune_old_logs()
    check("prune removes a log older than 30 days",
          not os.path.exists(old_log11))
    check("prune keeps today's log", os.path.exists(log_file11))
    app.log_event(f"a line carrying the key {fake_key11}")
    report11 = app.write_debug_report()
    with open(report11, encoding="utf-8") as handle:
        report_text11 = handle.read()
    check("debug report is written under logs/",
          os.path.exists(report11) and report11.startswith(LOGS_DIR))
    check("debug report names the version",
          f"OLLAMA SYSTEM TEST v{app.__version__}" in report_text11,
          report_text11[:200])
    check("debug report carries no saved key",
          fake_key11 not in report_text11
          and "[key]" in report_text11)
    try:
        raise ValueError("boom-test-11")
    except ValueError:
        _exc_type11, _exc_value11, _exc_tb11 = sys.exc_info()
    crash_before11 = {f for f in os.listdir(LOGS_DIR)
                      if f.startswith("crash-")}
    app._ost_excepthook(_exc_type11, _exc_value11, _exc_tb11)
    crash_after11 = {f for f in os.listdir(LOGS_DIR)
                     if f.startswith("crash-")}
    new_crash11 = crash_after11 - crash_before11
    check("excepthook writes a crash file", len(new_crash11) == 1,
          str(new_crash11))
    if new_crash11:
        crash_text11 = open(
            os.path.join(LOGS_DIR, sorted(new_crash11)[0]),
            encoding="utf-8").read()
        check("crash file holds the traceback",
              "ValueError" in crash_text11 and "boom-test-11" in crash_text11,
              crash_text11[:300])
    check("excepthook logs the crash to the ring",
          any("Crash:" in line for line in app._LOG_RING))
    saved_hook11 = sys.excepthook
    app.install_excepthook()
    check("install_excepthook installs the hook",
          sys.excepthook is app._ost_excepthook)
    sys.excepthook = saved_hook11

    # ---------- v11: window checks (R4, R5, R8, R9, R10) ----------
    server11, _t11 = fake_ollama.start_server()
    host11, port11 = server11.server_address
    os.environ["OLLAMA_HOST"] = f"http://{host11}:{port11}"
    window11 = app.MainWindow()
    window11.show()
    qt_app.processEvents()
    help_texts11 = []
    for action in window11.menuBar().actions():
        if action.text() == "Help":
            help_texts11 = [a.text() for a in action.menu().actions()]
    check("Help menu has a Debug report action",
          "Debug report" in help_texts11, str(help_texts11))
    check("compare picker placeholder is plain words",
          window11.compare_model_a.itemText(0) == "Pick a model",
          window11.compare_model_a.itemText(0))
    window11.refresh_models()
    wait_until(lambda: window11.model_list.count() == 4, timeout=10,
               app=qt_app)
    set_checked_list(window11.model_list, {"fake-smart:2b"})
    set_checked_templates(window11, {"math"})
    window11.start_run()
    check("pick lists lock during a run",
          not window11.model_list.isEnabled()
          and not window11.template_list.isEnabled())
    done11 = wait_until(lambda: window11.worker is None, timeout=120,
                        app=qt_app)
    check("v11 run completes", done11)
    check("pick lists unlock when the run ends",
          window11.model_list.isEnabled()
          and window11.template_list.isEnabled())
    reports_before11 = (
        {f for f in os.listdir(RESULTS_DIR)
         if f.startswith("bench-results-")}
        if os.path.exists(RESULTS_DIR) else set()
    )
    window11.save_report()
    window11.save_report()
    qt_app.processEvents()
    reports_after11 = (
        {f for f in os.listdir(RESULTS_DIR)
         if f.startswith("bench-results-")}
        if os.path.exists(RESULTS_DIR) else set()
    )
    new_reports11 = reports_after11 - reports_before11
    check("two saves in one minute produce two reports",
          len(new_reports11) == 4, str(new_reports11))
    md_dir11 = os.path.join(RESULTS_DIR, "fake-markdown_1b")
    os.makedirs(md_dir11, exist_ok=True)
    with open(os.path.join(md_dir11, "PROFILE.md"), "w",
              encoding="utf-8") as handle:
        handle.write("# Fake Markdown\n\n**Bold** and plain.\n\n- one\n- two\n")
    window11.refresh_profiles()
    md_index11 = None
    for i in range(window11.profile_model_combo.count()):
        if window11.profile_model_combo.itemData(i) == "fake-markdown_1b":
            md_index11 = i
    check("profiles lists the markdown test model", md_index11 is not None)
    if md_index11 is not None:
        window11.profile_model_combo.setCurrentIndex(md_index11)
        window11.on_profile_model_changed()
        qt_app.processEvents()
        md_text11 = window11.profile_view.toPlainText()
        check("profile renders markdown, not raw text",
              "Fake Markdown" in md_text11
              and "#" not in md_text11 and "**" not in md_text11,
              md_text11[:200])
        check("profile view renders a heading",
              "<h1" in window11.profile_view.toHtml(),
              window11.profile_view.toHtml()[:300])
    with open(PROVIDERS_FILE, "w", encoding="utf-8") as handle:
        json.dump({"providers": [{
            "name": "del-me", "base_url": "http://127.0.0.1:9",
            "api_key": "test-key-not-real", "models": [],
        }]}, handle)
    window11.refresh_providers()
    del_index11 = None
    for i in range(window11.provider_list.count()):
        if window11.provider_list.item(i).text() == "del-me":
            del_index11 = i
    check("providers lists the removable test provider",
          del_index11 is not None)
    if del_index11 is not None:
        window11.provider_list.setCurrentRow(del_index11)
        question_calls11 = []
        real_question11 = QMessageBox.question

        def _mock_question11(parent, title, text, buttons=None, default=None):
            question_calls11.append((title, default))
            return QMessageBox.No

        QMessageBox.question = _mock_question11
        try:
            window11.remove_provider()
        finally:
            QMessageBox.question = real_question11
        kept11, _warns11 = app.load_providers(PROVIDERS_FILE)
        check("answering No keeps the provider",
              any(p["name"] == "del-me" for p in kept11))
        check("remove confirm defaults to No",
              bool(question_calls11)
              and question_calls11[0][1] == QMessageBox.No,
              str(question_calls11))

        def _mock_yes11(parent, title, text, buttons=None, default=None):
            return QMessageBox.Yes

        QMessageBox.question = _mock_yes11
        try:
            window11.remove_provider()
        finally:
            QMessageBox.question = real_question11
        gone11, _warns11 = app.load_providers(PROVIDERS_FILE)
        check("answering Yes removes the provider",
              not any(p["name"] == "del-me" for p in gone11))
    window11.close()
    window11.deleteLater()
    qt_app.processEvents()
    server11.shutdown()
    server11.server_close()
    if os.path.exists(PROVIDERS_FILE):
        os.remove(PROVIDERS_FILE)

    # ---------- v11: em-dash sweep (R10) ----------
    app_src11 = open(os.path.join(PKG_DIR, "ollama_system_test.py"),
                     encoding="utf-8").read()
    tree11 = ast.parse(app_src11)
    docstring_ids11 = set()
    for node in ast.walk(tree11):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            if (node.body and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    and isinstance(node.body[0].value.value, str)):
                docstring_ids11.add(id(node.body[0].value))
    dash_strings11 = []
    amp_strings11 = []
    for node in ast.walk(tree11):
        if (isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstring_ids11):
            if "—" in node.value:
                dash_strings11.append((node.lineno, node.value[:60]))
            if " & " in node.value:
                amp_strings11.append((node.lineno, node.value[:60]))
    check("no em-dash in any non-docstring string",
          not dash_strings11, str(dash_strings11[:3]))
    check("no bare & in any non-docstring string",
          not amp_strings11, str(amp_strings11[:3]))
    check("UNREACHABLE_HINT has no em-dash",
          "—" not in app.UNREACHABLE_HINT, app.UNREACHABLE_HINT)
    check("fmt_duration None is n/a", app.fmt_duration(None) == "n/a")
    check("fmt_tokens None is n/a", app.fmt_tokens(None) == "n/a")

    # ---------- v12: tab split (R1), How to Use (R3),
    # single instance (R6), setup checklist (R7) ----------
    # The checklist keys live in the real QSettings under the default
    # HOME — so the prior values are saved here and restored in the
    # finally, leaving no state behind. The results dir is a fresh
    # scratch dir, so the checklist starts with no run done.
    from PySide6.QtCore import QSettings, Qt
    from PySide6.QtWidgets import QTextEdit
    settings12 = QSettings("BrokeNBored", app.APP_NAME)
    prior_dismissed12 = settings12.value("setup_dismissed")
    prior_completed12 = settings12.value("setup_completed")
    settings12.remove("setup_dismissed")
    settings12.remove("setup_completed")
    settings12.sync()
    prior_results12 = os.environ.get("OST_RESULTS_DIR")
    results12 = os.path.join(SCRATCH, "results12")
    os.makedirs(results12, exist_ok=True)
    os.environ["OST_RESULTS_DIR"] = results12
    server12, _t12 = fake_ollama.start_server()
    host12, port12 = server12.server_address
    ollama_url12 = f"http://{host12}:{port12}"
    os.environ["OLLAMA_HOST"] = ollama_url12
    window12 = app.MainWindow()
    window12.show()
    wait_until(lambda: window12.model_list.count() == 4, timeout=10,
               app=qt_app)
    try:
        # R7 first: the checklist needs a window with no run yet,
        # and the R1 run below would complete it.
        check("checklist shows on a fresh window",
              window12.checklist_frame.isVisible())
        check("checklist step 1 ticked when Ollama is running",
              window12.checklist_step1.text() == "✓ Ollama is running.",
              window12.checklist_step1.text())
        check("checklist step 2 ticked with a model pulled",
              window12.checklist_step2.text()
              == "✓ At least one model is pulled.",
              window12.checklist_step2.text())
        check("checklist step 3 open with no run yet",
              window12.checklist_step3.text().startswith(
                  "○ Run your first test"),
              window12.checklist_step3.text())
        window12.checklist_dismiss.click()
        qt_app.processEvents()
        check("dismissing the checklist hides it for good",
              not window12.checklist_frame.isVisible()
              and settings12.value("setup_dismissed", False, type=bool))
        window12.refresh_checklist()
        qt_app.processEvents()
        check("dismissed checklist stays hidden on refresh",
              not window12.checklist_frame.isVisible())
        settings12.remove("setup_dismissed")
        settings12.sync()
        window12.refresh_checklist()
        qt_app.processEvents()
        check("checklist shows again once dismissal is cleared",
              window12.checklist_frame.isVisible())
        os.environ["OLLAMA_HOST"] = "http://127.0.0.1:1"
        window12.refresh_models()
        qt_app.processEvents()
        check("checklist steps open when Ollama is down",
              # v17: the app starts Ollama itself — the step says so.
              window12.checklist_step1.text().startswith(
                  "○ Ollama is not running yet")
              and window12.checklist_step2.text().startswith(
                  "○ Pull a model"),
              window12.checklist_step1.text() + " / "
              + window12.checklist_step2.text())
        os.environ["OLLAMA_HOST"] = ollama_url12
        window12.refresh_models()
        wait_until(lambda: window12.model_list.count() == 4, timeout=10,
                   app=qt_app)
        check("checklist steps tick again when Ollama returns",
              window12.checklist_step1.text() == "✓ Ollama is running."
              and window12.checklist_step2.text()
              == "✓ At least one model is pulled.")
        with open(os.path.join(results12, "fake-run.json"), "w",
                  encoding="utf-8") as handle:
            handle.write("{}")
        window12.refresh_checklist()
        qt_app.processEvents()
        check("checklist hides once all three steps are done",
              not window12.checklist_frame.isVisible()
              and settings12.value("setup_completed", False, type=bool))
        os.remove(os.path.join(results12, "fake-run.json"))
        os.environ["OLLAMA_HOST"] = "http://127.0.0.1:1"
        window12.refresh_models()
        qt_app.processEvents()
        check("completed checklist never reappears",
              not window12.checklist_frame.isVisible())
        os.environ["OLLAMA_HOST"] = ollama_url12
        window12.refresh_models()
        wait_until(lambda: window12.model_list.count() == 4, timeout=10,
                   app=qt_app)

        # R1: one job per tab.
        setup_tab12 = window12.tabs.widget(window12.TAB_SETUP)
        run_tab12 = window12.tabs.widget(window12.TAB_RUN)
        models_tab12 = window12.tabs.widget(window12.TAB_MODELS)
        check("setup tab holds the pick lists, Run and the budget",
              window12.model_list.parentWidget() is setup_tab12
              and window12.template_list.parentWidget() is setup_tab12
              and window12.run_button.parentWidget() is setup_tab12
              and window12.budget_spin.parentWidget() is setup_tab12)
        check("run tab holds the table, progress and result buttons",
              window12.table.parentWidget() is run_tab12
              and window12.progress_bar.parentWidget() is run_tab12
              and window12.cancel_button.parentWidget() is run_tab12
              and window12.save_button.parentWidget() is run_tab12
              and window12.export_csv_button.parentWidget() is run_tab12
              and window12.export_html_button.parentWidget() is run_tab12)
        check("models tab holds pull, the installed list and Refresh",
              window12.pull_input.parentWidget() is models_tab12
              and window12.pull_button.parentWidget() is models_tab12
              and window12.pull_bar.parentWidget() is models_tab12
              and window12.installed_list.parentWidget() is models_tab12
              and window12.refresh_button.parentWidget() is models_tab12
              and window12.start_ollama_button.parentWidget()
              is models_tab12)
        check("status label lives under the tabs, not inside one",
              window12.status_label.parent() is window12.centralWidget()
              and window12.status_label.parent() is not window12.tabs)
        status_visible12 = True
        for tab_index12 in range(window12.tabs.count()):
            window12.tabs.setCurrentIndex(tab_index12)
            qt_app.processEvents()
            status_visible12 = (
                status_visible12 and window12.status_label.isVisible()
            )
        check("status label stays visible on every tab",
              status_visible12)
        window12.tabs.setCurrentIndex(0)
        qt_app.processEvents()
        check("pick lists show their full height, no 180px cap",
              window12.model_list.maximumHeight() >= 10000
              and window12.template_list.maximumHeight() >= 10000,
              f"{window12.model_list.maximumHeight()}, "
              f"{window12.template_list.maximumHeight()}")
        installed12 = [window12.installed_list.item(r).text()
                       for r in range(window12.installed_list.count())]
        check("models tab lists the installed Ollama models",
              len(installed12) == 4
              and "fake-smart:2b [vision, tools, long_context]"
              in installed12,
              str(installed12))
        check("installed list is read-only, no checkboxes",
              all(not (window12.installed_list.item(r).flags()
                       & Qt.ItemIsUserCheckable)
                  for r in range(window12.installed_list.count())))

        # R3: the How to Use index.
        help_texts12 = []
        for action in window12.menuBar().actions():
            if action.text() == "Help":
                help_texts12 = [a.text() for a in action.menu().actions()]
        check("Help menu has a How to Use action",
              "How to Use" in help_texts12, str(help_texts12))
        how_sections12 = ["First time", "Picking models and templates",
                          "Running a test", "Reading the results",
                          "Past runs", "Pulling a model", "Providers",
                          "Saving and sharing", "If something goes wrong",
                          "Shortcuts"]
        check("How to Use covers every part of the app",
              all(s in app.HOW_TO_USE_TEXT for s in how_sections12))
        check("How to Use text has no em-dash or bare &",
              "—" not in app.HOW_TO_USE_TEXT
              and " & " not in app.HOW_TO_USE_TEXT)
        window12.show_how_to_use()
        qt_app.processEvents()
        dialog12 = window12._how_to_use_dialog
        check("How to Use opens its own window",
              dialog12 is not None and dialog12.isVisible()
              and dialog12.windowTitle() == "How to Use")
        dialog_text12 = ""
        if dialog12 is not None:
            for child in dialog12.findChildren(QTextEdit):
                dialog_text12 += child.toPlainText()
        check("How to Use window shows the manual",
              "First time" in dialog_text12
              and "If something goes wrong" in dialog_text12,
              dialog_text12[:120])
        window12.show_how_to_use()
        qt_app.processEvents()
        check("How to Use opens once, not a window per click",
              window12._how_to_use_dialog is dialog12)
        if dialog12 is not None:
            dialog12.close()
            qt_app.processEvents()

        # R1: a run switches to the Run tab.
        window12.tabs.setCurrentIndex(0)
        set_checked_list(window12.model_list, {"fake-smart:2b"})
        set_checked_templates(window12, {"math"})
        window12.start_run()
        check("starting a run switches to the Run tab",
              window12.tabs.currentIndex() == window12.TAB_RUN,
              str(window12.tabs.currentIndex()))
        done12 = wait_until(lambda: window12.worker is None, timeout=120,
                            app=qt_app)
        check("v12 run completes", done12)
        check("results stay on the Run tab after the run",
              window12.tabs.currentIndex() == window12.TAB_RUN)

        # R6: single instance.
        server_in12, first_in12 = app.acquire_single_instance(
            "ost-v12-inproc-probe")
        check("first acquire takes the name",
              first_in12 and server_in12 is not None)
        server_in2_12, first_in2_12 = app.acquire_single_instance(
            "ost-v12-inproc-probe")
        check("second acquire is refused while the first holds it",
              not first_in2_12 and server_in2_12 is None)
        if server_in12 is not None:
            server_in12.close()
        server_in3_12, first_in3_12 = app.acquire_single_instance(
            "ost-v12-inproc-probe")
        check("the name frees up when the first copy closes",
              first_in3_12 and server_in3_12 is not None)
        if server_in3_12 is not None:
            server_in3_12.close()

        # A second PROCESS holding the name refuses this one.
        probe12 = os.path.join(SCRATCH, "single-instance-probe.py")
        with open(probe12, "w", encoding="utf-8") as handle:
            handle.write(
                "import sys, time\n"
                "from PySide6.QtCore import QCoreApplication\n"
                "from PySide6.QtNetwork import QLocalServer\n"
                "app = QCoreApplication(sys.argv)\n"
                "QLocalServer.removeServer('ost-v12-subprocess-probe')\n"
                "server = QLocalServer()\n"
                "assert server.listen('ost-v12-subprocess-probe')\n"
                "print('ready', flush=True)\n"
                "time.sleep(20)\n"
            )
        proc12 = subprocess.Popen(
            [sys.executable, "-u", probe12],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        ready12 = False
        try:
            import select
            readable12, _w12, _x12 = select.select(
                [proc12.stdout], [], [], 30)
            if readable12:
                ready12 = proc12.stdout.readline().strip() == "ready"
        except Exception:
            ready12 = False
        check("subprocess probe holds the single-instance name", ready12)
        if ready12:
            _srv_sub12, first_sub12 = app.acquire_single_instance(
                "ost-v12-subprocess-probe")
            check("acquire is refused while another process holds it",
                  not first_sub12)
        else:
            check("acquire is refused while another process holds it",
                  False, "probe never became ready")
        proc12.kill()
        proc12.wait(timeout=10)
        _srv_sub2_12, first_sub2_12 = app.acquire_single_instance(
            "ost-v12-subprocess-probe")
        check("the name frees up when the other process exits",
              first_sub2_12)
        if _srv_sub2_12 is not None:
            _srv_sub2_12.close()

        # A knock on the window's server reaches the window.
        server_win12, first_win12 = app.acquire_single_instance(
            "ost-v12-window-probe")
        check("window probe server listens", first_win12)
        if server_win12 is not None:
            window12b = app.MainWindow(instance_server=server_win12)
            window12b.show()
            qt_app.processEvents()
            knocked12 = []
            server_win12.newConnection.connect(
                lambda: knocked12.append(True))
            _srv_knock12, first_knock12 = app.acquire_single_instance(
                "ost-v12-window-probe")
            check("a second copy is refused while the window holds it",
                  not first_knock12)
            knocked12_done = wait_until(lambda: bool(knocked12),
                                        timeout=10, app=qt_app)
            check("the knock reaches the running window",
                  knocked12_done)
            window12b.close()
            window12b.deleteLater()
            qt_app.processEvents()
            server_win12.close()
    finally:
        window12.close()
        window12.deleteLater()
        qt_app.processEvents()
        server12.shutdown()
        server12.server_close()
        if prior_dismissed12 is None:
            settings12.remove("setup_dismissed")
        else:
            settings12.setValue("setup_dismissed", prior_dismissed12)
        if prior_completed12 is None:
            settings12.remove("setup_completed")
        else:
            settings12.setValue("setup_completed", prior_completed12)
        settings12.sync()
        if prior_results12 is None:
            os.environ.pop("OST_RESULTS_DIR", None)
        else:
            os.environ["OST_RESULTS_DIR"] = prior_results12

    # ---------- v13: run preview (R11), honest progress (R12),
    # calm status + finish moment (R13), answers + TTFT (R15) ----------
    # Unit checks first (no window needed), then a real run through
    # a fresh window on the fake server. The checklist keys live in
    # the real QSettings, so the prior values are saved here and
    # restored in the finally, leaving no state behind.
    check("fmt_eta seconds", app.fmt_eta(45) == "45s", app.fmt_eta(45))
    check("fmt_eta minutes", app.fmt_eta(180) == "3m", app.fmt_eta(180))
    check("fmt_eta hours and minutes",
          app.fmt_eta(3900) == "1h 5m", app.fmt_eta(3900))
    check("fmt_eta whole hours", app.fmt_eta(3600) == "1h",
          app.fmt_eta(3600))
    check("fmt_eta None is n/a", app.fmt_eta(None) == "n/a")
    check("fmt_eta rounds to whole seconds",
          app.fmt_eta(59.6) == "1m", app.fmt_eta(59.6))

    by_id13 = {t["id"]: t for t in templates}
    tgt13 = app.ollama_target("fake-smart:2b", ["text"])
    check("step count is warmup + tasks",
          app.count_run_steps([tgt13], [by_id13["coding"]]) == 6,
          str(app.count_run_steps([tgt13], [by_id13["coding"]])))
    check("step count covers the Speed context lengths",
          app.count_run_steps([tgt13], [by_id13["speed"]])
          == 1 + len(by_id13["speed"]["context_lengths"]),
          str(app.count_run_steps([tgt13], [by_id13["speed"]])))
    check("step count counts multi-turn turns",
          app.count_run_steps([tgt13], [by_id13["multi_turn"]])
          == 1 + sum(len(t["turns"])
                     for t in by_id13["multi_turn"]["tasks"]),
          str(app.count_run_steps([tgt13], [by_id13["multi_turn"]])))
    check("step count skips templates the model cannot run",
          app.count_run_steps([tgt13], [by_id13["vision"]]) == 1,
          str(app.count_run_steps([tgt13], [by_id13["vision"]])))
    check("step count is at least 1 with nothing to run",
          app.count_run_steps([], []) == 1,
          str(app.count_run_steps([], [])))

    empty13 = os.path.join(SCRATCH, "results13-empty")
    os.makedirs(empty13, exist_ok=True)
    check("no past runs means no measured speed",
          app.latest_decode_speed(empty13, "fake-smart:2b") is None)
    pv13 = app.build_run_preview([tgt13], [by_id13["coding"]], empty13)
    check("preview names the model and template counts",
          "1 model, 1 template, 6 steps in total." in pv13["text"],
          pv13["text"])
    check("preview says the time is an upper estimate",
          "at most about" in pv13["text"]
          and "upper estimate" in pv13["text"], pv13["text"])
    check("preview says local runs cost nothing",
          "nothing to pay" in pv13["text"], pv13["text"])
    check("preview total steps matches the counter",
          pv13["total_steps"] == 6, str(pv13["total_steps"]))
    check("preview cost is None for a local-only run",
          pv13["cost"] is None)
    # A past run at 100 tok/s makes the same preview much shorter
    # than the 8 tok/s default for a model that never ran here.
    past13 = os.path.join(SCRATCH, "results13-past")
    past_dir13 = os.path.join(past13, "fake-smart_2b", "sys1")
    os.makedirs(past_dir13, exist_ok=True)
    with open(os.path.join(
            past_dir13, "2026-11-14T00-00-00-coding.json"),
            "w", encoding="utf-8") as handle:
        json.dump({"timestamp": "2026-11-14T00:00:00",
                   "template": "coding", "template_name": "Coding",
                   "template_score": 100.0,
                   "avg_decode_tok_s": 100.0}, handle)
    check("measured speed comes from the newest past run",
          app.latest_decode_speed(past13, "fake-smart:2b") == 100.0,
          str(app.latest_decode_speed(past13, "fake-smart:2b")))
    pv13_fast = app.build_run_preview(
        [tgt13], [by_id13["coding"]], past13)
    check("a fast past run shortens the time estimate",
          pv13_fast["seconds_upper"] < pv13["seconds_upper"],
          f"{pv13_fast['seconds_upper']:.1f} vs "
          f"{pv13['seconds_upper']:.1f}")
    ptgt13 = {"label": "prov / m", "kind": "provider", "model": "m",
              "provider_name": "prov", "provider": {"name": "prov"},
              "capabilities": ["text"],
              "price_input_per_1m": 2.0, "price_output_per_1m": 8.0}
    pv13p = app.build_run_preview([ptgt13], [by_id13["math"]], empty13)
    check("provider preview names the model and its cost",
          "prov / m" in pv13p["text"] and "about $" in pv13p["text"],
          pv13p["text"])
    check("provider preview total is a number",
          pv13p["cost"] is not None
          and pv13p["cost"]["total"] is not None)
    ptgt13u = dict(ptgt13, label="prov / u",
                   price_input_per_1m=None, price_output_per_1m=None)
    pv13u = app.build_run_preview([ptgt13u], [by_id13["math"]], empty13)
    check("unknown-price preview says cost unknown plainly",
          "no prices saved: cost unknown" in pv13u["text"], pv13u["text"])
    check("unknown-price preview total is None",
          pv13u["cost"]["total"] is None)

    from PySide6.QtCore import QSettings as QSettings13
    from PySide6.QtWidgets import QDialog
    settings13 = QSettings13("BrokeNBored", app.APP_NAME)
    prior_dismissed13 = settings13.value("setup_dismissed")
    prior_completed13 = settings13.value("setup_completed")
    prior_results13 = os.environ.get("OST_RESULTS_DIR")
    results13 = os.path.join(SCRATCH, "results13")
    os.makedirs(results13, exist_ok=True)
    os.environ["OST_RESULTS_DIR"] = results13
    server13, _t13 = fake_ollama.start_server()
    host13, port13 = server13.server_address
    os.environ["OLLAMA_HOST"] = f"http://{host13}:{port13}"
    window13 = app.MainWindow()
    window13.show()
    wait_until(lambda: window13.model_list.count() == 4, timeout=10,
               app=qt_app)
    try:
        # R11: the dialog itself, driven without exec (a real modal
        # dialog would hang the suite).
        dialog13 = app.RunPreviewDialog(window13, pv13["text"])
        check("preview dialog shows the preview text",
              dialog13.text_view.toPlainText() == pv13["text"])
        check("preview dialog has Run and Adjust buttons",
              dialog13.run_button.text() == "Run"
              and dialog13.adjust_button.text() == "Adjust")
        check("Run is the dialog default",
              dialog13.run_button.isDefault())
        dialog13.run_button.click()
        check("pressing Run accepts the dialog",
              dialog13.result() == QDialog.Accepted,
              str(dialog13.result()))
        dialog13b = app.RunPreviewDialog(window13, pv13["text"])
        dialog13b.adjust_button.click()
        check("pressing Adjust rejects the dialog",
              dialog13b.result() == QDialog.Rejected,
              str(dialog13b.result()))
        dialog13.close()
        dialog13b.close()

        # R13/R12/R15: the starting state, then a real run.
        check("run chip starts on Ready",
              window13.run_chip.text() == "Ready",
              window13.run_chip.text())
        check("finish label starts hidden",
              not window13.finish_label.isVisible())
        check("progress detail starts empty",
              window13.progress_detail.text() == "",
              repr(window13.progress_detail.text()))
        check("results table has the six v13 columns",
              [window13.table.horizontalHeaderItem(c).text()
               for c in range(window13.table.columnCount())]
              == ["Model", "Overall score", "Avg decode tok/s",
                  "Avg TTFT", "Avg prompt tok/s", "Status"],
              str([window13.table.horizontalHeaderItem(c).text()
                   for c in range(window13.table.columnCount())]))
        set_checked_list(window13.model_list,
                         {"fake-smart:2b", "fake-partial:3b",
                          "fake-dumb:1b"})
        set_checked_templates(window13, {"math", "coding"})
        window13.run_button.click()
        mid13 = wait_until(
            lambda: "steps done" in window13.progress_detail.text(),
            timeout=30, app=qt_app)
        check("progress line shows a step count mid-run", mid13,
              window13.progress_detail.text())
        check("run chip says Running during a run",
              window13.run_chip.text() == "Running",
              window13.run_chip.text())
        check("Cancel is enabled during a run",
              window13.cancel_button.isEnabled())
        check("progress line shows elapsed time",
              "elapsed" in window13.progress_detail.text(),
              window13.progress_detail.text())
        check("progress line estimates the time left",
              "left" in window13.progress_detail.text()
              or "estimating" in window13.progress_detail.text(),
              window13.progress_detail.text())
        done13 = wait_until(
            lambda: window13.worker is None
            and window13.save_button.isEnabled(),
            timeout=300, app=qt_app)
        check("v13 run completes", done13)
        check("run chip says Done after a run",
              window13.run_chip.text() == "Done",
              window13.run_chip.text())
        check("finish label shows after a run",
              window13.finish_label.isVisible())
        check("finish label names the best model and score",
              "fake-smart:2b" in window13.finish_label.text()
              and "100%" in window13.finish_label.text(),
              window13.finish_label.text())
        check("finish label names the best speed",
              "20.0 tok/s" in window13.finish_label.text(),
              window13.finish_label.text())
        check("finish label names the run totals",
              "3 models, 2 templates" in window13.finish_label.text(),
              window13.finish_label.text())
        check("progress line keeps its final count",
              "48 of 48 steps done" in window13.progress_detail.text(),
              window13.progress_detail.text())
        row_smart13 = row_for(window13, "fake-smart:2b")
        row_partial13 = row_for(window13, "fake-partial:3b")
        row_dumb13 = row_for(window13, "fake-dumb:1b")
        check("smart prompt speed is 40.0 tok/s",
              row_smart13 is not None
              and table_text(window13, row_smart13, 4) == "40.0",
              table_text(window13, row_smart13, 4)
              if row_smart13 is not None else "no row")
        check("partial prompt speed is 20.0 tok/s",
              row_partial13 is not None
              and table_text(window13, row_partial13, 4) == "20.0",
              table_text(window13, row_partial13, 4)
              if row_partial13 is not None else "no row")
        check("dumb prompt speed is 10.0 tok/s",
              row_dumb13 is not None
              and table_text(window13, row_dumb13, 4) == "10.0",
              table_text(window13, row_dumb13, 4)
              if row_dumb13 is not None else "no row")
        ttft13 = (table_text(window13, row_smart13, 3)
                  if row_smart13 is not None else "")
        check("smart TTFT is shown in ms or seconds",
              ttft13.endswith(" ms") or ttft13.endswith(" s"), ttft13)
        check("status is in the last column after the run",
              row_smart13 is not None
              and table_text(window13, row_smart13, 5) == "done",
              table_text(window13, row_smart13, 5)
              if row_smart13 is not None else "no row")
        if row_smart13 is not None:
            window13.table.selectRow(row_smart13)
            window13.on_table_clicked(row_smart13, 0)
            detail13 = window13.detail_view.toPlainText()
            check("detail view shows the saved answer",
                  "def fib" in detail13, detail13[:400])
            check("detail view shows first-token latency",
                  "First token" in detail13, detail13[:200])
            check("detail view shows prompt speed",
                  "prompt 40.0 tok/s" in detail13, detail13[:200])

        # Turn answers: drive show_detail with a fabricated
        # multi-turn result, no run needed.
        window13.results["probe-model"] = {
            "name": "probe-model", "status": "done",
            "overall_score": 100.0, "avg_decode_tok_s": 20.0,
            "avg_ttft_s": 0.5, "avg_prompt_tok_s": 40.0,
            "total_tokens": None,
            "templates": {"multi_turn": {
                "template": "multi_turn",
                "template_name": "Multi-turn",
                "template_score": 100.0, "passed": 1, "total": 1,
                "not_run": 0, "avg_decode_tok_s": 20.0,
                "tasks": [{"id": "mt1", "passed": True,
                           "decode_tok_s": 20.0,
                           "answer": "last turn answer",
                           "turns": [
                               {"prompt": "first?", "passed": True,
                                "answer": "first turn answer"},
                               {"prompt": "second?", "passed": True,
                                "answer": "second turn answer"},
                           ]}],
            }},
        }
        window13.show_detail("probe-model")
        detail_mt13 = window13.detail_view.toPlainText()
        check("detail view shows each turn's answer",
              "first turn answer" in detail_mt13
              and "second turn answer" in detail_mt13, detail_mt13)
        del window13.results["probe-model"]

        # A cancelled run: chip says Stopped, no finish headline.
        set_checked_list(window13.model_list, {"fake-smart:2b"})
        set_checked_templates(window13, {"math", "coding"})
        window13.run_button.click()
        mid13b = wait_until(
            lambda: "steps done" in window13.progress_detail.text(),
            timeout=30, app=qt_app)
        check("second run starts (for the cancel check)", mid13b)
        window13.cancel_button.click()
        qt_app.processEvents()
        cancelled13 = wait_until(lambda: window13.worker is None,
                                 timeout=120, app=qt_app)
        check("cancelled run stops", cancelled13)
        check("run chip says Stopped after a cancel",
              window13.run_chip.text() == "Stopped",
              window13.run_chip.text())
        check("no finish headline after a cancel",
              not window13.finish_label.isVisible())
    finally:
        window13.close()
        window13.deleteLater()
        qt_app.processEvents()
        server13.shutdown()
        server13.server_close()
        if prior_dismissed13 is None:
            settings13.remove("setup_dismissed")
        else:
            settings13.setValue("setup_dismissed", prior_dismissed13)
        if prior_completed13 is None:
            settings13.remove("setup_completed")
        else:
            settings13.setValue("setup_completed", prior_completed13)
        settings13.sync()
        if prior_results13 is None:
            os.environ.pop("OST_RESULTS_DIR", None)
        else:
            os.environ["OST_RESULTS_DIR"] = prior_results13

    # ---------- v14: bento overview (R14), charts (R16), providers
    # rebuild (R17), settings window (R18) ----------
    # Fabricated runs for two models: smart scores Math 100 then 80
    # and Coding 40; partial scores Math 60 and Coding 80.
    def write_run14(folder, model, template, name, score, speed,
                    stamp):
        path = os.path.join(
            folder, f"{stamp.replace(':', '-')}-{template}.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({
                "model": model, "template": template,
                "template_name": name, "template_score": score,
                "avg_decode_tok_s": speed, "timestamp": stamp,
                "system_id": "sys1",
            }, handle)

    unit14 = os.path.join(SCRATCH, "results14-unit")
    smart_dir14 = os.path.join(unit14, "fake-smart-2b", "sys1")
    partial_dir14 = os.path.join(unit14, "fake-partial-3b", "sys1")
    os.makedirs(smart_dir14, exist_ok=True)
    os.makedirs(partial_dir14, exist_ok=True)
    write_run14(smart_dir14, "fake-smart:2b", "math", "Math", 100.0,
                20.0, "2026-11-13T10:00:00")
    write_run14(smart_dir14, "fake-smart:2b", "coding", "Coding", 40.0,
                18.0, "2026-11-13T11:00:00")
    write_run14(smart_dir14, "fake-smart:2b", "math", "Math", 80.0,
                22.0, "2026-11-14T09:00:00")
    write_run14(partial_dir14, "fake-partial:3b", "math", "Math", 60.0,
                10.0, "2026-11-13T10:00:00")
    write_run14(partial_dir14, "fake-partial:3b", "coding", "Coding",
                80.0, 9.0, "2026-11-13T11:00:00")
    nums14 = app.profile_numbers(unit14, "fake-smart-2b")
    check("profile_numbers latest overall is 60",
          nums14 is not None and nums14["overall_latest"] == 60.0,
          str(nums14["overall_latest"]) if nums14 else "None")
    check("profile_numbers best overall is 70",
          nums14 is not None and nums14["overall_best"] == 70.0,
          str(nums14["overall_best"]) if nums14 else "None")
    check("profile_numbers average speed is 20",
          nums14 is not None and nums14["avg_speed"] == 20.0,
          str(nums14["avg_speed"]) if nums14 else "None")
    check("profile_numbers strengths are Math",
          nums14 is not None and nums14["strengths"] == ["Math"],
          str(nums14["strengths"]) if nums14 else "None")
    check("profile_numbers weaknesses are Coding",
          nums14 is not None and nums14["weaknesses"] == ["Coding"],
          str(nums14["weaknesses"]) if nums14 else "None")
    check("profile_numbers template scores best first",
          nums14 is not None
          and nums14["template_scores"] == [("Math", 80.0),
                                            ("Coding", 40.0)],
          str(nums14["template_scores"]) if nums14 else "None")
    check("profile_numbers speed points oldest first",
          nums14 is not None
          and nums14["speed_points"]
          == [("2026-11-13T10:00:00", 20.0),
              ("2026-11-13T11:00:00", 18.0),
              ("2026-11-14T09:00:00", 22.0)],
          str(nums14["speed_points"]) if nums14 else "None")
    check("profile_numbers counts 3 runs",
          nums14 is not None and nums14["runs_count"] == 3,
          str(nums14["runs_count"]) if nums14 else "None")
    check("profile_numbers is None with no runs",
          app.profile_numbers(unit14, "no-such-model") is None)
    _name14, text14 = app.build_profile(unit14, "fake-smart-2b")
    check("build_profile still renders the latest score",
          "Latest overall score: 60%" in text14, text14)
    check("build_profile still renders the run count",
          "Runs: 3" in text14, text14)
    check("build_profile verdict still names strengths",
          "Best at: Math." in text14 and "Weakest at: Coding." in text14,
          text14)
    check("nice step of 20 is 5", app._nice_step(20) == 5.0,
          str(app._nice_step(20)))
    check("nice step of 22 is 10", app._nice_step(22) == 10.0,
          str(app._nice_step(22)))
    check("nice step of 2.5 is 1", app._nice_step(2.5) == 1.0,
          str(app._nice_step(2.5)))
    check("short date of an ISO stamp is Nov 13",
          app._short_date("2026-11-13T10:00:00") == "Nov 13",
          app._short_date("2026-11-13T10:00:00"))
    check("short date of garbage is the first 10 chars",
          app._short_date("not-a-date-at-all") == "not-a-date",
          app._short_date("not-a-date-at-all"))
    bars14 = app.BarChartWidget()
    check("bars title with no data",
          bars14.takeaway_title() == "No scored templates yet.",
          bars14.takeaway_title())
    bars14.set_data([("Math", 80.0), ("Coding", 40.0)])
    check("bars title names strongest and weakest",
          bars14.takeaway_title()
          == "Strongest: Math 80%. Weakest: Coding 40%.",
          bars14.takeaway_title())
    bars14.set_data([("Math", 100.0), ("Coding", 100.0)])
    check("bars title when all are equal",
          bars14.takeaway_title() == "Every template at 100%.",
          bars14.takeaway_title())
    bars14.set_data([("Math", 80.0, 60.0), ("Coding", 40.0, 80.0)],
                    ("A", "B"))
    check("bars title when pairs are even",
          bars14.takeaway_title()
          == "A and B are even across the templates.",
          bars14.takeaway_title())
    bars14.set_data([("Math", 80.0, 60.0), ("Coding", 90.0, 80.0)],
                    ("A", "B"))
    check("bars title names the pair leader",
          bars14.takeaway_title() == "A leads on 2 templates.",
          bars14.takeaway_title())
    line14 = app.LineChartWidget()
    check("line title with no data",
          line14.takeaway_title() == "No runs with speed yet.",
          line14.takeaway_title())
    line14.set_data([("Nov 13", 20.0)])
    check("line title with one point",
          line14.takeaway_title() == "One run so far: 20.0 tok/s.",
          line14.takeaway_title())
    line14.set_data([("Nov 13", 20.0), ("Nov 14", 21.0)])
    check("line title when speed is steady",
          line14.takeaway_title()
          == "Speed is steady around 21.0 tok/s.",
          line14.takeaway_title())
    line14.set_data([("Nov 13", 10.0), ("Nov 14", 20.0)])
    check("line title when speed rose",
          line14.takeaway_title()
          == "Speed rose from 10.0 to 20.0 tok/s.",
          line14.takeaway_title())
    line14.set_data([("Nov 13", 20.0), ("Nov 14", 10.0)])
    check("line title when speed fell",
          line14.takeaway_title()
          == "Speed fell from 20.0 to 10.0 tok/s.",
          line14.takeaway_title())
    # The charts paint without error, with data and without.
    bars14.set_data([("Math", 80.0), ("Coding", 40.0)])
    bars14.show()
    line14.set_data([("Nov 13", 10.0), ("Nov 14", 20.0)])
    line14.show()
    qt_app.processEvents()
    check("bars chart paints with data",
          not bars14.grab().isNull())
    check("line chart paints with data",
          not line14.grab().isNull())
    bars14.set_data([])
    line14.set_data([])
    qt_app.processEvents()
    check("bars chart paints with no data",
          not bars14.grab().isNull())
    check("line chart paints with no data",
          not line14.grab().isNull())
    bars14.set_data([("Math", 80.0, 60.0)], ("A", "B"))
    qt_app.processEvents()
    check("paired bars chart paints",
          not bars14.grab().isNull())
    bars14.close()
    line14.close()

    pserver14, _pt14 = fake_ollama.start_provider_server()
    phost14, pport14 = pserver14.server_address
    provider14 = {"name": "fake",
                  "base_url": f"http://{phost14}:{pport14}/v1",
                  "api_key": fake_ollama.PROVIDER_API_KEY}
    try:
        ok14, ids14, msg14 = app.provider_fetch_models(provider14)
        check("fetch models returns the provider list",
              ok14 and ids14 == ["fake-gpt-dumb", "fake-gpt-partial",
                                 "fake-gpt-smart"],
              f"{ok14} {ids14} {msg14}")
        check("fetch models message names the count",
              "3 models listed" in msg14, msg14)
        ok14b, ids14b, msg14b = app.provider_fetch_models(
            dict(provider14, api_key="wrong-key"))
        check("fetch models with a bad key fails with no ids",
              not ok14b and ids14b == []
              and "rejected the key" in msg14b,
              f"{ok14b} {ids14b} {msg14b}")
        ok14c, ids14c, msg14c = app.provider_fetch_models(
            dict(provider14, base_url=""))
        check("fetch models with no base URL fails",
              not ok14c and ids14c == [] and "No base URL" in msg14c,
              f"{ok14c} {ids14c} {msg14c}")
        ok14d, msg14d = app.provider_test_connection(provider14)
        check("test connection keeps its (ok, message) shape",
              ok14d and "Connected" in msg14d, f"{ok14d} {msg14d}")

        # Settings round-trip. The keys live in the real QSettings,
        # so the prior values are saved here and restored in the
        # finally below, leaving no state behind.
        from PySide6.QtCore import QSettings as QSettings14
        settings14 = QSettings14("BrokeNBored", app.APP_NAME)
        prior14 = {
            key: settings14.value(key)
            for key in ("models_folder", "ollama_host",
                        "default_budget_minutes", "template_sets",
                        "font_point_size", "reduce_motion",
                        "setup_dismissed", "setup_completed")
        }
        prior_env_host14 = app._ENV_OLLAMA_HOST
        prior_env_models14 = app._ENV_OLLAMA_MODELS
        prior_host14 = os.environ.get("OLLAMA_HOST")
        prior_results14 = os.environ.get("OST_RESULTS_DIR")
        prior_providers14 = os.environ.get("OST_PROVIDERS_FILE")
        prior_caps14 = os.environ.get("OST_CAPABILITIES_FILE")
        prior_path14 = os.environ.get("PATH")
        prior_pid14 = os.environ.get("FAKE_OLLAMA_PIDFILE")
        prior_calls14 = os.environ.get("FAKE_OLLAMA_CALLS")
        prior_envfile14 = os.environ.get("FAKE_OLLAMA_ENVFILE")
        prior_models_env14 = os.environ.get("OLLAMA_MODELS")
        try:
            saved14 = {
                "models_folder": "/tmp/ost-v14-models",
                "ollama_host": "http://127.0.0.1:11434",
                "default_budget_minutes": 45,
                "template_sets": {"fake-smart:2b": ["math"]},
                "font_point_size": 14,
                "reduce_motion": True,
            }
            app.save_app_settings(saved14)
            check("settings round-trip",
                  app.load_app_settings() == saved14,
                  str(app.load_app_settings()))
            settings14.setValue("template_sets", "{not json")
            check("settings with bad template JSON reads empty sets",
                  app.load_app_settings()["template_sets"] == {},
                  str(app.load_app_settings()["template_sets"]))
            for key in ("models_folder", "ollama_host",
                        "default_budget_minutes", "template_sets",
                        "font_point_size", "reduce_motion"):
                settings14.remove(key)
            settings14.sync()
            check("settings with no keys read as defaults",
                  app.load_app_settings() == {
                      "models_folder": "", "ollama_host": "",
                      "default_budget_minutes": 0, "template_sets": {},
                      "font_point_size": 0, "reduce_motion": False},
                  str(app.load_app_settings()))
            # The budget spin starts from the saved default.
            app.save_app_settings({
                "models_folder": "", "ollama_host": "",
                "default_budget_minutes": 45, "template_sets": {}})

            results14 = os.path.join(SCRATCH, "results14")
            shutil.copytree(unit14, results14, dirs_exist_ok=True)
            os.environ["OST_RESULTS_DIR"] = results14
            os.environ["OST_PROVIDERS_FILE"] = os.path.join(
                SCRATCH, "providers14.json")
            os.environ["OST_CAPABILITIES_FILE"] = os.path.join(
                SCRATCH, "capabilities14.json")
            server14, _t14 = fake_ollama.start_server()
            host14, port14 = server14.server_address
            os.environ["OLLAMA_HOST"] = f"http://{host14}:{port14}"
            window14 = app.MainWindow()
            window14.show()
            wait_until(lambda: window14.model_list.count() == 4,
                       timeout=10, app=qt_app)
            try:
                check("budget spin starts from the saved default",
                      window14.budget_spin.value() == 45,
                      str(window14.budget_spin.value()))
                # R14/R16: the profile glance for the smart model.
                window14.tabs.setCurrentIndex(window14.TAB_PROFILES)
                for row14 in range(window14.profile_model_combo.count()):
                    if (window14.profile_model_combo.itemData(row14)
                            == "fake-smart-2b"):
                        window14.profile_model_combo.setCurrentIndex(row14)
                qt_app.processEvents()
                check("hero tile shows the latest overall score",
                      window14.profile_hero_value.text() == "60%",
                      window14.profile_hero_value.text())
                check("best tile shows the best overall score",
                      window14.profile_best_value.text() == "70%",
                      window14.profile_best_value.text())
                check("speed tile shows the average speed",
                      window14.profile_speed_value.text() == "20.0 tok/s",
                      window14.profile_speed_value.text())
                check("strongest tile names Math",
                      window14.profile_strong_value.text() == "Math",
                      window14.profile_strong_value.text())
                check("weakest tile names Coding",
                      window14.profile_weak_value.text() == "Coding",
                      window14.profile_weak_value.text())
                check("tiles show when the model has runs",
                      window14.profile_tiles.isVisible())
                check("bars chart holds the template scores",
                      window14.profile_bars.rows
                      == [("Math", 80.0), ("Coding", 40.0)],
                      str(window14.profile_bars.rows))
                check("bars title names strongest and weakest",
                      window14.profile_bars_title.text()
                      == "Strongest: Math 80%. Weakest: Coding 40%.",
                      window14.profile_bars_title.text())
                check("speed chart holds the run speeds",
                      window14.profile_speed_chart.points
                      == [("Nov 13", 20.0), ("Nov 13", 18.0),
                          ("Nov 14", 22.0)],
                      str(window14.profile_speed_chart.points))
                check("speed title says steady around 22",
                      window14.profile_speed_title.text()
                      == "Speed is steady around 22.0 tok/s.",
                      window14.profile_speed_title.text())
                # No runs: no tiles, no charts.
                empty14 = os.path.join(SCRATCH, "results14-empty")
                os.makedirs(empty14, exist_ok=True)
                os.environ["OST_RESULTS_DIR"] = empty14
                window14.refresh_profiles()
                qt_app.processEvents()
                check("tiles hide when the model has no runs",
                      not window14.profile_tiles.isVisible())
                check("charts clear when the model has no runs",
                      window14.profile_bars.rows == []
                      and window14.profile_bars_title.text() == ""
                      and window14.profile_speed_chart.points == [],
                      str(window14.profile_bars.rows))
                os.environ["OST_RESULTS_DIR"] = results14
                window14.refresh_profiles()
                window14.refresh_compare()
                qt_app.processEvents()
                # R14/R16 on Compare: two models side by side.
                window14.tabs.setCurrentIndex(window14.TAB_COMPARE)
                for combo14, slug14 in (
                        (window14.compare_model_a, "fake-smart-2b"),
                        (window14.compare_model_b, "fake-partial-3b")):
                    for row14 in range(combo14.count()):
                        if combo14.itemData(row14) == slug14:
                            combo14.setCurrentIndex(row14)
                qt_app.processEvents()
                check("compare tile A names the model and numbers",
                      window14.compare_caption_a.text() == "fake-smart:2b"
                      and "60% overall" in window14.compare_value_a.text()
                      and "20.0 tok/s average"
                      in window14.compare_value_a.text()
                      and "Best: Math 80%"
                      in window14.compare_value_a.text(),
                      window14.compare_value_a.text())
                check("compare tile B names the model and numbers",
                      window14.compare_caption_b.text() == "fake-partial:3b"
                      and "70% overall" in window14.compare_value_b.text()
                      and "9.5 tok/s average"
                      in window14.compare_value_b.text()
                      and "Best: Coding 80%"
                      in window14.compare_value_b.text(),
                      window14.compare_value_b.text())
                check("compare chart title says the pair is even",
                      window14.compare_chart_title.text()
                      == "fake-smart:2b and fake-partial:3b are even "
                      "across the templates.",
                      window14.compare_chart_title.text())
                check("compare chart caption says which bar is which",
                      window14.compare_chart_caption.text()
                      == "Top bar: fake-smart:2b. "
                      "Bottom bar: fake-partial:3b.",
                      window14.compare_chart_caption.text())
                check("compare chart holds both models scores",
                      window14.compare_chart.rows
                      == [("Coding", 40.0, 80.0), ("Math", 80.0, 60.0)],
                      str(window14.compare_chart.rows))
                for row14 in range(window14.compare_model_b.count()):
                    if (window14.compare_model_b.itemData(row14)
                            == "fake-smart-2b"):
                        window14.compare_model_b.setCurrentIndex(row14)
                qt_app.processEvents()
                check("compare glance hides for the same model twice",
                      not window14.compare_tiles.isVisible()
                      and window14.compare_chart.rows == [])
                # R17 (v19): the simplified Providers tab — an empty
                # state with one button, a detail page once a
                # provider exists, Advanced collapsed. The provider
                # arrives via the stub wizard; the real wizard is
                # driven in the v19 section.
                window14.tabs.setCurrentIndex(window14.TAB_PROVIDERS)
                qt_app.processEvents()
                check("providers tab starts on the empty state",
                      window14.provider_stack.currentIndex() == 0,
                      str(window14.provider_stack.currentIndex()))
                check("empty state names the Models tab",
                      "No cloud providers" in
                      window14.provider_empty_label.text()
                      and "Models tab" in
                      window14.provider_empty_label.text(),
                      window14.provider_empty_label.text())
                check("empty state has one add button",
                      window14.provider_add_empty_button.isVisible()
                      and window14.provider_add_empty_button.text()
                      == "Add a cloud provider")
                provider14b = {
                    "name": "fake",
                    "base_url": f"http://{phost14}:{pport14}/v1",
                    "api_key": fake_ollama.PROVIDER_API_KEY,
                    "models": [
                        {"id": "fake-gpt-smart", "display_name": "",
                         "price_input_per_1m": None,
                         "price_output_per_1m": None,
                         "capabilities": ["text"]},
                    ],
                }
                app.AddProviderDialog = _stub_wizard_class(provider14b)
                window14.provider_add_button.click()
                qt_app.processEvents()
                app.AddProviderDialog = real_wizard
                check("adding a provider shows the detail page",
                      window14.provider_stack.currentIndex() == 1,
                      str(window14.provider_stack.currentIndex()))
                check("detail page shows the provider name and address",
                      window14.provider_name_input.text() == "fake"
                      and window14.provider_url_input.text()
                      == f"http://{phost14}:{pport14}/v1",
                      f"{window14.provider_name_input.text()} "
                      f"{window14.provider_url_input.text()}")
                check("detail page labels the address in plain words",
                      any("Where it answers" in label.text()
                          for label in window14.findChildren(QLabel)))
                check("Advanced starts collapsed on the detail page",
                      not window14.provider_advanced_box.isVisible())
                # R18: the Settings window (driven without exec — a
                # real modal dialog would hang the suite).
                from PySide6.QtGui import QAction as QAction14
                check("File menu has a Settings action",
                      any(a.text() == "Settings" for a in
                          window14.findChildren(QAction14)))
                app._ENV_OLLAMA_HOST = None
                app._ENV_OLLAMA_MODELS = None
                app.save_app_settings({
                    "models_folder": "/tmp/ost-v14-models",
                    "ollama_host": "http://127.0.0.1:11434",
                    "default_budget_minutes": 45,
                    "template_sets": {"fake-smart:2b": ["math"]},
                })
                dialog14 = app.SettingsDialog(
                    app.load_app_settings(), window14)
                check("settings fields show the saved values",
                      dialog14.models_folder_input.text()
                      == "/tmp/ost-v14-models"
                      and dialog14.ollama_host_input.text()
                      == "http://127.0.0.1:11434"
                      and dialog14.budget_spin.value() == 45,
                      f"{dialog14.models_folder_input.text()} "
                      f"{dialog14.ollama_host_input.text()} "
                      f"{dialog14.budget_spin.value()}")
                check("settings dialog names the saved usual set",
                      "fake-smart:2b" in
                      dialog14.template_sets_label.text()
                      and "Math" in dialog14.template_sets_label.text(),
                      dialog14.template_sets_label.text())
                dialog14.models_folder_input.setText("/tmp/new-models")
                dialog14.ollama_host_input.setText(
                    "http://127.0.0.1:11555")
                dialog14.budget_spin.setValue(30)
                dialog14.accept()
                check("settings accept returns the edited values",
                      dialog14.result_values == {
                          "models_folder": "/tmp/new-models",
                          "ollama_host": "http://127.0.0.1:11555",
                          "default_budget_minutes": 30,
                          "template_sets": {"fake-smart:2b": ["math"]},
                          # v15: the dialog also returns the font
                          # size (untouched here, so the current
                          # size) and the motion switch (off).
                          "font_point_size": dialog14.font_spin.value(),
                          "reduce_motion": False},
                      str(dialog14.result_values))
                dialog14.close()
                app._ENV_OLLAMA_HOST = "http://env-host:11434"
                app._ENV_OLLAMA_MODELS = "/env/models"
                dialog14b = app.SettingsDialog(
                    app.load_app_settings(), window14)
                check("environment-set fields are locked",
                      not dialog14b.models_folder_input.isEnabled()
                      and not dialog14b.ollama_host_input.isEnabled())
                check("locked fields show the environment values",
                      dialog14b.models_folder_input.text() == "/env/models"
                      and dialog14b.ollama_host_input.text()
                      == "http://env-host:11434")
                dialog14b.accept()
                check("locked fields keep the stored values on save",
                      dialog14b.result_values["models_folder"]
                      == "/tmp/ost-v14-models"
                      and dialog14b.result_values["ollama_host"]
                      == "http://127.0.0.1:11434",
                      str(dialog14b.result_values))
                dialog14b.close()
                app._ENV_OLLAMA_HOST = None
                app._ENV_OLLAMA_MODELS = None
                # _apply_settings: the saved address reaches the
                # process, and clearing it takes it back out.
                window14._apply_settings({
                    "models_folder": "", "ollama_host":
                    "http://127.0.0.1:9",
                    "default_budget_minutes": 15,
                    "template_sets": {}})
                check("applying settings sets the Ollama address",
                      os.environ.get("OLLAMA_HOST")
                      == "http://127.0.0.1:9"
                      and window14.budget_spin.value() == 15,
                      f"{os.environ.get('OLLAMA_HOST')} "
                      f"{window14.budget_spin.value()}")
                window14._apply_settings({
                    "models_folder": "", "ollama_host": "",
                    "default_budget_minutes": 0,
                    "template_sets": {}})
                check("clearing the saved address removes it",
                      "OLLAMA_HOST" not in os.environ)
                os.environ["OLLAMA_HOST"] = f"http://{host14}:{port14}"
                app._ENV_OLLAMA_HOST = "http://env-host:11434"
                os.environ["OLLAMA_HOST"] = "http://env-host:11434"
                window14._apply_settings({
                    "models_folder": "", "ollama_host":
                    "http://127.0.0.1:9",
                    "default_budget_minutes": 0,
                    "template_sets": {}})
                check("applying settings never overrides the env",
                      os.environ.get("OLLAMA_HOST")
                      == "http://env-host:11434",
                      os.environ.get("OLLAMA_HOST"))
                app._ENV_OLLAMA_HOST = None
                os.environ["OLLAMA_HOST"] = f"http://{host14}:{port14}"
                # R18: usual template sets follow the model.
                window14.tabs.setCurrentIndex(window14.TAB_SETUP)
                app.save_app_settings({
                    "models_folder": "", "ollama_host": "",
                    "default_budget_minutes": 0,
                    "template_sets": {"fake-smart:2b": ["math"]},
                })
                set_checked_list(window14.model_list, {"fake-smart:2b"})
                qt_app.processEvents()
                ticked14 = {
                    window14.template_list.item(r).data(Qt.UserRole)
                    for r in range(window14.template_list.count())
                    if window14.template_list.item(r).checkState()
                    == Qt.Checked
                }
                check("ticking a model ticks its usual templates",
                      ticked14 == {"math"}, str(ticked14))
                check("the usual-set label names the model",
                      window14.template_set_label.text()
                      == "Usual templates for fake-smart:2b.",
                      window14.template_set_label.text())
                for r in range(window14.template_list.count()):
                    item14 = window14.template_list.item(r)
                    if (item14.data(Qt.UserRole) == "coding"
                            and item14.flags() & Qt.ItemIsEnabled):
                        item14.setCheckState(Qt.Checked)
                qt_app.processEvents()
                check("a hand-changed template clears the label",
                      window14.template_set_label.text() == "",
                      repr(window14.template_set_label.text()))
                # A run saves the run's templates as the usual set.
                app.save_app_settings({
                    "models_folder": "", "ollama_host": "",
                    "default_budget_minutes": 0,
                    "template_sets": {"fake-smart:2b": ["coding"]},
                })
                set_checked_list(window14.model_list, {"fake-smart:2b"})
                set_checked_templates(window14, {"math"})
                window14.run_button.click()
                ran14 = wait_until(
                    lambda: window14.worker is None
                    and window14.save_button.isEnabled(),
                    timeout=120, app=qt_app)
                check("v14 run completes", ran14)
                check("a run saves its templates as the usual set",
                      app.load_app_settings()["template_sets"].get(
                          "fake-smart:2b") == ["math"],
                      str(app.load_app_settings()["template_sets"]))
                # R18: Start Ollama passes the saved models folder.
                app.save_app_settings({
                    "models_folder": "/tmp/ost-v14-models",
                    "ollama_host": "", "default_budget_minutes": 0,
                    "template_sets": {}})
                os.environ["PATH"] = (FAKEBIN + os.pathsep
                                      + ORIGINAL_PATH)
                pidfile14 = os.path.join(VERIFY_DIR,
                                         "fake-ollama14.pid")
                envfile14 = os.path.join(VERIFY_DIR,
                                         "fake-ollama14.env")
                os.environ["FAKE_OLLAMA_PIDFILE"] = pidfile14
                os.environ["FAKE_OLLAMA_ENVFILE"] = envfile14
                if os.path.exists(envfile14):
                    os.remove(envfile14)
                os.environ["OLLAMA_HOST"] = (
                    f"http://127.0.0.1:{free_port()}")
                window14.start_ollama_button.click()
                started14 = wait_until(
                    lambda: window14.status_label.text()
                    == "Ollama is running.",
                    timeout=45, app=qt_app)
                check("start Ollama with a saved models folder",
                      started14, window14.status_label.text())
                try:
                    with open(envfile14, encoding="utf-8") as handle:
                        env_contents14 = handle.read()
                except OSError:
                    env_contents14 = "missing"
                check("the started Ollama gets the models folder",
                      env_contents14 == "/tmp/ost-v14-models",
                      env_contents14)
                kill_pidfile(pidfile14)
                os.environ["OLLAMA_MODELS"] = "/env/models"
                os.environ["OLLAMA_HOST"] = (
                    f"http://127.0.0.1:{free_port()}")
                if os.path.exists(envfile14):
                    os.remove(envfile14)
                window14.start_ollama_button.click()
                started14b = wait_until(
                    lambda: os.path.exists(envfile14)
                    and window14.status_label.text()
                    == "Ollama is running.",
                    timeout=45, app=qt_app)
                check("start Ollama again with env models folder",
                      started14b, window14.status_label.text())
                try:
                    with open(envfile14, encoding="utf-8") as handle:
                        env_contents14b = handle.read()
                except OSError:
                    env_contents14b = "missing"
                check("the starter never overrides OLLAMA_MODELS",
                      env_contents14b == "/env/models",
                      env_contents14b)
                kill_pidfile(pidfile14)
                del os.environ["OLLAMA_MODELS"]
            finally:
                window14.close()
                window14.deleteLater()
                qt_app.processEvents()
                server14.shutdown()
                server14.server_close()
        finally:
            app._ENV_OLLAMA_HOST = prior_env_host14
            app._ENV_OLLAMA_MODELS = prior_env_models14
            for key14, value14 in prior14.items():
                if value14 is None:
                    settings14.remove(key14)
                else:
                    settings14.setValue(key14, value14)
            settings14.sync()
            for env14, value14 in (
                    ("OLLAMA_HOST", prior_host14),
                    ("OST_RESULTS_DIR", prior_results14),
                    ("OST_PROVIDERS_FILE", prior_providers14),
                    ("OST_CAPABILITIES_FILE", prior_caps14),
                    ("PATH", prior_path14),
                    ("FAKE_OLLAMA_PIDFILE", prior_pid14),
                    ("FAKE_OLLAMA_CALLS", prior_calls14),
                    ("FAKE_OLLAMA_ENVFILE", prior_envfile14),
                    ("OLLAMA_MODELS", prior_models_env14)):
                if value14 is None:
                    os.environ.pop(env14, None)
                else:
                    os.environ[env14] = value14
    finally:
        pserver14.shutdown()
        pserver14.server_close()

    # ---------- v15: pull upgrades (R19), timeline scrub (R20),
    # accessibility (R21), keyboard navigation (R22),
    # micro-interactions (R23), v3 leftovers (R24) ----------
    # Unit checks: the re-run helpers, the template preview text,
    # the run headline, and the focus-ring stylesheets.
    templates15, _warnings15 = app.load_templates()
    by_id15 = {t["id"]: t for t in templates15}
    math15 = by_id15["math"]
    coding15 = by_id15["coding"]
    speed15 = by_id15["speed"]
    target_a15 = {"label": "a:1b", "kind": "ollama"}
    target_b15 = {"label": "b:1b", "kind": "ollama"}
    targets15 = [target_a15, target_b15]

    def pairs15(failed):
        return [(t["label"], x["id"]) for t, x in failed]

    results15a = {
        "a:1b": {"name": "a:1b", "status": "done", "templates": {
            "math": {"template_score": 80.0, "total": 5},
            "coding": {"template_score": None, "total": 0}}},
    }
    failed15a = app.failed_run_pairs(
        results15a, targets15, [math15, coding15])
    check("failed pairs: unscored with no tasks run, and a missing model",
          pairs15(failed15a)
          == [("a:1b", "coding"), ("b:1b", "math"), ("b:1b", "coding")],
          str(pairs15(failed15a)))
    failed15b = app.failed_run_pairs(
        {"a:1b": {"name": "a:1b", "status": "failed: model exploded",
                  "templates": {}}},
        [target_a15], [math15, coding15])
    check("failed pairs: a failed model fails every pair",
          pairs15(failed15b) == [("a:1b", "math"), ("a:1b", "coding")],
          str(pairs15(failed15b)))
    failed15c = app.failed_run_pairs(
        {"a:1b": {"name": "a:1b", "status": "done", "templates": {
            "math": {"skipped": "not eligible"},
            "coding": {"template_score": 60.0, "total": 5},
            "speed": {"template_score": None, "total": 5}}}},
        [target_a15], [math15, coding15, speed15])
    check("failed pairs: skipped and unscored-but-run pairs are not failures",
          pairs15(failed15c) == [],
          str(pairs15(failed15c)))
    failed15d = app.failed_run_pairs(
        {"a:1b": {"name": "a:1b", "status": "done", "templates": {
            "math": {"template_score": None, "total": 0,
                     "time_budget_reached": True},
            "coding": {"template_score": 60.0, "total": 5}}}},
        [target_a15], [math15, coding15])
    check("failed pairs: a budget-stopped template fails",
          pairs15(failed15d) == [("a:1b", "math")],
          str(pairs15(failed15d)))
    failed15e = app.failed_run_pairs(
        results15a, targets15, [math15, coding15],
        pairs={("a:1b", "math"), ("b:1b", "coding")})
    check("failed pairs: a pairs filter only considers its own pairs",
          pairs15(failed15e) == [("b:1b", "coding")],
          str(pairs15(failed15e)))
    steps15a = app.count_run_steps(
        [target_a15], [math15, coding15], pairs={("a:1b", "coding")})
    check("run steps count only the pairs in a re-run",
          steps15a == 6, str(steps15a))
    steps15b = app.count_run_steps(
        targets15, [math15, coding15], pairs={("a:1b", "math")})
    check("run steps skip a target with no pairs in a re-run",
          steps15b == 11, str(steps15b))
    provider_target15 = {
        "label": "p15 / m15", "kind": "provider",
        "capabilities": ["text"],
        "price_input_per_1m": 1.0, "price_output_per_1m": 2.0,
    }
    cost15 = app.estimate_run_cost(
        [provider_target15], [math15, coding15],
        pairs={("p15 / m15", "coding")})
    cost15_full = app.estimate_run_cost(
        [provider_target15], [math15, coding15])
    check("run cost counts only the pairs in a re-run",
          cost15["per_model"][0]["prompt_tokens"]
          < cost15_full["per_model"][0]["prompt_tokens"]
          and len(cost15["per_model"]) == 1,
          str(cost15["per_model"]))
    cost15_none = app.estimate_run_cost(
        [provider_target15], [math15, coding15],
        pairs={("someone-else", "math")})
    check("run cost skips a provider target with no pairs",
          cost15_none["per_model"] == [],
          str(cost15_none["per_model"]))
    preview15 = app.build_run_preview(
        [target_a15], [math15, coding15],
        os.path.join(SCRATCH, "no-results15"),
        pairs={("a:1b", "math"), ("a:1b", "coding")})
    check("re-run preview names the failed pairs",
          preview15["text"].startswith(
              "Re-running 2 failed pairs (1 model), 16 steps in total."),
          preview15["text"].splitlines()[0] if preview15["text"] else "")
    check("re-run preview counts only the paired steps",
          preview15["total_steps"] == 16,
          str(preview15["total_steps"]))
    math_preview15 = app.render_template_preview(math15)
    check("template preview names the tasks",
          "apples" in math_preview15 and "train" in math_preview15,
          math_preview15[:200])
    check("template preview shows the expected answers",
          "Expected: 21" in math_preview15,
          math_preview15[:200])
    multi_preview15 = app.render_template_preview(by_id15["multi_turn"])
    check("template preview shows chat turns and the first prompt",
          "2 turns" in multi_preview15 and "BANANA" in multi_preview15,
          multi_preview15[:300])
    vision_preview15 = app.render_template_preview(by_id15["vision"])
    check("template preview names non-generate task kinds",
          "(vision)" in vision_preview15,
          vision_preview15[:300])
    headline15 = app.format_run_headline({
        "timestamp": "2026-11-13T10:00:00", "template_name": "Math",
        "score": 80.0, "passed": 4, "total": 5, "speed": 20.0})
    check("run headline names date, score and speed",
          headline15 == "2026-11-13 10:00:00 - Math: 80% (4/5) at 20.0 tok/s",
          headline15)
    headline15b = app.format_run_headline({
        "timestamp": "2026-11-13T10:00:00", "template_name": "Speed",
        "score": None, "passed": None, "total": None, "speed": None})
    check("run headline for an unscored run says so",
          headline15b == "2026-11-13 10:00:00 - Speed: no scoring",
          headline15b)
    check("light stylesheet has a visible focus ring",
          ":focus" in app.LIGHT_STYLESHEET
          and "2px solid" in app.LIGHT_STYLESHEET,
          app.LIGHT_STYLESHEET[:200])
    check("dark stylesheet has a visible focus ring",
          ":focus" in app.DARK_STYLESHEET
          and "#7db8e8" in app.DARK_STYLESHEET,
          app.DARK_STYLESHEET[-200:])
    unit15 = os.path.join(SCRATCH, "results15-unit")
    unit_dir15 = os.path.join(unit15, "fake-smart-2b", "sys1")
    os.makedirs(unit_dir15, exist_ok=True)
    with open(os.path.join(unit_dir15, "2026-11-13T10-00-00-math.json"),
              "w", encoding="utf-8") as handle15:
        json.dump({"timestamp": "2026-11-13T10:00:00", "template": "math",
                   "template_name": "Math", "template_score": 80.0,
                   "passed": 4, "total": 5, "avg_decode_tok_s": 20.0},
                  handle15)
    runs15 = app.list_history_runs(unit15, "fake-smart-2b")
    check("history runs carry the decode speed",
          len(runs15) == 1 and runs15[0]["speed"] == 20.0,
          str(runs15))

    # GUI checks. The settings keys live in the real QSettings, so
    # the prior values are saved here and restored in the finally
    # below, leaving no state behind — including the font size.
    from PySide6.QtCore import QSettings as QSettings15
    from PySide6.QtTest import QTest as QTest15
    settings15 = QSettings15("BrokeNBored", app.APP_NAME)
    prior15 = {
        key: settings15.value(key)
        for key in ("models_folder", "ollama_host",
                    "default_budget_minutes", "template_sets",
                    "font_point_size", "reduce_motion",
                    "setup_dismissed", "setup_completed")
    }
    prior_host15 = os.environ.get("OLLAMA_HOST")
    prior_results15 = os.environ.get("OST_RESULTS_DIR")
    prior_providers15 = os.environ.get("OST_PROVIDERS_FILE")
    prior_caps15 = os.environ.get("OST_CAPABILITIES_FILE")
    prior_font15 = qt_app.font().pointSize()
    # Start clean: no saved font size, no reduced motion, checklist
    # neither dismissed nor completed, no usual template sets.
    app.save_app_settings({"models_folder": "", "ollama_host": "",
                           "default_budget_minutes": 0, "template_sets": {},
                           "font_point_size": 0, "reduce_motion": False})
    settings15.remove("setup_dismissed")
    settings15.remove("setup_completed")
    settings15.sync()
    results15 = os.path.join(SCRATCH, "results15")
    os.makedirs(results15, exist_ok=True)
    os.environ["OST_RESULTS_DIR"] = results15
    os.environ["OST_PROVIDERS_FILE"] = os.path.join(
        SCRATCH, "providers15.json")
    os.environ["OST_CAPABILITIES_FILE"] = os.path.join(
        SCRATCH, "capabilities15.json")
    server15, _t15 = fake_ollama.start_server()
    host15, port15 = server15.server_address
    ollama_url15 = f"http://{host15}:{port15}"
    os.environ["OLLAMA_HOST"] = ollama_url15
    window15 = app.MainWindow()
    window15.show()
    wait_until(lambda: window15.model_list.count() == 4,
               timeout=10, app=qt_app)
    try:
        # R23: the first-run hint shows while no run exists, and the
        # checklist is up on a first boot.
        check("first-run hint shows with no runs",
              window15.first_run_hint.isVisible())
        check("setup checklist shows on a first boot",
              window15.checklist_frame.isVisible())
        # R21: the Settings accessibility section. The preset sets
        # the spin to the startup size plus three and applies it
        # live; Cancel puts the opening size back.
        default_font15 = app.default_font_point_size()
        dialog15 = app.SettingsDialog(app.load_app_settings(), window15)
        check("settings font spin starts at the current size",
              dialog15.font_spin.value() == default_font15,
              str(dialog15.font_spin.value()))
        dialog15.bigger_text_button.click()
        qt_app.processEvents()
        check("bigger-text preset sets the spin to default plus three",
              dialog15.font_spin.value()
              == min(20, default_font15 + app.FONT_PRESET_BONUS_PT),
              str(dialog15.font_spin.value()))
        check("bigger-text preset applies the font live",
              qt_app.font().pointSize()
              == min(20, default_font15 + app.FONT_PRESET_BONUS_PT),
              str(qt_app.font().pointSize()))
        dialog15.reject()
        qt_app.processEvents()
        check("cancelling Settings puts the font size back",
              qt_app.font().pointSize() == default_font15,
              str(qt_app.font().pointSize()))
        dialog15b = app.SettingsDialog(app.load_app_settings(), window15)
        dialog15b.font_spin.setValue(14)
        dialog15b.reduce_motion_check.setChecked(True)
        dialog15b.accept()
        check("accepting Settings returns the font size and motion",
              dialog15b.result_values["font_point_size"] == 14
              and dialog15b.result_values["reduce_motion"] is True,
              str(dialog15b.result_values))
        values15 = dict(dialog15b.result_values)
        # _apply_settings applies the Ollama address too, and the
        # dialog's empty address would drop the fake server's — hand
        # it the fake address so the window keeps talking to it.
        values15["ollama_host"] = ollama_url15
        window15._apply_settings(values15)
        check("applying Settings sets the font size",
              qt_app.font().pointSize() == 14,
              str(qt_app.font().pointSize()))
        check("applying Settings sets reduced motion",
              window15._reduce_motion is True)
        window15._reduce_motion = False
        app.apply_font_size(0)
        # R21: the Settings fields scroll at bigger text sizes — the
        # dialog's content is taller than the dialog at 20 pt, and
        # no explanation label may be clipped. The no-clip test is
        # height against heightForWidth at the label's own width:
        # sizeHint overstates a wrapped label's height, because it
        # is computed at a narrower width than the label gets.
        from PySide6.QtWidgets import QScrollArea as QScrollArea15
        from PySide6.QtWidgets import QDialogButtonBox as QDialogButtonBox15
        app.apply_font_size(20)
        dialog15c = app.SettingsDialog(
            app.load_app_settings(), window15)
        dialog15c.show()
        for _ in range(10):
            qt_app.processEvents()
        scroll15 = dialog15c.findChild(QScrollArea15)
        check("settings fields live in a scroll area",
              scroll15 is not None)
        clipped15 = [
            label.text()[:30]
            for label in dialog15c.findChildren(QLabel)
            if label.wordWrap()
            and label.height() < label.heightForWidth(label.width())]
        check("no settings label is clipped at 20 pt text",
              clipped15 == [], str(clipped15))
        buttons15 = dialog15c.findChild(QDialogButtonBox15)
        check("settings Save stays visible at 20 pt text",
              buttons15 is not None
              and buttons15.button(
                  QDialogButtonBox15.Ok).isVisibleTo(dialog15c))
        dialog15c.close()
        qt_app.processEvents()
        # A modeless dialog closed under offscreen leaves the
        # application with no active window — and with no active
        # window, setFocus stops emitting focusChanged, so the
        # focus memory below would go deaf. A real window manager
        # re-activates the window when a dialog closes; do the
        # same here before the focus checks run.
        window15.activateWindow()
        qt_app.processEvents()
        app.apply_font_size(0)
        # R21: the checklist's Bigger text button — one tap, saved
        # right away.
        window15.checklist_bigger_text.click()
        qt_app.processEvents()
        check("checklist Bigger text applies the preset font",
              qt_app.font().pointSize()
              == min(20, default_font15 + app.FONT_PRESET_BONUS_PT),
              str(qt_app.font().pointSize()))
        check("checklist Bigger text saves the preset size",
              app.load_app_settings()["font_point_size"]
              == min(20, default_font15 + app.FONT_PRESET_BONUS_PT),
              str(app.load_app_settings()["font_point_size"]))
        app.save_app_settings({"models_folder": "", "ollama_host": "",
                               "default_budget_minutes": 0,
                               "template_sets": {}, "font_point_size": 0,
                               "reduce_motion": False})
        app.apply_font_size(0)
        # R22: Tab in visual order on the setup tab, and focus that
        # comes back where it was after a tab change.
        window15.tabs.setCurrentIndex(window15.TAB_SETUP)
        window15.model_filter.setFocus()
        qt_app.processEvents()
        QTest15.keyClick(window15.model_filter, Qt.Key_Tab)
        qt_app.processEvents()
        check("Tab moves from the model filter to the model list",
              window15.focusWidget() is window15.model_list)
        QTest15.keyClick(window15.focusWidget(), Qt.Key_Tab)
        qt_app.processEvents()
        check("Tab moves from the model list to Select all",
              window15.focusWidget() is window15.select_all_button)
        window15.template_filter.setFocus()
        qt_app.processEvents()
        window15.tabs.setCurrentIndex(window15.TAB_RUN)
        qt_app.processEvents()
        check("a fresh tab focuses its most likely first action",
              window15.focusWidget() is window15.table)
        window15.tabs.setCurrentIndex(window15.TAB_SETUP)
        qt_app.processEvents()
        check("focus returns where it was on the setup tab",
              window15.focusWidget() is window15.template_filter)
        check("Run has its Ctrl+R shortcut",
              window15.run_shortcut.key().toString() == "Ctrl+R",
              window15.run_shortcut.key().toString())
        check("Cancel has its Escape shortcut",
              window15.cancel_shortcut.key().toString() == "Esc",
              window15.cancel_shortcut.key().toString())
        # R23: tick feedback — every tick says what is picked now.
        set_checked_list(window15.model_list, {"fake-smart:2b"})
        qt_app.processEvents()
        check("ticking a model says what is picked",
              window15.status_label.text() == "1 model, 0 templates picked.",
              window15.status_label.text())
        set_checked_templates(window15, {"math"})
        qt_app.processEvents()
        check("ticking a template updates the picked counts",
              window15.status_label.text() == "1 model, 1 template picked.",
              window15.status_label.text())
        window15.clear_models()
        window15.clear_templates()
        qt_app.processEvents()
        check("clearing the ticks says nothing is picked",
              window15.status_label.text() == "0 models, 0 templates picked.",
              window15.status_label.text())
        # R24: the template preview — read a template's tasks first.
        # The dialog is opened through the seam, with exec stubbed so
        # no modal blocks the suite.
        preview_dialog15 = app.TemplatePreviewDialog(window15, math15)
        check("template preview dialog shows the tasks",
              "apples" in preview_dialog15.text_view.toPlainText()
              and "Expected: 21" in preview_dialog15.text_view.toPlainText())
        real_exec15 = app.TemplatePreviewDialog.exec
        exec_calls15 = []
        app.TemplatePreviewDialog.exec = (
            lambda self: exec_calls15.append(True) or QDialog.Rejected)
        try:
            for row15 in range(window15.template_list.count()):
                if (window15.template_list.item(row15).data(Qt.UserRole)
                        == "math"):
                    window15.template_list.setCurrentRow(row15)
                    break
            window15._preview_template()
            check("Preview opens the template preview",
                  len(exec_calls15) == 1, str(len(exec_calls15)))
            window15.template_list.setCurrentRow(-1)
            window15.template_list.clearSelection()
            window15._preview_template()
            check("Preview with no selection opens nothing",
                  len(exec_calls15) == 1, str(len(exec_calls15)))
        finally:
            app.TemplatePreviewDialog.exec = real_exec15
        # R20: the timeline scrub — three fabricated runs for the
        # smart model, newest first.
        scrub_dir15 = os.path.join(
            results15, "fake-smart-2b", window15.sys_id)
        os.makedirs(scrub_dir15, exist_ok=True)
        for stamp15, score15 in (("2026-11-13T10:00:00", 80.0),
                                 ("2026-11-12T10:00:00", 60.0),
                                 ("2026-11-11T10:00:00", 40.0)):
            with open(os.path.join(
                    scrub_dir15,
                    f"{stamp15.replace(':', '-')}-math.json"),
                    "w", encoding="utf-8") as handle15:
                json.dump({"timestamp": stamp15, "template": "math",
                           "template_name": "Math",
                           "template_score": score15,
                           "passed": int(score15 // 20), "total": 5,
                           "avg_decode_tok_s": 20.0}, handle15)
        window15.refresh_history()
        qt_app.processEvents()
        for row15 in range(window15.history_model_combo.count()):
            if (window15.history_model_combo.itemData(row15)
                    == "fake-smart-2b"):
                window15.history_model_combo.setCurrentIndex(row15)
                break
        window15.on_history_model_changed()
        qt_app.processEvents()
        check("scrub slider is enabled with three runs",
              window15.history_scrub_slider.isEnabled())
        check("scrub slider spans the runs",
              window15.history_scrub_slider.maximum() == 2,
              str(window15.history_scrub_slider.maximum()))
        check("scrub readout names the newest run",
              "80%" in window15.history_scrub_label.text(),
              window15.history_scrub_label.text())
        check("hovering a run previews it",
              "80%" in window15.history_list.item(0).toolTip(),
              window15.history_list.item(0).toolTip())
        detail_before15 = window15.history_view.toPlainText()
        window15.history_scrub_slider.setValue(2)
        qt_app.processEvents()
        check("scrubbing to the oldest run updates the readout",
              "40%" in window15.history_scrub_label.text(),
              window15.history_scrub_label.text())
        check("scrubbing moves the highlight",
              window15.history_list.currentRow() == 2,
              str(window15.history_list.currentRow()))
        check("scrubbing does not open the run",
              window15.history_view.toPlainText() == detail_before15)
        window15.history_list.setCurrentRow(1)
        qt_app.processEvents()
        check("picking a run moves the slider to match",
              window15.history_scrub_slider.value() == 1
              and "60%" in window15.history_scrub_label.text(),
              window15.history_scrub_label.text())
        # R24: try one model — ask one question, no run.
        window15.tabs.setCurrentIndex(window15.TAB_MODELS)
        window15.tryit_model_combo.setCurrentText("fake-smart:2b")
        window15.tryit_input.setText("What is 2 plus 2?")
        window15.tryit_ask_button.click()
        answered15 = wait_until(
            lambda: "Asking" not in window15.tryit_answer.toPlainText(),
            timeout=15, app=qt_app)
        check("try-it answers the question",
              answered15
              and len(window15.tryit_answer.toPlainText()) > 0,
              window15.tryit_answer.toPlainText()[:120])
        window15.tryit_input.setText("")
        window15.tryit_ask_button.click()
        qt_app.processEvents()
        check("try-it with no question says so",
              window15.tryit_answer.toPlainText()
              == "Type a question first.",
              window15.tryit_answer.toPlainText())
        # R19: pull upgrades, against a slow pull server — the gaps
        # between pull events stretch to 2 s, so the suite can stop
        # a pull mid-download.
        server15.pull_slow = True
        window15.pull_input.setText("slow-a:1b")
        window15.pull_button.click()
        qt_app.processEvents()
        check("Pull stays enabled while a pull runs",
              window15.pull_button.isEnabled())
        check("Stop pull is enabled while a pull runs",
              window15.pull_cancel_button.isEnabled())
        speed_shown15 = wait_until(
            lambda: "MB/s" in window15.pull_status.text(),
            timeout=12, app=qt_app)
        check("pull progress shows the speed",
              speed_shown15, window15.pull_status.text())
        check("pull progress shows the time left",
              "left" in window15.pull_status.text(),
              window15.pull_status.text())
        window15.pull_cancel_button.click()
        stopped15 = wait_until(
            lambda: "stopped at" in window15.pull_status.text(),
            timeout=10, app=qt_app)
        check("stopping a pull names how far it got",
              stopped15, window15.pull_status.text())
        check("a stopped pull keeps the bar where it stopped",
              window15.pull_bar.value() > 0,
              str(window15.pull_bar.value()))
        check("a stopped pull puts its name back in the box",
              window15.pull_input.text() == "slow-a:1b",
              window15.pull_input.text())
        check("a stopped pull is recorded for a resume",
              "slow-a:1b" in window15.pull_interrupted)
        # The queue: two pulls behind a slow one, a duplicate refused,
        # one removed, and the next one starting when the running
        # pull is stopped.
        window15.pull_input.setText("slow-b:1b")
        window15.pull_button.click()
        window15.pull_input.setText("queued-a:1b")
        window15.pull_button.click()
        qt_app.processEvents()
        check("a pull behind a running pull is queued",
              window15.pull_queue_list.count() == 1
              and window15.pull_queue_list.isVisible(),
              str(window15.pull_queue_list.count()))
        window15.pull_input.setText("queued-a:1b")
        window15.pull_button.click()
        # No processEvents here: the refusal message shares the pull
        # status line with the running pull's progress, and the next
        # progress event would overwrite it before the check reads
        # it. The click sets the message synchronously.
        check("queueing the same model twice is refused",
              "already pulling or queued" in window15.pull_status.text()
              and window15.pull_queue_list.count() == 1,
              window15.pull_status.text())
        window15.pull_input.setText("queued-b:1b")
        window15.pull_button.click()
        qt_app.processEvents()
        check("a second pull queues behind the first",
              window15.pull_queue_list.count() == 2,
              str(window15.pull_queue_list.count()))
        window15.pull_queue_list.setCurrentRow(1)
        window15.pull_queue_remove_button.click()
        qt_app.processEvents()
        check("a queued pull can be removed",
              window15.pull_queue_list.count() == 1,
              str(window15.pull_queue_list.count()))
        window15.pull_cancel_button.click()
        queued_started15 = wait_until(
            lambda: window15.pull_worker is not None
            and window15.pull_worker.name == "queued-a:1b",
            timeout=10, app=qt_app)
        check("stopping a pull starts the next queued pull",
              queued_started15,
              window15.pull_worker.name
              if window15.pull_worker is not None else "no worker")
        window15.pull_cancel_button.click()
        queue_drained15 = wait_until(
            lambda: window15.pull_worker is None,
            timeout=10, app=qt_app)
        check("the queue drains when its pull is stopped",
              queue_drained15
              and window15.pull_queue_list.count() == 0)
        # Resume: the stopped pull picks up where it left off.
        server15.pull_slow = False
        window15.pull_input.setText("slow-a:1b")
        window15.pull_button.click()
        qt_app.processEvents()
        check("re-pulling a stopped pull resumes it",
              window15._pull_resuming is True)
        resumed15 = wait_until(
            lambda: "Pulled slow-a:1b." in window15.pull_status.text(),
            timeout=10, app=qt_app)
        check("a resumed pull completes",
              resumed15, window15.pull_status.text())
        check("a completed resume clears the interrupted record",
              "slow-a:1b" not in window15.pull_interrupted)
        # R24: delete a model — Yes removes it, No leaves it.
        window15.pull_input.setText("del-me:1b")
        window15.pull_button.click()
        pulled15 = wait_until(
            lambda: "Pulled del-me:1b." in window15.pull_status.text(),
            timeout=10, app=qt_app)
        check("a model pulls for the delete test",
              pulled15, window15.pull_status.text())

        def find_installed15(name):
            for row15 in range(window15.installed_list.count()):
                item15 = window15.installed_list.item(row15)
                if item15.data(Qt.UserRole) == name:
                    return row15
            return None

        row15 = find_installed15("del-me:1b")
        check("the pulled model is in the installed list",
              row15 is not None)
        if row15 is not None:
            window15.installed_list.setCurrentRow(row15)
            qt_app.processEvents()
            check("Delete is enabled with a model selected",
                  window15.delete_model_button.isEnabled())
            # The suite-wide question stub answers Yes.
            window15.delete_model_button.click()
            deleted15 = wait_until(
                lambda: "del-me:1b" not in server15.pulled,
                timeout=10, app=qt_app)
            check("deleting a model removes it from Ollama",
                  deleted15)
            check("deleting a model says so",
                  window15.status_label.text() == "Deleted del-me:1b.",
                  window15.status_label.text())
            check("a deleted model leaves the installed list",
                  find_installed15("del-me:1b") is None)
        window15.pull_input.setText("del-no:1b")
        window15.pull_button.click()
        wait_until(lambda: "Pulled del-no:1b." in window15.pull_status.text(),
                   timeout=10, app=qt_app)
        row15 = find_installed15("del-no:1b")
        real_question15 = QMessageBox.question
        QMessageBox.question = lambda *a, **k: QMessageBox.No
        try:
            if row15 is not None:
                window15.installed_list.setCurrentRow(row15)
                qt_app.processEvents()
                window15.delete_model_button.click()
                qt_app.processEvents()
        finally:
            QMessageBox.question = real_question15
        check("answering No keeps the model",
              "del-no:1b" in server15.pulled
              and find_installed15("del-no:1b") is not None)
        # R24: re-run failed only — a real run with the broken model,
        # then a re-run that only re-runs what failed. The preview
        # seam records the pairs each run was asked for.
        preview_calls15 = []
        real_preview15 = app.MainWindow._show_run_preview

        def fake_preview15(self, targets, templates, pairs=None):
            preview_calls15.append(
                (sorted(t["label"] for t in targets),
                 sorted(t["id"] for t in templates),
                 None if pairs is None else sorted(pairs)))
            return True

        app.MainWindow._show_run_preview = fake_preview15
        try:
            set_checked_list(window15.model_list,
                             {"fake-smart:2b", "fake-broken:1b"})
            set_checked_templates(window15, {"math", "coding"})
            window15.run_button.click()
            run_done15 = wait_until(
                lambda: window15.worker is None
                and window15.save_button.isEnabled(),
                timeout=120, app=qt_app)
            check("the re-run test run finishes",
                  run_done15,
                  window15.status_label.text())
            check("the first run asked for the whole run",
                  preview_calls15
                  and preview_calls15[0][2] is None,
                  str(preview_calls15))
            check("Re-run failed only is offered after a failed run",
                  window15.rerun_failed_button.isEnabled())
            check("the finish headline has its tick",
                  window15.finish_label.isVisible()
                  and window15.finish_label.text().startswith("✓"),
                  window15.finish_label.text())
            check("the finish headline fades in",
                  window15.finish_label.graphicsEffect() is not None)
            window15._reduce_motion = True
            window15._show_finish_label(10.0)
            qt_app.processEvents()
            check("reduced motion skips the finish fade",
                  window15.finish_label.graphicsEffect() is None)
            window15._reduce_motion = False
            window15._show_finish_label(10.0)
            qt_app.processEvents()
            check("the finish fade returns with motion on",
                  window15.finish_label.graphicsEffect() is not None)
            check("the first-run hint is gone after a run",
                  not window15.first_run_hint.isVisible())
            window15.rerun_failed_button.click()
            rerun_done15 = wait_until(
                lambda: window15.worker is None
                and window15.save_button.isEnabled(),
                timeout=120, app=qt_app)
            check("the re-run finishes",
                  rerun_done15,
                  window15.status_label.text())
            check("the re-run asked for only the failed pairs",
                  len(preview_calls15) == 2
                  and preview_calls15[1][2]
                  == [("fake-broken:1b", "coding"),
                      ("fake-broken:1b", "math")],
                  str(preview_calls15))
            check("the re-run ran only the failed model",
                  window15.table.rowCount() == 1
                  and window15.table.item(0, 0).text() == "fake-broken:1b",
                  str(window15.table.rowCount()))
        finally:
            app.MainWindow._show_run_preview = real_preview15
    finally:
        window15.close()
        window15.deleteLater()
        qt_app.processEvents()
        server15.shutdown()
        server15.server_close()
        if prior_font15 and prior_font15 > 0:
            app.apply_font_size(prior_font15)
        else:
            app.apply_font_size(0)
        for key15, value15 in prior15.items():
            if value15 is None:
                settings15.remove(key15)
            else:
                settings15.setValue(key15, value15)
        settings15.sync()
        for env15, value15 in (
                ("OLLAMA_HOST", prior_host15),
                ("OST_RESULTS_DIR", prior_results15),
                ("OST_PROVIDERS_FILE", prior_providers15),
                ("OST_CAPABILITIES_FILE", prior_caps15)):
            if value15 is None:
                os.environ.pop(env15, None)
            else:
                os.environ[env15] = value15

    # ================= v16: math scoring rework (M1-M6) ===================
    # ---------- v16 unit: last number, tolerance, words, fractions --------
    # M1: the last number is the answer.
    check("last number scores: wrong working, right conclusion passes",
          app.score_numeric(
              "I first guessed 19, but the answer is 21.", 21)[0])
    check("last number scores: right number in the working, "
          "wrong conclusion fails",
          not app.score_numeric(
              "I first guessed 21, but the answer is 19.", 21)[0])
    check("last number scores: the detail names the final answer",
          app.score_numeric("I guessed 21, but it is 19.", 21)[1]
          == "final answer was 19, expected 21")
    # M2: exact unless the task says otherwise.
    check("exact match: 149 fails the train task",
          not app.score_numeric("149 miles", 150)[0])
    check("exact match: 151 fails the train task",
          not app.score_numeric("151 miles", 150)[0])
    check("exact match: 150 passes the train task",
          app.score_numeric("150 miles", 150)[0])
    check("approximate task still allows 1%",
          app.score_numeric("149 miles", 150, approximate=True)[0]
          and not app.score_numeric("148 miles", 150,
                                    approximate=True)[0])
    check("absolute tolerance allows what it says",
          app.score_numeric("151 miles", 150, tolerance=2)[0]
          and not app.score_numeric("153 miles", 150,
                                    tolerance=2)[0])
    # M5: number words.
    check("number words: twenty-one is 21",
          app._spell_number_words("twenty-one") == "21")
    check("number words: one hundred and fifty is 150",
          app._spell_number_words("one hundred and fifty") == "150")
    check("number words: someone is untouched",
          app._spell_number_words("someone said twenty one things")
          == "someone said 21 things")
    check("number words pass the apples task",
          app.score_numeric("He has twenty-one apples.", 21)[0])
    check("number words pass the train task",
          app.score_numeric(
              "The train travels one hundred and fifty miles.",
              150)[0])
    # M6: fractions and scientific notation.
    check("fractions: 1/2 extracts as 0.5",
          app._extract_numbers("The answer is 1/2.") == [0.5])
    check("fractions: 5/8 extracts as 0.625",
          app._extract_numbers("5/8 of the pizza") == [0.625])
    check("scientific notation: 1.5e3 extracts as 1500",
          app._extract_numbers("1.5e3 miles") == [1500.0])
    check("fraction answer passes the pizza task",
          app.score_numeric("She keeps 5/8 of it.", 0.625)[0])
    check("existing extraction still works",
          app._extract_numbers("1,000 and 2.5") == [1000.0, 2.5]
          and app._extract_numbers("no numbers here") == [])
    # M4: partial credit for right setup.
    taxi16 = {"id": "taxi", "rule": "numeric", "expected": 32.5,
              "steps": [25]}
    check("partial credit: right setup, wrong final scores 0.5",
          app.task_credit(
              taxi16, "The ride costs 25 before the tip, so 30 total.",
              False) == 0.5)
    check("partial credit: fully wrong scores 0",
          app.task_credit(
              taxi16, "The ride costs 20 before the tip, so 30 total.",
              False) == 0.0)
    check("partial credit: all right scores 1",
          app.task_credit(
              taxi16, "The ride costs 25 before the tip, so 32.5 total.",
              True) == 1.0)
    check("partial credit: the task fails unless every part is right",
          app.score_numeric_task(
              "The ride costs 25 before the tip, so 30 total.",
              taxi16)[0] is False
          and app.score_numeric_task(
              "The ride costs 25 before the tip, so 32.5 total.",
              taxi16)[0] is True)
    check("partial credit: the detail names the right step",
          "step 25 right" in app.score_numeric_task(
              "The ride costs 25 before the tip, so 30 total.",
              taxi16)[1])
    check("partial mark shows its credit",
          app.task_mark({"passed": False, "credit": 0.5})
          == "partial (0.5)"
          and app.task_mark({"passed": False, "credit": 0.0}) == "fail"
          and app.task_mark({"passed": True, "credit": 1.0}) == "pass")
    # M3: the template itself.
    templates16, _warnings16 = app.load_templates()
    by_id16 = {t["id"]: t for t in templates16}
    math16 = by_id16["math"]
    check("math template keeps its 5 easy tasks first",
          [t["id"] for t in math16["tasks"][:5]]
          == ["apples", "train", "percent", "stickers", "rectangle"])
    check("math template has a fraction answer, algebra, "
          "and a unit conversion",
          any(t["id"] == "pizza" and t["expected"] == 0.625
              for t in math16["tasks"])
          and any(t["id"] == "solve-x" and t["expected"] == 5
                  for t in math16["tasks"])
          and any(t["id"] == "milliliters" and t["expected"] == 2500
                  for t in math16["tasks"]))
    check("math step tasks carry their intermediate values",
          any(t["id"] == "muffins" and t.get("steps") == [48]
              for t in math16["tasks"])
          and any(t["id"] == "taxi" and t.get("steps") == [25]
                  for t in math16["tasks"]))

    # ---------- v16 GUI: fake-run separation + partial credit ------------
    # A real run through the window: smart, partial, and dumb on math
    # only. The template must separate them (100/50/0), and a
    # partial-credit answer must score half through the worker, the
    # run file, the summary, and the detail view.
    results16 = os.path.join(SCRATCH, "results16")
    os.makedirs(results16, exist_ok=True)
    prior_results16 = os.environ.get("OST_RESULTS_DIR")
    prior_host16 = os.environ.get("OLLAMA_HOST")
    server16, thread16 = fake_ollama.start_server()
    host16, port16 = server16.server_address
    os.environ["OLLAMA_HOST"] = f"http://{host16}:{port16}"
    os.environ["OST_RESULTS_DIR"] = results16
    window16 = None
    try:
        window16 = app.MainWindow()
        window16.show()
        wait_until(lambda: window16.model_list.count() >= 3,
                   timeout=10, app=qt_app)
        set_checked_list(window16.model_list,
                         {"fake-smart:2b", "fake-partial:3b",
                          "fake-dumb:1b"})
        set_checked_templates(window16, {"math"})
        window16.run_button.click()
        done16 = wait_until(
            lambda: window16.worker is None
            and window16.save_button.isEnabled(),
            timeout=180, app=qt_app)
        check("v16 math run completes", done16)
        smart16 = window16.results.get("fake-smart:2b", {}).get(
            "templates", {}).get("math", {})
        partial16 = window16.results.get("fake-partial:3b", {}).get(
            "templates", {}).get("math", {})
        dumb16 = window16.results.get("fake-dumb:1b", {}).get(
            "templates", {}).get("math", {})
        check("math separates smart from partial from dumb",
              smart16.get("template_score") == 100.0
              and partial16.get("template_score") == 50.0
              and dumb16.get("template_score") == 0.0,
              f"{smart16.get('template_score')} / "
              f"{partial16.get('template_score')} / "
              f"{dumb16.get('template_score')}")
        check("math run records 10 tasks per model",
              smart16.get("total") == 10
              and partial16.get("total") == 10
              and dumb16.get("total") == 10,
              str(smart16.get("total")))
        check("math run records passed counts as credits",
              smart16.get("passed") == 10
              and partial16.get("passed") == 5
              and dumb16.get("passed") == 0,
              f"{smart16.get('passed')} / {partial16.get('passed')} / "
              f"{dumb16.get('passed')}")

        # Partial credit end to end: a canned answer with the right
        # step and the wrong final, through a real worker run. The
        # probe prompt matches no earlier fake key, so the appended
        # answer is the one the smart brain gives.
        fake_ollama.SMART_ANSWERS.append(
            ("partial-credit probe",
             "The fare is 25 before the tip, so 30 in total."))
        probe_template16 = {
            "id": "math-partial", "name": "Math partial",
            "num_predict": 300, "scored": True,
            "tasks": [{
                "id": "taxi", "rule": "numeric", "expected": 32.5,
                "steps": [25],
                "prompt": "A partial-credit probe: the fare is 25 "
                          "before the tip, so 30 in total. What is "
                          "the total?",
            }],
        }
        probe_results16 = {}
        probe_finished16 = {"done": False}
        probe_worker16 = app.BenchWorker(
            [app.ollama_target("fake-smart:2b")],
            [probe_template16], window16.sys_id, window16.machine,
            results16)
        probe_worker16.model_done.connect(
            lambda name, result: probe_results16.__setitem__(
                name, result))
        probe_worker16.run_finished.connect(
            lambda reason: probe_finished16.__setitem__("done", reason))
        probe_worker16.start()
        probe_ok16 = wait_until(lambda: probe_finished16["done"],
                                timeout=120, app=qt_app)
        probe_worker16.wait()
        check("partial-credit worker run finishes", probe_ok16)
        probe_math16 = probe_results16.get(
            "fake-smart:2b", {}).get("templates", {}).get(
                "math-partial", {})
        check("partial-credit run scores 50% with passed 0.5",
              probe_math16.get("template_score") == 50.0
              and probe_math16.get("passed") == 0.5
              and probe_math16.get("total") == 1,
              f"{probe_math16.get('template_score')} / "
              f"{probe_math16.get('passed')} / "
              f"{probe_math16.get('total')}")
        probe_tasks16 = probe_math16.get("tasks", [])
        check("partial-credit task record carries credit 0.5",
              len(probe_tasks16) == 1
              and probe_tasks16[0].get("credit") == 0.5
              and probe_tasks16[0].get("passed") is False,
              str(probe_tasks16))
        summary16 = app.render_run_summary(probe_math16)
        check("summary marks a partial task with its credit",
              "partial (0.5)  taxi" in summary16, summary16)
        window16.results["probe-model"] = probe_results16.get(
            "fake-smart:2b", {})
        window16.show_detail("probe-model")
        qt_app.processEvents()
        check("detail view marks a partial task with its credit",
              "partial (0.5)  taxi"
              in window16.detail_view.toPlainText(),
              window16.detail_view.toPlainText()[:400])
    finally:
        fake_ollama.SMART_ANSWERS[:] = [
            pair for pair in fake_ollama.SMART_ANSWERS
            if pair[0] != "partial-credit probe"
        ]
        if window16 is not None:
            window16.close()
            window16.deleteLater()
            qt_app.processEvents()
        server16.shutdown()
        server16.server_close()
        if prior_results16 is None:
            os.environ.pop("OST_RESULTS_DIR", None)
        else:
            os.environ["OST_RESULTS_DIR"] = prior_results16
        if prior_host16 is None:
            os.environ.pop("OLLAMA_HOST", None)
        else:
            os.environ["OLLAMA_HOST"] = prior_host16

    # ================= v17: Ollama starts itself, Speed scores, resume =====
    # ---------- v17 unit: speed credit, local address, loader, manifest ---
    check("speed_credit 0 tok/s is 0",
          app.speed_credit(0) == 0.0, str(app.speed_credit(0)))
    check("speed_credit 10 tok/s is 0.5",
          app.speed_credit(10) == 0.5, str(app.speed_credit(10)))
    check("speed_credit 20 tok/s is 1.0",
          app.speed_credit(20) == 1.0, str(app.speed_credit(20)))
    check("speed_credit 40 tok/s caps at 1.0",
          app.speed_credit(40) == 1.0, str(app.speed_credit(40)))
    check("speed_credit None (failed job) is 0",
          app.speed_credit(None) == 0.0, str(app.speed_credit(None)))
    check("speed full-score anchor is 20 tok/s",
          app.SPEED_FULL_SCORE_TOK_S == 20.0,
          str(app.SPEED_FULL_SCORE_TOK_S))
    prior_host17 = os.environ.get("OLLAMA_HOST")
    try:
        os.environ.pop("OLLAMA_HOST", None)
        check("local address: no host set is local",
              app.ollama_address_is_local())
        os.environ["OLLAMA_HOST"] = "http://127.0.0.1:11434"
        check("local address: 127.0.0.1 is local",
              app.ollama_address_is_local())
        os.environ["OLLAMA_HOST"] = "http://localhost:11434"
        check("local address: localhost is local",
              app.ollama_address_is_local())
        os.environ["OLLAMA_HOST"] = "http://[::1]:11434"
        check("local address: ::1 is local", app.ollama_address_is_local())
        os.environ["OLLAMA_HOST"] = "http://192.168.1.5:11434"
        check("local address: a LAN address is not local",
              not app.ollama_address_is_local())
        os.environ["OLLAMA_HOST"] = "http://ollama.invalid:11434"
        check("local address: a remote name is not local",
              not app.ollama_address_is_local())
    finally:
        if prior_host17 is None:
            os.environ.pop("OLLAMA_HOST", None)
        else:
            os.environ["OLLAMA_HOST"] = prior_host17
    # The loader accepts score_by "speed" and drops anything else.
    templates17 = os.path.join(SCRATCH, "templates17")
    os.makedirs(templates17, exist_ok=True)
    with open(os.path.join(templates17, "good.json"), "w",
              encoding="utf-8") as handle:
        json.dump({"id": "good-speed", "name": "Good Speed",
                   "scored": False, "context_lengths": [1024],
                   "score_by": "speed"}, handle)
    with open(os.path.join(templates17, "bad.json"), "w",
              encoding="utf-8") as handle:
        json.dump({"id": "bad-speed", "name": "Bad Speed",
                   "scored": False, "context_lengths": [1024],
                   "score_by": "bogus"}, handle)
    loaded17, warns17 = app.load_templates(templates17)
    check("loader keeps a speed-scored template",
          any(t["id"] == "good-speed"
              and t.get("score_by") == "speed" for t in loaded17),
          str(loaded17))
    check("loader drops an unknown score_by with a warning",
          not any(t["id"] == "bad-speed" for t in loaded17)
          and any("score_by" in w for w in warns17), str(warns17))
    # The real Speed template carries score_by and stays unscored.
    speed17 = next((t for t in templates if t["id"] == "speed"), None)
    check("speed template has score_by speed and stays unscored",
          speed17 is not None and speed17.get("score_by") == "speed"
          and speed17["scored"] is False, str(speed17))
    # meminfo: the fixture reads 16 GB; a missing file reads None.
    check("meminfo fixture reads 16 GB",
          app.mem_available_bytes() == 16777216 * 1024,
          str(app.mem_available_bytes()))
    prior_meminfo17 = os.environ.get("OST_MEMINFO_FILE")
    try:
        os.environ["OST_MEMINFO_FILE"] = os.path.join(
            SCRATCH, "meminfo-missing")
        check("meminfo missing reads None",
              app.mem_available_bytes() is None)
    finally:
        if prior_meminfo17 is None:
            os.environ.pop("OST_MEMINFO_FILE", None)
        else:
            os.environ["OST_MEMINFO_FILE"] = prior_meminfo17
    # Manifest helpers: round-trip, resumable, latest, plan, merge.
    results17u = os.path.join(SCRATCH, "results17-unit")
    os.makedirs(results17u, exist_ok=True)
    targets17u = [
        {"label": "fake-smart:2b", "kind": "ollama",
         "model": "fake-smart:2b", "provider_name": None},
        {"label": "gone-model:9b", "kind": "ollama",
         "model": "gone-model:9b", "provider_name": None},
    ]
    templates17u = [{"id": "coding", "name": "Coding"},
                    {"id": "math", "name": "Math"}]
    manifest17u = app.new_manifest("20261115-120000", targets17u,
                                   templates17u, "sys-unit")
    check("new manifest has every pair pending",
          manifest17u["pairs"]
          == {"fake-smart:2b|coding": "pending",
              "fake-smart:2b|math": "pending",
              "gone-model:9b|coding": "pending",
              "gone-model:9b|math": "pending"},
          str(manifest17u["pairs"]))
    check("manifest targets carry no provider entry",
          all("provider" not in entry
              for entry in manifest17u["targets"]),
          str(manifest17u["targets"]))
    check("a manifest with pending pairs is resumable",
          app.manifest_is_resumable(manifest17u))
    app.save_manifest(results17u, manifest17u)
    reloaded17u = app.load_manifest(results17u, "20261115-120000")
    check("manifest round-trips through disk",
          reloaded17u is not None
          and reloaded17u["pairs"] == manifest17u["pairs"],
          str(reloaded17u))
    check("load_manifest returns None for an unknown id",
          app.load_manifest(results17u, "no-such-run") is None)
    check("the _runs folder never lists as a model",
          "_runs" not in app.list_history_models(results17u),
          str(app.list_history_models(results17u)))
    done17u = app.new_manifest("20261115-110000", targets17u[:1],
                               templates17u, "sys-unit")
    done17u["pairs"] = {key: "done" for key in done17u["pairs"]}
    done17u["status"] = "done"
    app.save_manifest(results17u, done17u)
    check("a manifest with all pairs done is not resumable",
          not app.manifest_is_resumable(done17u))
    check("latest resumable manifest is the unfinished one",
          (app.latest_resumable_manifest(results17u) or {}).get("run_id")
          == "20261115-120000")
    # The plan: gone pairs skipped with a note, the rest to run.
    smart_target17u = app.ollama_target("fake-smart:2b", {"text"})
    plan_targets17u, plan_pairs17u, plan_pre17u, plan_notes17u = (
        app.build_resume_plan(
            manifest17u, {"fake-smart:2b": smart_target17u},
            {"coding": templates17u[0], "math": templates17u[1]},
            results17u))
    check("resume plan runs only the available pairs",
          plan_pairs17u == {("fake-smart:2b", "coding"),
                            ("fake-smart:2b", "math")},
          str(plan_pairs17u))
    check("resume plan notes the missing model",
          any("gone-model:9b" in note and "no longer installed" in note
              for note in plan_notes17u), str(plan_notes17u))
    check("resume plan marks the gone pairs skipped in the manifest",
          manifest17u["pairs"]["gone-model:9b|coding"] == "skipped"
          and manifest17u["pairs"]["gone-model:9b|math"] == "skipped",
          str(manifest17u["pairs"]))
    check("resume plan targets are only the models with pairs",
          [t["label"] for t in plan_targets17u] == ["fake-smart:2b"],
          str(plan_targets17u))
    # Merge: done pairs fold into the result, scores recomputed.
    result17u = {
        "name": "fake-smart:2b", "status": "done",
        "templates": {"coding": {
            "template": "coding", "template_name": "Coding",
            "template_score": 100.0, "tasks": [
                {"decode_tok_s": 20.0, "ttft_s": 0.1,
                 "prompt_tok_s": 40.0}],
            "prompt_tokens": 10, "completion_tokens": 20,
            "total_tokens": 30, "estimated_cost_usd": None}},
        "overall_score": 100.0, "avg_decode_tok_s": 20.0,
        "avg_ttft_s": 0.1, "avg_prompt_tok_s": 40.0,
        "prompt_tokens": 10, "completion_tokens": 20,
        "total_tokens": 30, "estimated_cost_usd": None,
    }
    app.merge_resume_result(result17u, {"math": {
        "template": "math", "template_name": "Math",
        "template_score": 50.0, "tasks": [
            {"decode_tok_s": 10.0, "ttft_s": 0.2,
             "prompt_tok_s": 20.0}],
        "prompt_tokens": 5, "completion_tokens": 5,
        "total_tokens": 10, "estimated_cost_usd": None}})
    check("merge folds the done pairs into the result",
          set(result17u["templates"]) == {"coding", "math"}
          and result17u["overall_score"] == 75.0
          and result17u["avg_decode_tok_s"] == 15.0
          and result17u["total_tokens"] == 40,
          str(result17u.get("overall_score")))

    # ---------- v17 GUI: restart, RAM check, resume, headless --resume ----
    results17 = os.path.join(SCRATCH, "results17")
    os.makedirs(results17, exist_ok=True)
    prior_results17 = os.environ.get("OST_RESULTS_DIR")
    prior_host17b = os.environ.get("OLLAMA_HOST")
    server17, thread17 = fake_ollama.start_server()
    host17, port17 = server17.server_address
    os.environ["OLLAMA_HOST"] = f"http://{host17}:{port17}"
    os.environ["OST_RESULTS_DIR"] = results17
    window17 = None
    window17b = None
    try:
        window17 = app.MainWindow()
        window17.show()
        wait_until(lambda: window17.model_list.count() == 4,
                   timeout=10, app=qt_app)
        check("v17 window lists the four fake models",
              window17.model_list.count() == 4,
              str(window17.model_list.count()))
        speed_label17 = None
        for row17 in range(window17.template_list.count()):
            item17 = window17.template_list.item(row17)
            if item17.data(Qt.UserRole) == "speed":
                speed_label17 = item17.text()
        check("template list labels Speed with its lengths",
              speed_label17 == "Speed (3 lengths)", str(speed_label17))
        check("Resume disabled with no run yet",
              not window17.resume_button.isEnabled())

        # Mid-run restart, in its own results dir so its unfinished
        # manifest does not linger into the resume checks.
        results17restart = os.path.join(SCRATCH, "results17-restart")
        os.makedirs(results17restart, exist_ok=True)
        os.environ["OST_RESULTS_DIR"] = results17restart
        real_start17 = app.start_ollama_process
        restart_calls17 = []

        def _revive17(models_folder=None):
            restart_calls17.append(models_folder)
            server17.dead = False
            return True, ""

        try:
            app.start_ollama_process = _revive17
            server17.dead = True
            set_checked_list(window17.model_list, {"fake-smart:2b"})
            set_checked_templates(window17, {"coding"})
            window17.run_button.click()
            done17r = wait_until(
                lambda: window17.worker is None
                and window17.save_button.isEnabled(),
                timeout=120, app=qt_app)
            check("restart run completes after one death", done17r)
            check("restart run status is Done",
                  "Done." in window17.status_label.text(),
                  window17.status_label.text())
            smart_row17 = row_for(window17, "fake-smart:2b")
            check("restart run model completes",
                  smart_row17 is not None
                  and table_text(window17, smart_row17, 5) == "done",
                  table_text(window17, smart_row17, 5)
                  if smart_row17 is not None else "no row")
            check("restart was attempted exactly once",
                  len(restart_calls17) == 1, str(len(restart_calls17)))
        finally:
            app.start_ollama_process = real_start17
            server17.dead = False

        restart_calls17b = []

        def _revive_then_die17(models_folder=None):
            restart_calls17b.append(models_folder)
            server17.dead = False

            def _die_later():
                time.sleep(0.3)
                server17.dead = True

            threading.Thread(target=_die_later, daemon=True).start()
            return True, ""

        try:
            app.start_ollama_process = _revive_then_die17
            server17.dead = True
            set_checked_list(window17.model_list, {"fake-smart:2b"})
            set_checked_templates(window17, {"coding"})
            window17.run_button.click()
            done17r2 = wait_until(
                lambda: window17.worker is None
                and window17.run_button.isEnabled(),
                timeout=120, app=qt_app)
            check("second-death run finishes", done17r2)
            check("second-death run status is the Ollama-died message",
                  window17.status_label.text() == app.OLLAMA_DIED_MESSAGE,
                  window17.status_label.text())
            check("second death did not trigger a second restart",
                  len(restart_calls17b) == 1, str(len(restart_calls17b)))
        finally:
            app.start_ollama_process = real_start17
            server17.dead = False
        os.environ["OST_RESULTS_DIR"] = results17

        # RAM check: too big is skipped, the run carries on.
        results17ram = os.path.join(SCRATCH, "results17-ram")
        os.makedirs(results17ram, exist_ok=True)
        meminfo_small17 = os.path.join(SCRATCH, "meminfo-small")
        with open(meminfo_small17, "w", encoding="utf-8") as handle:
            handle.write("MemTotal:       16777216 kB\n"
                         "MemAvailable:    1572864 kB\n")
        prior_meminfo17b = os.environ.get("OST_MEMINFO_FILE")
        try:
            os.environ["OST_MEMINFO_FILE"] = meminfo_small17
            os.environ["OST_RESULTS_DIR"] = results17ram
            set_checked_list(window17.model_list,
                             {"fake-smart:2b", "fake-dumb:1b"})
            set_checked_templates(window17, {"math"})
            window17.run_button.click()
            done17ram = wait_until(
                lambda: window17.worker is None
                and window17.save_button.isEnabled(),
                timeout=120, app=qt_app)
            check("RAM-skip run completes", done17ram)
            smart_row17ram = row_for(window17, "fake-smart:2b")
            check("model too big for free RAM is skipped, not failed",
                  smart_row17ram is not None
                  and table_text(window17, smart_row17ram, 5).startswith(
                      "skipped: needs"),
                  table_text(window17, smart_row17ram, 5)
                  if smart_row17ram is not None else "no row")
            dumb_row17ram = row_for(window17, "fake-dumb:1b")
            check("the run carries on with the next model",
                  dumb_row17ram is not None
                  and table_text(window17, dumb_row17ram, 5) == "done",
                  table_text(window17, dumb_row17ram, 5)
                  if dumb_row17ram is not None else "no row")
            check("a skipped model writes no run files",
                  not os.path.isdir(
                      os.path.join(results17ram, "fake-smart_2b")))
            # Unreadable meminfo: the check stays off, the run proceeds.
            os.environ["OST_MEMINFO_FILE"] = os.path.join(
                SCRATCH, "meminfo-missing")
            set_checked_list(window17.model_list, {"fake-smart:2b"})
            set_checked_templates(window17, {"math"})
            window17.run_button.click()
            done17ram2 = wait_until(
                lambda: window17.worker is None
                and window17.save_button.isEnabled(),
                timeout=120, app=qt_app)
            smart_row17ram2 = row_for(window17, "fake-smart:2b")
            check("unreadable meminfo runs the model anyway",
                  done17ram2 and smart_row17ram2 is not None
                  and table_text(window17, smart_row17ram2, 5) == "done",
                  table_text(window17, smart_row17ram2, 5)
                  if smart_row17ram2 is not None else "no row")
        finally:
            if prior_meminfo17b is None:
                os.environ.pop("OST_MEMINFO_FILE", None)
            else:
                os.environ["OST_MEMINFO_FILE"] = prior_meminfo17b
            os.environ["OST_RESULTS_DIR"] = results17

        # Resume: cancel after one pair, resume runs only the rest.
        set_checked_list(window17.model_list, {"fake-smart:2b"})
        set_checked_templates(window17, {"coding", "math"})
        window17.run_button.click()
        coding_done17 = wait_until(
            lambda: "coding" in window17.results.get(
                "fake-smart:2b", {}).get("templates", {}),
            timeout=60, app=qt_app)
        check("first pair finished before the cancel", coding_done17)
        if window17.worker is not None:
            window17.worker.request_cancel()
        cancelled17 = wait_until(
            lambda: window17.worker is None
            and window17.run_button.isEnabled(),
            timeout=60, app=qt_app)
        check("cancelled run ends", cancelled17)
        manifest17a = app.latest_resumable_manifest(results17)
        check("a cancelled run leaves a resumable manifest",
              manifest17a is not None
              and manifest17a["pairs"].get("fake-smart:2b|coding") == "done"
              and manifest17a["pairs"].get("fake-smart:2b|math") == "pending",
              str((manifest17a or {}).get("pairs")))
        check("Resume button offers the unfinished run",
              window17.resume_button.isEnabled()
              and window17.resume_button.text()
              == "Resume last run (1 of 2 pairs left)",
              window17.resume_button.text())
        gen_before_resume17a = server17.generate_count
        window17.resume_button.click()
        resumed17a = wait_until(
            lambda: window17.worker is None
            and window17.save_button.isEnabled(),
            timeout=120, app=qt_app)
        check("resumed run completes", resumed17a)
        check("resume ran only the missing pair",
              server17.generate_count - gen_before_resume17a == 11,
              str(server17.generate_count - gen_before_resume17a))
        smart17a = window17.results.get("fake-smart:2b", {})
        check("resumed run shows the whole run, not just the resumed part",
              set(smart17a.get("templates", {})) == {"coding", "math"}
              and smart17a.get("overall_score") == 100.0,
              str(sorted(smart17a.get("templates", {}))))
        check("Resume disabled once the run is finished",
              not window17.resume_button.isEnabled())

        # Resume: failed pairs are retried, done pairs are not.
        set_checked_list(window17.model_list,
                         {"fake-smart:2b", "fake-broken:1b"})
        set_checked_templates(window17, {"coding"})
        window17.run_button.click()
        done17b = wait_until(
            lambda: window17.worker is None
            and window17.save_button.isEnabled(),
            timeout=120, app=qt_app)
        check("failed-pairs run completes", done17b)
        manifest17b = app.latest_resumable_manifest(results17)
        check("a failed pair leaves a resumable manifest",
              manifest17b is not None
              and manifest17b["pairs"].get("fake-broken:1b|coding") == "failed"
              and manifest17b["pairs"].get("fake-smart:2b|coding") == "done",
              str((manifest17b or {}).get("pairs")))
        smart_dir17b = os.path.join(results17, "fake-smart_2b", sid)
        smart_files17b = sorted(os.listdir(smart_dir17b))
        smart_hash17b = file_hash(os.path.join(
            smart_dir17b,
            [f for f in smart_files17b if f.endswith("-coding.json")][-1]))
        gen_before_resume17b = server17.generate_count
        window17.resume_button.click()
        resumed17b = wait_until(
            lambda: window17.worker is None
            and window17.save_button.isEnabled(),
            timeout=120, app=qt_app)
        check("failed-pairs resume completes", resumed17b)
        # v18 (RV4): the expectation was 2 — warmup plus the task.
        # A model that fails its warmup never runs a task (a model
        # that cannot say hello cannot do the templates), so a
        # resumed failed pair costs exactly the warmup call: 1.
        # The warmup-fail-fast behaviour is deliberate; the count
        # was the stale part.
        check("resume retried only the failed pair",
              server17.generate_count - gen_before_resume17b == 1,
              str(server17.generate_count - gen_before_resume17b))
        smart_files17b_after = sorted(os.listdir(smart_dir17b))
        check("the done pair was not re-run",
              smart_files17b_after == smart_files17b
              and file_hash(os.path.join(
                  smart_dir17b,
                  [f for f in smart_files17b_after
                   if f.endswith("-coding.json")][-1]))
              == smart_hash17b,
              str(smart_files17b_after))

        # Resume works across a fresh window object.
        window17.close()
        window17.deleteLater()
        qt_app.processEvents()
        window17 = None
        window17b = app.MainWindow()
        window17b.show()
        wait_until(lambda: window17b.model_list.count() == 4,
                   timeout=10, app=qt_app)
        check("a fresh window offers the unfinished run",
              window17b.resume_button.isEnabled()
              and window17b.resume_button.text()
              == "Resume last run (1 of 2 pairs left)",
              window17b.resume_button.text())
        window17b.resume_button.click()
        resumed17c = wait_until(
            lambda: window17b.worker is None
            and window17b.save_button.isEnabled(),
            timeout=120, app=qt_app)
        check("fresh-window resume completes", resumed17c)
        smart17c = window17b.results.get("fake-smart:2b", {})
        check("fresh-window resume shows the whole run",
              set(smart17c.get("templates", {})) == {"coding"}
              and smart17c.get("overall_score") == 100.0,
              str(sorted(smart17c.get("templates", {}))))
        broken17c = window17b.results.get("fake-broken:1b", {})
        check("fresh-window resume retried the failed model",
              broken17c.get("status", "").startswith("failed"),
              str(broken17c.get("status")))

        # Resume skips a deleted model, plainly.
        results17c = os.path.join(SCRATCH, "results17-crafted")
        os.makedirs(results17c, exist_ok=True)
        manifest17c = app.new_manifest(
            "20261115-130000",
            [{"label": "fake-smart:2b", "kind": "ollama",
              "model": "fake-smart:2b", "provider_name": None},
             {"label": "gone-model:9b", "kind": "ollama",
              "model": "gone-model:9b", "provider_name": None}],
            [{"id": "coding", "name": "Coding"}], "sys-unit")
        app.save_manifest(results17c, manifest17c)
        os.environ["OST_RESULTS_DIR"] = results17c
        window17b.refresh_models()
        wait_until(lambda: window17b.resume_button.isEnabled(),
                   timeout=10, app=qt_app)
        check("Resume offers a crafted run with a deleted model",
              window17b.resume_button.isEnabled()
              and window17b.resume_button.text()
              == "Resume last run (2 of 2 pairs left)",
              window17b.resume_button.text())
        window17b.resume_button.click()
        resumed17d = wait_until(
            lambda: window17b.worker is None
            and window17b.save_button.isEnabled(),
            timeout=120, app=qt_app)
        check("crafted resume completes", resumed17d)
        check("crafted resume ran only the installed model",
              window17b.table.rowCount() == 1
              and window17b.table.item(0, 0).text() == "fake-smart:2b",
              str(window17b.table.rowCount()))
        check("crafted resume marked the gone pair skipped",
              (app.load_manifest(results17c, "20261115-130000") or {})
              .get("pairs", {}).get("gone-model:9b|coding") == "skipped")
        os.environ["OST_RESULTS_DIR"] = results17

        # Headless --resume, while the fake server is still up.
        help17 = subprocess.run(
            [sys.executable, "-u", "ollama_system_test.py", "--headless",
             "--help"],
            cwd=PKG_DIR, env=dict(os.environ),
            capture_output=True, text=True, timeout=120)
        check("--help documents --resume",
              "--resume" in help17.stdout, help17.stdout)
        empty17 = os.path.join(SCRATCH, "results17-empty")
        os.makedirs(empty17, exist_ok=True)

        def headless17_env(results_dir):
            env = dict(os.environ)
            env.pop("DISPLAY", None)
            env.pop("QT_QPA_PLATFORM", None)
            env["OLLAMA_HOST"] = f"http://{host17}:{port17}"
            env["OST_RESULTS_DIR"] = results_dir
            env["OST_PROVIDERS_FILE"] = os.path.join(
                SCRATCH, "headless17-providers.json")
            env["OST_CAPABILITIES_FILE"] = os.path.join(
                SCRATCH, "headless17-caps.json")
            return env

        no_resume17 = subprocess.run(
            [sys.executable, "-u", "ollama_system_test.py", "--headless",
             "--resume"],
            cwd=PKG_DIR, env=headless17_env(empty17),
            capture_output=True, text=True, timeout=120)
        check("headless --resume with no run exits 1, plainly",
              no_resume17.returncode == 1
              and "No run to resume." in no_resume17.stdout
              and "Traceback" not in no_resume17.stderr,
              f"exit {no_resume17.returncode}\n{no_resume17.stdout[-300:]}")
        bad_id17 = subprocess.run(
            [sys.executable, "-u", "ollama_system_test.py", "--headless",
             "--resume", "no-such-run"],
            cwd=PKG_DIR, env=headless17_env(results17),
            capture_output=True, text=True, timeout=120)
        check("headless --resume with a bad id exits 1, plainly",
              bad_id17.returncode == 1
              and "No run with id no-such-run." in bad_id17.stdout
              and "Traceback" not in bad_id17.stderr,
              f"exit {bad_id17.returncode}\n{bad_id17.stdout[-300:]}")
        resume17h = subprocess.run(
            [sys.executable, "-u", "ollama_system_test.py", "--headless",
             "--resume"],
            cwd=PKG_DIR, env=headless17_env(results17),
            capture_output=True, text=True, timeout=300)
        check("headless --resume completes the run",
              resume17h.returncode == 0
              and "Resuming the run from" in resume17h.stdout
              and "Traceback" not in resume17h.stderr,
              f"exit {resume17h.returncode}\n"
              f"{resume17h.stdout[-400:]}\n{resume17h.stderr[-400:]}")
        run_id17b = (manifest17b or {}).get("run_id")
        specific17 = subprocess.run(
            [sys.executable, "-u", "ollama_system_test.py", "--headless",
             "--resume", run_id17b],
            cwd=PKG_DIR, env=headless17_env(results17),
            capture_output=True, text=True, timeout=300)
        check("headless --resume <run-id> resumes that run",
              specific17.returncode == 0
              and "Resuming the run from" in specific17.stdout,
              f"exit {specific17.returncode}\n"
              f"{specific17.stdout[-400:]}")
    finally:
        if window17 is not None:
            window17.close()
            window17.deleteLater()
        if window17b is not None:
            window17b.close()
            window17b.deleteLater()
        qt_app.processEvents()
        server17.shutdown()
        server17.server_close()
        if prior_results17 is None:
            os.environ.pop("OST_RESULTS_DIR", None)
        else:
            os.environ["OST_RESULTS_DIR"] = prior_results17
        if prior_host17b is None:
            os.environ.pop("OLLAMA_HOST", None)
        else:
            os.environ["OLLAMA_HOST"] = prior_host17b

    # ---------- v17: refresh auto-starts Ollama, pulls queue behind it -----
    # A fake ollama that serves but lists nothing: refresh has no CLI
    # fallback, so the auto-start is the only way the models appear.
    fakebin_nolist17 = os.path.join(SCRATCH, "fakebin-nolist")
    os.makedirs(fakebin_nolist17, exist_ok=True)
    with open(os.path.join(FAKEBIN, "ollama"), encoding="utf-8") as handle:
        fakebin_src17 = handle.read()
    fakebin_src17 = fakebin_src17.replace(
        '        print("NAME ID SIZE MODIFIED")\n'
        '        print("cli-model-a:2b abcdef 1.5 GB 2 days ago")\n'
        '        print("cli-model-b:1b 123456 900 MB 3 days ago")\n',
        "")
    nolist_path17 = os.path.join(fakebin_nolist17, "ollama")
    with open(nolist_path17, "w", encoding="utf-8") as handle:
        handle.write(fakebin_src17)
    os.chmod(nolist_path17, 0o755)
    prior_path17 = os.environ.get("PATH")
    prior_host17c = os.environ.get("OLLAMA_HOST")
    window17r = None
    window17p = None
    try:
        pidfile17r = os.path.join(SCRATCH, "fake-ollama17r.pid")
        os.environ["FAKE_OLLAMA_PIDFILE"] = pidfile17r
        os.environ["PATH"] = fakebin_nolist17 + os.pathsep + ORIGINAL_PATH
        os.environ["OLLAMA_HOST"] = f"http://127.0.0.1:{free_port()}"
        window17r = app.MainWindow()
        window17r.show()
        started17r = wait_until(
            lambda: window17r.model_list.count() == 2
            and window17r.status_label.text() == "Ollama is running.",
            timeout=45, app=qt_app)
        check("refresh auto-starts Ollama with no click", started17r,
              window17r.status_label.text())
        served17r = {window17r.model_list.item(r).text()
                     for r in range(window17r.model_list.count())}
        check("auto-started Ollama lists its models",
              served17r == {"serve-model-a:2b", "serve-model-b:1b"},
              str(served17r))
        kill_pidfile(pidfile17r)
        window17r.close()
        window17r.deleteLater()
        window17r = None
        qt_app.processEvents()

        # A pull with Ollama down queues behind the auto-start, then
        # runs: the fake serve has no /api/pull, so it fails plainly
        # — the point is the pull was attempted, not blocked.
        pidfile17p = os.path.join(SCRATCH, "fake-ollama17p.pid")
        os.environ["FAKE_OLLAMA_PIDFILE"] = pidfile17p
        os.environ["PATH"] = FAKEBIN + os.pathsep + ORIGINAL_PATH
        os.environ["OLLAMA_HOST"] = f"http://127.0.0.1:{free_port()}"
        window17p = app.MainWindow()
        window17p.show()
        wait_until(lambda: window17p.model_list.count() == 2,
                   timeout=10, app=qt_app)
        window17p.pull_input.setText("pull-model:1b")
        window17p.pull_button.click()
        pulled17 = wait_until(
            lambda: window17p.pull_status.text().startswith("Pull failed:"),
            timeout=45, app=qt_app)
        check("a pull queues behind the auto-start, then runs",
              pulled17, window17p.pull_status.text())
        served17p = {window17p.model_list.item(r).text()
                     for r in range(window17p.model_list.count())}
        check("the pull's auto-start listed the served models",
              served17p == {"serve-model-a:2b", "serve-model-b:1b"},
              str(served17p))
        kill_pidfile(pidfile17p)
    finally:
        if window17r is not None:
            window17r.close()
            window17r.deleteLater()
        if window17p is not None:
            window17p.close()
            window17p.deleteLater()
        qt_app.processEvents()
        os.environ.pop("FAKE_OLLAMA_PIDFILE", None)
        if prior_path17 is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = prior_path17
        if prior_host17c is None:
            os.environ.pop("OLLAMA_HOST", None)
        else:
            os.environ["OLLAMA_HOST"] = prior_host17c

    # ---------- v18: review fixes RV1-RV13 -------------------------------
    # RV1: a slow first token no longer kills the run. The first
    # reply waits FIRST_REPLY_TIMEOUT (300 s); a timeout there is
    # "slow", not "down".
    check("first-reply window is 300 seconds",
          app.FIRST_REPLY_TIMEOUT == 300.0,
          str(app.FIRST_REPLY_TIMEOUT))
    check("a slow first reply is not 'Ollama down'",
          not app.is_unreachable(app.OllamaError(app.SLOW_REPLY))
          and not app.SLOW_REPLY.startswith(app.UNREACHABLE),
          app.SLOW_REPLY)
    check("a slow first reply keeps its own message",
          app.describe_error(app.OllamaError(app.SLOW_REPLY))
          == app.SLOW_REPLY)

    # _set_response_read_timeout restores the per-read timeout once
    # the first reply arrives, and never crashes on a response shape
    # it does not know.
    class _FakeSock18:
        def __init__(self):
            self.timeout = None

        def settimeout(self, value):
            self.timeout = value

    class _FakeRaw18:
        def __init__(self, sock):
            self._sock = sock

    class _FakeFp18:
        def __init__(self, raw):
            self.raw = raw

    class _FakeResp18:
        def __init__(self, fp):
            self.fp = fp

    sock18 = _FakeSock18()
    app._set_response_read_timeout(
        _FakeResp18(_FakeFp18(_FakeRaw18(sock18))), app.READ_TIMEOUT)
    check("the read timeout is restored after the first reply",
          sock18.timeout == app.READ_TIMEOUT, str(sock18.timeout))
    app._set_response_read_timeout(object(), app.READ_TIMEOUT)
    check("an unknown response shape does not crash the restore",
          True)

    # RV3: address normalisation, unit level.
    check("a bare address gains http://",
          app.normalize_ollama_address("127.0.0.1:11436")
          == "http://127.0.0.1:11436")
    check("a bare hostname gains http://",
          app.normalize_ollama_address("localhost:11434")
          == "http://localhost:11434")
    check("an address with a scheme is untouched",
          app.normalize_ollama_address("https://ollama.lan:11434")
          == "https://ollama.lan:11434")
    check("an empty address stays empty",
          app.normalize_ollama_address("") == ""
          and app.normalize_ollama_address("   ") == "")
    check("a trailing slash is trimmed",
          app.normalize_ollama_address("http://localhost:11434/")
          == "http://localhost:11434")

    # RV12: template preview separates tasks with a blank line.
    preview18 = app.render_template_preview({
        "name": "T", "description": "", "capability": "text",
        "scored": True, "num_predict": 200,
        "tasks": [
            {"id": "a", "prompt": "first", "expected": "1"},
            {"id": "b", "prompt": "second", "expected": "2"},
        ],
    })
    check("template preview separates tasks with a blank line",
          "Expected: 1\n\n  b" in preview18, preview18)

    # RV2: the pull close-race, deterministically — a response whose
    # read raises AttributeError (the buffer gone under it) still
    # reports back instead of dying silent.
    import urllib.request as urllib_request18
    real_urlopen18 = urllib_request18.urlopen

    class _RaceResp18:
        """A read with a stop landing mid-read: the flag goes up,
        then the buffer is gone."""

        def __init__(self, worker):
            self.worker = worker

        def readline(self):
            self.worker._cancelled = True
            raise AttributeError(
                "'NoneType' object has no attribute 'peek'")

        def close(self):
            pass

    class _DeadResp18:
        def readline(self):
            raise AttributeError(
                "'NoneType' object has no attribute 'peek'")

        def close(self):
            pass

    class _BoomResp18:
        def readline(self):
            raise RuntimeError("boom")

        def close(self):
            pass

    signals18 = []
    try:
        worker18a = app.PullWorker("race-a:1b")
        urllib_request18.urlopen = (
            lambda req, timeout=None: _RaceResp18(worker18a))
        worker18a.cancelled.connect(
            lambda name: signals18.append(("cancelled", name)))
        worker18a.failed.connect(
            lambda msg: signals18.append(("failed", msg)))
        worker18a.run()
        check("a close-race on a stopping pull reports stopped",
              signals18 == [("cancelled", "race-a:1b")], str(signals18))
        signals18.clear()
        urllib_request18.urlopen = (
            lambda req, timeout=None: _DeadResp18())
        worker18b = app.PullWorker("dead-b:1b")
        worker18b.cancelled.connect(
            lambda name: signals18.append(("cancelled", name)))
        worker18b.failed.connect(
            lambda msg: signals18.append(("failed", msg)))
        worker18b.run()
        check("a close-race on a live pull reports failed",
              len(signals18) == 1 and signals18[0][0] == "failed",
              str(signals18))
        signals18.clear()
        urllib_request18.urlopen = (
            lambda req, timeout=None: _BoomResp18())
        worker18c = app.PullWorker("boom:1b")
        worker18c.cancelled.connect(
            lambda name: signals18.append(("cancelled", name)))
        worker18c.failed.connect(
            lambda msg: signals18.append(("failed", msg)))
        worker18c.run()
        check("an unexpected pull error still reports back",
              signals18 == [("failed",
                             "The pull stopped unexpectedly. Try again.")],
              str(signals18))
    finally:
        urllib_request18.urlopen = real_urlopen18

    # ---------- v18: window checks ---------------------------------------
    from PySide6.QtCore import QSettings as QSettings18
    from PySide6.QtCore import Qt as Qt18
    from PySide6.QtWidgets import QScrollArea as QScrollArea18
    settings18 = QSettings18("BrokeNBored", app.APP_NAME)
    prior18 = {
        key: settings18.value(key)
        for key in ("models_folder", "ollama_host",
                    "default_budget_minutes", "template_sets",
                    "font_point_size", "reduce_motion",
                    "setup_dismissed", "setup_completed")
    }
    prior_host18 = os.environ.get("OLLAMA_HOST")
    prior_results18 = os.environ.get("OST_RESULTS_DIR")
    prior_providers18 = os.environ.get("OST_PROVIDERS_FILE")
    prior_caps18 = os.environ.get("OST_CAPABILITIES_FILE")
    prior_font18 = qt_app.font().pointSize()
    app.save_app_settings({"models_folder": "", "ollama_host": "",
                           "default_budget_minutes": 0, "template_sets": {},
                           "font_point_size": 0, "reduce_motion": False})
    settings18.remove("setup_dismissed")
    settings18.remove("setup_completed")
    settings18.sync()
    results18 = os.path.join(SCRATCH, "results18")
    os.makedirs(results18, exist_ok=True)
    os.environ["OST_RESULTS_DIR"] = results18
    os.environ["OST_PROVIDERS_FILE"] = os.path.join(
        SCRATCH, "providers18.json")
    os.environ["OST_CAPABILITIES_FILE"] = os.path.join(
        SCRATCH, "capabilities18.json")
    server18, _t18 = fake_ollama.start_server()
    host18, port18 = server18.server_address
    ollama_url18 = f"http://{host18}:{port18}"
    os.environ["OLLAMA_HOST"] = ollama_url18
    window18 = app.MainWindow()
    window18.show()
    wait_until(lambda: window18.model_list.count() == 4,
               timeout=10, app=qt_app)
    try:
        # RV10: idle bars show no % — "0%" reads like a stalled job.
        check("the run bar shows no % while idle",
              window18.progress_bar.format() == "",
              repr(window18.progress_bar.format()))
        check("the pull bar shows no % while idle",
              window18.pull_bar.format() == "",
              repr(window18.pull_bar.format()))
        # RV9: the try-it box starts blank — nothing selected by
        # default, ever — and Ask with no pick says so.
        check("the try-it box starts blank",
              window18.tryit_model_combo.currentIndex() == -1
              and window18.tryit_model_combo.currentText() == "",
              repr(window18.tryit_model_combo.currentText()))
        window18.tryit_input.setText("What is 2 plus 2?")
        window18.tryit_ask_button.click()
        qt_app.processEvents()
        check("Ask with no model picked says so",
              window18.tryit_answer.toPlainText()
              == "Pick a model first.",
              window18.tryit_answer.toPlainText())
        window18.tryit_input.setText("")
        # RV8: the Providers tab scrolls.
        providers_tab18 = window18.tabs.widget(window18.TAB_PROVIDERS)
        scroll18 = providers_tab18.findChild(QScrollArea18)
        check("the Providers tab has a scroll area",
              scroll18 is not None)
        # RV11: Settings header is plain words, and the dialog fits
        # at its default size with no scrollbar.
        dialog18 = app.SettingsDialog(app.load_app_settings(), window18)
        labels18 = [w.text() for w in dialog18.findChildren(QLabel)]
        check("the Settings header is plain words",
              labels18
              and labels18[0]
              == "Settings for this app. They stay on this PC.",
              str(labels18[:1]))
        dialog18.show()
        qt_app.processEvents()
        settings_scroll18 = dialog18.findChild(QScrollArea18)
        check("Settings fits at its default size, no scrollbar",
              settings_scroll18 is not None
              and not settings_scroll18.verticalScrollBar().isVisible())
        dialog18.reject()
        qt_app.processEvents()
        # RV3: Settings saves a bare address with its scheme.
        # v19: the address field only saves when its override is
        # ticked — unticked means the detected address.
        dialog18c = app.SettingsDialog(app.load_app_settings(), window18)
        dialog18c.ollama_host_override.setChecked(True)
        dialog18c.ollama_host_input.setText("127.0.0.1:11436")
        dialog18c.accept()
        check("Settings saves a bare address with its scheme",
              dialog18c.result_values["ollama_host"]
              == "http://127.0.0.1:11436",
              str(dialog18c.result_values["ollama_host"]))
        dialog18d = app.SettingsDialog(app.load_app_settings(), window18)
        check("Settings address override starts unticked",
              not dialog18d.ollama_host_override.isChecked())
        dialog18d.accept()
        check("an unticked address override saves empty (detected)",
              dialog18d.result_values["ollama_host"] == "",
              repr(dialog18d.result_values["ollama_host"]))
        dialog18d.close()
        # RV3 end to end: a bare OLLAMA_HOST still lists models.
        os.environ["OLLAMA_HOST"] = f"{host18}:{port18}"
        window18.refresh_models()
        qt_app.processEvents()
        check("a bare Ollama address still lists models",
              window18.model_list.count() == 4,
              str(window18.model_list.count()))
        os.environ["OLLAMA_HOST"] = ollama_url18
        window18.refresh_models()
        qt_app.processEvents()
        # RV5: a failed screening is not cached, and the screen says so.
        real_show18 = app.ollama_show

        def _show_fail18(model):
            if model == "fake-smart:2b":
                raise app.OllamaError("show exploded")
            return real_show18(model)

        app.ollama_show = _show_fail18
        try:
            app.save_capabilities({})
            window18.refresh_models()
            qt_app.processEvents()
            caps18 = app.load_capabilities()
            check("a failed screening is not cached",
                  "fake-smart:2b" not in caps18
                  and "fake-partial:3b" in caps18,
                  str(sorted(caps18)))
            check("a failed screening says so on screen",
                  "Could not check what fake-smart:2b can do"
                  in window18.status_label.text(),
                  window18.status_label.text())
            check("a failed screening still lists the model",
                  any(window18.model_list.item(r).text().startswith(
                      "fake-smart:2b")
                      for r in range(window18.model_list.count())))
        finally:
            app.ollama_show = real_show18
            window18.refresh_models()
            qt_app.processEvents()
        # RV1: a slow first token (12 s — past the old 10 s window)
        # succeeds instead of blaming Ollama.
        server18.headers_delay = 12
        slow_first18 = app.generate_once(
            "fake-smart:2b", "Say hello in one sentence.", 64)
        check("a slow first token still answers",
              bool((slow_first18.get("answer") or "").strip()),
              str(slow_first18.get("answer", ""))[:80])
        server18.headers_delay = 0
        # RV1: past the first-reply window, the error is "slow",
        # not "down".
        server18.headers_delay = 5
        real_first18 = app.FIRST_REPLY_TIMEOUT
        try:
            app.FIRST_REPLY_TIMEOUT = 2
            slow_err18 = None
            try:
                app.generate_once(
                    "fake-smart:2b", "Say hello in one sentence.", 64)
            except app.OllamaError as exc:
                slow_err18 = exc
            check("a first reply past the window says slow, not down",
                  slow_err18 is not None
                  and str(slow_err18) == app.SLOW_REPLY
                  and not app.is_unreachable(slow_err18),
                  str(slow_err18))
        finally:
            app.FIRST_REPLY_TIMEOUT = real_first18
            server18.headers_delay = 0
        # RV1: slow models fail their warmup and the run carries on
        # with the next model — it does not end as "Ollama down".
        server18.headers_delay = 5
        app.FIRST_REPLY_TIMEOUT = 2
        try:
            set_checked_list(window18.model_list,
                             {"fake-smart:2b", "fake-partial:3b"})
            set_checked_templates(window18, {"coding"})
            window18.run_button.click()
            slow_run18 = wait_until(
                lambda: window18.worker is None
                and window18.save_button.isEnabled(),
                timeout=60, app=qt_app)
            check("a run with slow models completes, not Ollama down",
                  slow_run18
                  and window18.status_label.text().startswith("Done."),
                  window18.status_label.text())
            check("a slow model fails plainly as slow",
                  window18.results.get("fake-smart:2b", {}).get("status")
                  == "failed: Ollama took too long to answer",
                  str(window18.results.get("fake-smart:2b", {}).get(
                      "status")))
            check("the second slow model was tried too",
                  window18.results.get("fake-partial:3b", {}).get(
                      "status")
                  == "failed: Ollama took too long to answer",
                  str(window18.results.get("fake-partial:3b", {}).get(
                      "status")))
            check("slow failed pairs leave a resumable run",
                  window18.resume_button.isEnabled())
        finally:
            app.FIRST_REPLY_TIMEOUT = real_first18
            server18.headers_delay = 0
        # RV7 + RV10: during a long call the progress line's elapsed
        # time climbs on its own, and the run bar shows its %.
        server18.slow = True
        try:
            set_checked_list(window18.model_list, {"fake-smart:2b"})
            set_checked_templates(window18, {"coding"})
            window18.run_button.click()
            detail_started18 = wait_until(
                lambda: bool(window18.progress_detail.text()),
                timeout=10, app=qt_app)
            detail_before18 = window18.progress_detail.text()
            check("the run bar shows its % while a run is on",
                  window18.progress_bar.format() == "%p%",
                  repr(window18.progress_bar.format()))
            wait_until(lambda: False, timeout=2.5, app=qt_app)
            detail_after18 = window18.progress_detail.text()
            check("the elapsed time climbs during a long call",
                  detail_started18
                  and detail_after18 != detail_before18
                  and ("2s elapsed" in detail_after18
                       or "3s elapsed" in detail_after18),
                  f"before={detail_before18!r} "
                  f"after={detail_after18!r}")
        finally:
            server18.slow = False
            if window18.worker is not None:
                window18.cancel_button.click()
                wait_until(lambda: window18.worker is None,
                           timeout=30, app=qt_app)
        # RV2 + RV6: stop a slow pull, re-pull at once — it queues
        # behind the dying pull instead of being refused, and the
        # progress names the model, not a blob hash.
        server18.pull_slow = True
        window18.pull_input.setText("slow-a:1b")
        window18.pull_button.click()
        pulling18 = wait_until(
            lambda: "MB" in window18.pull_status.text(),
            timeout=12, app=qt_app)
        check("pull progress names the model",
              pulling18
              and window18.pull_status.text().startswith(
                  "Pulling slow-a:1b:"),
              window18.pull_status.text())
        window18.pull_cancel_button.click()
        # No processEvents: the stopped signal has not arrived yet,
        # so the old pull is still dying in its slot — or already
        # dead with its signal still queued. Either way the re-pull
        # must go: queued behind the dying pull, or started at once
        # resuming the partial download. Both are correct; which one
        # happens is a thread race, so the test accepts either.
        window18.pull_input.setText("slow-a:1b")
        window18.pull_button.click()
        repull_status18 = window18.pull_status.text()
        check("a re-pull while the old pull dies is queued or resumed",
              repull_status18.startswith("Queued slow-a:1b")
              or repull_status18.startswith("Resuming slow-a:1b"),
              repull_status18)
        queued_pull18 = wait_until(
            lambda: window18.pull_worker is not None
            and window18.pull_worker.name == "slow-a:1b"
            and window18.pull_queue_list.count() == 0,
            timeout=10, app=qt_app)
        check("the re-pull runs (queued or resumed)",
              queued_pull18,
              window18.pull_worker.name
              if window18.pull_worker is not None else "no worker")
        window18.pull_cancel_button.click()
        drained18 = wait_until(
            lambda: window18.pull_worker is None,
            timeout=10, app=qt_app)
        check("the re-pulled pull stops cleanly", drained18)
        server18.pull_slow = False
        # RV2: the queue branch itself, deterministically — a worker
        # that is still running and stopping queues the re-pull
        # instead of refusing it. (The live test above accepts either
        # outcome, so this stub pins the branch.)
        class _StoppingWorker18:
            name = "stop-q:1b"

            def isRunning(self):
                return True

            def is_stopping(self):
                return True

        window18.pull_worker = _StoppingWorker18()
        window18.pull_input.setText("stop-q:1b")
        window18.pull_button.click()
        check("a re-pull while the old pull stops is queued",
              "Queued stop-q:1b" in window18.pull_status.text()
              and window18.pull_queue_list.count() == 1,
              window18.pull_status.text())
        window18.pull_worker = None
        window18.pull_queue.clear()
        window18._refresh_pull_queue()
        # RV2: a worker that died without reporting (the old wedge)
        # no longer blocks the slot — a pull clears it and goes.
        wedge18 = app.PullWorker("wedge:1b")
        wedge18.start()
        wedge_done18 = wait_until(
            lambda: not wedge18.isRunning(), timeout=10, app=qt_app)
        window18.pull_worker = wedge18
        window18.pull_input.setText("wedge:1b")
        window18.pull_button.click()
        check("a dead pull worker no longer wedges the slot",
              wedge_done18
              and window18.pull_worker is not wedge18
              and "already pulling or queued"
              not in window18.pull_status.text(),
              window18.pull_status.text())
        wedge_pulled18 = wait_until(
            lambda: "Pulled wedge:1b." in window18.pull_status.text(),
            timeout=15, app=qt_app)
        check("the pull after a wedged slot completes",
              wedge_pulled18, window18.pull_status.text())
        # RV10: a failed pull leaves the bar idle, no "0%".
        server18.pull_fail = True
        window18.pull_input.setText("fail-me:1b")
        window18.pull_button.click()
        failed18 = wait_until(
            lambda: window18.pull_status.text().startswith("Pull failed:"),
            timeout=10, app=qt_app)
        check("a failed pull leaves the bar idle, no 0%",
              failed18
              and window18.pull_bar.value() == 0
              and window18.pull_bar.format() == "",
              f"{window18.pull_status.text()} "
              f"value={window18.pull_bar.value()} "
              f"format={window18.pull_bar.format()!r}")
        server18.pull_fail = False
        # RV12: provider list rows carry no checkbox flag.
        app.save_providers(
            [{"name": "fakeprov",
              "base_url": "http://127.0.0.1:9/v1",
              "api_key": "sk-fake-not-real", "models": []}],
            os.environ["OST_PROVIDERS_FILE"])
        window18.refresh_providers()
        qt_app.processEvents()
        prov_item18 = window18.provider_list.item(0)
        check("provider list rows carry no checkbox flag",
              prov_item18 is not None
              and not (prov_item18.flags() & Qt18.ItemIsUserCheckable))
    finally:
        window18.close()
        window18.deleteLater()
        qt_app.processEvents()
        server18.shutdown()
        server18.server_close()
        if prior_host18 is None:
            os.environ.pop("OLLAMA_HOST", None)
        else:
            os.environ["OLLAMA_HOST"] = prior_host18
        if prior_results18 is None:
            os.environ.pop("OST_RESULTS_DIR", None)
        else:
            os.environ["OST_RESULTS_DIR"] = prior_results18
        if prior_providers18 is None:
            os.environ.pop("OST_PROVIDERS_FILE", None)
        else:
            os.environ["OST_PROVIDERS_FILE"] = prior_providers18
        if prior_caps18 is None:
            os.environ.pop("OST_CAPABILITIES_FILE", None)
        else:
            os.environ["OST_CAPABILITIES_FILE"] = prior_caps18
        app.apply_font_size(0)
        for key18, value18 in prior18.items():
            if value18 is None:
                settings18.remove(key18)
            else:
                settings18.setValue(key18, value18)
        settings18.sync()
        if qt_app.font().pointSize() != prior_font18:
            app.apply_font_size(prior_font18)

    # ---------- v18: empty model list placeholder --------------------------
    # RV12: with Ollama down and nothing to list, the Models tab says
    # Ollama is starting instead of showing a blank box. The window
    # is built with the fake server up, then the server is swapped
    # for a dead port with no ollama on PATH, so neither the API nor
    # the CLI can list anything — and the starter is stubbed, so the
    # test checks the placeholder, not the starter.
    prior_host18b = os.environ.get("OLLAMA_HOST")
    prior_path18b = os.environ.get("PATH")
    prior_providers18b = os.environ.get("OST_PROVIDERS_FILE")
    prior_caps18b = os.environ.get("OST_CAPABILITIES_FILE")
    os.environ["OST_PROVIDERS_FILE"] = os.path.join(
        SCRATCH, "providers18b.json")
    os.environ["OST_CAPABILITIES_FILE"] = os.path.join(
        SCRATCH, "capabilities18b.json")
    server18b, _t18b = fake_ollama.start_server()
    host18b, port18b = server18b.server_address
    os.environ["OLLAMA_HOST"] = f"http://{host18b}:{port18b}"
    window18b = app.MainWindow()
    window18b.show()
    wait_until(lambda: window18b.model_list.count() == 4,
               timeout=10, app=qt_app)
    try:
        empty_dir18b = os.path.join(SCRATCH, "empty-path18b")
        os.makedirs(empty_dir18b, exist_ok=True)
        os.environ["PATH"] = empty_dir18b
        os.environ["OLLAMA_HOST"] = f"http://127.0.0.1:{free_port()}"
        window18b.starter = object()
        window18b.refresh_models()
        # The placeholder lives on the Models tab, which is not the
        # opening tab — show it first, or isVisible stays False
        # because the tab page itself is hidden.
        window18b.tabs.setCurrentIndex(window18b.TAB_MODELS)
        qt_app.processEvents()
        check("an empty model list says Ollama is starting",
              window18b.installed_empty_label.isVisible()
              and "Starting Ollama"
              in window18b.installed_empty_label.text(),
              window18b.installed_empty_label.text())
        window18b.starter = None
        os.environ["PATH"] = prior_path18b
        os.environ["OLLAMA_HOST"] = f"http://{host18b}:{port18b}"
        window18b.refresh_models()
        qt_app.processEvents()
        check("the placeholder hides once models list again",
              not window18b.installed_empty_label.isVisible()
              and window18b.installed_list.count() == 4,
              str(window18b.installed_list.count()))
    finally:
        window18b.close()
        window18b.deleteLater()
        qt_app.processEvents()
        server18b.shutdown()
        server18b.server_close()
        if prior_host18b is None:
            os.environ.pop("OLLAMA_HOST", None)
        else:
            os.environ["OLLAMA_HOST"] = prior_host18b
        if prior_path18b is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = prior_path18b
        if prior_providers18b is None:
            os.environ.pop("OST_PROVIDERS_FILE", None)
        else:
            os.environ["OST_PROVIDERS_FILE"] = prior_providers18b
        if prior_caps18b is None:
            os.environ.pop("OST_CAPABILITIES_FILE", None)
        else:
            os.environ["OST_CAPABILITIES_FILE"] = prior_caps18b

    # ---------- v19: wizard, ONE Save, detected Settings ---------------
    from PySide6.QtTest import QTest as QTest19
    from PySide6.QtWidgets import QDialog as QDialog19

    # Detection helpers (unit-level, no window needed).
    prior_env_host19 = app._ENV_OLLAMA_HOST
    prior_env_models19 = app._ENV_OLLAMA_MODELS
    try:
        app._ENV_OLLAMA_HOST = None
        app._ENV_OLLAMA_MODELS = None
        check("detect_ollama_address defaults to localhost",
              app.detect_ollama_address() == "http://localhost:11434",
              app.detect_ollama_address())
        check("detect_models_folder defaults to ~/.ollama/models",
              app.detect_models_folder() == os.path.join(
                  os.path.expanduser("~"), ".ollama", "models"),
              app.detect_models_folder())
        app._ENV_OLLAMA_HOST = "127.0.0.1:11436"
        app._ENV_OLLAMA_MODELS = "/env/models"
        check("detect_ollama_address honors the env, with a scheme",
              app.detect_ollama_address() == "http://127.0.0.1:11436",
              app.detect_ollama_address())
        check("detect_models_folder honors the env",
              app.detect_models_folder() == "/env/models",
              app.detect_models_folder())
        check("known providers are the eight services",
              [p[0] for p in app.KNOWN_PROVIDERS] == [
                  "openai", "openrouter", "groq", "together",
                  "deepseek", "xai", "mistral", "lmstudio"],
              str([p[0] for p in app.KNOWN_PROVIDERS]))
        check("only LM Studio needs no key",
              [p[0] for p in app.KNOWN_PROVIDERS if not p[3]]
              == ["lmstudio"])
    finally:
        app._ENV_OLLAMA_HOST = prior_env_host19
        app._ENV_OLLAMA_MODELS = prior_env_models19

    # The wizard, driven directly (it is modal in the app, so the
    # window tests use the stub; here the real dialog is shown and
    # clicked through, step by step).
    pserver19, _pt19 = fake_ollama.start_provider_server()
    phost19, pport19 = pserver19.server_address
    providers19 = os.path.join(SCRATCH, "providers19.json")
    prior_providers19 = os.environ.get("OST_PROVIDERS_FILE")
    os.environ["OST_PROVIDERS_FILE"] = providers19
    if os.path.exists(providers19):
        os.remove(providers19)

    def _click_wizard_row19(wizard, row):
        # A real click: a programmatic setCurrentRow is not a pick
        # (Qt's focus auto-select must not count either).
        item = wizard.provider_list.item(row)
        wizard.provider_list.scrollToItem(item)
        rect = wizard.provider_list.visualItemRect(item)
        QTest19.mouseClick(wizard.provider_list.viewport(),
                           Qt.LeftButton, pos=rect.center())
        qt_app.processEvents()

    window19 = None
    oserver19 = None
    prior_host19 = os.environ.get("OLLAMA_HOST")
    prior_results19 = os.environ.get("OST_RESULTS_DIR")
    prior_caps19 = os.environ.get("OST_CAPABILITIES_FILE")
    try:
        wizard19 = app.AddProviderDialog()
        wizard19.show()
        qt_app.processEvents()
        check("wizard starts on step 1 with nothing picked",
              wizard19.stack.currentIndex() == 0
              and wizard19.provider_list.currentRow() == -1
              and not wizard19.next_button.isEnabled()
              and "Step 1 of 4" in wizard19.step_label.text(),
              f"row {wizard19.provider_list.currentRow()} "
              f"next {wizard19.next_button.isEnabled()} "
              f"{wizard19.step_label.text()}")
        check("the address field starts hidden",
              not wizard19.address_row.isVisible())
        # Focus alone must not pick a row: Qt makes row 0 current
        # when the list takes focus, which is not a choice.
        wizard19.provider_list.setFocus()
        qt_app.processEvents()
        check("focus alone picks nothing",
              wizard19._picked is None
              and not wizard19.next_button.isEnabled(),
              str(wizard19._picked))
        _click_wizard_row19(wizard19, 0)
        check("picking OpenAI fills the name and the address",
              wizard19.name_input.text() == "openai"
              and "https://api.openai.com/v1" in wizard19.pick_info.text()
              and not wizard19.address_row.isVisible()
              and wizard19.next_button.isEnabled(),
              f"{wizard19.name_input.text()} "
              f"{wizard19.pick_info.text()}")
        wizard19.next_button.click()
        qt_app.processEvents()
        check("wizard advances to the key step",
              wizard19.stack.currentIndex() == 1
              and "Step 2 of 4" in wizard19.step_label.text(),
              wizard19.step_label.text())
        check("key step names the service and shows the field",
              "OpenAI" in wizard19.key_intro.text()
              and wizard19.key_input.isVisible(),
              wizard19.key_intro.text())
        wizard19.back_button.click()
        qt_app.processEvents()
        check("Back returns to the pick step with the pick kept",
              wizard19.stack.currentIndex() == 0
              and wizard19.name_input.text() == "openai",
              wizard19.name_input.text())
        _click_wizard_row19(wizard19, 8)
        check("Other reveals the address field and clears the name",
              wizard19.address_row.isVisible()
              and wizard19.name_input.text() == ""
              and not wizard19.next_button.isEnabled())
        wizard19.name_input.setText("fake")
        qt_app.processEvents()
        check("Next still waits for the address",
              not wizard19.next_button.isEnabled())
        wizard19.url_input.setText(f"http://{phost19}:{pport19}/v1")
        qt_app.processEvents()
        check("name plus address enables Next",
              wizard19.next_button.isEnabled())
        wizard19.next_button.click()
        qt_app.processEvents()
        wizard19.key_input.setText(fake_ollama.PROVIDER_API_KEY)
        wizard19.next_button.click()
        qt_app.processEvents()
        check("wizard advances to the test step",
              wizard19.stack.currentIndex() == 2
              and "Step 3 of 4" in wizard19.step_label.text(),
              wizard19.step_label.text())
        check("wizard Next waits for a passing test",
              not wizard19.next_button.isEnabled())
        wizard19.test_button.click()
        tested19 = wait_until(
            lambda: "Connected" in wizard19.test_status.text(),
            timeout=30, app=qt_app,
        )
        check("wizard test reports Connected", tested19,
              wizard19.test_status.text())
        check("a passing test enables Next",
              wizard19.next_button.isEnabled())
        wizard19.next_button.click()
        qt_app.processEvents()
        check("wizard advances to the tick step",
              wizard19.stack.currentIndex() == 3
              and "Step 4 of 4" in wizard19.step_label.text(),
              wizard19.step_label.text())
        check("tick step lists the provider models",
              [wizard19.models_list.item(i).text()
               for i in range(wizard19.models_list.count())]
              == ["fake-gpt-dumb", "fake-gpt-partial", "fake-gpt-smart"],
              str([wizard19.models_list.item(i).text()
                   for i in range(wizard19.models_list.count())]))
        check("tick step warns tests cost real money",
              any("cost real money" in w.text()
                  for w in wizard19.findChildren(QLabel)))
        check("no model is ticked by default",
              all(wizard19.models_list.item(i).checkState()
                  == Qt.Unchecked
                  for i in range(wizard19.models_list.count())))
        wizard19.models_list.item(0).setCheckState(Qt.Checked)
        wizard19.models_list.item(2).setCheckState(Qt.Checked)
        wizard19.finish_button.click()
        qt_app.processEvents()
        check("finishing accepts the wizard",
              wizard19.result() == QDialog19.Accepted)
        check("finished provider carries the ticked models",
              wizard19.result_provider is not None
              and wizard19.result_provider["name"] == "fake"
              and wizard19.result_provider["base_url"]
              == f"http://{phost19}:{pport19}/v1"
              and wizard19.result_provider["api_key"]
              == fake_ollama.PROVIDER_API_KEY
              and [m["id"] for m in wizard19.result_provider["models"]]
              == ["fake-gpt-dumb", "fake-gpt-smart"],
              str(wizard19.result_provider))
        check("ticked models default to text with no prices",
              wizard19.result_provider["models"][0]
              == {"id": "fake-gpt-dumb", "display_name": "",
                  "price_input_per_1m": None,
                  "price_output_per_1m": None,
                  "capabilities": ["text"]},
              str(wizard19.result_provider["models"][0]))
        wizard19.close()
        qt_app.processEvents()
        # The finished provider is saved, so the duplicate check
        # has something to catch.
        app.save_providers([wizard19.result_provider], providers19)
        wizard19b = app.AddProviderDialog()
        wizard19b.show()
        qt_app.processEvents()
        _click_wizard_row19(wizard19b, 8)
        wizard19b.name_input.setText("fake")
        wizard19b.url_input.setText(f"http://{phost19}:{pport19}/v1")
        qt_app.processEvents()
        wizard19b.next_button.click()
        qt_app.processEvents()
        check("a duplicate name is caught on step 1",
              wizard19b.stack.currentIndex() == 0
              and "already exists" in wizard19b.page_status.text(),
              wizard19b.page_status.text())
        wizard19b.close()
        qt_app.processEvents()
        # A bad key fails the test plainly and Next stays off.
        wizard19c = app.AddProviderDialog()
        wizard19c.show()
        qt_app.processEvents()
        _click_wizard_row19(wizard19c, 8)
        wizard19c.name_input.setText("badkey")
        wizard19c.url_input.setText(f"http://{phost19}:{pport19}/v1")
        wizard19c.next_button.click()
        qt_app.processEvents()
        wizard19c.key_input.setText("sk-wrong")
        wizard19c.next_button.click()
        qt_app.processEvents()
        wizard19c.test_button.click()
        bad19 = wait_until(
            lambda: "rejected the key" in wizard19c.test_status.text(),
            timeout=30, app=qt_app,
        )
        check("wizard test with a bad key fails plainly", bad19,
              wizard19c.test_status.text())
        check("a failed test leaves Next off",
              not wizard19c.next_button.isEnabled())
        wizard19c.close()
        qt_app.processEvents()
        # LM Studio needs no key: the key field hides.
        wizard19d = app.AddProviderDialog()
        wizard19d.show()
        qt_app.processEvents()
        _click_wizard_row19(wizard19d, 7)
        check("LM Studio says no key is needed",
              "no key needed" in wizard19d.pick_info.text(),
              wizard19d.pick_info.text())
        wizard19d.next_button.click()
        qt_app.processEvents()
        check("the key field hides for a local server",
              not wizard19d.key_input.isVisible()
              and "needs no key" in wizard19d.key_intro.text(),
              wizard19d.key_intro.text())
        wizard19d.close()
        qt_app.processEvents()
        # Add-models mode: starts at the test step, Back is off,
        # and Finish returns only the ticked ids.
        wizard19e = app.AddProviderDialog(
            existing=wizard19.result_provider)
        wizard19e.show()
        qt_app.processEvents()
        check("add-models mode starts at the test step",
              wizard19e.stack.currentIndex() == 2
              and not wizard19e.back_button.isEnabled(),
              f"{wizard19e.stack.currentIndex()} "
              f"{wizard19e.back_button.isEnabled()}")
        wizard19e.test_button.click()
        tested19e = wait_until(
            lambda: "Connected" in wizard19e.test_status.text(),
            timeout=30, app=qt_app,
        )
        check("add-models test reports Connected", tested19e,
              wizard19e.test_status.text())
        wizard19e.next_button.click()
        qt_app.processEvents()
        for i in range(wizard19e.models_list.count()):
            wizard19e.models_list.item(i).setCheckState(Qt.Checked)
        wizard19e.finish_button.click()
        qt_app.processEvents()
        check("add-models mode returns the ticked ids",
              wizard19e.result_model_ids
              == ["fake-gpt-dumb", "fake-gpt-partial", "fake-gpt-smart"],
              str(wizard19e.result_model_ids))
        wizard19e.close()
        qt_app.processEvents()
        # The window side: Add models stages only new ids, ONE Save
        # keeps them, and a bad Advanced price is refused plainly.
        oserver19, _t19 = fake_ollama.start_server()
        ohost19, oport19 = oserver19.server_address
        os.environ["OLLAMA_HOST"] = f"http://{ohost19}:{oport19}"
        results19 = os.path.join(SCRATCH, "results19")
        os.environ["OST_RESULTS_DIR"] = results19
        caps19 = os.path.join(SCRATCH, "capabilities19.json")
        os.environ["OST_CAPABILITIES_FILE"] = caps19
        window19 = app.MainWindow()
        window19.show()
        wait_until(lambda: window19.model_list.count() == 6,
                   timeout=10, app=qt_app)
        check("window lists locals plus the wizard provider models",
              window19.model_list.count() == 6,
              str(window19.model_list.count()))
        window19.tabs.setCurrentIndex(window19.TAB_PROVIDERS)
        qt_app.processEvents()
        check("window opens on the detail page with a provider saved",
              window19.provider_stack.currentIndex() == 1)
        app.AddProviderDialog = _stub_wizard_class(
            None, ["fake-gpt-smart", "fake-gpt-partial"])
        window19.provider_add_models_button.click()
        qt_app.processEvents()
        app.AddProviderDialog = real_wizard
        check("Add models stages only the new id",
              window19.provider_models_list.count() == 3
              and "Added 1 model" in window19.provider_status.text(),
              f"{window19.provider_models_list.count()} "
              f"{window19.provider_status.text()}")
        with open(providers19, encoding="utf-8") as handle:
            before_save19 = json.load(handle)
        check("staging alone does not touch the file",
              len(before_save19["providers"][0]["models"]) == 2,
              str(before_save19))
        window19.provider_save_button.click()
        saved19 = wait_until(
            lambda: "Saved fake." in window19.provider_status.text(),
            timeout=10, app=qt_app,
        )
        check("ONE Save keeps the staged model", saved19,
              window19.provider_status.text())
        with open(providers19, encoding="utf-8") as handle:
            after_save19 = json.load(handle)
        check("the file gains the staged model after Save",
              [m["id"] for m in after_save19["providers"][0]["models"]]
              == ["fake-gpt-dumb", "fake-gpt-smart", "fake-gpt-partial"],
              str(after_save19))
        # Advanced: a bad price is refused, a good one stages.
        partial_item19 = None
        for row in range(window19.provider_models_list.count()):
            if window19.provider_models_list.item(row).text().startswith(
                    "fake-gpt-partial"):
                partial_item19 = window19.provider_models_list.item(row)
        check("the staged model is in the list",
              partial_item19 is not None)
        if partial_item19 is not None:
            window19.provider_models_list.itemClicked.emit(partial_item19)
            qt_app.processEvents()
            window19.provider_advanced_toggle.setChecked(True)
            qt_app.processEvents()
            window19.advanced_price_in_input.setText("abc")
            window19.advanced_price_in_input.editingFinished.emit()
            qt_app.processEvents()
            check("Advanced refuses a bad price",
                  "must be a number" in window19.provider_status.text(),
                  window19.provider_status.text())
            window19.advanced_price_in_input.setText("0.25")
            window19.advanced_price_in_input.editingFinished.emit()
            qt_app.processEvents()
            partial_text19 = ""
            for row in range(window19.provider_models_list.count()):
                if window19.provider_models_list.item(row).text().startswith(
                        "fake-gpt-partial"):
                    partial_text19 = (
                        window19.provider_models_list.item(row).text())
            check("a good price stages into the list",
                  "$0.25" in partial_text19, partial_text19)
        # Settings detected UI: empty saved values show the found
        # values with the fields hidden; saved values start ticked.
        app._ENV_OLLAMA_HOST = None
        app._ENV_OLLAMA_MODELS = None
        dialog19 = app.SettingsDialog(
            {"models_folder": "", "ollama_host": "",
             "default_budget_minutes": 0, "template_sets": {}},
            window19)
        dialog19.show()
        qt_app.processEvents()
        check("Settings shows the detected address and folder",
              any("Found automatically" in w.text()
                  and "localhost:11434" in w.text()
                  for w in dialog19.findChildren(QLabel))
              and any("Found automatically" in w.text()
                      and ".ollama" in w.text()
                      for w in dialog19.findChildren(QLabel)))
        check("detected Settings hides both fields",
              not dialog19.models_folder_override.isChecked()
              and not dialog19.ollama_host_override.isChecked()
              and not dialog19._folder_row_widget.isVisible()
              and not dialog19.ollama_host_input.isVisible())
        dialog19.accept()
        check("detected Settings saves empty for both",
              dialog19.result_values["models_folder"] == ""
              and dialog19.result_values["ollama_host"] == "",
              str(dialog19.result_values))
        dialog19b = app.SettingsDialog(
            {"models_folder": "/tmp/ost-models",
             "ollama_host": "http://127.0.0.1:11555",
             "default_budget_minutes": 0, "template_sets": {}},
            window19)
        dialog19b.show()
        qt_app.processEvents()
        check("saved overrides start ticked with their values",
              dialog19b.models_folder_override.isChecked()
              and dialog19b.ollama_host_override.isChecked()
              and dialog19b.models_folder_input.text() == "/tmp/ost-models"
              and dialog19b.ollama_host_input.text()
              == "http://127.0.0.1:11555"
              and dialog19b._folder_row_widget.isVisible()
              and dialog19b.ollama_host_input.isVisible())
        dialog19b.models_folder_override.setChecked(False)
        dialog19b.accept()
        check("unticking an override clears it on save",
              dialog19b.result_values["models_folder"] == ""
              and dialog19b.result_values["ollama_host"]
              == "http://127.0.0.1:11555",
              str(dialog19b.result_values))
        app._ENV_OLLAMA_HOST = "http://env-host:11434"
        app._ENV_OLLAMA_MODELS = "/env/models"
        dialog19c = app.SettingsDialog(
            {"models_folder": "", "ollama_host": "",
             "default_budget_minutes": 0, "template_sets": {}},
            window19)
        dialog19c.show()
        qt_app.processEvents()
        check("env-locked overrides are disabled",
              not dialog19c.models_folder_override.isEnabled()
              and not dialog19c.ollama_host_override.isEnabled())
        check("env-locked labels say so",
              any("Set outside the app" in w.text()
                  and "env-host" in w.text()
                  for w in dialog19c.findChildren(QLabel))
              and any("Set outside the app" in w.text()
                      and "/env/models" in w.text()
                      for w in dialog19c.findChildren(QLabel)))
        dialog19c.close()
        app._ENV_OLLAMA_HOST = None
        app._ENV_OLLAMA_MODELS = None
    finally:
        if window19 is not None:
            window19.close()
            window19.deleteLater()
            qt_app.processEvents()
        if oserver19 is not None:
            oserver19.shutdown()
            oserver19.server_close()
        pserver19.shutdown()
        pserver19.server_close()
        if prior_providers19 is None:
            os.environ.pop("OST_PROVIDERS_FILE", None)
        else:
            os.environ["OST_PROVIDERS_FILE"] = prior_providers19
        if prior_host19 is None:
            os.environ.pop("OLLAMA_HOST", None)
        else:
            os.environ["OLLAMA_HOST"] = prior_host19
        if prior_results19 is None:
            os.environ.pop("OST_RESULTS_DIR", None)
        else:
            os.environ["OST_RESULTS_DIR"] = prior_results19
        if prior_caps19 is None:
            os.environ.pop("OST_CAPABILITIES_FILE", None)
        else:
            os.environ["OST_CAPABILITIES_FILE"] = prior_caps19

    # ---------- v20: live bigger text + Profiles fits the screen -----
    # Both defects were found during v19 verification and proven by
    # probe: apply_font_size moved the app font but no open window's
    # widgets, and the Profiles tab forced the window minimum to
    # 1313x822 at 14 pt with runs. Fabricated runs (same shape as
    # v14) so the Profiles tab has tiles and charts to show.
    from PySide6.QtWidgets import QScrollArea as QScrollArea20

    def write_run20(folder, model, template, name, score, speed,
                    stamp):
        path = os.path.join(
            folder, f"{stamp.replace(':', '-')}-{template}.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({
                "model": model, "template": template,
                "template_name": name, "template_score": score,
                "avg_decode_tok_s": speed, "timestamp": stamp,
                "system_id": "sys1",
            }, handle)

    results20 = os.path.join(SCRATCH, "results20")
    smart_dir20 = os.path.join(results20, "fake-smart-2b", "sys1")
    os.makedirs(smart_dir20, exist_ok=True)
    write_run20(smart_dir20, "fake-smart:2b", "math", "Math", 100.0,
                20.0, "2026-11-13T10:00:00")
    write_run20(smart_dir20, "fake-smart:2b", "coding", "Coding", 40.0,
                18.0, "2026-11-13T11:00:00")
    write_run20(smart_dir20, "fake-smart:2b", "math", "Math", 80.0,
                22.0, "2026-11-14T09:00:00")
    prior_results20 = os.environ.get("OST_RESULTS_DIR")
    prior_providers20 = os.environ.get("OST_PROVIDERS_FILE")
    prior_caps20 = os.environ.get("OST_CAPABILITIES_FILE")
    prior_host20 = os.environ.get("OLLAMA_HOST")
    os.environ["OST_RESULTS_DIR"] = results20
    os.environ["OST_PROVIDERS_FILE"] = os.path.join(
        SCRATCH, "providers20.json")
    os.environ["OST_CAPABILITIES_FILE"] = os.path.join(
        SCRATCH, "capabilities20.json")
    server20, _t20 = fake_ollama.start_server()
    host20, port20 = server20.server_address
    os.environ["OLLAMA_HOST"] = f"http://{host20}:{port20}"
    window20 = app.MainWindow()
    window20.show()
    wait_until(lambda: window20.model_list.count() == 4,
               timeout=10, app=qt_app)
    prior_font20 = qt_app.font().pointSize()
    default20 = app.default_font_point_size()
    try:
        # Defect 1: the live apply must reach the OPEN window's
        # widgets, not just the app font — the v15 checks only
        # read qt_app.font(), which is why this stayed broken.
        check("open window label starts at the default size",
              window20.status_label.font().pointSize() == prior_font20,
              str(window20.status_label.font().pointSize()))
        app.apply_font_size(14)
        qt_app.processEvents()
        check("live apply re-sizes the open window's labels",
              window20.status_label.font().pointSize() == 14,
              str(window20.status_label.font().pointSize()))
        check("live apply re-sizes the Providers tab too",
              window20.provider_empty_label.font().pointSize() == 14,
              str(window20.provider_empty_label.font().pointSize()))
        # Tiles: pick the model with runs on the Profiles tab.
        window20.tabs.setCurrentIndex(window20.TAB_PROFILES)
        for row20 in range(window20.profile_model_combo.count()):
            if (window20.profile_model_combo.itemData(row20)
                    == "fake-smart-2b"):
                window20.profile_model_combo.setCurrentIndex(row20)
        qt_app.processEvents()
        check("tiles show at 14 pt with runs",
              window20.profile_tiles.isVisible())
        check("hero value keeps its relative size live",
              window20.profile_hero_value.font().pointSize() == 22
              and window20.profile_hero_value.font().bold(),
              f"{window20.profile_hero_value.font().pointSize()} "
              f"bold {window20.profile_hero_value.font().bold()}")
        check("small tile values keep their relative size live",
              window20.profile_best_value.font().pointSize() == 18
              and window20.profile_speed_value.font().pointSize() == 18,
              f"{window20.profile_best_value.font().pointSize()} "
              f"{window20.profile_speed_value.font().pointSize()}")
        caps20 = [c for c in window20.profile_tiles.findChildren(QLabel)
                  if c.property("font_size_delta") == -1]
        check("tile captions keep their relative size live",
              len(caps20) == 5
              and all(c.font().pointSize() == 13 for c in caps20),
              str([c.font().pointSize() for c in caps20]))
        # Defect 2: the Profiles tab fits the screen at 14 pt
        # with runs — it forced 1313x822 before v20.
        page20 = window20.tabs.widget(window20.TAB_PROFILES)
        check("Profiles tab lives in a scroll area",
              page20.findChild(QScrollArea20) is not None)
        hint20 = page20.minimumSizeHint()
        check("Profiles tab minimum is small at 14 pt with runs",
              hint20.width() <= 200 and hint20.height() <= 200,
              f"{hint20.width()}x{hint20.height()}")
        win_min20 = window20.minimumSizeHint()
        check("window minimum fits 1366x768 at 14 pt with runs",
              win_min20.width() <= 1366 and win_min20.height() <= 768,
              f"{win_min20.width()}x{win_min20.height()}")
        window20.resize(980, 720)
        qt_app.processEvents()
        check("window can resize to 720 tall at 14 pt with runs",
              window20.height() == 720, str(window20.height()))
        check("window width stays on a 1366 screen at 14 pt",
              window20.width() <= 1366, str(window20.width()))
        # Restoring the size restores the open window too.
        app.apply_font_size(0)
        qt_app.processEvents()
        check("restoring the size restores the open window's labels",
              window20.status_label.font().pointSize() == default20,
              str(window20.status_label.font().pointSize()))
        check("restoring the size restores the tile values",
              window20.profile_hero_value.font().pointSize()
              == default20 + 8
              and window20.profile_best_value.font().pointSize()
              == default20 + 4,
              f"{window20.profile_hero_value.font().pointSize()} "
              f"{window20.profile_best_value.font().pointSize()}")
        # The path Joshua uses: the Settings spin, live, then
        # Cancel putting the opening size back.
        dialog20 = app.SettingsDialog(app.load_app_settings(), window20)
        dialog20.show()
        qt_app.processEvents()
        dialog20.font_spin.setValue(14)
        qt_app.processEvents()
        check("Settings spin re-sizes the open window live",
              window20.status_label.font().pointSize() == 14,
              str(window20.status_label.font().pointSize()))
        dialog20.reject()
        qt_app.processEvents()
        check("cancelling Settings restores the open window's size",
              window20.status_label.font().pointSize() == default20,
              str(window20.status_label.font().pointSize()))
        # Cancel with a non-default size already applied puts THAT
        # size back, not the startup default (found by the drive:
        # _font_at_open used to be the startup default, so Cancel
        # after applying 14 pt put 9 pt back).
        app.apply_font_size(14)
        qt_app.processEvents()
        dialog20b = app.SettingsDialog(app.load_app_settings(), window20)
        dialog20b.show()
        qt_app.processEvents()
        dialog20b.font_spin.setValue(20)
        qt_app.processEvents()
        check("spin to 20 pt grows the open window live",
              window20.status_label.font().pointSize() == 20,
              str(window20.status_label.font().pointSize()))
        dialog20b.reject()
        qt_app.processEvents()
        check("cancelling Settings restores the size it opened with",
              window20.status_label.font().pointSize() == 14,
              str(window20.status_label.font().pointSize()))
        app.apply_font_size(0)
    finally:
        app.apply_font_size(prior_font20)
        if window20 is not None:
            window20.close()
            window20.deleteLater()
            qt_app.processEvents()
        server20.shutdown()
        server20.server_close()
        if prior_results20 is None:
            os.environ.pop("OST_RESULTS_DIR", None)
        else:
            os.environ["OST_RESULTS_DIR"] = prior_results20
        if prior_providers20 is None:
            os.environ.pop("OST_PROVIDERS_FILE", None)
        else:
            os.environ["OST_PROVIDERS_FILE"] = prior_providers20
        if prior_caps20 is None:
            os.environ.pop("OST_CAPABILITIES_FILE", None)
        else:
            os.environ["OST_CAPABILITIES_FILE"] = prior_caps20
        if prior_host20 is None:
            os.environ.pop("OLLAMA_HOST", None)
        else:
            os.environ["OLLAMA_HOST"] = prior_host20

    QMessageBox.question = _suite_real_question
    app.MainWindow._show_run_preview = _suite_real_preview

    shutil.rmtree(SCRATCH, ignore_errors=True)

    print(f"\n{ PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
