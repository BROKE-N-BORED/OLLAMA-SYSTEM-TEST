"""End-to-end verification of OLLAMA SYSTEM TEST v10 against a fake Ollama
server and a fake OpenAI-compatible provider server, both with canned
answers per model. All 1005 v9 checks are carried over (the v9 sections
sit at the end: search/filter on the model + template lists, dark mode,
README fixes + repo hygiene); the v10 checks cover the real speech
clips, transcript normalization, and the fixture generator keeping
existing clips.

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
about 7 minutes. Exit code 0 = every check passed. Prints PASS/FAIL
per check.
"""

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
FAKEBIN = os.path.join(VERIFY_DIR, "fakebin")
FAKEBIN_SLEEP = os.path.join(VERIFY_DIR, "fakebin-sleep")
ORIGINAL_PATH = os.environ["PATH"]

os.environ["OST_RESULTS_DIR"] = RESULTS_DIR
os.environ["OST_PROVIDERS_FILE"] = PROVIDERS_FILE
os.environ["OST_CAPABILITIES_FILE"] = CAPABILITIES_FILE
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

MODELS = ["serve-model-a:2b", "serve-model-b:1b"]


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
            with open(pidfile, "w") as handle:
                handle.write(str(os.getpid()))
        calls = os.environ.get("FAKE_OLLAMA_CALLS")
        if calls:
            with open(calls, "a") as handle:
                handle.write("serve\\n")
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
    check("fmt_bytes None is dash", app.fmt_bytes(None) == "—")
    check("fmt_duration 0.5s is ms", app.fmt_duration(0.5) == "500 ms")
    check("fmt_score 100", app.fmt_score(100.0) == "100%")
    check("fmt_score None is dash", app.fmt_score(None) == "—")
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
    check("ollama_reachable false with no server",
          app.ollama_reachable() is False)

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
    for tid in ("coding", "math", "reasoning", "instruction", "knowledge"):
        check(f"{tid} has 5 tasks", len(by_id[tid]["tasks"]) == 5,
              str(len(by_id[tid].get("tasks", []))))
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
    check("screen_ollama_model keeps the cache on failure",
          app.screen_ollama_model("no-such-model:9b", {"no-such-model:9b": ["text", "vision"]})
          == {"text", "vision"})
    check("screen_ollama_model unknown model is text-only",
          app.screen_ollama_model("no-such-model:9b", {}) == {"text"})

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
        for brain, want in (("smart", 100.0), ("dumb", 0.0), ("partial", 60.0)):
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
    path1 = app.save_run_file(RESULTS_DIR, "fake-smart:2b", sid, dict(run_data))
    hash1 = file_hash(path1)
    path2 = app.save_run_file(RESULTS_DIR, "fake-smart:2b", sid, dict(run_data))
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
    # v8: the cost guard and the delete confirm are QMessageBox.question
    # dialogs, and a real modal dialog would hang the suite — nobody
    # is here to click it. The carried provider sections tick models
    # with no prices saved, so the guard would ask on every run.
    # Answer Yes to all of them for the carried sections; the v8
    # sections replace this stub with a controllable one (and restore
    # it when they finish), and the real dialog is restored at the
    # end of main().
    from PySide6.QtWidgets import QMessageBox
    _suite_real_question = QMessageBox.question
    QMessageBox.question = lambda *args, **kwargs: QMessageBox.Yes
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
    check("five tabs incl Compare and Providers", window.tabs.count() == 5
          and [window.tabs.tabText(i) for i in range(5)] ==
          ["Benchmark", "Past runs", "Profiles", "Compare", "Providers"],
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
        "fake-partial:3b": ("60%", "10.0", "done"),
        "fake-dumb:1b": ("0%", "5.0", "done"),
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
                  table_text(window, row, 3) == status, table_text(window, row, 3))
    broken_row = row_for(window, "fake-broken:1b")
    check("broken row present", broken_row is not None)
    if broken_row is not None:
        check("broken status",
              table_text(window, broken_row, 3) == "failed: model exploded",
              table_text(window, broken_row, 3))
        check("broken score is dash", table_text(window, broken_row, 1) == "—")
    order = [table_text(window, r, 0) for r in range(window.table.rowCount())]
    check("table sorted best score first, failed last",
          order == ["fake-smart:2b", "fake-partial:3b", "fake-dumb:1b", "fake-broken:1b"],
          str(order))

    # Detail pane: smart selected by default (row 0).
    detail = window.detail_view.toPlainText()
    check("detail shows smart coding 100%", "Coding: 100% (5/5)" in detail, detail[:400])
    check("detail shows per-task pass", "pass  fibonacci" in detail, detail[:400])
    check("detail shows speed unscored", "Speed: no scoring" in detail, detail[:400])
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
            check(f"partial {tid} 60%", run["template_score"] == 60.0,
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
            check(f"dumb {tid} 0%", run["template_score"] == 0.0,
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
              "None yet — no template under 50%." in prof, prof)

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
    window.tabs.setCurrentIndex(1)
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
    window.tabs.setCurrentIndex(2)
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
    check("regenerated profile weaknesses Coding and Math",
          "- Coding" in prof_text.split("## Weaknesses")[1]
          and "- Math" in prof_text.split("## Weaknesses")[1], prof_text)
    check("regenerated profile verdict Strong", "Strong on this PC" in prof_text, prof_text)
    check("regenerate leaves run files untouched", file_hash(coding_file) == coding_hash)

    # ---------- pull: success ----------
    window.tabs.setCurrentIndex(0)
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
          and table_text(window3, smart_row, 3) == "failed: " + app.UNREACHABLE_HINT,
          table_text(window3, smart_row, 3) if smart_row is not None else "no row")
    check("mid-stream partial never attempted",
          row_for(window3, "fake-partial:3b") is None)
    check("mid-stream close happened twice (warmup + retry)",
          server3.mid_stream_closes == 2, f"got {server3.mid_stream_closes}")
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
          and table_text(window4, smart_row, 3) == "failed: " + app.UNREACHABLE_HINT,
          table_text(window4, smart_row, 3) if smart_row is not None else "no row")
    window4.close()
    window4.deleteLater()
    qt_app.processEvents()
    killer.join(timeout=10)
    server4.server_close()

    # ---------- pre-run check: API dead, CLI fallback list, Run blocked ----------
    os.environ["PATH"] = FAKEBIN + os.pathsep + ORIGINAL_PATH
    os.environ["OLLAMA_HOST"] = f"http://127.0.0.1:{free_port()}"
    window5 = app.MainWindow()
    window5.show()
    wait_until(lambda: window5.model_list.count() == 2, timeout=10, app=qt_app)
    check("pre-run check window lists CLI models", window5.model_list.count() == 2,
          str(window5.model_list.count()))
    check("pre-run check run button enabled", window5.run_button.isEnabled())
    set_checked_list(window5.model_list, {"cli-model-a:2b"})
    set_checked_templates(window5, {"speed"})
    window5.run_button.click()
    qt_app.processEvents()
    time.sleep(0.5)
    qt_app.processEvents()
    check("pre-run check blocks the worker", window5.worker is None)
    check("pre-run check status is the Ollama-down run message",
          window5.status_label.text() == app.OLLAMA_DOWN_RUN_MESSAGE,
          window5.status_label.text())
    check("pre-run check table stays empty", window5.table.rowCount() == 0,
          str(window5.table.rowCount()))
    check("pre-run check run button stays enabled", window5.run_button.isEnabled())

    # ---------- Start Ollama: success, then already-running ----------
    fake_port = free_port()
    os.environ["OLLAMA_HOST"] = f"http://127.0.0.1:{fake_port}"
    pidfile = os.path.join(VERIFY_DIR, "fake-ollama.pid")
    callsfile = os.path.join(VERIFY_DIR, "serve-calls.txt")
    os.environ["FAKE_OLLAMA_PIDFILE"] = pidfile
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
    app.START_OLLAMA_TIMEOUT = 3.0
    app.START_OLLAMA_POLL = 0.2
    os.environ["PATH"] = FAKEBIN_SLEEP + os.pathsep + ORIGINAL_PATH
    os.environ["OLLAMA_HOST"] = f"http://127.0.0.1:{free_port()}"
    pidfile2 = os.path.join(VERIFY_DIR, "fake-ollama-sleep.pid")
    os.environ["FAKE_OLLAMA_PIDFILE"] = pidfile2
    window6 = app.MainWindow()
    window6.show()
    window6.start_ollama_button.click()
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
    window8 = app.MainWindow()
    window8.show()
    wait_until(lambda: window8.model_list.count() == 0, timeout=10, app=qt_app)
    check("ollama down: no models", window8.model_list.count() == 0)
    check("ollama down: status message exact",
          window8.status_label.text() == app.OLLAMA_DOWN_MESSAGE,
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
    check("fmt_cost None is dash", app.fmt_cost(None) == "—")
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
        for brain, want in (("smart", 100.0), ("dumb", 0.0), ("partial", 60.0)):
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
    windowP.tabs.setCurrentIndex(4)
    qt_app.processEvents()
    windowP.provider_name_input.setText("fake-provider")
    windowP.provider_url_input.setText(f"http://{phost}:{pport}/v1")
    windowP.provider_key_input.setText("sk-fake-good-key")
    windowP.provider_test_button.click()
    tested = wait_until(
        lambda: "Connected" in windowP.provider_test_status.text(),
        timeout=30, app=qt_app,
    )
    check("UI test connection succeeds", tested,
          windowP.provider_test_status.text())
    windowP.provider_model_id_input.setText("fake-gpt-smart")
    windowP.provider_model_name_input.setText("Smart")
    windowP.provider_price_in_input.setText("0.15")
    windowP.provider_price_out_input.setText("0.60")
    windowP.provider_add_model_button.click()
    qt_app.processEvents()
    check("UI add model with prices",
          windowP.provider_models_list.count() == 1
          and "fake-gpt-smart" in windowP.provider_models_list.item(0).text()
          and "$0.15" in windowP.provider_models_list.item(0).text(),
          windowP.provider_models_list.item(0).text()
          if windowP.provider_models_list.count() else "empty")
    windowP.provider_model_id_input.setText("fake-gpt-partial")
    windowP.provider_add_model_button.click()
    windowP.provider_model_id_input.setText("fake-gpt-dumb")
    windowP.provider_add_model_button.click()
    qt_app.processEvents()
    check("UI adds 3 provider models",
          windowP.provider_models_list.count() == 3,
          str(windowP.provider_models_list.count()))
    windowP.provider_model_id_input.setText("fake-gpt-smart")
    windowP.provider_add_model_button.click()
    qt_app.processEvents()
    check("UI rejects a duplicate model id",
          windowP.provider_models_list.count() == 3
          and "already on this provider" in windowP.provider_status.text(),
          windowP.provider_status.text())
    windowP.provider_model_id_input.setText("fake-gpt-x")
    windowP.provider_price_in_input.setText("abc")
    windowP.provider_add_model_button.click()
    qt_app.processEvents()
    check("UI rejects a bad price",
          windowP.provider_models_list.count() == 3
          and "must be a number" in windowP.provider_status.text(),
          windowP.provider_status.text())
    windowP.provider_price_in_input.setText("")
    windowP.provider_save_button.click()
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
    windowP.tabs.setCurrentIndex(4)
    qt_app.processEvents()
    windowP.grab().save(os.path.join(VERIFY_DIR, "screenshot-providers.png"))

    # Edit with the key field left empty: the stored key must survive.
    windowP.provider_save_button.click()
    qt_app.processEvents()
    with open(PROVIDERS_FILE, encoding="utf-8") as handle:
        saved_data = json.load(handle)
    check("re-saving with an empty key field keeps the stored key",
          saved_data["providers"][0]["api_key"] == "sk-fake-good-key")
    # Test connection with a wrong key typed in the form (not saved).
    windowP.provider_key_input.setText("sk-wrong")
    windowP.provider_test_button.click()
    bad_key_test = wait_until(
        lambda: "rejected the key" in windowP.provider_test_status.text(),
        timeout=30, app=qt_app,
    )
    check("UI test connection with a bad key fails plainly", bad_key_test,
          windowP.provider_test_status.text())
    windowP.provider_key_input.setText("")
    with open(PROVIDERS_FILE, encoding="utf-8") as handle:
        saved_data = json.load(handle)
    check("testing a bad key does not overwrite the saved key",
          saved_data["providers"][0]["api_key"] == "sk-fake-good-key")
    # Switching tabs must not wipe an edit in progress (the tab only
    # reloads when providers.json actually changed on disk).
    windowP.provider_name_input.setText("renamed-in-form")
    windowP.tabs.setCurrentIndex(0)
    qt_app.processEvents()
    windowP.tabs.setCurrentIndex(4)
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
              table_text(windowP, prow, 3) == "done", table_text(windowP, prow, 3))
        toks = table_text(windowP, prow, 2)
        check("provider row has a measured speed",
              toks != "—" and float(toks) > 100, toks)
    detail = windowP.detail_view.toPlainText()
    check("detail shows 1,580 prompt tokens", "1,580 in" in detail, detail[:300])
    check("detail shows 15,800 completion tokens", "15,800 out" in detail, detail[:300])
    check("detail shows exact cost $0.0097", "$0.0097" in detail, detail[:300])
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
              "Tokens used in template runs: 1,560 in, 15,600 out" in prof, prof)
        check("provider profile totals template cost",
              "Estimated cost in template runs: $0.0096" in prof, prof)
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
          and table_text(windowP, local_row, 3) == "done",
          table_text(windowP, local_row, 3) if local_row is not None else "no row")
    dead_row = row_for(windowP, "dead-provider / dead-model")
    check("dead provider model fails plainly",
          dead_row is not None
          and table_text(windowP, dead_row, 3)
          == "failed: provider dead-provider did not answer",
          table_text(windowP, dead_row, 3) if dead_row is not None else "no row")
    badkey_row = row_for(windowP, "badkey-provider / fake-gpt-smart")
    check("bad-key provider model fails plainly",
          badkey_row is not None
          and table_text(windowP, badkey_row, 3)
          == "failed: provider badkey-provider rejected the key: Incorrect API key provided",
          table_text(windowP, badkey_row, 3) if badkey_row is not None else "no row")

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

    # ---------- Qt app: mixed run still blocked when Ollama is down ----------
    os.environ["PATH"] = FAKEBIN + os.pathsep + ORIGINAL_PATH
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
    check("mixed run with Ollama down is blocked", windowP.worker is None)
    check("mixed run blocked status is the Ollama-down run message",
          windowP.status_label.text() == app.OLLAMA_DOWN_RUN_MESSAGE,
          windowP.status_label.text())
    os.environ["PATH"] = ORIGINAL_PATH

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
    windowP.tabs.setCurrentIndex(4)
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
    windowP.tabs.setCurrentIndex(4)
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
    windowP.tabs.setCurrentIndex(4)
    qt_app.processEvents()
    windowP.provider_new_button.click()
    qt_app.processEvents()
    check("capability checkboxes start with Text only",
          windowP.provider_cap_boxes["text"].isChecked()
          and not windowP.provider_cap_boxes["vision"].isChecked()
          and not windowP.provider_cap_boxes["image_generation"].isChecked()
          and not windowP.provider_cap_boxes["audio"].isChecked()
          and not windowP.provider_cap_boxes["tools"].isChecked()
          and not windowP.provider_cap_boxes["long_context"].isChecked())
    windowP.provider_name_input.setText("media-provider")
    windowP.provider_url_input.setText(f"http://{phost}:{pport}/v1")
    windowP.provider_key_input.setText("sk-fake-good-key")
    windowP.provider_model_id_input.setText("fake-gpt-smart")
    windowP.provider_cap_boxes["image_generation"].setChecked(True)
    windowP.provider_cap_boxes["audio"].setChecked(True)
    windowP.provider_add_model_button.click()
    qt_app.processEvents()
    check("provider models list shows the capability bracket",
          windowP.provider_models_list.count() == 1
          and windowP.provider_models_list.item(0).text()
          == "fake-gpt-smart [image_generation, audio]",
          windowP.provider_models_list.item(0).text()
          if windowP.provider_models_list.count() else "empty")
    windowP.grab().save(
        os.path.join(VERIFY_DIR, "screenshot-providers-caps.png"))
    # Update model: click it, change its capabilities, write back.
    media_item = windowP.provider_models_list.item(0)
    windowP.provider_models_list.itemClicked.emit(media_item)
    qt_app.processEvents()
    check("clicking a model loads its capabilities into the form",
          windowP.provider_cap_boxes["image_generation"].isChecked()
          and windowP.provider_cap_boxes["audio"].isChecked()
          and windowP.provider_model_id_input.text() == "fake-gpt-smart")
    windowP.provider_cap_boxes["audio"].setChecked(False)
    windowP.provider_update_model_button.click()
    qt_app.processEvents()
    check("Update model writes the changed capabilities",
          windowP.provider_models_list.item(0).text()
          == "fake-gpt-smart [image_generation]",
          windowP.provider_models_list.item(0).text())
    windowP.provider_models_list.itemClicked.emit(
        windowP.provider_models_list.item(0))
    qt_app.processEvents()
    windowP.provider_cap_boxes["audio"].setChecked(True)
    windowP.provider_update_model_button.click()
    qt_app.processEvents()
    check("Update model restores the audio capability",
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
        check("smart overall is a dash, not 0%",
              table_text(window6, row_for(window6, "fake-smart:2b"), 1) == "—",
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
        window6.tabs.setCurrentIndex(1)
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
    install = subprocess.run(
        ["sh", "install.sh"], cwd=install_good,
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
    check("__version__ is 10", app.__version__ == "10",
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
    check("title bar shows OLLAMA SYSTEM TEST v10",
          window7.windowTitle() == "OLLAMA SYSTEM TEST v10",
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
    check("File menu has Save report and Quit",
          file_items == ["Save report", "Quit"], str(file_items))
    help_menu7 = bar7.actions()[2].menu()
    check("Help menu has About",
          [a.text() for a in help_menu7.actions()] == ["About"],
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
          ollama_down.returncode == 1
          and "Ollama is not running" in ollama_down.stdout,
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
    check("math estimate: completion is 5 tasks x num_predict",
          math_c8 == 5 * by_id8["math"]["num_predict"], str(math_c8))
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
          "—" in csv8, csv8)
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
        check("window has 5 tabs with Compare after Profiles",
              window8.tabs.count() == 5
              and window8.tabs.tabText(3) == "Compare",
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

        # ---------- v8 T10: cost guard ------------------------------------
        set_checked_list(window8.model_list,
                         {"v8-provider / fake-gpt-smart"})
        set_checked_templates(window8, {"math"})
        question_answer8["answer"] = QMessageBox.No
        question_calls8.clear()
        window8.run_button.click()
        qt_app.processEvents()
        check("over-threshold provider run asks to confirm",
              len(question_calls8) == 1, str(len(question_calls8)))
        if question_calls8:
            check("confirm dialog names the estimated cost",
                  "$1.67" in question_calls8[0], question_calls8[0])
            check("confirm dialog names the model",
                  "v8-provider / fake-gpt-smart" in question_calls8[0],
                  question_calls8[0])
        check("answering No starts no run",
              window8.worker is None
              and window8.status_label.text() == "Run cancelled.",
              window8.status_label.text())
        question_answer8["answer"] = QMessageBox.Yes
        window8.run_button.click()
        done8 = wait_until(
            lambda: window8.worker is None
            and window8.save_button.isEnabled(),
            timeout=120, app=qt_app)
        check("answering Yes runs the provider model", done8)
        check("provider math run scored 100%",
              window8.results.get(
                  "v8-provider / fake-gpt-smart", {}).get(
                  "templates", {}).get("math", {}).get("template_score")
              == 100.0,
              str(window8.results.get("v8-provider / fake-gpt-smart", {})))

        # A cheap run proceeds with no dialog at all.
        set_checked_list(window8.model_list,
                         {"v8-provider / fake-gpt-partial"})
        set_checked_templates(window8, {"math"})
        question_calls8.clear()
        window8.run_button.click()
        done8c = wait_until(
            lambda: window8.worker is None
            and window8.save_button.isEnabled(),
            timeout=120, app=qt_app)
        check("cheap provider run asks no confirm",
              len(question_calls8) == 0, str(question_calls8))
        check("cheap provider run completes", done8c)
        check("partial math scored 60%",
              window8.results.get(
                  "v8-provider / fake-gpt-partial", {}).get(
                  "templates", {}).get("math", {}).get("template_score")
              == 60.0,
              str(window8.results.get("v8-provider / fake-gpt-partial", {})))

        # No prices saved: the estimate says so plainly and still asks.
        set_checked_list(window8.model_list,
                         {"v8-provider / fake-gpt-dumb"})
        set_checked_templates(window8, {"math"})
        question_answer8["answer"] = QMessageBox.No
        question_calls8.clear()
        window8.run_button.click()
        qt_app.processEvents()
        check("unknown-price run asks to confirm",
              len(question_calls8) == 1, str(len(question_calls8)))
        if question_calls8:
            check("unknown-price dialog says cost unknown plainly",
                  "no prices saved — cost unknown" in question_calls8[0],
                  question_calls8[0])
        check("answering No to the unknown-price run starts nothing",
              window8.worker is None)

        # A local-only run never sees the guard.
        set_checked_list(window8.model_list, {"fake-smart:2b"})
        set_checked_templates(window8, {"math"})
        question_calls8.clear()
        window8.run_button.click()
        done8l = wait_until(
            lambda: window8.worker is None
            and window8.save_button.isEnabled(),
            timeout=120, app=qt_app)
        check("local-only run asks no confirm",
              len(question_calls8) == 0, str(question_calls8))
        check("local run completes", done8l)

        # ---------- v8 T10: per-template time budget ----------------------
        # A real run through the worker: a 1-second budget on math
        # (5 tasks at ~0.65 s each against the fake) stops the
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
              == 5 - (budget_math8.get("budget_stopped") or 0),
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
              nobudget_math8.get("total") == 5
              and nobudget_math8.get("time_budget_reached") is not True
              and nobudget_math8.get("template_score") == 100.0,
              str(nobudget_math8.get("total")))
        # The history list marks the budget-stopped run.
        window8.tabs.setCurrentIndex(1)
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
        window8.tabs.setCurrentIndex(1)
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
        window8.tabs.setCurrentIndex(1)
        qt_app.processEvents()
        check("delete is disabled while a run is in progress",
              not window8.delete_run_button.isEnabled())
        window8.cancel_run()
        wait_until(lambda: window8.worker is None, timeout=60, app=qt_app)
        server8slow.shutdown()
        server8slow.server_close()
        os.environ["OLLAMA_HOST"] = f"http://{host8}:{port8}"
        window8.tabs.setCurrentIndex(1)
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
              == "Nothing to export yet — run a test first.",
              window8x.status_label.text())
        window8x.export_html()
        check("HTML export with no results says so plainly",
              window8x.status_label.text()
              == "Nothing to export yet — run a test first.",
              window8x.status_label.text())
        window8x.close()
        window8x.deleteLater()
        qt_app.processEvents()

        # Compare: two models side by side.
        window8.tabs.setCurrentIndex(3)
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

        check("compare shows smart math 100% vs partial 60%",
              compare_cell8("Math", 1) == "100%"
              and compare_cell8("Math", 2) == "60%",
              f"{compare_cell8('Math', 1)} / {compare_cell8('Math', 2)}")
        check("compare shows smart coding 100% vs partial 60%",
              compare_cell8("Coding", 1) == "100%"
              and compare_cell8("Coding", 2) == "60%",
              f"{compare_cell8('Coding', 1)} / {compare_cell8('Coding', 2)}")
        check("compare shows the overall rows",
              compare_cell8("Overall score", 1) == "100%"
              and compare_cell8("Overall score", 2) == "60%",
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
        check("title bar shows v10",
              window9.windowTitle() == "OLLAMA SYSTEM TEST v10",
              window9.windowTitle())
        check("__version__ is 10", app.__version__ == "10", app.__version__)

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
              visible_texts9(window9.template_list) == ["Math (5 tasks)"],
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
              visible_texts9(window9.template_list) == ["Math (5 tasks)"],
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
              and qt_app.styleSheet() == ""
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
              qt_app.styleSheet() == ""
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
        check("README states 103 prompts",
              "103 prompts" in readme9)
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

    QMessageBox.question = _suite_real_question

    shutil.rmtree(SCRATCH, ignore_errors=True)

    print(f"\n{ PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
