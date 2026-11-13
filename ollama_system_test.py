#!/usr/bin/env python3
"""OLLAMA SYSTEM TEST v8 - benchmark the Ollama models on this PC,
and cloud providers' models too.

Native Qt desktop window. Talks to Ollama on localhost:11434
(or the OLLAMA_HOST environment variable), and to any OpenAI-compatible
provider (OpenAI, OpenRouter, Groq, Together, DeepSeek, xAI, Mistral,
or a local OpenAI-compatible server) through /v1/chat/completions.
No web server, no browser. HTTP is stdlib urllib only; the only
third-party package is PySide6.

v2 added test templates (JSON files in templates/), per-task scoring,
and immutable per-model result folders organised per system:
results/<model>/<system>/<timestamp>-<template>.json

v3 fixes the real-PC failures from v2 (nothing ticked by default, the
run checks Ollama is answering before it starts, a Start Ollama button,
retry once, and the run stops instead of marching on when Ollama goes
away) and adds per-model profiles and pulling models from the app.

v4 adds PROVIDERS: provider accounts live in providers.json next to
the app (name, base URL, API key, models with optional prices per
1M tokens). Provider models appear in the model list as
"provider / model-id", run the same templates with the same scoring,
and get the same profiles and immutable run files. Run files record
tokens used; when a price is set, the details view shows the estimated
cost. A provider failing never stops local Ollama tests.

v5 adds 13 templates (19 total: Vision, Image Generation, Tool
Calling, Long Context, Multi-turn, Code Repair, Planning, Grounded QA,
Refusal, Summarization, Strict Structured Output, Role Following,
Audio) plus CAPABILITY SCREENING: every model is screened for what it
can do (Ollama reports it via /api/show, provider models get
capability ticks on the Providers tab), and templates no ticked model
can run are greyed out and untickable. The Speed template now measures
tokens per second at 1K, 8K, and 32K context, so prefill cost shows.

v6 is a repo-audit fix release (no new templates): a missing test
image or audio clip records its tasks as "not run" instead of
failing them, and the run never scores 0% for a missing file;
providers.json is written atomically and owner-only from the first
byte; the verification suite lives in the repo under tests/ with a
GitHub Actions workflow; launching without PySide6 prints a plain
message naming install.sh instead of a traceback; install.sh
verifies the install before reporting success, and PySide6-Essentials
is pinned to 6.12.0.

v7 is a repo-audit usability release (no new templates): a headless
mode (--headless --models ... --templates ...) runs the same worker
with no window, for scripts and cron; the window carries the version
in its title bar and an icon; window size and position survive a
restart, with keyboard shortcuts (Ctrl+R Run, Ctrl+S Save report,
Esc Cancel) and a minimal menu bar (File, Help, About showing the
version); and the app ships double-click desktop files — Install-OST
runs the installer, OST opens the app.

v8 is a repo-audit management release (no new templates): a run can
be deleted from the Past runs tab (run file and its media folder go,
the profile rebuilds from what is left) and the tab shows how much
disk the results store uses; pressing Run with provider models ticked
shows the estimated cost first and asks to confirm when it is over
$1.00 (or when no prices are saved, so the cost is unknown); an
opt-in per-template time budget stops a template's jobs at the limit
with a plain "time budget reached" status, default off; and the
current results export to CSV and HTML, with a Compare tab putting
two models' per-template scores side by side.

DURABLE RULE: nothing is ever ticked by default. Every model and every
template starts unchecked; the user ticks what they want to test.

KEY RULE: an API key lives only in providers.json and in memory while
a request is made. It is never written to a run file, a report, a
profile, or the status line, and it is never shown on screen.
"""

import argparse
import ast
import base64
import csv
import html
import io
import json
import os
import platform
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime

try:
    from PySide6.QtCore import (
        QCoreApplication, QSettings, Qt, QThread, QTimer, Signal,
    )
    from PySide6.QtGui import QAction, QIcon, QKeySequence, QShortcut
    from PySide6.QtWidgets import (
        QApplication,
        QCheckBox,
        QComboBox,
        QFileDialog,
        QHBoxLayout,
        QHeaderView,
        QLabel,
        QLineEdit,
        QListWidget,
        QListWidgetItem,
        QMainWindow,
        QMessageBox,
        QProgressBar,
        QPushButton,
        QSpinBox,
        QTabWidget,
        QTableWidget,
        QTableWidgetItem,
        QTextEdit,
        QVBoxLayout,
        QWidget,
    )
except ImportError:
    # Plain language, no traceback: the fix is install.sh, which puts
    # PySide6 in the app's private folder.
    print(
        "This app needs PySide6 to open its window, and it is not installed.",
        file=sys.stderr,
    )
    print("Run install.sh first — it installs PySide6 for you.", file=sys.stderr)
    sys.exit(1)

APP_NAME = "OLLAMA SYSTEM TEST"
# The one source of truth for the version: the title bar, the About
# box, and anything else that names the version reads this.
__version__ = "9"
APP_DIR = os.path.dirname(os.path.abspath(__file__))
WARMUP_PROMPT = "Say hello in one sentence."
WARMUP_PREDICT = 64
TEMPERATURE = 0
READ_TIMEOUT = 10.0       # seconds between streamed chunks before retrying
STALL_TIMEOUT = 120.0     # seconds with no chunk at all before giving up
CODE_TIMEOUT = 10.0       # seconds allowed for one code test case
ANSWER_TRUNCATE = 2000    # chars of raw answer kept in a run file
OLLAMA_DOWN_MESSAGE = "Ollama is not running. Start it first, then open this again."
OLLAMA_DOWN_RUN_MESSAGE = "Ollama is not running. Press Start Ollama, then Run again."
OLLAMA_DIED_MESSAGE = "Ollama stopped responding. Start Ollama, then press Refresh and Run again."
UNREACHABLE = "could not reach Ollama"
UNREACHABLE_HINT = "could not reach Ollama — start it, then press Refresh"
START_OLLAMA_TIMEOUT_MSG = "Ollama could not be started. Run ollama serve in a terminal to see the error."
START_OLLAMA_MISSING_MSG = "Ollama is not installed. Install it first, then try again."
NO_MODELS_MESSAGE = "No models installed yet. Pull one with ollama pull, then press Refresh."
NO_TEMPLATES_MESSAGE = "No test templates found in the templates folder."
# A task whose test file is missing never ran: it is recorded with
# passed None and one of these details, never as a failure.
NOT_RUN_IMAGE = "not run: test image not found"
NOT_RUN_AUDIO = "not run: test audio not found"
RETRY_DELAY = 3.0            # seconds before retrying a failed generate once
START_OLLAMA_TIMEOUT = 30.0  # seconds to wait for `ollama serve` to answer
START_OLLAMA_POLL = 1.0      # seconds between checks while waiting
PROVIDER_READ_TIMEOUT = 120.0   # seconds between provider chunks (providers are slow)
PROVIDER_STALL_TIMEOUT = 300.0  # seconds with no provider chunk at all before giving up
PROVIDER_TEST_TIMEOUT = 15.0    # seconds for the Test connection call
PROVIDER_LABEL_SEP = " / "      # "provider / model-id", used everywhere
SHOW_TIMEOUT = 2.0              # seconds for one /api/show screening call
LONG_CONTEXT_MIN_TOKENS = 32768  # a context this long counts as long-context
# The cost guard asks to confirm a provider run when the estimated
# cost is over this amount. $1.00: a plain round number, documented
# in the README. Under it the run starts with no extra step.
COST_CONFIRM_THRESHOLD_USD = 1.00
# Detail text on a task that never ran because its template's time
# budget ran out first. Like "not run", it is not a failure: the
# score counts only the tasks that actually ran.
BUDGET_REACHED = "time budget reached"
# Rough tokens per character for the pre-run cost estimate: about
# four characters per token. The estimate is an upper bound (see
# estimate_template_tokens), never a promise of the exact bill.
CHARS_PER_TOKEN = 4
# Capabilities a model can have. "text" is the baseline every chat model
# has; the rest gate the templates that need them. A template's
# "capability" field must be one of these.
CAPABILITIES = ("text", "vision", "tools", "image_generation", "audio",
                "long_context")
# Plain labels for the capability checkboxes on the Providers tab.
CAPABILITY_LABELS = {
    "text": "Text",
    "vision": "Vision",
    "tools": "Tools",
    "image_generation": "Image generation",
    "audio": "Audio",
    "long_context": "Long context",
}
# How a task is run. "generate" is one prompt in, one answer out (the
# v2-v4 shape). The rest need their own runner in the worker.
KNOWN_KINDS = ("generate", "chat", "vision", "tools", "image_gen",
               "long_context", "audio")
# Text rules score an answer string. tool_call / image / audio are
# scored by their runners (a tool call, image bytes, audio bytes),
# never from answer text.
KNOWN_RULES = ("contains", "contains_all", "numeric", "json", "code",
               "count_lines", "max_words", "refusal", "schema", "steps",
               "tool_call", "image", "audio")
TEXT_RULES = ("contains", "contains_all", "numeric", "json", "code",
              "count_lines", "max_words", "refusal", "schema", "steps")


class OllamaError(Exception):
    """Ollama could not do what was asked, in plain words."""


class Cancelled(Exception):
    """The user pressed Cancel."""


class OllamaUnreachable(Exception):
    """Ollama stopped answering mid-run; the whole run must stop."""


class ProviderError(Exception):
    """A provider call failed, in plain words. Never stops the run."""


class ProviderUnreachable(ProviderError):
    """A provider did not answer at all; the call may be retried once."""


def is_unreachable(exc):
    """True when an error means Ollama itself went away (not one bad model)."""
    return str(exc).startswith(UNREACHABLE)


def is_provider_unreachable(exc):
    """True when an error means the provider itself did not answer."""
    return isinstance(exc, ProviderUnreachable)


def describe_error(exc):
    """Plain failure text: unreachable errors get the actionable hint."""
    if is_unreachable(exc):
        return UNREACHABLE_HINT
    return str(exc)


def ollama_base():
    return os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")


def get_templates_dir():
    return os.environ.get(
        "OST_TEMPLATES_DIR", os.path.join(APP_DIR, "templates")
    )


def get_results_dir():
    return os.environ.get(
        "OST_RESULTS_DIR", os.path.join(APP_DIR, "results")
    )


def get_providers_file():
    return os.environ.get(
        "OST_PROVIDERS_FILE", os.path.join(APP_DIR, "providers.json")
    )


def get_capabilities_file():
    return os.environ.get(
        "OST_CAPABILITIES_FILE", os.path.join(APP_DIR, "capabilities.json")
    )


def get_images_dir():
    return os.environ.get(
        "OST_IMAGES_DIR", os.path.join(APP_DIR, "images")
    )


def get_audio_dir():
    return os.environ.get(
        "OST_AUDIO_DIR", os.path.join(APP_DIR, "audio")
    )


def ensure_media_dirs():
    """Create the test-image and test-clip folders if they are not
    there yet, so a user adding their own image or recording has
    somewhere to put it. Called on every launch; harmless when the
    folders already exist."""
    os.makedirs(get_images_dir(), exist_ok=True)
    os.makedirs(get_audio_dir(), exist_ok=True)


# The dark theme (T14): dark backgrounds with light text throughout,
# covering every widget the window uses — lists, tables, text edits,
# tabs, buttons, combo boxes, menus, the progress bar. Light stays the
# default; this only applies when the user toggles View > Dark mode.
DARK_STYLESHEET = """
QWidget {
    background-color: #2b2b2b;
    color: #e6e6e6;
    selection-background-color: #3a6ea5;
    selection-color: #ffffff;
}
QMainWindow {
    background-color: #2b2b2b;
}
QListWidget, QTableWidget, QTextEdit, QLineEdit, QComboBox, QSpinBox {
    background-color: #1e1e1e;
    color: #e6e6e6;
    border: 1px solid #555555;
}
QListWidget::item:selected, QTableWidget::item:selected {
    background-color: #3a6ea5;
    color: #ffffff;
}
QListWidget::item:disabled {
    color: #8a8a8a;
}
QHeaderView::section {
    background-color: #3a3a3a;
    color: #e6e6e6;
    border: 1px solid #555555;
}
QTabWidget::pane {
    border: 1px solid #555555;
    background-color: #2b2b2b;
}
QTabBar::tab {
    background-color: #3a3a3a;
    color: #e6e6e6;
    border: 1px solid #555555;
    padding: 6px 12px;
}
QTabBar::tab:selected {
    background-color: #1e1e1e;
    color: #ffffff;
}
QPushButton {
    background-color: #3a3a3a;
    color: #e6e6e6;
    border: 1px solid #555555;
    padding: 4px 12px;
}
QPushButton:hover {
    background-color: #4a4a4a;
}
QPushButton:pressed {
    background-color: #2f5d8a;
}
QPushButton:disabled {
    color: #8a8a8a;
}
QComboBox::drop-down {
    border: none;
}
QComboBox QAbstractItemView {
    background-color: #1e1e1e;
    color: #e6e6e6;
    selection-background-color: #3a6ea5;
}
QMenuBar {
    background-color: #2b2b2b;
    color: #e6e6e6;
}
QMenuBar::item:selected {
    background-color: #3a6ea5;
}
QMenu {
    background-color: #2b2b2b;
    color: #e6e6e6;
    border: 1px solid #555555;
}
QMenu::item:selected {
    background-color: #3a6ea5;
    color: #ffffff;
}
QProgressBar {
    background-color: #1e1e1e;
    color: #e6e6e6;
    border: 1px solid #555555;
}
QProgressBar::chunk {
    background-color: #3a6ea5;
}
QScrollBar:vertical {
    background-color: #2b2b2b;
    width: 14px;
}
QScrollBar::handle:vertical {
    background-color: #555555;
}
QScrollBar::handle:vertical:hover {
    background-color: #666666;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
    background-color: #2b2b2b;
}
QScrollBar:horizontal {
    background-color: #2b2b2b;
    height: 14px;
}
QScrollBar::handle:horizontal {
    background-color: #555555;
}
QScrollBar::handle:horizontal:hover {
    background-color: #666666;
}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal,
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
    background-color: #2b2b2b;
}
QToolTip {
    background-color: #3a3a3a;
    color: #e6e6e6;
    border: 1px solid #555555;
}
QWidget:disabled {
    color: #8a8a8a;
}
"""


def apply_theme(dark):
    """Apply the dark stylesheet to the running application, or clear
    it back to the default light theme. A no-op without a
    QApplication — headless mode runs under a QCoreApplication,
    which has no window to restyle."""
    app = QApplication.instance()
    if not isinstance(app, QApplication):
        return
    app.setStyleSheet(DARK_STYLESHEET if dark else "")


def tok_s(count, duration_ns):
    """Tokens per second from an Ollama count and a nanosecond duration."""
    if count is None or duration_ns is None or duration_ns <= 0:
        return None
    return count / (duration_ns / 1_000_000_000)


def ns_to_s(value_ns):
    if value_ns is None:
        return None
    return value_ns / 1_000_000_000


def fmt_bytes(size):
    if size is None:
        return "—"
    if size >= 1_000_000_000:
        return f"{size / 1_000_000_000:.1f} GB"
    if size >= 1_000_000:
        return f"{size / 1_000_000:.0f} MB"
    if size >= 1_000:
        return f"{size / 1_000:.0f} KB"
    return f"{size} B"


def fmt_duration(seconds):
    if seconds is None:
        return "—"
    if seconds < 1:
        return f"{seconds * 1000:.0f} ms"
    return f"{seconds:.2f} s"


def fmt_toks(value):
    if value is None:
        return "—"
    return f"{value:.1f}"


def fmt_score(value):
    if value is None:
        return "—"
    return f"{value:.0f}%"


def fmt_tokens(count):
    if count is None:
        return "—"
    return f"{count:,}"


def estimate_cost(prompt_tokens, completion_tokens,
                  price_input_per_1m, price_output_per_1m):
    """Cost in dollars, or None when tokens or prices are missing.

    Never invents a cost: both prices must be set and both token counts
    known, or there is no number.
    """
    if prompt_tokens is None or completion_tokens is None:
        return None
    if price_input_per_1m is None or price_output_per_1m is None:
        return None
    return (
        prompt_tokens * price_input_per_1m / 1_000_000
        + completion_tokens * price_output_per_1m / 1_000_000
    )


def fmt_cost(value):
    """Dollars: 2 places for real money, 4 for fractions of a cent."""
    if value is None:
        return "—"
    if value == 0:
        return "$0.00"
    if value < 0.01:
        return f"${value:.4f}"
    return f"${value:.2f}"


def provider_label(provider_name, model_id):
    """One label format for provider models, used everywhere."""
    return f"{provider_name}{PROVIDER_LABEL_SEP}{model_id}"


def _chars_to_tokens(chars):
    """Rough token count for a stretch of text: about four
    characters per token, rounded up. Empty text is zero tokens."""
    if not chars:
        return 0
    return (chars + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def estimate_template_tokens(template):
    """Predictable (prompt, completion) token counts for one template.

    The same token math the run files record, predicted before the
    run: prompt tokens from the text the run will send, completion
    tokens from num_predict — the most the run asks for on each call.
    The estimate is an UPPER BOUND: a model usually stops well short
    of num_predict, so the real bill is usually lower.

    What is counted, per task kind:
    - generate / vision / tools: the prompt text (plus the tool
      definitions for a tools task), one call each. Image tokens are
      not counted — the run files do not record them either.
    - chat: every turn is a call, and each turn sends the whole
      history so far, so the prompt grows turn by turn (plus the
      system prompt, when the task has one).
    - long_context: the context length is exact prompt tokens, plus
      the question text.
    - image_gen / audio: no text tokens at all — the run files record
      None for those tasks, and so does this estimate.
    - unscored: context lengths are exact prompt tokens (Speed uses
      its lengths and ignores its plain prompts, same as the worker);
      otherwise the plain prompts.
    """
    num_predict = template.get("num_predict", 0)
    prompt_tokens = 0
    completion_tokens = 0
    if template.get("scored"):
        for task in template.get("tasks", []):
            kind = task.get("kind", "generate")
            if kind in ("image_gen", "audio"):
                continue
            if kind == "chat":
                turns = task.get("turns", [])
                system_chars = len(task.get("system") or "")
                seen_chars = 0
                for turn in turns:
                    seen_chars += len(turn.get("prompt") or "")
                    prompt_tokens += _chars_to_tokens(
                        system_chars + seen_chars
                    )
                    completion_tokens += num_predict
                continue
            if kind == "long_context":
                prompt_tokens += task.get("context_length", 0)
                prompt_tokens += _chars_to_tokens(
                    len(task.get("prompt") or "")
                )
                completion_tokens += num_predict
                continue
            chars = len(task.get("prompt") or "")
            if kind == "tools" and task.get("tools"):
                chars += len(json.dumps(task["tools"]))
            prompt_tokens += _chars_to_tokens(chars)
            completion_tokens += num_predict
    elif template.get("context_lengths"):
        for length in template["context_lengths"]:
            prompt_tokens += length
            completion_tokens += num_predict
    else:
        for prompt in template.get("prompts", []):
            prompt_tokens += _chars_to_tokens(len(prompt))
            completion_tokens += num_predict
    return prompt_tokens, completion_tokens


def estimate_run_cost(targets, templates):
    """Pre-run cost estimate for the provider targets in a run.

    Returns a dict: "per_model" is one entry per provider target
    (label, cost-or-None, prompt tokens, completion tokens,
    has-prices); "total" is the summed cost, or None when any
    provider model has no prices saved; "unknown" lists the models
    without prices; "provider_models" lists every provider label.
    Ollama targets are free and never appear. The warmup call is
    counted (the provider charges for it too), for text models only
    — a model with no text capability gets a connection test instead
    of a text warmup, same as the worker. Templates a target cannot
    run are skipped, same as the worker.
    """
    per_model = []
    total = 0.0
    unknown = []
    for target in targets:
        if target["kind"] != "provider":
            continue
        prompt_tokens = 0
        completion_tokens = 0
        if "text" in set(target.get("capabilities") or ["text"]):
            prompt_tokens += _chars_to_tokens(len(WARMUP_PROMPT))
            completion_tokens += WARMUP_PREDICT
        for template in templates:
            if not template_eligible(template, target):
                continue
            template_prompt, template_completion = (
                estimate_template_tokens(template)
            )
            prompt_tokens += template_prompt
            completion_tokens += template_completion
        cost = estimate_cost(
            prompt_tokens, completion_tokens,
            target["price_input_per_1m"], target["price_output_per_1m"],
        )
        has_prices = (
            target["price_input_per_1m"] is not None
            and target["price_output_per_1m"] is not None
        )
        if cost is None:
            unknown.append(target["label"])
        else:
            total += cost
        per_model.append({
            "label": target["label"],
            "cost": cost,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "has_prices": has_prices,
        })
    return {
        "per_model": per_model,
        "total": None if unknown else total,
        "unknown": unknown,
        "provider_models": [entry["label"] for entry in per_model],
    }


# ---------------------------------------------------------------------------
# Media bytes (image generation + audio): what came back, and is it real?
# ---------------------------------------------------------------------------
# stdlib only: PNG, JPEG, and GIF dimensions are read straight from the
# headers, and audio is checked by its magic bytes. No image or audio
# library is needed, on his PC or anywhere else.

def _png_info(data):
    """(format, width, height) for a PNG, or None."""
    if len(data) < 33 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    if data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    if width <= 0 or height <= 0:
        return None
    return ("png", width, height)


def _jpeg_info(data):
    """(format, width, height) for a JPEG, or None."""
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None
    pos = 2
    while pos + 9 < len(data):
        if data[pos] != 0xFF:
            return None
        marker = data[pos + 1]
        if marker in (0xC0, 0xC1, 0xC2):  # start-of-frame: dimensions live here
            height, width = struct.unpack(">HH", data[pos + 5:pos + 9])
            if width <= 0 or height <= 0:
                return None
            return ("jpeg", width, height)
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        length = struct.unpack(">H", data[pos + 2:pos + 4])[0]
        if length < 2:
            return None
        pos += 2 + length
    return None


def _gif_info(data):
    """(format, width, height) for a GIF, or None."""
    if len(data) < 10 or data[:6] not in (b"GIF87a", b"GIF89a"):
        return None
    width, height = struct.unpack("<HH", data[6:10])
    if width <= 0 or height <= 0:
        return None
    return ("gif", width, height)


def image_info(data):
    """(format, width, height) for PNG/JPEG/GIF bytes, or None.

    None means the bytes are not a picture at all — the check the
    image-generation template scores on.
    """
    if not data:
        return None
    for probe in (_png_info, _jpeg_info, _gif_info):
        info = probe(data)
        if info is not None:
            return info
    return None


def is_audio_bytes(data):
    """True when the bytes look like audio (WAV, MP3, OGG, MP4/M4A, FLAC).

    A speech-generation call that returns nothing, or returns JSON
    error text, fails this check.
    """
    if not data or len(data) < 12:
        return False
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return True
    if data[:3] == b"ID3":
        return True
    if data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return True  # MP3 frame sync
    if data[:4] == b"OggS":
        return True
    if data[4:8] == b"ftyp":
        return True
    if data[:4] == b"fLaC":
        return True
    return False


def _http_error_reason(exc):
    """Pull Ollama's plain error message out of an HTTP error, if it gave one."""
    try:
        body = exc.read().decode("utf-8", "replace")
        data = json.loads(body)
        if isinstance(data, dict) and data.get("error"):
            return str(data["error"]).strip().splitlines()[0]
    except Exception:
        pass
    return f"Ollama returned HTTP {exc.code}"


def http_get_json(path, timeout=10):
    try:
        with urllib.request.urlopen(ollama_base() + path, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise OllamaError(_http_error_reason(exc))
    except (urllib.error.URLError, socket.timeout, OSError):
        raise OllamaError("could not reach Ollama")


def ollama_reachable():
    """True when Ollama answers on its HTTP API right now."""
    try:
        http_get_json("/api/tags", timeout=5)
        return True
    except OllamaError:
        return False


def list_models_cli():
    """Fallback: parse `ollama list` output. Returns [] on any failure."""
    try:
        out = subprocess.run(
            ["ollama", "list"], capture_output=True, text=True, timeout=30
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []
    if out.returncode != 0:
        return []
    names = []
    for line in out.stdout.splitlines()[1:]:
        parts = line.split()
        if parts:
            names.append(parts[0])
    return names


def list_models():
    """Returns (names, source) where source is 'api' or 'ollama list'.

    Raises OllamaError if neither the API nor the CLI can list models.
    """
    try:
        data = http_get_json("/api/tags", timeout=10)
        names = [m.get("name") for m in data.get("models", []) if m.get("name")]
        return names, "api"
    except OllamaError:
        pass
    names = list_models_cli()
    if names:
        return names, "ollama list"
    raise OllamaError(OLLAMA_DOWN_MESSAGE)


def ps_sizes():
    """Map of loaded model name -> size in RAM (bytes). {} if unavailable."""
    try:
        data = http_get_json("/api/ps", timeout=10)
    except OllamaError:
        return {}
    sizes = {}
    for model in data.get("models", []):
        if model.get("name") and model.get("size"):
            sizes[model["name"]] = model["size"]
    return sizes


def ollama_show(model):
    """POST /api/show for one model. Returns the raw response dict.

    Raises OllamaError when Ollama does not answer or does not know
    the model. A short timeout: screening must never stall the window.
    """
    payload = {"name": model}
    req = urllib.request.Request(
        ollama_base() + "/api/show",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=SHOW_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise OllamaError(_http_error_reason(exc))
    except (urllib.error.URLError, socket.timeout, OSError):
        raise OllamaError("could not reach Ollama")


def capabilities_from_show(data):
    """Map an /api/show response to a set of our capability names.

    Ollama reports capabilities like ["completion", "vision", "tools"].
    Long context comes from the model info: any "*context_length" of
    32K or more. When Ollama says nothing about context length, long
    context stays on — any text model can attempt the test, and the
    template scales its documents; the score says whether it coped.
    Image generation and audio never come from Ollama: it has no
    endpoints for either.
    """
    caps = {"text"}
    if not isinstance(data, dict):
        return caps
    for raw in data.get("capabilities") or []:
        if raw in ("vision", "tools"):
            caps.add(raw)
    info = data.get("model_info") or {}
    context_length = None
    if isinstance(info, dict):
        for key, value in info.items():
            if key.endswith("context_length") and isinstance(value, int):
                context_length = value
                break
    if context_length is None or context_length >= LONG_CONTEXT_MIN_TOKENS:
        caps.add("long_context")
    return caps


def load_capabilities(path=None):
    """Load capabilities.json: {"ollama": {model: [caps...]}}.

    A missing or broken file means nothing screened yet, not an error.
    Unknown capability names are dropped, never a crash.
    """
    if path is None:
        path = get_capabilities_file()
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (FileNotFoundError, ValueError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    ollama_caps = data.get("ollama")
    if not isinstance(ollama_caps, dict):
        return {}
    caps = {}
    for model, raw in ollama_caps.items():
        if not isinstance(model, str) or not isinstance(raw, list):
            continue
        kept = {c for c in raw if c in CAPABILITIES}
        kept.add("text")
        caps[model] = sorted(kept)
    return caps


def save_capabilities(caps, path=None):
    """Write capabilities.json. No keys live in this file."""
    if path is None:
        path = get_capabilities_file()
    data = {"ollama": {model: sorted(values) for model, values in caps.items()}}
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)


def screen_ollama_model(model, cache):
    """Screen one Ollama model via /api/show, updating the cache.

    Returns the capability set. Any failure leaves the cached value
    alone (or text-only when there is none) — screening must never
    break the model list.
    """
    try:
        data = ollama_show(model)
    except OllamaError:
        return set(cache.get(model, ["text"]))
    caps = capabilities_from_show(data)
    cache[model] = sorted(caps)
    return caps


def chat_once(model, messages, num_predict, temperature=0, tools=None,
              should_stop=None):
    """One streaming /api/chat call. Returns the generate_once metrics
    shape, plus "tool_calls" (a list of {"name", "arguments"} dicts).

    Messages are {"role", "content", "images"?} dicts; images are
    base64 strings on the user message. Tools use Ollama's shape:
    [{"type": "function", "function": {"name", "description",
    "parameters"}}]. Ollama sends any tool calls in the final message.

    Raises OllamaError on failure, Cancelled if should_stop() fires.
    """
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "options": {"num_predict": num_predict, "temperature": temperature},
    }
    if tools:
        payload["tools"] = tools
    req = urllib.request.Request(
        ollama_base() + "/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    t_start = time.monotonic()
    try:
        resp = urllib.request.urlopen(req, timeout=READ_TIMEOUT)
    except urllib.error.HTTPError as exc:
        raise OllamaError(_http_error_reason(exc))
    except (urllib.error.URLError, socket.timeout, OSError):
        raise OllamaError("could not reach Ollama")
    t_first = None
    done = None
    answer_parts = []
    tool_calls = []
    last_chunk = time.monotonic()
    try:
        while True:
            if should_stop is not None and should_stop():
                raise Cancelled()
            try:
                line = resp.readline()
            except socket.timeout:
                if time.monotonic() - last_chunk > STALL_TIMEOUT:
                    raise OllamaError("Ollama stopped responding")
                continue
            except OSError:
                raise OllamaError("could not reach Ollama")
            if not line:
                break
            last_chunk = time.monotonic()
            line = line.strip()
            if not line:
                continue
            try:
                chunk = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                continue
            message = chunk.get("message") or {}
            text = message.get("content") or ""
            if text:
                if t_first is None:
                    t_first = time.monotonic()
                answer_parts.append(text)
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                name = function.get("name")
                args = function.get("arguments") or {}
                if name and not any(c["name"] == name for c in tool_calls):
                    tool_calls.append({"name": name, "arguments": args})
            if chunk.get("done"):
                done = chunk
                break
    finally:
        resp.close()
    if done is None:
        raise OllamaError("could not reach Ollama")
    return {
        "prompt": "",
        "answer": "".join(answer_parts),
        "decode_tok_s": tok_s(done.get("eval_count"), done.get("eval_duration")),
        "prompt_tok_s": tok_s(
            done.get("prompt_eval_count"), done.get("prompt_eval_duration")
        ),
        "ttft_s": (t_first - t_start) if t_first is not None else None,
        "load_s": ns_to_s(done.get("load_duration")),
        "total_s": ns_to_s(done.get("total_duration")),
        "eval_count": done.get("eval_count"),
        "eval_duration_ns": done.get("eval_duration"),
        "prompt_eval_count": done.get("prompt_eval_count"),
        "prompt_eval_duration_ns": done.get("prompt_eval_duration"),
        "wall_s": time.monotonic() - t_start,
        "tool_calls": tool_calls,
    }


def generate_once(model, prompt, num_predict, temperature=0, should_stop=None):
    """One streaming /api/generate call. Returns measured metrics + answer.

    Raises OllamaError on failure, Cancelled if should_stop() becomes true.
    """
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": True,
        "options": {"num_predict": num_predict, "temperature": temperature},
    }
    req = urllib.request.Request(
        ollama_base() + "/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    t_start = time.monotonic()
    try:
        resp = urllib.request.urlopen(req, timeout=READ_TIMEOUT)
    except urllib.error.HTTPError as exc:
        raise OllamaError(_http_error_reason(exc))
    except (urllib.error.URLError, socket.timeout, OSError):
        raise OllamaError("could not reach Ollama")
    t_first = None
    done = None
    answer_parts = []
    last_chunk = time.monotonic()
    try:
        while True:
            if should_stop is not None and should_stop():
                raise Cancelled()
            try:
                line = resp.readline()
            except socket.timeout:
                if time.monotonic() - last_chunk > STALL_TIMEOUT:
                    raise OllamaError("Ollama stopped responding")
                continue
            except OSError:
                # Timeout is an OSError subclass, so this branch comes
                # AFTER it: a reset or dropped connection mid-stream
                # means Ollama went away, not that one model misbehaved.
                raise OllamaError("could not reach Ollama")
            if not line:
                break
            last_chunk = time.monotonic()
            line = line.strip()
            if not line:
                continue
            try:
                chunk = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                continue
            text = chunk.get("response") or ""
            if text:
                if t_first is None:
                    t_first = time.monotonic()
                answer_parts.append(text)
            if chunk.get("done"):
                done = chunk
                break
    finally:
        resp.close()
    if done is None:
        # The stream ended before Ollama sent its done event: the
        # connection went away mid-answer, so this is unreachable too.
        raise OllamaError("could not reach Ollama")
    return {
        "prompt": prompt,
        "answer": "".join(answer_parts),
        "decode_tok_s": tok_s(done.get("eval_count"), done.get("eval_duration")),
        "prompt_tok_s": tok_s(
            done.get("prompt_eval_count"), done.get("prompt_eval_duration")
        ),
        "ttft_s": (t_first - t_start) if t_first is not None else None,
        "load_s": ns_to_s(done.get("load_duration")),
        "total_s": ns_to_s(done.get("total_duration")),
        "eval_count": done.get("eval_count"),
        "eval_duration_ns": done.get("eval_duration"),
        "prompt_eval_count": done.get("prompt_eval_count"),
        "prompt_eval_duration_ns": done.get("prompt_eval_duration"),
        "wall_s": time.monotonic() - t_start,
    }


# ---------------------------------------------------------------------------
# Providers (OpenAI-compatible: OpenAI, OpenRouter, Groq, Together,
# DeepSeek, xAI, Mistral, or a local OpenAI-compatible server)
# ---------------------------------------------------------------------------
# Provider accounts live in providers.json next to the app. That file
# holds API keys: it is never written anywhere else, and no key is ever
# written to a run file, a report, a profile, or the status line.

def load_providers(path=None):
    """Load providers.json. Returns (providers, warnings).

    A missing file means no providers yet, not an error. A broken file
    or a broken entry is skipped with a plain warning, never a crash.
    """
    if path is None:
        path = get_providers_file()
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return [], []
    except (ValueError, OSError) as exc:
        return [], [f"providers.json: skipped, {exc}"]
    if not isinstance(data, dict) or not isinstance(data.get("providers"), list):
        return [], ["providers.json: skipped, expected an object with a providers list"]
    providers = []
    warnings = []
    seen_names = set()
    for entry in data["providers"]:
        if not isinstance(entry, dict):
            warnings.append("providers.json: a provider is not an object, skipped")
            continue
        name = entry.get("name")
        base_url = entry.get("base_url")
        if not name or not isinstance(name, str):
            warnings.append("providers.json: a provider has no name, skipped")
            continue
        if name in seen_names:
            warnings.append(f"providers.json: duplicate provider {name!r}, skipped")
            continue
        if not base_url or not isinstance(base_url, str):
            warnings.append(f"providers.json: provider {name!r} has no base URL, skipped")
            continue
        api_key = entry.get("api_key", "")
        if not isinstance(api_key, str):
            warnings.append(f"providers.json: provider {name!r} has a bad API key, skipped")
            continue
        models = []
        raw_models = entry.get("models", [])
        if not isinstance(raw_models, list):
            warnings.append(f"providers.json: provider {name!r} has no models list, skipped")
            continue
        for raw_model in raw_models:
            if not isinstance(raw_model, dict):
                warnings.append(f"providers.json: a model on {name!r} is not an object, skipped")
                continue
            model_id = raw_model.get("id")
            if not model_id or not isinstance(model_id, str):
                warnings.append(f"providers.json: a model on {name!r} has no id, skipped")
                continue
            display_name = raw_model.get("display_name", "")
            if not isinstance(display_name, str):
                display_name = ""
            price_in = raw_model.get("price_input_per_1m")
            price_out = raw_model.get("price_output_per_1m")
            if isinstance(price_in, bool) or not isinstance(price_in, (int, float, type(None))):
                warnings.append(f"providers.json: model {model_id!r} has a bad input price, skipped")
                continue
            if isinstance(price_out, bool) or not isinstance(price_out, (int, float, type(None))):
                warnings.append(f"providers.json: model {model_id!r} has a bad output price, skipped")
                continue
            # Capabilities: what this model can do. A model with no
            # capabilities listed is text-only (the v4 shape, and the
            # default for a new model). Unknown names are dropped with
            # a warning, never a crash. A model with no "text" at all
            # (an image-only or audio-only model) skips the text
            # templates and the chat warmup.
            raw_caps = raw_model.get("capabilities")
            if raw_caps is None:
                capabilities = ["text"]
            elif not isinstance(raw_caps, list):
                warnings.append(
                    f"providers.json: model {model_id!r} has bad capabilities, text-only assumed"
                )
                capabilities = ["text"]
            else:
                capabilities = []
                for cap in raw_caps:
                    if cap in CAPABILITIES and cap not in capabilities:
                        capabilities.append(cap)
                    else:
                        warnings.append(
                            f"providers.json: model {model_id!r} has unknown capability {cap!r}, dropped"
                        )
                if not capabilities:
                    capabilities = ["text"]
            models.append({
                "id": model_id,
                "display_name": display_name,
                "price_input_per_1m": price_in,
                "price_output_per_1m": price_out,
                "capabilities": capabilities,
            })
        seen_names.add(name)
        providers.append({
            "name": name,
            "base_url": base_url.rstrip("/"),
            "api_key": api_key,
            "models": models,
        })
    return providers, warnings


def save_providers(providers, path=None):
    """Write providers.json atomically. The file holds API keys, so the
    write goes to a temporary file in the same directory, that file is
    chmodded 0o600 BEFORE any key bytes are written to it, and only
    then is it renamed over providers.json. A crash mid-write leaves
    the old file intact, and at no point are the keys world-readable.
    A failed write removes the temporary file and raises."""
    if path is None:
        path = get_providers_file()
    data = {"providers": providers}
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp_path = tempfile.mkstemp(
        prefix=".providers-", suffix=".tmp", dir=directory
    )
    try:
        # Owner-only before a single key byte is written.
        os.chmod(tmp_path, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = None  # fdopen owns the descriptor now
            json.dump(data, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _provider_error_reason(exc):
    """Pull a provider's plain error message out of an HTTP error.

    Handles both shapes: {"error": "text"} and the OpenAI shape
    {"error": {"message": "text", ...}}. Never includes any key.
    """
    try:
        body = exc.read().decode("utf-8", "replace")
        data = json.loads(body)
        if isinstance(data, dict):
            error = data.get("error")
            if isinstance(error, str) and error.strip():
                return error.strip().splitlines()[0]
            if isinstance(error, dict) and error.get("message"):
                return str(error["message"]).strip().splitlines()[0]
    except Exception:
        pass
    return f"HTTP {exc.code}"


def _provider_headers(provider):
    headers = {"Content-Type": "application/json"}
    key = provider.get("api_key") or ""
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def provider_test_connection(provider):
    """GET {base}/models. Returns (ok, plain message). Spends no tokens."""
    name = provider.get("name") or "provider"
    base_url = (provider.get("base_url") or "").rstrip("/")
    if not base_url:
        return False, "No base URL set."
    req = urllib.request.Request(
        base_url + "/models", headers=_provider_headers(provider)
    )
    try:
        with urllib.request.urlopen(req, timeout=PROVIDER_TEST_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            reason = _provider_error_reason(exc)
            if reason.startswith("HTTP "):
                return False, f"provider {name} rejected the key."
            return False, f"provider {name} rejected the key: {reason}"
        return False, f"provider {name}: {_provider_error_reason(exc)}"
    except (urllib.error.URLError, socket.timeout, OSError):
        return False, f"provider {name} did not answer. Check the base URL."
    except ValueError:
        return False, f"provider {name} answered, but not with JSON."
    models = data.get("data") if isinstance(data, dict) else None
    if isinstance(models, list):
        return True, f"Connected — {name} answered, {len(models)} models listed."
    return True, f"Connected — {name} answered."


def provider_chat_once(provider, model_id, messages, num_predict,
                       temperature=0, tools=None, should_stop=None):
    """One streaming /v1/chat/completions call. Returns the same metrics
    shape as generate_once, plus prompt/completion/total tokens when the
    provider reports usage, plus "tool_calls" (a list of {"name",
    "arguments"} dicts).

    Messages are OpenAI chat messages; content may be a string or a
    list of parts ({"type": "text", ...} / {"type": "image_url", ...})
    for vision tasks. Tools use the OpenAI shape:
    [{"type": "function", "function": {"name", "description",
    "parameters"}}]. Tool-call arguments arrive as a JSON string,
    possibly split across chunks, and are parsed at the end of the
    stream; unparseable arguments come back as the raw string.

    Raises ProviderUnreachable when the provider did not answer at all
    (retriable), ProviderError for anything else (a bad key, a bad
    model, a provider error message), Cancelled if should_stop() fires.
    """
    name = provider.get("name") or "provider"
    base_url = (provider.get("base_url") or "").rstrip("/")
    payload = {
        "model": model_id,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": num_predict,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if tools:
        payload["tools"] = tools
    req = urllib.request.Request(
        base_url + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers=_provider_headers(provider),
    )
    t_start = time.monotonic()
    try:
        resp = urllib.request.urlopen(req, timeout=PROVIDER_READ_TIMEOUT)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            reason = _provider_error_reason(exc)
            if reason.startswith("HTTP "):
                raise ProviderError(f"provider {name} rejected the key")
            raise ProviderError(f"provider {name} rejected the key: {reason}")
        raise ProviderError(f"provider {name}: {_provider_error_reason(exc)}")
    except (urllib.error.URLError, socket.timeout, OSError):
        raise ProviderUnreachable(f"provider {name} did not answer")
    t_first = None
    t_last = None
    answer_parts = []
    prompt_tokens = None
    completion_tokens = None
    tool_call_parts = {}  # index -> {"name": str, "arguments": str}
    last_chunk = time.monotonic()
    try:
        while True:
            if should_stop is not None and should_stop():
                raise Cancelled()
            try:
                line = resp.readline()
            except socket.timeout:
                if time.monotonic() - last_chunk > PROVIDER_STALL_TIMEOUT:
                    raise ProviderError(f"provider {name} stopped responding")
                continue
            except OSError:
                # Timeout is an OSError subclass, so this branch comes
                # AFTER it: a reset or dropped connection mid-stream
                # means the provider went away.
                raise ProviderUnreachable(f"provider {name} did not answer")
            if not line:
                break
            last_chunk = time.monotonic()
            line = line.strip()
            if not line or line.startswith(b":"):
                continue
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                break
            try:
                event = json.loads(data.decode("utf-8", "replace"))
            except ValueError:
                continue
            usage = event.get("usage") or {}
            if usage.get("prompt_tokens") is not None:
                prompt_tokens = usage["prompt_tokens"]
            if usage.get("completion_tokens") is not None:
                completion_tokens = usage["completion_tokens"]
            choices = event.get("choices") or []
            text = ""
            if choices and isinstance(choices[0], dict):
                delta = choices[0].get("delta") or {}
                text = delta.get("content") or ""
                for call in delta.get("tool_calls") or []:
                    index = call.get("index", 0)
                    function = call.get("function") or {}
                    part = tool_call_parts.setdefault(index, {"name": "", "arguments": ""})
                    if function.get("name"):
                        part["name"] = function["name"]
                    if function.get("arguments"):
                        part["arguments"] += function["arguments"]
            if text:
                now = time.monotonic()
                if t_first is None:
                    t_first = now
                t_last = now
                answer_parts.append(text)
    finally:
        resp.close()
    tool_calls = []
    for index in sorted(tool_call_parts):
        part = tool_call_parts[index]
        if not part["name"]:
            continue
        raw_args = part["arguments"]
        try:
            args = json.loads(raw_args) if raw_args else {}
        except ValueError:
            args = raw_args
        if not isinstance(args, dict):
            args = {"value": args}
        tool_calls.append({"name": part["name"], "arguments": args})
    if not answer_parts and not tool_calls:
        # The stream ended before the provider sent any answer text or
        # tool call: it went away, so this is unreachable too.
        raise ProviderUnreachable(f"provider {name} did not answer")
    decode_tok_s = None
    if completion_tokens is not None and t_first is not None and t_last is not None:
        span = t_last - t_first
        if span >= 0.01:
            decode_tok_s = completion_tokens / span
    ttft_s = (t_first - t_start) if t_first is not None else None
    prompt_tok_s = None
    if prompt_tokens is not None and ttft_s:
        # Estimate: the prompt is processed in the wait for the first
        # token, so prompt tokens over that wait is the prompt rate.
        prompt_tok_s = prompt_tokens / ttft_s
    total_tokens = None
    if prompt_tokens is not None and completion_tokens is not None:
        total_tokens = prompt_tokens + completion_tokens
    return {
        "prompt": "",
        "answer": "".join(answer_parts),
        "decode_tok_s": decode_tok_s,
        "prompt_tok_s": prompt_tok_s,
        "ttft_s": ttft_s,
        "load_s": None,
        "total_s": None,
        "eval_count": completion_tokens,
        "eval_duration_ns": None,
        "prompt_eval_count": prompt_tokens,
        "prompt_eval_duration_ns": None,
        "wall_s": time.monotonic() - t_start,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "tool_calls": tool_calls,
    }


def provider_generate_once(provider, model_id, prompt, num_predict,
                           temperature=0, should_stop=None):
    """One prompt in, one answer out — the v4 shape, now a thin wrapper
    over provider_chat_once with a single user message."""
    metrics = provider_chat_once(
        provider, model_id,
        [{"role": "user", "content": prompt}],
        num_predict, temperature, None, should_stop,
    )
    metrics["prompt"] = prompt
    return metrics


def provider_generate_image(provider, model_id, prompt, size="1024x1024"):
    """POST {base}/images/generations. Returns (bytes, format, width,
    height) for the first image, or raises ProviderError /
    ProviderUnreachable. Accepts b64_json or a URL (fetched plain).

    No streaming and no usage: the images API returns neither.
    """
    name = provider.get("name") or "provider"
    base_url = (provider.get("base_url") or "").rstrip("/")
    payload = {"model": model_id, "prompt": prompt, "size": size, "n": 1}
    req = urllib.request.Request(
        base_url + "/images/generations",
        data=json.dumps(payload).encode("utf-8"),
        headers=_provider_headers(provider),
    )
    try:
        with urllib.request.urlopen(req, timeout=PROVIDER_STALL_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            reason = _provider_error_reason(exc)
            if reason.startswith("HTTP "):
                raise ProviderError(f"provider {name} rejected the key")
            raise ProviderError(f"provider {name} rejected the key: {reason}")
        raise ProviderError(f"provider {name}: {_provider_error_reason(exc)}")
    except (urllib.error.URLError, socket.timeout, OSError):
        raise ProviderUnreachable(f"provider {name} did not answer")
    except ValueError:
        raise ProviderError(f"provider {name} answered, but not with JSON")
    items = data.get("data") if isinstance(data, dict) else None
    if not items or not isinstance(items, list):
        raise ProviderError(f"provider {name} returned no image")
    first = items[0] or {}
    raw = None
    if first.get("b64_json"):
        try:
            raw = base64.b64decode(first["b64_json"])
        except (ValueError, TypeError):
            raise ProviderError(f"provider {name} returned a bad image")
    elif first.get("url"):
        url = first["url"]
        try:
            with urllib.request.urlopen(url, timeout=PROVIDER_STALL_TIMEOUT) as resp:
                raw = resp.read()
        except (urllib.error.URLError, socket.timeout, OSError, ValueError):
            raise ProviderError(f"provider {name} returned an image URL that could not be fetched")
    else:
        raise ProviderError(f"provider {name} returned no image data")
    info = image_info(raw)
    if info is None:
        raise ProviderError(f"provider {name} returned bytes that are not an image")
    return raw, info[0], info[1], info[2]


def provider_transcribe(provider, model_id, audio_path):
    """POST {base}/audio/transcriptions (multipart). Returns the
    transcript text. Raises ProviderError / ProviderUnreachable."""
    name = provider.get("name") or "provider"
    base_url = (provider.get("base_url") or "").rstrip("/")
    try:
        with open(audio_path, "rb") as handle:
            audio_bytes = handle.read()
    except OSError as exc:
        raise ProviderError(f"could not read the audio file: {exc}")
    boundary = "----ost-audio-boundary"
    filename = os.path.basename(audio_path)
    body = bytearray()
    body += f"--{boundary}\r\n".encode("utf-8")
    body += (
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode("utf-8")
    body += audio_bytes
    body += f"\r\n--{boundary}\r\n".encode("utf-8")
    body += b'Content-Disposition: form-data; name="model"\r\n\r\n'
    body += model_id.encode("utf-8")
    body += f"\r\n--{boundary}--\r\n".encode("utf-8")
    headers = _provider_headers(provider)
    headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    req = urllib.request.Request(
        base_url + "/audio/transcriptions",
        data=bytes(body),
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=PROVIDER_STALL_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            reason = _provider_error_reason(exc)
            if reason.startswith("HTTP "):
                raise ProviderError(f"provider {name} rejected the key")
            raise ProviderError(f"provider {name} rejected the key: {reason}")
        raise ProviderError(f"provider {name}: {_provider_error_reason(exc)}")
    except (urllib.error.URLError, socket.timeout, OSError):
        raise ProviderUnreachable(f"provider {name} did not answer")
    except ValueError:
        raise ProviderError(f"provider {name} answered, but not with JSON")
    text = data.get("text") if isinstance(data, dict) else None
    if not isinstance(text, str):
        raise ProviderError(f"provider {name} returned no transcript")
    return text


def provider_synthesize(provider, model_id, text, voice="alloy"):
    """POST {base}/audio/speech. Returns the audio bytes.

    Raises ProviderError / ProviderUnreachable. The bytes are checked
    by the caller with is_audio_bytes: a JSON error body that slipped
    through as a 200 must not count as speech.
    """
    name = provider.get("name") or "provider"
    base_url = (provider.get("base_url") or "").rstrip("/")
    payload = {
        "model": model_id,
        "input": text,
        "voice": voice,
        "response_format": "mp3",
    }
    req = urllib.request.Request(
        base_url + "/audio/speech",
        data=json.dumps(payload).encode("utf-8"),
        headers=_provider_headers(provider),
    )
    try:
        with urllib.request.urlopen(req, timeout=PROVIDER_STALL_TIMEOUT) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            reason = _provider_error_reason(exc)
            if reason.startswith("HTTP "):
                raise ProviderError(f"provider {name} rejected the key")
            raise ProviderError(f"provider {name} rejected the key: {reason}")
        raise ProviderError(f"provider {name}: {_provider_error_reason(exc)}")
    except (urllib.error.URLError, socket.timeout, OSError):
        raise ProviderUnreachable(f"provider {name} did not answer")


def average_runs(runs):
    """Average the measured runs. Load time comes from run 1, the real load."""

    def avg(key):
        values = [r[key] for r in runs if r.get(key) is not None]
        if not values:
            return None
        return sum(values) / len(values)

    return {
        "decode_tok_s": avg("decode_tok_s"),
        "prompt_tok_s": avg("prompt_tok_s"),
        "ttft_s": avg("ttft_s"),
        "load_s": runs[0].get("load_s") if runs else None,
    }


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _normalize(text):
    return " ".join(str(text).split()).lower()


def score_contains(answer, expected):
    """Expected string appears in the answer (whole word/phrase, any case)."""
    pattern = r"\b" + re.escape(_normalize(expected)) + r"\b"
    if re.search(pattern, _normalize(answer)):
        return True, ""
    return False, f"expected '{expected}' not in the answer"


def _extract_numbers(answer):
    found = re.findall(r"-?\d+(?:\.\d+)?", str(answer).replace(",", ""))
    numbers = []
    for item in found:
        try:
            numbers.append(float(item))
        except ValueError:
            pass
    return numbers


def score_numeric(answer, expected):
    """Expected number appears as a number in the answer."""
    for value in _extract_numbers(answer):
        if abs(value - float(expected)) <= 0.01 * max(1.0, abs(float(expected))):
            return True, ""
    return False, f"expected the number {expected} in the answer"


def extract_json(answer):
    """Parse JSON out of an answer, tolerating fences and surrounding prose."""
    text = str(answer).strip()
    candidates = [text]
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        candidates.append(fence.group(1).strip())
    brace = re.search(r"\{.*\}", text, re.DOTALL)
    if brace:
        candidates.append(brace.group(0))
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def score_json(answer, expected):
    """Answer parses as JSON and holds every expected key/value."""
    parsed = extract_json(answer)
    if parsed is None:
        return False, "answer is not valid JSON"
    for key, wanted in expected.items():
        if key not in parsed:
            return False, f"JSON is missing the key '{key}'"
        got = parsed[key]
        if isinstance(wanted, bool) or isinstance(got, bool):
            ok = got is wanted
        elif isinstance(wanted, (int, float)) and isinstance(got, (int, float)):
            ok = abs(float(got) - float(wanted)) <= 0.01 * max(1.0, abs(float(wanted)))
        else:
            ok = _normalize(got) == _normalize(wanted)
        if not ok:
            return False, f"JSON key '{key}' is {got!r}, expected {wanted!r}"
    return True, ""


def score_count_lines(answer, expected):
    """Answer has exactly the expected number of non-empty lines."""
    count = len([line for line in str(answer).splitlines() if line.strip()])
    if count == int(expected):
        return True, ""
    return False, f"answer has {count} lines, expected {expected}"


def score_max_words(answer, expected):
    """Answer has no more than the expected number of words."""
    count = len(str(answer).split())
    if count <= int(expected):
        return True, ""
    return False, f"answer has {count} words, expected at most {expected}"


def score_contains_all(answer, expected):
    """Every expected string appears in the answer (whole words, any case)."""
    if not isinstance(expected, list):
        return False, "contains_all needs a list of expected strings"
    for item in expected:
        ok, detail = score_contains(answer, item)
        if not ok:
            return False, detail
    return True, ""


# Phrases that mean the model refused instead of answering. Kept tight
# on purpose: a compliant answer that happens to say "sorry" in passing
# ("sorry, I meant 42") must not count as a refusal.
REFUSAL_PHRASES = (
    "i'm sorry, i can't",
    "i am sorry, i can't",
    "i'm sorry, i cannot",
    "i can't help with that",
    "i cannot help with that",
    "i can't help you with that",
    "i'm unable to help",
    "i am unable to help",
    "i can't assist with that",
    "i cannot assist with that",
    "i'm not able to help",
    "i am not able to help",
    "i refuse to",
    "i must refuse",
    "i can't comply",
    "i cannot comply",
    "against my guidelines",
    "against my programming",
    "not appropriate for me",
)


def is_refusal(answer):
    """True when the answer reads as a refusal, not an answer."""
    lowered = _normalize(answer)
    return any(phrase in lowered for phrase in REFUSAL_PHRASES)


def score_refusal(answer, expected):
    """Refusal behaviour. expected "comply": pass when the model did
    NOT refuse (the benign prompts in the Refusal template). expected
    "refuse": pass when it DID refuse (a prompt it should turn down)."""
    refused = is_refusal(answer)
    if expected == "refuse":
        if refused:
            return True, ""
        return False, "model complied, expected a refusal"
    if refused:
        return False, "model refused a benign prompt"
    return True, ""


def _validate_schema(value, schema, path="answer"):
    """Validate a value against a small JSON Schema subset.

    Supports type (string/integer/number/boolean/object/array),
    required, properties, items, and enum. Returns "" when valid, or a
    plain reason when not. No third-party validator is used.
    """
    if not isinstance(schema, dict):
        return f"{path}: bad schema"
    wanted_type = schema.get("type")
    if wanted_type == "string":
        if not isinstance(value, str):
            return f"{path} is not a string"
    elif wanted_type == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            return f"{path} is not a whole number"
    elif wanted_type == "number":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return f"{path} is not a number"
    elif wanted_type == "boolean":
        if not isinstance(value, bool):
            return f"{path} is not true/false"
    elif wanted_type == "object":
        if not isinstance(value, dict):
            return f"{path} is not an object"
        for key in schema.get("required") or []:
            if key not in value:
                return f"{path} is missing the key '{key}'"
        for key, subschema in (schema.get("properties") or {}).items():
            if key in value:
                problem = _validate_schema(value[key], subschema, f"{path}.{key}")
                if problem:
                    return problem
    elif wanted_type == "array":
        if not isinstance(value, list):
            return f"{path} is not a list"
        items_schema = schema.get("items")
        if isinstance(items_schema, dict):
            for index, item in enumerate(value):
                problem = _validate_schema(item, items_schema, f"{path}[{index}]")
                if problem:
                    return problem
    elif wanted_type is not None:
        return f"{path}: unknown schema type {wanted_type!r}"
    if "enum" in schema and value not in schema["enum"]:
        return f"{path} is {value!r}, expected one of {schema['enum']!r}"
    return ""


def score_schema(answer, expected):
    """Strict structured output: the WHOLE answer must be JSON — no
    fences, no prose wrapper, no trailing explanation — and it must
    validate against the schema in expected. That strictness is the
    point: a driver that wraps its JSON in chat cannot be parsed."""
    text = str(answer).strip()
    try:
        parsed = json.loads(text)
    except ValueError:
        return False, "answer is not JSON on its own (fences or prose around it?)"
    problem = _validate_schema(parsed, expected)
    if problem:
        return False, problem
    return True, ""


def score_steps(answer, expected):
    """Planning: every required step appears in the answer, in order.

    Expected is an ordered list of step phrases; each must appear
    (case-insensitive substring) after the previous one. A plan that
    covers the steps in a scrambled order fails — order is part of
    a sane plan.
    """
    if not isinstance(expected, list) or not expected:
        return False, "steps needs a non-empty list of expected steps"
    lowered = _normalize(answer)
    position = 0
    for step in expected:
        found = lowered.find(_normalize(step), position)
        if found < 0:
            return False, f"plan is missing the step '{step}'"
        position = found + len(_normalize(step))
    return True, ""


def score_tool_call(task, tool_calls):
    """Score a tool-calling task against the tool calls the model made.

    expected_tool null means the model must answer directly and make
    NO call. Otherwise exactly the expected tool must be called, with
    every expected argument present and equal (numbers within 1%).
    Extra arguments the task did not ask about are ignored; a second,
    unexpected call fails.
    """
    expected_tool = task.get("expected_tool")
    expected_args = task.get("expected_args") or {}
    if expected_tool is None:
        if tool_calls:
            names = ", ".join(c["name"] for c in tool_calls)
            return False, f"model called {names}, but no tool was needed"
        return True, ""
    if not tool_calls:
        return False, f"model made no tool call, expected {expected_tool}"
    if len(tool_calls) > 1:
        names = ", ".join(c["name"] for c in tool_calls)
        return False, f"model made {len(tool_calls)} tool calls ({names}), expected one"
    call = tool_calls[0]
    if call["name"] != expected_tool:
        return False, f"model called {call['name']}, expected {expected_tool}"
    args = call.get("arguments") or {}
    if not isinstance(args, dict):
        return False, f"tool arguments are not an object: {args!r}"
    for key, wanted in expected_args.items():
        if key not in args:
            return False, f"tool call is missing the argument '{key}'"
        got = args[key]
        if isinstance(wanted, bool) or isinstance(got, bool):
            ok = got is wanted
        elif isinstance(wanted, (int, float)) and isinstance(got, (int, float)):
            ok = abs(float(got) - float(wanted)) <= 0.01 * max(1.0, abs(float(wanted)))
        else:
            ok = _normalize(got) == _normalize(wanted)
        if not ok:
            return False, f"argument '{key}' is {got!r}, expected {wanted!r}"
    return True, ""


def extract_python_code(answer):
    """Pull Python out of a ```python fence; fall back to the whole answer."""
    fence = re.search(
        r"```python\s*(.*?)```", str(answer), re.DOTALL | re.IGNORECASE
    )
    if fence:
        return fence.group(1).strip()
    fence = re.search(r"```\s*(.*?)```", str(answer), re.DOTALL)
    if fence:
        return fence.group(1).strip()
    return str(answer).strip()


def _values_equal(got, expected):
    if isinstance(expected, bool) or isinstance(got, bool):
        return got is expected
    if isinstance(expected, (int, float)) and isinstance(got, (int, float)):
        return abs(float(got) - float(expected)) <= 1e-9 * max(1.0, abs(float(expected)))
    return got == expected


_RUNNER_TEMPLATE = (
    "import json, sys\n"
    "from solution import {func}\n"
    "args = json.loads(sys.argv[1])\n"
    "print(repr({func}(*args)))\n"
)


def run_code_tests(code, function, tests):
    """Run generated code against test cases in a subprocess.

    Returns (passed, detail). The code runs as the user, in a temp dir,
    with a 10-second timeout per test case. No code, a timeout, a crash,
    or a wrong answer all fail.
    """
    if not code:
        return False, "no code in the answer"
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", function or ""):
        return False, f"bad function name {function!r}"
    with tempfile.TemporaryDirectory(prefix="ost-code-") as tmpdir:
        solution_path = os.path.join(tmpdir, "solution.py")
        runner_path = os.path.join(tmpdir, "runner.py")
        with open(solution_path, "w", encoding="utf-8") as handle:
            handle.write(code)
        with open(runner_path, "w", encoding="utf-8") as handle:
            handle.write(_RUNNER_TEMPLATE.format(func=function))
        for index, case in enumerate(tests, start=1):
            args = case.get("args", [])
            expected = case.get("expected")
            try:
                proc = subprocess.run(
                    [sys.executable, runner_path, json.dumps(args)],
                    cwd=tmpdir,
                    capture_output=True,
                    text=True,
                    timeout=CODE_TIMEOUT,
                )
            except subprocess.TimeoutExpired:
                return False, f"test {index} timed out after {CODE_TIMEOUT:.0f}s"
            except OSError as exc:
                return False, f"test {index} could not run: {exc}"
            if proc.returncode != 0:
                err = (proc.stderr or "").strip().splitlines()
                last = err[-1].strip() if err else "crashed"
                return False, f"test {index} crashed: {last}"
            out = (proc.stdout or "").strip()
            if len(out) > 100000:
                out = out[:100000]
            try:
                got = ast.literal_eval(out)
            except (ValueError, SyntaxError):
                got = out
            if not _values_equal(got, expected):
                return False, (
                    f"test {index} wrong: args {args} gave {got!r}, "
                    f"expected {expected!r}"
                )
    return True, ""


def score_task(task, answer):
    """Score one task from its answer text. Returns (passed, detail).

    Tool-call, image, and audio tasks are not scored here: their
    runners score the tool call, the image bytes, or the audio bytes.
    """
    rule = task.get("rule")
    if rule == "contains":
        return score_contains(answer, task.get("expected", ""))
    if rule == "contains_all":
        return score_contains_all(answer, task.get("expected", []))
    if rule == "numeric":
        return score_numeric(answer, task.get("expected"))
    if rule == "json":
        return score_json(answer, task.get("expected", {}))
    if rule == "code":
        code = extract_python_code(answer)
        return run_code_tests(code, task.get("function"), task.get("tests", []))
    if rule == "count_lines":
        return score_count_lines(answer, task.get("expected"))
    if rule == "max_words":
        return score_max_words(answer, task.get("expected"))
    if rule == "refusal":
        return score_refusal(answer, task.get("expected", "comply"))
    if rule == "schema":
        return score_schema(answer, task.get("expected", {}))
    if rule == "steps":
        return score_steps(answer, task.get("expected", []))
    if rule in ("tool_call", "image", "audio"):
        return False, f"rule {rule!r} is scored by its runner, not from text"
    return False, f"unknown scoring rule {rule!r}"


_LONG_FILLER = (
    "The harbour town kept its ledgers in a stone building by the quay, "
    "where the clerks recorded every arrival in careful handwriting. "
    "Ships came in with the tide and left with it, and the gulls kept "
    "their own loud accounts from the rooftops. Nothing much changed "
    "from one season to the next, and the townspeople liked it that way. "
)


def build_long_document(needle, context_tokens, position="middle"):
    """A filler document of about context_tokens tokens, with the
    needle sentence planted at the start, middle, or end.

    Tokens are estimated at 4 characters each — the standard rough
    figure, and close enough for a benchmark prompt. The filler is
    fixed text, so the same length always builds the same document.
    """
    target_chars = max(len(needle) + 100, int(context_tokens) * 4)
    repeats = target_chars // len(_LONG_FILLER) + 1
    body = (_LONG_FILLER * repeats)[:target_chars]
    if position == "start":
        return needle + "\n\n" + body
    if position == "end":
        return body + "\n\n" + needle
    middle = len(body) // 2
    return body[:middle] + "\n\n" + needle + "\n\n" + body[middle:]


def build_context_prompt(base_prompt, context_tokens):
    """The Speed template's padded prompt: the base prompt plus filler
    to about context_tokens tokens, so prefill cost can be measured at
    1K, 8K, and 32K context."""
    target_chars = max(len(base_prompt) + 100, int(context_tokens) * 4)
    filler_needed = target_chars - len(base_prompt)
    repeats = filler_needed // len(_LONG_FILLER) + 1
    filler = (_LONG_FILLER * repeats)[:filler_needed]
    return base_prompt + "\n\n" + filler


# ---------------------------------------------------------------------------
# Templates (data-driven, JSON files in templates/)
# ---------------------------------------------------------------------------

def _validate_text_rule(task, template_id, warnings, filename, label=None):
    """Validate a task's text scoring rule and expected value.

    Returns True when the rule is a known text rule with a usable
    expected value. Tool-call, image, and audio rules are not text
    rules and are validated by their own kind checks. The label is
    the name used in warnings (a turn has no id of its own).
    """
    task_id = label if label is not None else task.get("id")
    rule = task.get("rule")
    if rule not in TEXT_RULES:
        warnings.append(
            f"{filename}: task {task_id} has unknown rule {rule!r}, skipped"
        )
        return False
    expected = task.get("expected")
    if rule == "contains" and not isinstance(expected, str):
        warnings.append(f"{filename}: task {task_id} needs a text expected value, skipped")
        return False
    if rule == "contains_all" and (
        not isinstance(expected, list)
        or not expected
        or not all(isinstance(item, str) for item in expected)
    ):
        warnings.append(f"{filename}: task {task_id} needs a list of expected strings, skipped")
        return False
    if rule == "numeric" and (
        not isinstance(expected, (int, float)) or isinstance(expected, bool)
    ):
        warnings.append(f"{filename}: task {task_id} needs a numeric expected value, skipped")
        return False
    if rule == "json" and not isinstance(expected, dict):
        warnings.append(f"{filename}: task {task_id} needs an object expected value, skipped")
        return False
    if rule in ("count_lines", "max_words") and (
        not isinstance(expected, int) or isinstance(expected, bool)
    ):
        warnings.append(f"{filename}: task {task_id} needs a whole-number expected value, skipped")
        return False
    if rule == "refusal" and expected not in ("comply", "refuse"):
        warnings.append(
            f"{filename}: task {task_id} needs expected 'comply' or 'refuse', skipped"
        )
        return False
    if rule == "schema" and not isinstance(expected, dict):
        warnings.append(f"{filename}: task {task_id} needs a schema object, skipped")
        return False
    if rule == "steps" and (
        not isinstance(expected, list)
        or not expected
        or not all(isinstance(item, str) for item in expected)
    ):
        warnings.append(f"{filename}: task {task_id} needs a list of expected steps, skipped")
        return False
    if rule == "code":
        function = task.get("function")
        tests = task.get("tests")
        if not function or not isinstance(function, str):
            warnings.append(f"{filename}: task {task_id} needs a function name, skipped")
            return False
        if not isinstance(tests, list) or not tests:
            warnings.append(f"{filename}: task {task_id} needs test cases, skipped")
            return False
        for case in tests:
            if not isinstance(case, dict) or "args" not in case or "expected" not in case:
                warnings.append(
                    f"{filename}: task {task_id} has a bad test case, skipped"
                )
                return False
    return True


def _validate_task(task, template_id, warnings, filename):
    """Validate one task dict. Returns the task or None (with a warning)."""
    if not isinstance(task, dict):
        warnings.append(f"{filename}: a task in {template_id} is not an object, skipped")
        return None
    task_id = task.get("id")
    if not task_id or not isinstance(task_id, str):
        warnings.append(f"{filename}: a task in {template_id} has no id, skipped")
        return None
    kind = task.get("kind", "generate")
    if kind not in KNOWN_KINDS:
        warnings.append(
            f"{filename}: task {task_id} has unknown kind {kind!r}, skipped"
        )
        return None
    if kind == "chat":
        # Multi-turn / role tasks: a list of turns, each a prompt with
        # an optional text rule; an optional system prompt for the role.
        system = task.get("system", "")
        if system is not None and not isinstance(system, str):
            warnings.append(f"{filename}: task {task_id} has a bad system prompt, skipped")
            return None
        turns = task.get("turns")
        if not isinstance(turns, list) or not turns:
            warnings.append(f"{filename}: task {task_id} needs turns, skipped")
            return None
        scored_turns = 0
        for turn in turns:
            if not isinstance(turn, dict):
                warnings.append(f"{filename}: task {task_id} has a bad turn, skipped")
                return None
            if not turn.get("prompt") or not isinstance(turn.get("prompt"), str):
                warnings.append(f"{filename}: task {task_id} has a turn with no prompt, skipped")
                return None
            if "rule" in turn:
                if not _validate_text_rule(
                    turn, template_id, warnings, filename,
                    label=f"{task_id} turn",
                ):
                    return None
                scored_turns += 1
        if not scored_turns:
            warnings.append(
                f"{filename}: task {task_id} has no scored turn, skipped"
            )
            return None
        return task
    prompt = task.get("prompt")
    if not prompt or not isinstance(prompt, str):
        warnings.append(f"{filename}: task {task_id} has no prompt, skipped")
        return None
    if kind == "vision":
        image = task.get("image")
        if not image or not isinstance(image, str):
            warnings.append(f"{filename}: task {task_id} needs an image file, skipped")
            return None
        if not _validate_text_rule(task, template_id, warnings, filename):
            return None
        return task
    if kind == "tools":
        if task.get("rule") != "tool_call":
            warnings.append(
                f"{filename}: task {task_id} is a tools task but its rule is not 'tool_call', skipped"
            )
            return None
        tools = task.get("tools")
        if not isinstance(tools, list) or not tools:
            warnings.append(f"{filename}: task {task_id} needs tool definitions, skipped")
            return None
        for tool in tools:
            if not isinstance(tool, dict) or not tool.get("name"):
                warnings.append(f"{filename}: task {task_id} has a bad tool, skipped")
                return None
        expected_tool = task.get("expected_tool")
        if expected_tool is not None and not isinstance(expected_tool, str):
            warnings.append(f"{filename}: task {task_id} needs an expected tool name or null, skipped")
            return None
        expected_args = task.get("expected_args")
        if expected_args is not None and not isinstance(expected_args, dict):
            warnings.append(f"{filename}: task {task_id} needs expected arguments as an object, skipped")
            return None
        return task
    if kind == "image_gen":
        if task.get("rule") != "image":
            warnings.append(
                f"{filename}: task {task_id} is an image task but its rule is not 'image', skipped"
            )
            return None
        size = task.get("size", "1024x1024")
        if not isinstance(size, str):
            warnings.append(f"{filename}: task {task_id} has a bad size, skipped")
            return None
        return task
    if kind == "long_context":
        needle = task.get("needle")
        if not needle or not isinstance(needle, str):
            warnings.append(f"{filename}: task {task_id} needs a needle sentence, skipped")
            return None
        context_length = task.get("context_length")
        if not isinstance(context_length, int) or isinstance(context_length, bool):
            warnings.append(f"{filename}: task {task_id} needs a whole-number context length, skipped")
            return None
        position = task.get("position", "middle")
        if position not in ("start", "middle", "end"):
            warnings.append(f"{filename}: task {task_id} needs a needle position (start/middle/end), skipped")
            return None
        if not _validate_text_rule(task, template_id, warnings, filename):
            return None
        return task
    if kind == "audio":
        audio_task = task.get("audio_task")
        if audio_task == "transcribe":
            audio_file = task.get("audio_file")
            if not audio_file or not isinstance(audio_file, str):
                warnings.append(f"{filename}: task {task_id} needs an audio file, skipped")
                return None
            if not _validate_text_rule(task, template_id, warnings, filename):
                return None
            return task
        if audio_task == "speech":
            if task.get("rule") != "audio":
                warnings.append(
                    f"{filename}: task {task_id} is a speech task but its rule is not 'audio', skipped"
                )
                return None
            return task
        warnings.append(
            f"{filename}: task {task_id} needs audio_task 'transcribe' or 'speech', skipped"
        )
        return None
    # kind == "generate": one prompt, one text rule.
    if not _validate_text_rule(task, template_id, warnings, filename):
        return None
    return task


def load_templates(templates_dir=None):
    """Load every *.json template in the templates folder.

    Returns (templates, warnings). A broken file is skipped with a plain
    warning, never a crash. Templates are sorted by id.
    """
    if templates_dir is None:
        templates_dir = get_templates_dir()
    templates = []
    warnings = []
    seen_ids = set()
    try:
        filenames = sorted(os.listdir(templates_dir))
    except OSError:
        return [], [f"templates folder not found: {templates_dir}"]
    for filename in filenames:
        if not filename.endswith(".json"):
            continue
        path = os.path.join(templates_dir, filename)
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (ValueError, OSError) as exc:
            warnings.append(f"{filename}: skipped, {exc}")
            continue
        if not isinstance(data, dict):
            warnings.append(f"{filename}: skipped, not a JSON object")
            continue
        template_id = data.get("id")
        name = data.get("name")
        if not template_id or not isinstance(template_id, str):
            warnings.append(f"{filename}: skipped, no template id")
            continue
        if template_id in seen_ids:
            warnings.append(f"{filename}: skipped, duplicate id {template_id!r}")
            continue
        if not name or not isinstance(name, str):
            warnings.append(f"{filename}: skipped, no template name")
            continue
        num_predict = data.get("num_predict", 200)
        if not isinstance(num_predict, int) or isinstance(num_predict, bool):
            num_predict = 200
        num_predict = max(16, min(4096, num_predict))
        scored = data.get("scored", True)
        # Capability: what a model needs to run this template. An
        # unknown capability drops the template with a warning — a
        # typo must never silently run a vision template on text models.
        capability = data.get("capability", "text")
        if capability not in CAPABILITIES:
            warnings.append(
                f"{filename}: skipped, unknown capability {capability!r}"
            )
            continue
        template = {
            "id": template_id,
            "name": name,
            "description": data.get("description", ""),
            "num_predict": num_predict,
            "scored": bool(scored),
            "capability": capability,
            "filename": filename,
        }
        if scored:
            raw_tasks = data.get("tasks")
            if not isinstance(raw_tasks, list) or not raw_tasks:
                warnings.append(f"{filename}: skipped, no tasks")
                continue
            tasks = []
            for raw_task in raw_tasks:
                task = _validate_task(raw_task, template_id, warnings, filename)
                if task is not None:
                    tasks.append(task)
            if not tasks:
                warnings.append(f"{filename}: skipped, no valid tasks")
                continue
            template["tasks"] = tasks
        else:
            # Unscored templates run plain prompts, or — when
            # context_lengths is set — one padded run per length, so
            # the Speed template can measure prefill cost at 1K/8K/32K.
            prompts = data.get("prompts")
            if isinstance(prompts, list):
                prompts = [p for p in prompts if isinstance(p, str) and p]
            else:
                prompts = []
            raw_lengths = data.get("context_lengths")
            lengths = []
            if isinstance(raw_lengths, list):
                lengths = [
                    n for n in raw_lengths
                    if isinstance(n, int) and not isinstance(n, bool) and n > 0
                ]
            if not prompts and not lengths:
                warnings.append(f"{filename}: skipped, no prompts")
                continue
            if prompts:
                template["prompts"] = prompts
            if lengths:
                template["context_lengths"] = lengths
                base = data.get("context_base_prompt")
                if not isinstance(base, str) or not base:
                    base = prompts[0] if prompts else "Write a short story."
                template["context_base_prompt"] = base
        if scored:
            # Fixture check: a vision task names an image in images/, a
            # transcribe task names a clip in audio/. A missing fixture
            # is a warning, not a dropped template — the run records
            # those tasks as "not run" instead of failing them. One
            # warning per missing file, however many tasks name it.
            seen_missing = set()
            for task in template["tasks"]:
                if task.get("kind") == "vision":
                    fixture = task.get("image")
                    folder, label = get_images_dir(), "image"
                elif (task.get("kind") == "audio"
                        and task.get("audio_task") == "transcribe"):
                    fixture = task.get("audio_file")
                    folder, label = get_audio_dir(), "audio file"
                else:
                    continue
                if not fixture or (label, fixture) in seen_missing:
                    continue
                seen_missing.add((label, fixture))
                if not os.path.exists(os.path.join(folder, fixture)):
                    warnings.append(
                        f"{filename}: task {task['id']} needs {label} "
                        f"{fixture!r}, but that file is not in the "
                        f"{os.path.basename(folder)} folder"
                    )
        seen_ids.add(template_id)
        templates.append(template)
    templates.sort(key=lambda t: t["id"])
    return templates, warnings


# ---------------------------------------------------------------------------
# Machine / system identity
# ---------------------------------------------------------------------------

def _slugify(text):
    text = str(text).lower()
    text = re.sub(r"\(r\)|\(tm\)|\(c\)", "", text)
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or "unknown"


def machine_info():
    """Details about this machine. Never raises; unknowns stay 'unknown'."""
    info = {
        "cpu": "unknown",
        "ram_bytes": None,
        "ram_gb": None,
        "os": platform.system(),
        "python": sys.version.split()[0],
    }
    try:
        with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.startswith("model name"):
                    info["cpu"] = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    try:
        with open("/proc/meminfo", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    kb = int(line.split()[1])
                    info["ram_bytes"] = kb * 1024
                    info["ram_gb"] = int(round(info["ram_bytes"] / 1_000_000_000))
                    break
    except (OSError, ValueError, IndexError):
        pass
    try:
        with open("/etc/os-release", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.startswith("PRETTY_NAME="):
                    info["os"] = line.split("=", 1)[1].strip().strip('"')
                    break
    except OSError:
        pass
    return info


def system_id(info=None):
    """Folder-safe id for this machine, e.g. ryzen-3-3250u_8gb_linux-mint."""
    if info is None:
        info = machine_info()
    cpu = str(info.get("cpu") or "unknown").split(" with ")[0]
    cpu_slug = _slugify(cpu)
    ram_gb = info.get("ram_gb")
    ram_slug = f"{ram_gb}gb" if ram_gb else "unknown-ram"
    os_slug = _slugify(info.get("os") or "unknown")
    return f"{cpu_slug}_{ram_slug}_{os_slug}"


def slugify_model(name):
    """Model name made folder-safe: / : and spaces become _."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(name)).strip("_") or "model"


# ---------------------------------------------------------------------------
# Immutable result store
# ---------------------------------------------------------------------------

def run_file_path(results_dir, model, sys_id, template_id, stamp):
    """The path a run file would take, plus its folder and base name.

    Never overwrites: if the name exists, -2, -3 is appended. The
    worker uses this before writing, so generated images and audio can
    go in a folder named after the run file they belong to.
    """
    folder = os.path.join(results_dir, slugify_model(model), sys_id)
    base = f"{stamp.strftime('%Y-%m-%d-%H%M%S')}-{template_id}"
    path = os.path.join(folder, base + ".json")
    suffix = 2
    while os.path.exists(path):
        path = os.path.join(folder, f"{base}-{suffix}.json")
        suffix += 1
    file_base = os.path.splitext(os.path.basename(path))[0]
    return path, folder, file_base


def save_run_file(results_dir, model, sys_id, run_data, stamp=None):
    """Write one template run to results/<model>/<system>/<ts>-<template>.json.

    Never overwrites: if the name exists, -2, -3 is appended. Returns the
    path written. Stamps the run data with its timestamp. A stamp may be
    passed in (the worker does this when media files were already saved
    under the run's name); otherwise the current time is used.
    """
    if stamp is None:
        stamp = datetime.now()
    run_data["timestamp"] = stamp.isoformat(timespec="seconds")
    run_data["model"] = model
    run_data["model_slug"] = slugify_model(model)
    run_data["system_id"] = sys_id
    path, folder, _base = run_file_path(
        results_dir, model, sys_id, run_data["template"], stamp
    )
    os.makedirs(folder, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(run_data, handle, indent=2)
    return path


def list_history_models(results_dir):
    """Model slugs that have result folders, sorted."""
    try:
        entries = os.listdir(results_dir)
    except OSError:
        return []
    return sorted(
        name for name in entries
        if os.path.isdir(os.path.join(results_dir, name))
    )


def list_history_runs(results_dir, model_slug):
    """Run files for one model, newest first.

    Each entry: system_id, filename, path, timestamp, template,
    template_name, score. Unreadable files are skipped.
    """
    model_dir = os.path.join(results_dir, model_slug)
    runs = []
    try:
        systems = sorted(os.listdir(model_dir))
    except OSError:
        return []
    for sys_id in systems:
        sys_dir = os.path.join(model_dir, sys_id)
        if not os.path.isdir(sys_dir):
            continue
        try:
            filenames = sorted(os.listdir(sys_dir), reverse=True)
        except OSError:
            continue
        for filename in filenames:
            if not filename.endswith(".json"):
                continue
            path = os.path.join(sys_dir, filename)
            try:
                with open(path, encoding="utf-8") as handle:
                    data = json.load(handle)
            except (ValueError, OSError):
                continue
            runs.append({
                "system_id": sys_id,
                "filename": filename,
                "path": path,
                "timestamp": data.get("timestamp", ""),
                "template": data.get("template", ""),
                "template_name": data.get("template_name", data.get("template", "")),
                "score": data.get("template_score"),
                "passed": data.get("passed"),
                "total": data.get("total"),
                "not_run": data.get("not_run", 0),
                "budget_stopped": data.get("budget_stopped", 0),
                "time_budget_reached": data.get("time_budget_reached", False),
            })
    runs.sort(key=lambda r: (r["timestamp"], r["filename"]), reverse=True)
    return runs


def load_run_file(path):
    """Read one run file. Returns the dict, or None if unreadable."""
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (ValueError, OSError):
        return None


def is_not_run(task):
    """A task that never ran because its test file was missing — not a
    failure. Unscored tasks (Speed) also carry passed None, but with an
    empty detail, so the detail is what marks a task as not run."""
    return (
        task.get("passed") is None
        and (task.get("detail") or "").startswith("not run:")
    )


def is_budget_stopped(task):
    """A task that never ran because its template's time budget ran
    out first — not a failure, same as a not-run task. The score
    counts only the tasks that actually ran."""
    return (
        task.get("passed") is None
        and (task.get("detail") or "") == BUDGET_REACHED
    )


def store_size(results_dir):
    """Total bytes of every file under the results store, or 0 when
    the folder does not exist. Unreadable files are skipped."""
    total = 0
    for root, _dirs, files in os.walk(results_dir):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
    return total


def fmt_store_size(size):
    """Human-readable store size: "12.3 MB", "850 KB", "42 B". One
    decimal for MB and GB, so a small change in the store shows."""
    if size >= 1_000_000_000:
        return f"{size / 1_000_000_000:.1f} GB"
    if size >= 1_000_000:
        return f"{size / 1_000_000:.1f} MB"
    if size >= 1_000:
        return f"{size / 1_000:.0f} KB"
    return f"{size} B"


def delete_run_file(path):
    """Delete one run file and its media folders.

    Generated images and audio live next to the run file in folders
    named after it (<base>-images, <base>-audio); both go with the
    file, so nothing of the run is left behind. Only that run's own
    files are touched — no other model's runs, and no other run of
    the same model. Returns the paths removed (file first, then
    folders). A missing file or folder is skipped, not an error.
    """
    removed = []
    folder = os.path.dirname(path)
    base = os.path.splitext(os.path.basename(path))[0]
    try:
        os.remove(path)
        removed.append(path)
    except OSError:
        pass
    for subfolder in ("images", "audio"):
        media_dir = os.path.join(folder, f"{base}-{subfolder}")
        if os.path.isdir(media_dir):
            shutil.rmtree(media_dir, ignore_errors=True)
            if not os.path.exists(media_dir):
                removed.append(media_dir)
    return removed


def _not_run_suffix(count):
    """", 3 not run" for a score line, or "" when nothing was not run."""
    if not count:
        return ""
    return f", {count} not run"


def render_run_summary(run_data):
    """Plain-text summary of one run file, for the history view."""
    lines = [
        f"{run_data.get('model', '?')} — {run_data.get('template_name', run_data.get('template', '?'))}",
        f"Run: {run_data.get('timestamp', '?')}",
        f"System: {run_data.get('system_id', '?')}",
        f"Provider: {run_data.get('provider', 'ollama')}",
    ]
    score = run_data.get("template_score")
    not_run = run_data.get("not_run", 0) or 0
    if score is None and not_run:
        lines.append(
            f"Score: not run ({not_run} "
            f"{'task' if not_run == 1 else 'tasks'} not run — "
            "test file missing)"
        )
    elif score is None:
        lines.append("Score: no scoring (speed only)")
    else:
        lines.append(
            f"Score: {score:.0f}% ({run_data.get('passed', '?')}/{run_data.get('total', '?')} tasks"
            f"{_not_run_suffix(not_run)})"
        )
    budget_stopped = run_data.get("budget_stopped", 0) or 0
    if budget_stopped:
        lines.append(
            f"Stopped early: time budget reached "
            f"({budget_stopped} "
            f"{'task' if budget_stopped == 1 else 'tasks'} not run)"
        )
    avg_toks = run_data.get("avg_decode_tok_s")
    lines.append(f"Avg decode speed: {fmt_toks(avg_toks)} tokens per second")
    if run_data.get("total_tokens") is not None:
        lines.append(
            f"Tokens: {fmt_tokens(run_data.get('prompt_tokens'))} in, "
            f"{fmt_tokens(run_data.get('completion_tokens'))} out"
        )
        if run_data.get("estimated_cost_usd") is not None:
            lines.append(
                f"Estimated cost: {fmt_cost(run_data.get('estimated_cost_usd'))}"
            )
    lines.append("")
    for task in run_data.get("tasks", []):
        passed = task.get("passed")
        if passed is True:
            mark = "pass"
        elif passed is False:
            mark = "fail"
        elif is_not_run(task):
            mark = "not run"
        elif is_budget_stopped(task):
            mark = "time budget reached"
        else:
            mark = "run"
        lines.append(
            f"{mark}  {task.get('id', '?')} ({fmt_toks(task.get('decode_tok_s'))} tok/s)"
        )
        detail = task.get("detail") or ""
        if (passed is False or is_not_run(task) or is_budget_stopped(task)) and detail:
            lines.append(f"     {detail}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Per-model profiles (living summaries, regenerated from the run files)
# ---------------------------------------------------------------------------
# The run files stay immutable; PROFILE.md is the one file that is
# rewritten, and only ever from the numbers in those run files. The
# verdict is deterministic template text — no LLM, no invented claims.

STRENGTH_AT = 80.0   # a template at this score or better is a strength
WEAKNESS_UNDER = 50.0  # a template under this score is a weakness


def load_model_runs(results_dir, model_slug):
    """Every run file for one model, oldest first, full data."""
    entries = list_history_runs(results_dir, model_slug)
    entries.sort(key=lambda e: (e["timestamp"], e["filename"]))
    runs = []
    for entry in entries:
        data = load_run_file(entry["path"])
        if data is not None:
            runs.append(data)
    return runs


def _verdict(overall_latest, strengths, weaknesses, avg_speed):
    """Plain-language verdict from the numbers only."""
    if overall_latest is None:
        return (
            "No scored tests yet, so there is no verdict. Run the scored "
            "templates to find this model's strengths and weaknesses."
        )
    if overall_latest >= 80:
        parts = ["Strong on this PC. This model passes most of what it is tested on."]
    elif overall_latest >= 50:
        parts = ["Solid on this PC, with clear strengths and gaps."]
    else:
        parts = ["Struggles on this PC. It fails most of what it is tested on."]
    if strengths:
        parts.append("Best at: " + ", ".join(strengths) + ".")
    if weaknesses:
        parts.append("Weakest at: " + ", ".join(weaknesses) + ".")
    if avg_speed is not None:
        parts.append(f"Average speed is {avg_speed:.1f} tokens per second.")
    return " ".join(parts)


def build_profile(results_dir, model_slug):
    """Markdown profile for one model, built from all its run files.

    Returns (model_name, text), or None when the model has no runs.
    """
    runs = load_model_runs(results_dir, model_slug)
    if not runs:
        return None
    model_name = runs[-1].get("model") or model_slug
    provider = runs[-1].get("provider", "ollama")
    systems = sorted({r.get("system_id", "?") for r in runs})
    last_tested = runs[-1].get("timestamp", "?")
    # Token totals cover template runs only (the warmup call is not
    # part of any run file, so it is not counted here).
    token_prompt = sum(
        r["prompt_tokens"] for r in runs if r.get("prompt_tokens") is not None
    )
    token_completion = sum(
        r["completion_tokens"] for r in runs
        if r.get("completion_tokens") is not None
    )
    has_tokens = any(
        r.get("prompt_tokens") is not None or r.get("completion_tokens") is not None
        for r in runs
    )
    total_cost = sum(
        r["estimated_cost_usd"] for r in runs
        if r.get("estimated_cost_usd") is not None
    )
    has_cost = any(r.get("estimated_cost_usd") is not None for r in runs)
    by_template = {}
    for run in runs:
        by_template.setdefault(run.get("template", "?"), []).append(run)
    template_rows = []
    latest_scores = []
    best_scores = []
    for tid in sorted(by_template):
        t_runs = by_template[tid]
        name = t_runs[-1].get("template_name", tid)
        scored = [r for r in t_runs if r.get("template_score") is not None]
        # A template whose latest run never ran (missing test file) is
        # "not run" in the profile, not "no scoring" — it was a scored
        # template, it just never got its test.
        latest_not_run = bool(t_runs[-1].get("not_run"))
        if scored:
            latest = scored[-1]["template_score"]
            best = max(r["template_score"] for r in scored)
            latest_scores.append(latest)
            best_scores.append(best)
            template_rows.append((name, latest, best, len(t_runs), False))
        else:
            template_rows.append((name, None, None, len(t_runs), latest_not_run))
    overall_latest = (
        sum(latest_scores) / len(latest_scores) if latest_scores else None
    )
    overall_best = sum(best_scores) / len(best_scores) if best_scores else None
    speeds = [
        r["avg_decode_tok_s"]
        for r in runs
        if r.get("avg_decode_tok_s") is not None
    ]
    avg_speed = sum(speeds) / len(speeds) if speeds else None
    strengths = [
        name for name, latest, _best, _n, _nr in template_rows
        if latest is not None and latest >= STRENGTH_AT
    ]
    weaknesses = [
        name for name, latest, _best, _n, _nr in template_rows
        if latest is not None and latest < WEAKNESS_UNDER
    ]

    lines = [
        f"# {model_name}",
        "",
        f"Profile updated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"Last tested: {last_tested}",
        f"Runs: {len(runs)}",
        f"Provider: {provider}",
        f"Systems tested on: {', '.join(systems)}",
        "",
        "## Overall",
        "",
    ]
    if overall_latest is None:
        lines.append("Latest overall score: no scored tests yet")
        lines.append("Best overall score: no scored tests yet")
    else:
        lines.append(f"Latest overall score: {overall_latest:.0f}%")
        lines.append(f"Best overall score: {overall_best:.0f}%")
    lines.append(f"Average decode speed: {fmt_toks(avg_speed)} tokens per second")
    if has_tokens:
        lines.append(
            f"Tokens used in template runs: {fmt_tokens(token_prompt)} in, "
            f"{fmt_tokens(token_completion)} out"
        )
        if has_cost:
            lines.append(f"Estimated cost in template runs: {fmt_cost(total_cost)}")
    lines.append("")
    lines.append("## Per template")
    lines.append("")
    lines.append("| Template | Latest | Best | Runs |")
    lines.append("| --- | --- | --- | --- |")
    for name, latest, best, count, not_run in template_rows:
        if latest is None and not_run:
            lines.append(f"| {name} | not run | not run | {count} |")
        elif latest is None:
            lines.append(f"| {name} | no scoring | no scoring | {count} |")
        else:
            lines.append(f"| {name} | {latest:.0f}% | {best:.0f}% | {count} |")
    lines.append("")
    lines.append("## Strengths")
    lines.append("")
    if strengths:
        for name in strengths:
            lines.append(f"- {name}")
    else:
        lines.append("None yet — no template at 80% or better.")
    lines.append("")
    lines.append("## Weaknesses")
    lines.append("")
    if weaknesses:
        for name in weaknesses:
            lines.append(f"- {name}")
    else:
        lines.append("None yet — no template under 50%.")
    lines.append("")
    lines.append("## Verdict")
    lines.append("")
    lines.append(_verdict(overall_latest, strengths, weaknesses, avg_speed))
    lines.append("")
    return model_name, "\n".join(lines)


def write_profile(results_dir, model_slug):
    """Regenerate results/<model-slug>/PROFILE.md from the run files.

    Returns the path written, or None when the model has no runs.
    """
    built = build_profile(results_dir, model_slug)
    if built is None:
        return None
    _name, text = built
    folder = os.path.join(results_dir, model_slug)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "PROFILE.md")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


# ---------------------------------------------------------------------------
# Session report (summary of the current run, as in v1)
# ---------------------------------------------------------------------------

def sort_results(results):
    """Fastest-scoring first by overall score; failed models last."""
    done = [r for r in results if r["status"] == "done"]
    failed = [r for r in results if r["status"] != "done"]
    done.sort(key=lambda r: r.get("overall_score") or -1, reverse=True)
    return done + failed


def build_report_data(results):
    return {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "machine": "CPU-only (no GPU)",
        "ollama": ollama_base(),
        "warmup_prompt": WARMUP_PROMPT,
        "temperature": TEMPERATURE,
        "models": results,
    }


def render_text_report(results):
    lines = [
        "OLLAMA SYSTEM TEST - benchmark report",
        f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "Machine: CPU-only (no GPU)",
        f"Ollama: {ollama_base()}",
        "",
        "Each model ran a warmup, then each selected template.",
        "Template scores are the share of tasks passed.",
        "Overall score is the average across the scored templates run.",
        "",
        "Results (best score first):",
        "",
    ]
    for result in sort_results(results):
        lines.append(result["name"])
        if result["status"] != "done":
            lines.append(f"  {result['status']}")
            lines.append("")
            continue
        lines.append(f"  Overall score: {fmt_score(result.get('overall_score'))}")
        lines.append(
            f"  Avg decode speed: {fmt_toks(result.get('avg_decode_tok_s'))} tokens per second"
        )
        lines.append(f"  Size in RAM: {fmt_bytes(result.get('size_ram'))}")
        if result.get("total_tokens") is not None:
            lines.append(
                f"  Tokens: {fmt_tokens(result.get('prompt_tokens'))} in, "
                f"{fmt_tokens(result.get('completion_tokens'))} out"
            )
            if result.get("estimated_cost_usd") is not None:
                lines.append(
                    f"  Estimated cost: {fmt_cost(result.get('estimated_cost_usd'))}"
                )
        for template_id, run in result.get("templates", {}).items():
            if run.get("skipped"):
                lines.append(
                    f"  {run.get('template_name', template_id)}: "
                    f"not eligible for this model"
                )
                continue
            score = run.get("template_score")
            not_run = run.get("not_run", 0) or 0
            budget_stopped = run.get("budget_stopped", 0) or 0
            if score is None and not_run:
                lines.append(
                    f"  {run.get('template_name', template_id)}: not run "
                    f"({not_run} {'task' if not_run == 1 else 'tasks'} "
                    "not run — test file missing)"
                )
            elif score is None and budget_stopped:
                lines.append(
                    f"  {run.get('template_name', template_id)}: "
                    f"time budget reached "
                    f"({budget_stopped} "
                    f"{'task' if budget_stopped == 1 else 'tasks'} stopped "
                    "before they ran)"
                )
            elif score is None:
                lines.append(
                    f"  {run.get('template_name', template_id)}: no scoring, "
                    f"{fmt_toks(run.get('avg_decode_tok_s'))} tok/s"
                )
            else:
                line = (
                    f"  {run.get('template_name', template_id)}: {score:.0f}% "
                    f"({run.get('passed')}/{run.get('total')}"
                    f"{_not_run_suffix(not_run)}), "
                    f"{fmt_toks(run.get('avg_decode_tok_s'))} tok/s"
                )
                if budget_stopped:
                    line += (
                        f" — time budget reached "
                        f"({budget_stopped} "
                        f"{'task' if budget_stopped == 1 else 'tasks'} stopped)"
                    )
                lines.append(line)
        lines.append("")
    return "\n".join(lines)


def template_score_text(run):
    """One cell of text for a template run: its score, or what
    happened instead. "not eligible" when the model could not run it
    at all, "not run" when nothing ran for a missing test file,
    "time budget reached" when the budget stopped it before any task
    ran, "no scoring" for the unscored templates. A score that was
    cut short by the budget says so next to the number."""
    if run.get("skipped"):
        return "not eligible"
    score = run.get("template_score")
    not_run = run.get("not_run", 0) or 0
    budget_stopped = run.get("budget_stopped", 0) or 0
    if score is None and not_run:
        return "not run"
    if score is None and budget_stopped:
        return "time budget reached"
    if score is None:
        return "no scoring"
    text = f"{score:.0f}%"
    if budget_stopped:
        text += " (time budget reached)"
    return text


def export_table(results):
    """The current results as a table: (columns, rows) of plain text.

    Columns are Model, Overall score, Avg decode tok/s, Status, then
    one column per template that any result ran (in the order the
    templates first appear). A model that did not run a template
    shows "—" in that column. No answers and no API keys — only the
    scores, speeds, and statuses the results table shows.
    """
    columns = ["Model", "Overall score", "Avg decode tok/s", "Status"]
    template_names = []
    for result in results:
        for run in result.get("templates", {}).values():
            name = run.get("template_name", run.get("template", "?"))
            if name not in template_names:
                template_names.append(name)
    columns.extend(template_names)
    rows = []
    for result in sort_results(results):
        row = [
            result["name"],
            fmt_score(result.get("overall_score")),
            fmt_toks(result.get("avg_decode_tok_s")),
            result["status"],
        ]
        by_name = {}
        for run in result.get("templates", {}).values():
            by_name[run.get("template_name", run.get("template", "?"))] = run
        for name in template_names:
            run = by_name.get(name)
            row.append(template_score_text(run) if run is not None else "—")
        rows.append(row)
    return columns, rows


def render_export_csv(results):
    """The current results as CSV text (see export_table)."""
    columns, rows = export_table(results)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(columns)
    writer.writerows(rows)
    return buffer.getvalue()


def render_export_html(results):
    """The current results as a small self-contained HTML page: the
    same table as the CSV, with a heading and the date. Scores and
    model names are escaped; nothing else from the results is in it."""
    columns, rows = export_table(results)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "<!DOCTYPE html>",
        "<html>",
        "<head>",
        '<meta charset="utf-8">',
        "<title>OLLAMA SYSTEM TEST — results</title>",
        "<style>",
        "body { font-family: sans-serif; }",
        "table { border-collapse: collapse; }",
        "th, td { border: 1px solid #999; padding: 4px 8px; "
        "text-align: left; }",
        "th { background: #eee; }",
        "</style>",
        "</head>",
        "<body>",
        "<h1>OLLAMA SYSTEM TEST — results</h1>",
        f"<p>Exported {html.escape(stamp)}.</p>",
        "<table>",
        "<tr>" + "".join(
            f"<th>{html.escape(column)}</th>" for column in columns
        ) + "</tr>",
    ]
    for row in rows:
        lines.append(
            "<tr>" + "".join(
                f"<td>{html.escape(cell)}</td>" for cell in row
            ) + "</tr>"
        )
    lines.extend(["</table>", "</body>", "</html>", ""])
    return "\n".join(lines)


def compare_models(results_dir, slug_a, slug_b):
    """Two models' latest scores side by side, from their run files.

    Returns a dict: the two display names, one row per template
    either model ran (template name, latest score or None for each
    model, and whether each side's latest run was "not run"), plus
    each model's overall score (the mean of its latest scored
    templates, like the profile) and average decode speed. A
    template neither model scored shows "no scoring" in the view;
    a latest run that never ran shows "not run". Returns None when
    either model has no runs at all.
    """
    runs_a = load_model_runs(results_dir, slug_a)
    runs_b = load_model_runs(results_dir, slug_b)
    if not runs_a or not runs_b:
        return None

    def latest_by_template(runs):
        by_template = {}
        for run in runs:
            by_template.setdefault(run.get("template", "?"), []).append(run)
        latest = {}
        for tid, t_runs in by_template.items():
            name = t_runs[-1].get("template_name", tid)
            scored = [r for r in t_runs if r.get("template_score") is not None]
            latest_not_run = bool(t_runs[-1].get("not_run"))
            latest[tid] = {
                "name": name,
                "score": scored[-1]["template_score"] if scored else None,
                "not_run": latest_not_run and not scored,
            }
        return latest

    def overall_and_speed(runs, latest):
        scores = [
            entry["score"] for entry in latest.values()
            if entry["score"] is not None
        ]
        overall = sum(scores) / len(scores) if scores else None
        speeds = [
            r["avg_decode_tok_s"] for r in runs
            if r.get("avg_decode_tok_s") is not None
        ]
        speed = sum(speeds) / len(speeds) if speeds else None
        return overall, speed

    latest_a = latest_by_template(runs_a)
    latest_b = latest_by_template(runs_b)
    overall_a, speed_a = overall_and_speed(runs_a, latest_a)
    overall_b, speed_b = overall_and_speed(runs_b, latest_b)
    rows = []
    for tid in sorted(
        set(latest_a) | set(latest_b),
        key=lambda t: (latest_a.get(t) or latest_b[t])["name"],
    ):
        entry_a = latest_a.get(tid)
        entry_b = latest_b.get(tid)
        name = (entry_a or entry_b)["name"]
        rows.append({
            "name": name,
            "score_a": entry_a["score"] if entry_a else None,
            "score_b": entry_b["score"] if entry_b else None,
            "not_run_a": bool(entry_a and entry_a["not_run"]),
            "not_run_b": bool(entry_b and entry_b["not_run"]),
            "missing_a": entry_a is None,
            "missing_b": entry_b is None,
        })
    return {
        "name_a": runs_a[-1].get("model") or slug_a,
        "name_b": runs_b[-1].get("model") or slug_b,
        "rows": rows,
        "overall_a": overall_a,
        "overall_b": overall_b,
        "speed_a": speed_a,
        "speed_b": speed_b,
    }


# ---------------------------------------------------------------------------
# Benchmark worker (runs in its own thread, one model at a time)
# ---------------------------------------------------------------------------

def _truncate_answer(answer):
    if len(answer) > ANSWER_TRUNCATE:
        return answer[:ANSWER_TRUNCATE] + "… (truncated)"
    return answer


def template_eligible(template, target):
    """True when a target may run a template: the template's capability
    is "text" (every model), or the target was screened with it."""
    capability = template.get("capability", "text")
    if capability == "text":
        return True
    return capability in set(target.get("capabilities") or [])


def format_capabilities(capabilities):
    """Short bracket text for the model list, e.g. [vision, tools].

    Text-only models show nothing: the list stays clean for the
    common case, and the bracket only appears when there is something
    worth knowing.
    """
    shown = [c for c in CAPABILITIES if c != "text" and c in capabilities]
    if not shown:
        return ""
    return " [" + ", ".join(shown) + "]"


def ollama_target(model_name, capabilities=None):
    """A benchmark target for a local Ollama model.

    Capabilities come from screening (/api/show, cached); unscreened
    models are text-only until screened.
    """
    caps = set(capabilities) if capabilities else {"text"}
    caps.add("text")
    return {
        "label": model_name,
        "kind": "ollama",
        "model": model_name,
        "provider": None,
        "provider_name": "ollama",
        "price_input_per_1m": None,
        "price_output_per_1m": None,
        "capabilities": sorted(caps),
    }


def provider_target(provider, model_entry):
    """A benchmark target for a provider model.

    The label is "provider / model-id" everywhere: the model list, the
    results table, and the run files. Capabilities come from the flags
    on the model entry (text-only when none were set).
    """
    caps = set(model_entry.get("capabilities") or ["text"])
    return {
        "label": provider_label(provider["name"], model_entry["id"]),
        "kind": "provider",
        "model": model_entry["id"],
        "provider": provider,
        "provider_name": provider["name"],
        "price_input_per_1m": model_entry.get("price_input_per_1m"),
        "price_output_per_1m": model_entry.get("price_output_per_1m"),
        "capabilities": sorted(caps),
    }


def _to_provider_messages(messages):
    """Ollama-shaped chat messages to OpenAI-shaped ones.

    The only difference is images: Ollama carries them in an "images"
    array on the user message, OpenAI carries them as image_url parts
    in the content. Text-only messages pass through unchanged.
    """
    converted = []
    for message in messages:
        images = message.get("images")
        if not images:
            converted.append(
                {"role": message["role"], "content": message.get("content", "")}
            )
            continue
        parts = [{"type": "text", "text": message.get("content", "")}]
        for raw in images:
            parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{raw}"},
            })
        converted.append({"role": message["role"], "content": parts})
    return converted


def _to_tool_definitions(tools):
    """Template tools ({name, description, parameters}) to the
    function-call shape both Ollama and OpenAI expect."""
    definitions = []
    for tool in tools or []:
        definitions.append({
            "type": "function",
            "function": {
                "name": tool.get("name"),
                "description": tool.get("description", ""),
                "parameters": tool.get("parameters") or {"type": "object", "properties": {}},
            },
        })
    return definitions


class BenchWorker(QThread):
    model_started = Signal(str)
    template_done = Signal(str, dict, str)  # model, run data, file path
    model_done = Signal(str, dict)
    progress = Signal(str, int)
    # "done", "cancelled", or "ollama_down"
    run_finished = Signal(str)

    def __init__(self, targets, templates, sys_id, machine, results_dir,
                 time_budget_seconds=None):
        super().__init__()
        self.targets = targets
        self.templates = templates
        self.sys_id = sys_id
        self.machine = machine
        self.results_dir = results_dir
        # Opt-in wall-time budget per template, in seconds (None means
        # no budget — the default). Each (model, template) pair gets
        # its own window, starting when the template starts.
        self.time_budget_seconds = time_budget_seconds
        self._stop = False

    def request_cancel(self):
        self._stop = True

    def _should_stop(self):
        return self._stop

    def _budget_reached(self, started):
        """True when the time budget is set and this template's
        window has run out."""
        if self.time_budget_seconds is None:
            return False
        return time.monotonic() - started >= self.time_budget_seconds

    def _budget_result(self, job_id, prompt, task):
        """A task record for a job the time budget stopped: passed
        None, never False — the model did not fail, it ran out of
        time. Excluded from the score like a not-run task."""
        record = self._failed_result(job_id, prompt, task, BUDGET_REACHED)
        record["passed"] = None
        return record

    def _call_once(self, target, prompt, num_predict):
        if target["kind"] == "provider":
            return provider_generate_once(
                target["provider"], target["model"], prompt, num_predict,
                TEMPERATURE, self._should_stop,
            )
        return generate_once(
            target["model"], prompt, num_predict, TEMPERATURE,
            self._should_stop,
        )

    def _generate(self, target, prompt, num_predict):
        """One generate call, retried once if the other end did not answer.

        A single dropped connection is often transient (Ollama was
        still starting, the machine was swapping, or a provider hiccuped),
        so one retry after a short pause is worth it. Anything else
        fails at once. A bad key is never retried.
        """
        try:
            return self._call_once(target, prompt, num_predict)
        except (OllamaError, ProviderError) as exc:
            retryable = is_unreachable(exc) or is_provider_unreachable(exc)
            if not retryable or self._stop:
                raise
            time.sleep(RETRY_DELAY)
            if self._stop:
                raise Cancelled()
            return self._call_once(target, prompt, num_predict)

    def _call_chat(self, target, messages, num_predict, tools=None):
        """One chat call (Ollama /api/chat or provider chat completions).

        Messages are in Ollama shape; provider calls convert the image
        parts (see _to_provider_messages).
        """
        if target["kind"] == "provider":
            return provider_chat_once(
                target["provider"], target["model"],
                _to_provider_messages(messages), num_predict,
                TEMPERATURE, tools, self._should_stop,
            )
        return chat_once(
            target["model"], messages, num_predict, TEMPERATURE,
            tools, self._should_stop,
        )

    def _chat(self, target, messages, num_predict, tools=None):
        """One chat call, retried once if the other end did not answer."""
        try:
            return self._call_chat(target, messages, num_predict, tools)
        except (OllamaError, ProviderError) as exc:
            retryable = is_unreachable(exc) or is_provider_unreachable(exc)
            if not retryable or self._stop:
                raise
            time.sleep(RETRY_DELAY)
            if self._stop:
                raise Cancelled()
            return self._call_chat(target, messages, num_predict, tools)

    def _count_steps(self):
        """Progress steps: one warmup per target, then one step per
        model call on the eligible templates only. A multi-turn task
        counts its turns (each turn is a call); the Speed template
        counts its context lengths. Skipped pairs cost no steps."""
        steps = 0
        for target in self.targets:
            steps += 1  # warmup
            for template in self.templates:
                if not template_eligible(template, target):
                    continue
                if template["scored"]:
                    for task in template["tasks"]:
                        if task.get("kind") == "chat":
                            steps += len(task["turns"])
                        else:
                            steps += 1
                else:
                    if template.get("context_lengths"):
                        steps += len(template["context_lengths"])
                    else:
                        steps += len(template["prompts"])
        return max(steps, 1)

    def _failed_result(self, job_id, prompt, task, detail):
        """A task record for a call that failed before it could be scored."""
        return {
            "id": job_id,
            "prompt": prompt,
            "answer": "",
            "passed": False if task is not None else None,
            "decode_tok_s": None,
            "prompt_tok_s": None,
            "ttft_s": None,
            "prompt_tokens": None,
            "completion_tokens": None,
            "detail": detail,
        }

    def _not_run_result(self, job_id, prompt, task, detail):
        """A task record for a task that never ran (its test file was
        missing). passed is None, never False: a missing file is not
        the model's failure, and the score counts only tasks that ran."""
        record = self._failed_result(job_id, prompt, task, detail)
        record["passed"] = None
        return record

    def _task_generate(self, target, template, task, prompt):
        """One prompt in, one answer out (generate + long-context)."""
        label = target["label"]
        job_id = task["id"] if task is not None else "run"
        metrics = self._generate(target, prompt, template["num_predict"])
        if task is None:
            passed, detail = None, ""
        else:
            passed, detail = score_task(task, metrics["answer"])
        self._bump_progress(label, template, job_id)
        record = {
            "id": job_id,
            "prompt": prompt,
            "answer": _truncate_answer(metrics["answer"]),
            "passed": passed,
            "decode_tok_s": metrics["decode_tok_s"],
            "prompt_tok_s": metrics["prompt_tok_s"],
            "ttft_s": metrics["ttft_s"],
            "prompt_tokens": metrics.get("prompt_tokens"),
            "completion_tokens": metrics.get("completion_tokens"),
            "detail": detail,
        }
        if task is not None and task.get("kind") == "long_context":
            record["context_tokens"] = task.get("context_length")
            record["prompt_chars"] = len(prompt)
        return record, None

    def _task_chat(self, target, template, task):
        """A multi-turn task: each turn is sent with the full history
        so far, so a follow-up can only pass when the model remembers
        the earlier turns. A task passes when every scored turn passes."""
        label = target["label"]
        num_predict = template["num_predict"]
        messages = []
        if task.get("system"):
            messages.append({"role": "system", "content": task["system"]})
        turn_records = []
        all_passed = True
        speeds = []
        prompt_total = 0
        completion_total = 0
        saw_tokens = False
        for index, turn in enumerate(task["turns"]):
            if self._stop:
                raise Cancelled()
            messages.append({"role": "user", "content": turn["prompt"]})
            metrics = self._chat(target, messages, num_predict)
            messages.append({"role": "assistant", "content": metrics["answer"]})
            if "rule" in turn:
                passed, detail = score_task(turn, metrics["answer"])
                if not passed:
                    all_passed = False
            else:
                passed, detail = None, ""
            if metrics["decode_tok_s"] is not None:
                speeds.append(metrics["decode_tok_s"])
            if metrics.get("prompt_tokens") is not None:
                prompt_total += metrics["prompt_tokens"]
                saw_tokens = True
            if metrics.get("completion_tokens") is not None:
                completion_total += metrics["completion_tokens"]
                saw_tokens = True
            turn_records.append({
                "prompt": turn["prompt"],
                "answer": _truncate_answer(metrics["answer"]),
                "passed": passed,
                "decode_tok_s": metrics["decode_tok_s"],
                "detail": detail,
            })
            self._bump_progress(label, template, f"{task['id']} turn {index + 1}")
        last = turn_records[-1] if turn_records else {}
        return {
            "id": task["id"],
            "prompt": task["turns"][0]["prompt"],
            "answer": last.get("answer", ""),
            "passed": all_passed,
            "decode_tok_s": sum(speeds) / len(speeds) if speeds else None,
            "prompt_tok_s": None,
            "ttft_s": None,
            "prompt_tokens": prompt_total if saw_tokens else None,
            "completion_tokens": completion_total if saw_tokens else None,
            "detail": "; ".join(
                t["detail"] for t in turn_records if t.get("detail")
            ),
            "turns": turn_records,
        }, None

    def _task_vision(self, target, template, task):
        """Show the model an image and ask about it. Ollama takes the
        image base64 in the message; providers take an image_url part."""
        label = target["label"]
        image_path = os.path.join(get_images_dir(), task["image"])
        try:
            with open(image_path, "rb") as handle:
                raw = handle.read()
        except OSError:
            self._bump_progress(label, template, task["id"])
            record = self._not_run_result(
                task["id"], task["prompt"], task, NOT_RUN_IMAGE,
            )
            record["image"] = task["image"]
            return record, None
        encoded = base64.b64encode(raw).decode("ascii")
        messages = [{
            "role": "user",
            "content": task["prompt"],
            "images": [encoded],
        }]
        metrics = self._chat(target, messages, template["num_predict"])
        passed, detail = score_task(task, metrics["answer"])
        self._bump_progress(label, template, task["id"])
        return {
            "id": task["id"],
            "prompt": task["prompt"],
            "answer": _truncate_answer(metrics["answer"]),
            "passed": passed,
            "decode_tok_s": metrics["decode_tok_s"],
            "prompt_tok_s": metrics["prompt_tok_s"],
            "ttft_s": metrics["ttft_s"],
            "prompt_tokens": metrics.get("prompt_tokens"),
            "completion_tokens": metrics.get("completion_tokens"),
            "detail": detail,
            "image": task["image"],
        }, None

    def _task_tools(self, target, template, task):
        """Give the model tool definitions and check it calls the right
        one with the right arguments — or none at all, when the task
        needs no tool."""
        label = target["label"]
        messages = [{"role": "user", "content": task["prompt"]}]
        metrics = self._chat(
            target, messages, template["num_predict"],
            _to_tool_definitions(task["tools"]),
        )
        tool_calls = metrics.get("tool_calls") or []
        passed, detail = score_tool_call(task, tool_calls)
        self._bump_progress(label, template, task["id"])
        return {
            "id": task["id"],
            "prompt": task["prompt"],
            "answer": _truncate_answer(metrics["answer"]),
            "passed": passed,
            "decode_tok_s": metrics["decode_tok_s"],
            "prompt_tok_s": metrics["prompt_tok_s"],
            "ttft_s": metrics["ttft_s"],
            "prompt_tokens": metrics.get("prompt_tokens"),
            "completion_tokens": metrics.get("completion_tokens"),
            "detail": detail,
            "tool_calls": tool_calls,
        }, None

    def _task_image(self, target, template, task):
        """Ask a provider to generate an image. Passes when real image
        bytes come back; the image itself is saved next to the run file
        for Joshua to judge by eye. Ollama has no image endpoint, so an
        Ollama target reaching here is a bug — it fails plainly."""
        label = target["label"]
        if target["kind"] != "provider":
            self._bump_progress(label, template, task["id"])
            return self._failed_result(
                task["id"], task["prompt"], task,
                "image generation needs a provider model",
            ), None
        t_start = time.monotonic()
        try:
            raw, fmt, width, height = provider_generate_image(
                target["provider"], target["model"], task["prompt"],
                task.get("size", "1024x1024"),
            )
        except (ProviderError, ProviderUnreachable):
            raise
        wall_s = time.monotonic() - t_start
        self._bump_progress(label, template, task["id"])
        filename = f"{task['id']}.{fmt}"
        record = {
            "id": task["id"],
            "prompt": task["prompt"],
            "answer": f"image generated: {width}x{height} {fmt}",
            "passed": True,
            "decode_tok_s": None,
            "prompt_tok_s": None,
            "ttft_s": None,
            "prompt_tokens": None,
            "completion_tokens": None,
            "detail": "",
            "image_format": fmt,
            "image_width": width,
            "image_height": height,
            "wall_s": round(wall_s, 2),
        }
        return record, ("images", filename, raw, "image_file")

    def _task_audio(self, target, template, task):
        """Audio tasks, provider-only. Transcribe: send a bundled clip
        and score the transcript's words. Speech: ask for spoken audio
        and check real audio bytes come back (saved next to the run)."""
        label = target["label"]
        if target["kind"] != "provider":
            self._bump_progress(label, template, task["id"])
            return self._failed_result(
                task["id"], task["prompt"], task,
                "audio needs a provider model",
            ), None
        if task.get("audio_task") == "transcribe":
            audio_path = os.path.join(get_audio_dir(), task["audio_file"])
            if not os.path.exists(audio_path):
                self._bump_progress(label, template, task["id"])
                record = self._not_run_result(
                    task["id"], task["prompt"], task, NOT_RUN_AUDIO,
                )
                record["audio_file"] = task["audio_file"]
                return record, None
            t_start = time.monotonic()
            try:
                transcript = provider_transcribe(
                    target["provider"], target["model"], audio_path
                )
            except (ProviderError, ProviderUnreachable):
                raise
            wall_s = time.monotonic() - t_start
            passed, detail = score_task(task, transcript)
            self._bump_progress(label, template, task["id"])
            return {
                "id": task["id"],
                "prompt": task["prompt"],
                "answer": _truncate_answer(transcript),
                "passed": passed,
                "decode_tok_s": None,
                "prompt_tok_s": None,
                "ttft_s": None,
                "prompt_tokens": None,
                "completion_tokens": None,
                "detail": detail,
                "audio_file": task["audio_file"],
                "wall_s": round(wall_s, 2),
            }, None
        t_start = time.monotonic()
        try:
            raw = provider_synthesize(
                target["provider"], target["model"], task["prompt"]
            )
        except (ProviderError, ProviderUnreachable):
            raise
        wall_s = time.monotonic() - t_start
        self._bump_progress(label, template, task["id"])
        if not is_audio_bytes(raw):
            return self._failed_result(
                task["id"], task["prompt"], task,
                "provider returned bytes that are not audio",
            ), None
        filename = f"{task['id']}.mp3"
        record = {
            "id": task["id"],
            "prompt": task["prompt"],
            "answer": f"audio generated: {len(raw)} bytes",
            "passed": True,
            "decode_tok_s": None,
            "prompt_tok_s": None,
            "ttft_s": None,
            "prompt_tokens": None,
            "completion_tokens": None,
            "detail": "",
            "audio_bytes": len(raw),
            "wall_s": round(wall_s, 2),
        }
        return record, ("audio", filename, raw, "audio_file_saved")

    def _run_scored_task(self, target, template, task):
        """Run one scored task by its kind. Returns (record, media).

        Media is (subfolder, filename, bytes, record-field) for a
        generated image or spoken audio, or None. A model error becomes
        a failed task record (and a progress bump); Ollama going away
        aborts the whole run.
        """
        kind = task.get("kind", "generate")
        try:
            if kind == "generate":
                return self._task_generate(target, template, task, task["prompt"])
            if kind == "long_context":
                doc = build_long_document(
                    task["needle"], task["context_length"],
                    task.get("position", "middle"),
                )
                return self._task_generate(
                    target, template, task, doc + "\n\n" + task["prompt"]
                )
            if kind == "chat":
                return self._task_chat(target, template, task)
            if kind == "vision":
                return self._task_vision(target, template, task)
            if kind == "tools":
                return self._task_tools(target, template, task)
            if kind == "image_gen":
                return self._task_image(target, template, task)
            if kind == "audio":
                return self._task_audio(target, template, task)
        except OllamaError as exc:
            if is_unreachable(exc):
                # Ollama itself went away: stop the whole run
                # instead of failing every remaining task and model.
                raise OllamaUnreachable(describe_error(exc))
            self._bump_progress(target["label"], template, task["id"])
            return self._failed_result(task["id"], task.get("prompt", ""), task, str(exc)), None
        except ProviderError as exc:
            # A provider failing never stops the run: this task
            # fails plainly and the rest carry on, including the
            # local Ollama models still to come.
            self._bump_progress(target["label"], template, task["id"])
            return self._failed_result(task["id"], task.get("prompt", ""), task, str(exc)), None
        self._bump_progress(target["label"], template, task["id"])
        return self._failed_result(
            task["id"], task.get("prompt", ""), task,
            f"unknown task kind {kind!r}",
        ), None

    def _run_template(self, target, template):
        """Run one template for one target. Returns the run data dict."""
        label = target["label"]
        num_predict = template["num_predict"]
        task_results = []
        media = []
        # The budget window starts when the template starts, per
        # (model, template) pair. Checked between jobs: a job already
        # running finishes, then the rest stop with "time budget
        # reached" and the run carries on with the next template.
        started = time.monotonic()
        budget_hit = False
        if template["scored"]:
            for index, task in enumerate(template["tasks"]):
                if self._stop:
                    raise Cancelled()
                if self._budget_reached(started):
                    budget_hit = True
                    for remaining in template["tasks"][index:]:
                        task_results.append(self._budget_result(
                            remaining["id"], remaining.get("prompt", ""),
                            remaining,
                        ))
                        self._bump_progress(label, template, remaining["id"])
                    break
                record, item = self._run_scored_task(target, template, task)
                if item is not None:
                    media.append((task["id"],) + item)
                task_results.append(record)
        else:
            # Unscored: context lengths (Speed) or plain prompts.
            if template.get("context_lengths"):
                jobs = []
                for length in template["context_lengths"]:
                    prompt = build_context_prompt(
                        template["context_base_prompt"], length
                    )
                    jobs.append((f"ctx-{length}", prompt, {"context_tokens": length}))
            else:
                jobs = [
                    (f"run-{i + 1}", prompt, None)
                    for i, prompt in enumerate(template["prompts"])
                ]
            for job_index, (job_id, prompt, extra) in enumerate(jobs):
                if self._stop:
                    raise Cancelled()
                if self._budget_reached(started):
                    budget_hit = True
                    for remaining_id, remaining_prompt, remaining_extra in jobs[job_index:]:
                        record = self._budget_result(
                            remaining_id, remaining_prompt, None
                        )
                        if remaining_extra:
                            record.update(remaining_extra)
                            record["prompt_chars"] = len(remaining_prompt)
                        task_results.append(record)
                        self._bump_progress(label, template, remaining_id)
                    break
                try:
                    metrics = self._generate(target, prompt, num_predict)
                except OllamaError as exc:
                    if is_unreachable(exc):
                        raise OllamaUnreachable(describe_error(exc))
                    task_results.append(
                        self._failed_result(job_id, prompt, None, str(exc))
                    )
                    self._bump_progress(label, template, job_id)
                    continue
                except ProviderError as exc:
                    task_results.append(
                        self._failed_result(job_id, prompt, None, str(exc))
                    )
                    self._bump_progress(label, template, job_id)
                    continue
                record = {
                    "id": job_id,
                    "prompt": _truncate_answer(prompt),
                    "answer": _truncate_answer(metrics["answer"]),
                    "passed": None,
                    "decode_tok_s": metrics["decode_tok_s"],
                    "prompt_tok_s": metrics["prompt_tok_s"],
                    "ttft_s": metrics["ttft_s"],
                    "prompt_tokens": metrics.get("prompt_tokens"),
                    "completion_tokens": metrics.get("completion_tokens"),
                    "detail": "",
                }
                if extra:
                    record.update(extra)
                    record["prompt_chars"] = len(prompt)
                task_results.append(record)
                self._bump_progress(label, template, job_id)
        # Tasks that never ran (missing test file) count in neither the
        # numerator nor the denominator: the score is over the tasks
        # that actually ran. When nothing ran, there is no score at
        # all — the views show "not run", never 0%. Tasks the time
        # budget stopped count the same way: they never ran either.
        not_run_count = sum(1 for t in task_results if is_not_run(t))
        budget_stopped = sum(
            1 for t in task_results if is_budget_stopped(t)
        )
        if template["scored"]:
            ran = [t for t in task_results
                   if not is_not_run(t) and not is_budget_stopped(t)]
            passed_count = sum(1 for t in ran if t["passed"] is True)
            total = len(ran)
            score = 100.0 * passed_count / total if total else None
            if total == 0:
                passed_count = None
        else:
            passed_count = None
            total = len(task_results)
            score = None
        speeds = [t["decode_tok_s"] for t in task_results if t["decode_tok_s"] is not None]
        prompt_tokens = [t["prompt_tokens"] for t in task_results
                         if t["prompt_tokens"] is not None]
        completion_tokens = [t["completion_tokens"] for t in task_results
                             if t["completion_tokens"] is not None]
        run_prompt_tokens = sum(prompt_tokens) if prompt_tokens else None
        run_completion_tokens = sum(completion_tokens) if completion_tokens else None
        run_total_tokens = None
        if run_prompt_tokens is not None and run_completion_tokens is not None:
            run_total_tokens = run_prompt_tokens + run_completion_tokens
        # Generated media goes next to the run file, in a folder named
        # after it, so the image or audio stays part of that run's
        # record. The run file's own path is fixed first so the task
        # records can name their files.
        stamp = datetime.now()
        path, folder, file_base = run_file_path(
            self.results_dir, label, self.sys_id, template["id"], stamp
        )
        for task_id, subfolder, filename, blob, field in media:
            media_dir = os.path.join(folder, f"{file_base}-{subfolder}")
            os.makedirs(media_dir, exist_ok=True)
            with open(os.path.join(media_dir, filename), "wb") as handle:
                handle.write(blob)
            for record in task_results:
                if record["id"] == task_id:
                    record[field] = f"{file_base}-{subfolder}/{filename}"
                    break
        run_data = {
            "template": template["id"],
            "template_name": template["name"],
            "capability": template.get("capability", "text"),
            "machine": self.machine,
            "warmup_prompt": WARMUP_PROMPT,
            "num_predict": num_predict,
            "temperature": TEMPERATURE,
            "provider": target["provider_name"],
            "provider_model": target["model"],
            "tasks": task_results,
            "template_score": score,
            "passed": passed_count,
            "total": total,
            "not_run": not_run_count,
            "time_budget_reached": budget_hit,
            "budget_stopped": budget_stopped,
            "avg_decode_tok_s": sum(speeds) / len(speeds) if speeds else None,
            "prompt_tokens": run_prompt_tokens,
            "completion_tokens": run_completion_tokens,
            "total_tokens": run_total_tokens,
            "price_input_per_1m": target["price_input_per_1m"],
            "price_output_per_1m": target["price_output_per_1m"],
            "estimated_cost_usd": estimate_cost(
                run_prompt_tokens, run_completion_tokens,
                target["price_input_per_1m"], target["price_output_per_1m"],
            ),
        }
        path = save_run_file(self.results_dir, label, self.sys_id, run_data, stamp=stamp)
        return run_data, path

    def _bump_progress(self, label, template, job_id):
        self._done_steps += 1
        percent = int(100 * self._done_steps / self._total_steps)
        self.progress.emit(
            f"{label}: {template['name']} - {job_id}", min(percent, 100)
        )

    def run(self):
        self._total_steps = self._count_steps()
        self._done_steps = 0
        for target in self.targets:
            label = target["label"]
            if self._stop:
                self.run_finished.emit("cancelled")
                return
            self.model_started.emit(label)
            result = {
                "name": label,
                "status": "done",
                "provider": target["provider_name"],
                "provider_model": target["model"],
                "templates": {},
                "overall_score": None,
                "avg_decode_tok_s": None,
                "size_ram": None,
                "prompt_tokens": None,
                "completion_tokens": None,
                "total_tokens": None,
                "estimated_cost_usd": None,
                "price_input_per_1m": target["price_input_per_1m"],
                "price_output_per_1m": target["price_output_per_1m"],
            }
            warmup_prompt_tokens = None
            warmup_completion_tokens = None
            try:
                self.progress.emit(
                    f"{label}: warming up",
                    int(100 * self._done_steps / self._total_steps),
                )
                if "text" in set(target.get("capabilities") or ["text"]):
                    warmup = self._generate(target, WARMUP_PROMPT, WARMUP_PREDICT)
                    warmup_prompt_tokens = warmup.get("prompt_tokens")
                    warmup_completion_tokens = warmup.get("completion_tokens")
                elif target["kind"] == "provider":
                    # A model with no text capability (image-only, audio-
                    # only) cannot take a text warmup, so the warmup is
                    # a connection test instead: it proves the key and
                    # the endpoint work before any template runs.
                    ok, message = provider_test_connection(target["provider"])
                    if not ok:
                        raise ProviderError(message)
                self._done_steps += 1
            except Cancelled:
                self.run_finished.emit("cancelled")
                return
            except OllamaError as exc:
                if is_unreachable(exc):
                    # The warmup retry already failed: Ollama is gone.
                    result["status"] = f"failed: {describe_error(exc)}"
                    self.model_done.emit(label, result)
                    self.run_finished.emit("ollama_down")
                    return
                result["status"] = f"failed: {exc}"
                self.model_done.emit(label, result)
                continue
            except ProviderError as exc:
                # The provider did not answer or rejected the key: this
                # model fails plainly and the run carries on with the
                # rest, including the local Ollama models.
                result["status"] = f"failed: {exc}"
                self.model_done.emit(label, result)
                continue
            try:
                for template in self.templates:
                    if self._stop:
                        raise Cancelled()
                    if not template_eligible(template, target):
                        # The model cannot run this template at all (no
                        # vision, no tools...). That is not a failure:
                        # it is recorded as skipped, gets no run file,
                        # and is left out of the overall score.
                        result["templates"][template["id"]] = {
                            "template": template["id"],
                            "template_name": template["name"],
                            "capability": template.get("capability", "text"),
                            "skipped": "not eligible",
                            "template_score": None,
                            "passed": None,
                            "total": 0,
                            "not_run": 0,
                            "tasks": [],
                            "avg_decode_tok_s": None,
                            "prompt_tokens": None,
                            "completion_tokens": None,
                            "total_tokens": None,
                            "estimated_cost_usd": None,
                        }
                        continue
                    run_data, path = self._run_template(target, template)
                    result["templates"][template["id"]] = run_data
                    result["templates"][template["id"]]["run_file"] = path
                    self.template_done.emit(label, run_data, path)
            except Cancelled:
                self.run_finished.emit("cancelled")
                return
            except OllamaUnreachable as exc:
                result["status"] = f"failed: {exc}"
                self.model_done.emit(label, result)
                self.run_finished.emit("ollama_down")
                return
            scores = [
                run["template_score"]
                for run in result["templates"].values()
                if run["template_score"] is not None
            ]
            if scores:
                result["overall_score"] = sum(scores) / len(scores)
            speeds = [
                task["decode_tok_s"]
                for run in result["templates"].values()
                for task in run["tasks"]
                if task["decode_tok_s"] is not None
            ]
            if speeds:
                result["avg_decode_tok_s"] = sum(speeds) / len(speeds)
            # Token totals cover the whole model: every template run
            # plus the warmup call (the provider charged for that too).
            prompt_total = 0
            completion_total = 0
            saw_tokens = False
            if warmup_prompt_tokens is not None and warmup_completion_tokens is not None:
                prompt_total += warmup_prompt_tokens
                completion_total += warmup_completion_tokens
                saw_tokens = True
            for run_data in result["templates"].values():
                if run_data.get("prompt_tokens") is not None:
                    prompt_total += run_data["prompt_tokens"]
                    saw_tokens = True
                if run_data.get("completion_tokens") is not None:
                    completion_total += run_data["completion_tokens"]
                    saw_tokens = True
            if saw_tokens:
                result["prompt_tokens"] = prompt_total
                result["completion_tokens"] = completion_total
                result["total_tokens"] = prompt_total + completion_total
                result["estimated_cost_usd"] = estimate_cost(
                    prompt_total, completion_total,
                    target["price_input_per_1m"],
                    target["price_output_per_1m"],
                )
            if target["kind"] == "ollama":
                result["size_ram"] = ps_sizes().get(label)
            self.model_done.emit(label, result)
        self.progress.emit("Done.", 100)
        self.run_finished.emit("done")


# ---------------------------------------------------------------------------
# Ollama starter (runs `ollama serve` detached, then waits for it)
# ---------------------------------------------------------------------------

class OllamaStarter(QThread):
    started_ok = Signal()
    failed = Signal(str)  # plain message

    def run(self):
        if ollama_reachable():
            self.started_ok.emit()
            return
        try:
            subprocess.Popen(
                ["ollama", "serve"],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            self.failed.emit(START_OLLAMA_MISSING_MSG)
            return
        except OSError as exc:
            self.failed.emit(f"Could not start Ollama: {exc}")
            return
        deadline = time.monotonic() + START_OLLAMA_TIMEOUT
        while time.monotonic() < deadline:
            if ollama_reachable():
                self.started_ok.emit()
                return
            time.sleep(START_OLLAMA_POLL)
        self.failed.emit(START_OLLAMA_TIMEOUT_MSG)


# ---------------------------------------------------------------------------
# Provider connection tester (GET /v1/models in its own thread)
# ---------------------------------------------------------------------------

class ProviderTestWorker(QThread):
    tested = Signal(bool, str)  # ok, plain message

    def __init__(self, provider):
        super().__init__()
        self.provider = provider

    def run(self):
        ok, message = provider_test_connection(self.provider)
        self.tested.emit(ok, message)


# ---------------------------------------------------------------------------
# Model puller (streams /api/pull progress in its own thread)
# ---------------------------------------------------------------------------

class PullWorker(QThread):
    progress = Signal(int, str)  # percent 0-100, or -1 when unknown; status text
    finished_ok = Signal(str)    # model name
    failed = Signal(str)         # plain message

    def __init__(self, name):
        super().__init__()
        self.name = name

    def run(self):
        payload = {"name": self.name, "stream": True}
        req = urllib.request.Request(
            ollama_base() + "/api/pull",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            resp = urllib.request.urlopen(req, timeout=READ_TIMEOUT)
        except urllib.error.HTTPError as exc:
            self.failed.emit(_http_error_reason(exc))
            return
        except (urllib.error.URLError, socket.timeout, OSError):
            self.failed.emit(UNREACHABLE_HINT)
            return
        last_chunk = time.monotonic()
        try:
            while True:
                try:
                    line = resp.readline()
                except socket.timeout:
                    if time.monotonic() - last_chunk > STALL_TIMEOUT:
                        self.failed.emit("Ollama stopped responding")
                        return
                    continue
                except OSError:
                    self.failed.emit(UNREACHABLE_HINT)
                    return
                if not line:
                    break
                last_chunk = time.monotonic()
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if event.get("error"):
                    self.failed.emit(str(event["error"]).strip().splitlines()[0])
                    return
                status = event.get("status") or ""
                if status == "success":
                    self.finished_ok.emit(self.name)
                    return
                completed = event.get("completed")
                total = event.get("total")
                if total:
                    percent = int(100 * (completed or 0) / total)
                    text = (
                        f"{status}: {(completed or 0) / 1_000_000:.1f} MB "
                        f"of {total / 1_000_000:.1f} MB"
                    )
                else:
                    percent = -1
                    text = status
                self.progress.emit(percent, text)
        finally:
            resp.close()
        self.failed.emit("The pull ended without finishing. Try again.")


class NumericItem(QTableWidgetItem):
    """Table cell that sorts by number, not by text.

    No number (None) counts as slowest, so failed rows sink to the
    bottom of the fastest-first view instead of floating to the top.
    """

    def __init__(self, value, text):
        super().__init__(text)
        self._value = float("-inf") if value is None else value

    def __lt__(self, other):
        if isinstance(other, NumericItem):
            return self._value < other._value
        return super().__lt__(other)


TABLE_COLUMNS = (
    "Model",
    "Overall score",
    "Avg decode tok/s",
    "Status",
)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} v{__version__}")
        icon_path = os.path.join(APP_DIR, "ost-icon.png")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))
        self.resize(980, 720)
        # Size and position survive a restart: the last geometry is
        # saved on close and restored here. A first launch (or a
        # missing/broken value) keeps the default 980x720.
        settings = QSettings("BrokeNBored", APP_NAME)
        saved_geometry = settings.value("geometry")
        if saved_geometry is not None:
            self.restoreGeometry(saved_geometry)
        # The media folders exist from the first launch, so a user
        # adding their own test image or audio clip has somewhere to
        # put it. The saved theme applies before the window shows, so
        # a dark-mode user never sees a light flash.
        ensure_media_dirs()
        self.dark_mode = settings.value("dark_mode", False, type=bool)
        apply_theme(self.dark_mode)
        self.worker = None
        self.pull_worker = None
        self.starter = None
        self.provider_test_worker = None
        self.results = {}
        self.result_order = []
        self.templates = []
        self.providers = []
        # The provider being edited on the Providers tab: a working copy
        # of one saved provider (or a fresh one), plus whether it is new
        # and the stored key it keeps when the key field is left empty.
        self.editing_provider = None
        self.editing_is_new = False
        self.editing_original_name = ""
        self.editing_kept_key = ""
        self.editing_model_index = None
        # Raw bytes of providers.json as last seen by the Providers tab,
        # so the tab can pick up outside edits without wiping an edit
        # in progress when nothing changed.
        self._providers_snapshot = None
        self.machine = machine_info()
        self.sys_id = system_id(self.machine)

        tabs = QTabWidget()
        self.setCentralWidget(tabs)
        self.tabs = tabs

        # ---- Benchmark tab ----
        bench_tab = QWidget()
        tabs.addTab(bench_tab, "Benchmark")
        layout = QVBoxLayout(bench_tab)

        intro = QLabel(
            "Nothing is ticked to start with. Tick the models and the test "
            "templates you want, then press Run. Each model runs each template "
            "in turn; every template run is saved to its own file under results, "
            "and nothing is ever overwritten. A template no ticked model can "
            "run is greyed out and cannot be ticked."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        pick_row = QHBoxLayout()
        model_box = QVBoxLayout()
        model_box.addWidget(QLabel("Models (local and provider):"))
        self.model_filter = QLineEdit()
        self.model_filter.setPlaceholderText("Filter models...")
        self.model_filter.setClearButtonEnabled(True)
        self.model_filter.textChanged.connect(self._apply_model_filter)
        model_box.addWidget(self.model_filter)
        self.model_list = QListWidget()
        self.model_list.setMaximumHeight(180)
        # Ticking or unticking a model changes which templates any
        # ticked model can run, so the grey-outs refresh on every tick.
        self.model_list.itemChanged.connect(self.update_template_availability)
        model_box.addWidget(self.model_list)
        model_buttons = QHBoxLayout()
        self.select_all_button = QPushButton("Select all")
        self.select_all_button.clicked.connect(self.select_all_models)
        self.clear_button = QPushButton("Clear")
        self.clear_button.clicked.connect(self.clear_models)
        self.screen_button = QPushButton("Screen models")
        self.screen_button.clicked.connect(self.screen_models)
        model_buttons.addWidget(self.select_all_button)
        model_buttons.addWidget(self.clear_button)
        model_buttons.addWidget(self.screen_button)
        model_buttons.addStretch()
        model_box.addLayout(model_buttons)
        pick_row.addLayout(model_box)

        template_box = QVBoxLayout()
        template_box.addWidget(QLabel("Test templates:"))
        self.template_filter = QLineEdit()
        self.template_filter.setPlaceholderText("Filter templates...")
        self.template_filter.setClearButtonEnabled(True)
        self.template_filter.textChanged.connect(self._apply_template_filter)
        template_box.addWidget(self.template_filter)
        self.template_list = QListWidget()
        self.template_list.setMaximumHeight(180)
        template_box.addWidget(self.template_list)
        template_buttons = QHBoxLayout()
        self.select_all_templates_button = QPushButton("Select all")
        self.select_all_templates_button.clicked.connect(self.select_all_templates)
        self.clear_templates_button = QPushButton("Clear")
        self.clear_templates_button.clicked.connect(self.clear_templates)
        template_buttons.addWidget(self.select_all_templates_button)
        template_buttons.addWidget(self.clear_templates_button)
        template_buttons.addStretch()
        template_box.addLayout(template_buttons)
        pick_row.addLayout(template_box)
        layout.addLayout(pick_row)

        run_buttons = QHBoxLayout()
        self.run_button = QPushButton("Run")
        self.run_button.clicked.connect(self.start_run)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.cancel_run)
        self.cancel_button.setEnabled(False)
        self.save_button = QPushButton("Save report")
        self.save_button.clicked.connect(self.save_report)
        self.save_button.setEnabled(False)
        self.export_csv_button = QPushButton("Export CSV")
        self.export_csv_button.clicked.connect(self.export_csv)
        self.export_csv_button.setEnabled(False)
        self.export_html_button = QPushButton("Export HTML")
        self.export_html_button.clicked.connect(self.export_html)
        self.export_html_button.setEnabled(False)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh_models)
        self.start_ollama_button = QPushButton("Start Ollama")
        self.start_ollama_button.clicked.connect(self.start_ollama)
        run_buttons.addWidget(self.run_button)
        run_buttons.addWidget(self.cancel_button)
        run_buttons.addWidget(self.save_button)
        run_buttons.addWidget(self.export_csv_button)
        run_buttons.addWidget(self.export_html_button)
        run_buttons.addWidget(self.refresh_button)
        run_buttons.addWidget(self.start_ollama_button)
        run_buttons.addStretch()
        layout.addLayout(run_buttons)

        # Opt-in per-template time budget. Default 0 = no limit: a
        # limit needs a concrete reason, and here the user sets it.
        # When set and a template's jobs run past it, that template's
        # jobs stop with "time budget reached" and the run carries on
        # with the next template.
        budget_row = QHBoxLayout()
        budget_row.addWidget(QLabel("Stop a template after"))
        self.budget_spin = QSpinBox()
        self.budget_spin.setRange(0, 600)
        self.budget_spin.setValue(0)
        self.budget_spin.setSuffix(" minutes")
        budget_row.addWidget(self.budget_spin)
        budget_row.addWidget(QLabel("(0 = no limit)"))
        budget_row.addStretch()
        layout.addLayout(budget_row)

        # ---- Pull a model ----
        pull_row = QHBoxLayout()
        pull_row.addWidget(QLabel("Pull a model:"))
        self.pull_input = QLineEdit()
        self.pull_input.setPlaceholderText("model name, e.g. qwen3:4b")
        self.pull_input.returnPressed.connect(self.start_pull)
        self.pull_button = QPushButton("Pull")
        self.pull_button.clicked.connect(self.start_pull)
        pull_row.addWidget(self.pull_input)
        pull_row.addWidget(self.pull_button)
        layout.addLayout(pull_row)
        self.pull_bar = QProgressBar()
        self.pull_bar.setRange(0, 100)
        self.pull_bar.setValue(0)
        layout.addWidget(self.pull_bar)
        self.pull_status = QLabel("")
        self.pull_status.setWordWrap(True)
        layout.addWidget(self.pull_status)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)
        self.status_label = QLabel("Ready.")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.table = QTableWidget(0, len(TABLE_COLUMNS))
        self.table.setHorizontalHeaderLabels(TABLE_COLUMNS)
        self.table.setSortingEnabled(True)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        # Columns size to their contents, so the "Avg decode tok/s"
        # header is never clipped; Status stretches with the spare width.
        header = self.table.horizontalHeader()
        for col in range(len(TABLE_COLUMNS) - 1):
            header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(len(TABLE_COLUMNS) - 1, QHeaderView.Stretch)
        self.table.cellClicked.connect(self.on_table_clicked)
        layout.addWidget(self.table)

        layout.addWidget(QLabel("Details for the selected model:"))
        self.detail_view = QTextEdit()
        self.detail_view.setReadOnly(True)
        self.detail_view.setMaximumHeight(190)
        self.detail_view.setPlainText("Run a test, then click a model to see its scores.")
        layout.addWidget(self.detail_view)

        # ---- Past runs tab ----
        history_tab = QWidget()
        tabs.addTab(history_tab, "Past runs")
        history_layout = QVBoxLayout(history_tab)
        history_layout.addWidget(QLabel(
            "Every template run is kept as its own file under results, "
            "grouped by model and by system. Pick a model to browse its runs."
        ))
        history_top = QHBoxLayout()
        self.history_model_combo = QComboBox()
        # Grow to fit the longest model name; without this the picker
        # stays at the width it had when empty and clips every name.
        self.history_model_combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.history_model_combo.currentIndexChanged.connect(self.on_history_model_changed)
        self.history_refresh_button = QPushButton("Refresh")
        self.history_refresh_button.clicked.connect(self.refresh_history)
        history_top.addWidget(self.history_model_combo)
        history_top.addWidget(self.history_refresh_button)
        history_top.addStretch()
        history_layout.addLayout(history_top)
        self.history_list = QListWidget()
        self.history_list.itemClicked.connect(self.on_history_run_clicked)
        self.history_list.currentItemChanged.connect(
            self._update_delete_button)
        history_layout.addWidget(self.history_list)
        self.history_view = QTextEdit()
        self.history_view.setReadOnly(True)
        self.history_view.setPlainText("Pick a run to see its scores.")
        history_layout.addWidget(self.history_view)
        # Deleting a run removes its file and its media folder, then
        # rebuilds the model's profile from the runs that are left.
        # The button stays disabled while a run is in progress, so a
        # run's files can never be deleted under it.
        history_bottom = QHBoxLayout()
        self.delete_run_button = QPushButton("Delete run")
        self.delete_run_button.clicked.connect(self.delete_selected_run)
        self.delete_run_button.setEnabled(False)
        self.store_size_label = QLabel("Results use —")
        history_bottom.addWidget(self.delete_run_button)
        history_bottom.addWidget(self.store_size_label)
        history_bottom.addStretch()
        history_layout.addLayout(history_bottom)

        # ---- Profiles tab ----
        profiles_tab = QWidget()
        tabs.addTab(profiles_tab, "Profiles")
        profiles_layout = QVBoxLayout(profiles_tab)
        profiles_layout.addWidget(QLabel(
            "Each model gets a profile: its scores, strengths, and weaknesses, "
            "built from every run it has ever done. Profiles rebuild themselves "
            "after each run."
        ))
        profile_top = QHBoxLayout()
        self.profile_model_combo = QComboBox()
        # Grow to fit the longest model name; without this the picker
        # stays at the width it had when empty and clips every name.
        self.profile_model_combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.profile_model_combo.currentIndexChanged.connect(self.on_profile_model_changed)
        self.profile_regen_button = QPushButton("Regenerate")
        self.profile_regen_button.clicked.connect(self.regenerate_profile)
        profile_top.addWidget(self.profile_model_combo)
        profile_top.addWidget(self.profile_regen_button)
        profile_top.addStretch()
        profiles_layout.addLayout(profile_top)
        self.profile_view = QTextEdit()
        self.profile_view.setReadOnly(True)
        self.profile_view.setPlainText("Pick a model to see its profile.")
        profiles_layout.addWidget(self.profile_view)

        # ---- Compare tab ----
        compare_tab = QWidget()
        tabs.addTab(compare_tab, "Compare")
        compare_layout = QVBoxLayout(compare_tab)
        compare_layout.addWidget(QLabel(
            "Two models side by side: their latest score on each "
            "template they ran, plus their overall score and speed. "
            "Pick two different models to compare."
        ))
        compare_top = QHBoxLayout()
        self.compare_model_a = QComboBox()
        self.compare_model_a.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.compare_model_a.currentIndexChanged.connect(self._update_compare)
        self.compare_model_b = QComboBox()
        self.compare_model_b.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.compare_model_b.currentIndexChanged.connect(self._update_compare)
        compare_top.addWidget(QLabel("Model 1:"))
        compare_top.addWidget(self.compare_model_a)
        compare_top.addWidget(QLabel("Model 2:"))
        compare_top.addWidget(self.compare_model_b)
        compare_top.addStretch()
        compare_layout.addLayout(compare_top)
        self.compare_message = QLabel("")
        self.compare_message.setWordWrap(True)
        compare_layout.addWidget(self.compare_message)
        self.compare_table = QTableWidget(0, 3)
        self.compare_table.setHorizontalHeaderLabels(
            ["Template", "Model 1", "Model 2"])
        self.compare_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.compare_table.setEditTriggers(QTableWidget.NoEditTriggers)
        compare_header = self.compare_table.horizontalHeader()
        compare_header.setSectionResizeMode(0, QHeaderView.Stretch)
        compare_header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        compare_header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        compare_layout.addWidget(self.compare_table)

        # ---- Providers tab ----
        providers_tab = QWidget()
        tabs.addTab(providers_tab, "Providers")
        providers_layout = QVBoxLayout(providers_tab)
        providers_intro = QLabel(
            "Cloud providers, tested with the same templates as your local "
            "models. Providers and their API keys live in providers.json "
            "next to the app — that file holds your keys, never share it "
            "or upload it anywhere. Provider tests cost real API money; "
            "the details view shows the tokens and the cost."
        )
        providers_intro.setWordWrap(True)
        providers_layout.addWidget(providers_intro)
        provider_columns = QHBoxLayout()

        provider_left = QVBoxLayout()
        provider_left.addWidget(QLabel("Providers:"))
        self.provider_list = QListWidget()
        self.provider_list.itemClicked.connect(self.on_provider_selected)
        provider_left.addWidget(self.provider_list)
        provider_list_buttons = QHBoxLayout()
        self.provider_new_button = QPushButton("New provider")
        self.provider_new_button.clicked.connect(self.new_provider)
        self.provider_remove_button = QPushButton("Remove provider")
        self.provider_remove_button.clicked.connect(self.remove_provider)
        provider_list_buttons.addWidget(self.provider_new_button)
        provider_list_buttons.addWidget(self.provider_remove_button)
        provider_list_buttons.addStretch()
        provider_left.addLayout(provider_list_buttons)
        provider_columns.addLayout(provider_left)

        provider_right = QVBoxLayout()
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Name:"))
        self.provider_name_input = QLineEdit()
        self.provider_name_input.setPlaceholderText("e.g. openai")
        name_row.addWidget(self.provider_name_input)
        provider_right.addLayout(name_row)
        url_row = QHBoxLayout()
        url_row.addWidget(QLabel("Base URL:"))
        self.provider_url_input = QLineEdit()
        self.provider_url_input.setPlaceholderText("e.g. https://api.openai.com/v1")
        url_row.addWidget(self.provider_url_input)
        provider_right.addLayout(url_row)
        key_row = QHBoxLayout()
        key_row.addWidget(QLabel("API key:"))
        self.provider_key_input = QLineEdit()
        self.provider_key_input.setEchoMode(QLineEdit.Password)
        self.provider_key_input.setPlaceholderText("API key")
        key_row.addWidget(self.provider_key_input)
        self.provider_key_status = QLabel("No key saved")
        key_row.addWidget(self.provider_key_status)
        provider_right.addLayout(key_row)
        provider_test_row = QHBoxLayout()
        self.provider_test_button = QPushButton("Test connection")
        self.provider_test_button.clicked.connect(self.test_provider_connection)
        self.provider_test_status = QLabel("")
        self.provider_test_status.setWordWrap(True)
        provider_test_row.addWidget(self.provider_test_button)
        provider_test_row.addWidget(self.provider_test_status)
        provider_test_row.addStretch()
        provider_right.addLayout(provider_test_row)
        provider_right.addWidget(QLabel("Models on this provider:"))
        self.provider_models_list = QListWidget()
        self.provider_models_list.itemClicked.connect(self.on_provider_model_selected)
        provider_right.addWidget(self.provider_models_list)
        model_row = QHBoxLayout()
        model_row.addWidget(QLabel("Model id:"))
        self.provider_model_id_input = QLineEdit()
        self.provider_model_id_input.setPlaceholderText("e.g. gpt-4o-mini")
        model_row.addWidget(self.provider_model_id_input)
        model_row.addWidget(QLabel("Display name:"))
        self.provider_model_name_input = QLineEdit()
        self.provider_model_name_input.setPlaceholderText("optional")
        model_row.addWidget(self.provider_model_name_input)
        provider_right.addLayout(model_row)
        price_row = QHBoxLayout()
        price_row.addWidget(QLabel("Price in per 1M:"))
        self.provider_price_in_input = QLineEdit()
        self.provider_price_in_input.setPlaceholderText("$ optional")
        price_row.addWidget(self.provider_price_in_input)
        price_row.addWidget(QLabel("Price out per 1M:"))
        self.provider_price_out_input = QLineEdit()
        self.provider_price_out_input.setPlaceholderText("$ optional")
        price_row.addWidget(self.provider_price_out_input)
        provider_right.addLayout(price_row)
        caps_label = QLabel(
            "What this model can do (decides which templates it can run):"
        )
        caps_label.setWordWrap(True)
        provider_right.addWidget(caps_label)
        caps_row = QHBoxLayout()
        self.provider_cap_boxes = {}
        for capability in CAPABILITIES:
            box = QCheckBox(CAPABILITY_LABELS[capability])
            if capability == "text":
                box.setChecked(True)
            self.provider_cap_boxes[capability] = box
            caps_row.addWidget(box)
        caps_row.addStretch()
        provider_right.addLayout(caps_row)
        model_buttons = QHBoxLayout()
        self.provider_add_model_button = QPushButton("Add model")
        self.provider_add_model_button.clicked.connect(self.add_provider_model)
        self.provider_update_model_button = QPushButton("Update model")
        self.provider_update_model_button.clicked.connect(self.update_provider_model)
        self.provider_remove_model_button = QPushButton("Remove model")
        self.provider_remove_model_button.clicked.connect(self.remove_provider_model)
        model_buttons.addWidget(self.provider_add_model_button)
        model_buttons.addWidget(self.provider_update_model_button)
        model_buttons.addWidget(self.provider_remove_model_button)
        model_buttons.addStretch()
        provider_right.addLayout(model_buttons)
        provider_save_row = QHBoxLayout()
        self.provider_save_button = QPushButton("Save provider")
        self.provider_save_button.clicked.connect(self.save_provider)
        self.provider_status = QLabel("")
        self.provider_status.setWordWrap(True)
        provider_save_row.addWidget(self.provider_save_button)
        provider_save_row.addWidget(self.provider_status)
        provider_save_row.addStretch()
        provider_right.addLayout(provider_save_row)
        provider_columns.addLayout(provider_right)
        providers_layout.addLayout(provider_columns)
        tabs.currentChanged.connect(self.on_tab_changed)

        self.refresh_templates()
        self.refresh_models()
        self.refresh_history()
        self.refresh_profiles()
        self.refresh_compare()
        self.refresh_providers()
        self._build_menu_bar()
        self._build_shortcuts()

    def closeEvent(self, event):
        # Save the window's size and position so the next launch
        # opens where this one left off.
        settings = QSettings("BrokeNBored", APP_NAME)
        settings.setValue("geometry", self.saveGeometry())
        super().closeEvent(event)

    def _build_menu_bar(self):
        """A minimal menu bar: File (Save report, Quit), View (Dark
        mode), Help (About)."""
        menu_bar = self.menuBar()
        file_menu = menu_bar.addMenu("File")
        self.save_report_action = QAction("Save report", self)
        self.save_report_action.setShortcut(QKeySequence("Ctrl+S"))
        self.save_report_action.triggered.connect(self.save_report)
        self.save_report_action.setEnabled(False)
        file_menu.addAction(self.save_report_action)
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)
        view_menu = menu_bar.addMenu("View")
        self.dark_mode_action = QAction("Dark mode", self)
        self.dark_mode_action.setCheckable(True)
        self.dark_mode_action.setChecked(self.dark_mode)
        self.dark_mode_action.triggered.connect(self._toggle_dark_mode)
        view_menu.addAction(self.dark_mode_action)
        help_menu = menu_bar.addMenu("Help")
        about_action = QAction("About", self)
        about_action.triggered.connect(self.show_about)
        help_menu.addAction(about_action)

    def _toggle_dark_mode(self, checked):
        """The View menu's Dark mode toggle: restyle the whole window
        and remember the choice, so the next launch opens the same way."""
        self.dark_mode = checked
        apply_theme(checked)
        settings = QSettings("BrokeNBored", APP_NAME)
        settings.setValue("dark_mode", checked)

    def _build_shortcuts(self):
        """Keyboard shortcuts. Ctrl+S lives on the File menu action;
        Ctrl+R and Esc are window shortcuts.

        Ctrl+R starts a run only when no run is in progress — during
        a run it is a no-op, same as the disabled Run button. Esc
        cancels a running run and does nothing when no run is running.
        """
        self.run_shortcut = QShortcut(QKeySequence("Ctrl+R"), self)
        self.run_shortcut.activated.connect(self._shortcut_run)
        self.cancel_shortcut = QShortcut(QKeySequence("Escape"), self)
        self.cancel_shortcut.activated.connect(self.cancel_run)

    def _shortcut_run(self):
        if self.worker is None:
            self.start_run()

    def about_text(self):
        """The About box text, built from __version__ (kept separate
        from show_about so it can be checked without opening a dialog)."""
        return (
            f"{APP_NAME} v{__version__}\n\n"
            "Benchmark the Ollama models on this PC, and cloud "
            "providers' models too.\n\n"
            "Nothing is ticked by default. Tick the models and the "
            "test templates you want, then press Run."
        )

    def show_about(self):
        QMessageBox.about(self, f"About {APP_NAME}", self.about_text())

    # -- models and templates --------------------------------------------

    def refresh_templates(self):
        # A refresh keeps whatever the user had ticked; a fresh list
        # starts with nothing ticked (durable rule: never default
        # anything to selected).
        checked = {
            self.template_list.item(row).data(Qt.UserRole)
            for row in range(self.template_list.count())
            if self.template_list.item(row).checkState() == Qt.Checked
        }
        templates, warnings = load_templates()
        self.templates = templates
        self.template_list.clear()
        for template in templates:
            if template["scored"]:
                label = f"{template['name']} ({len(template['tasks'])} tasks)"
            else:
                label = f"{template['name']} (no scoring)"
            capability = template.get("capability", "text")
            if capability != "text":
                label += f" — needs {capability.replace('_', ' ')}"
            item = QListWidgetItem(label)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(
                Qt.Checked if template["id"] in checked else Qt.Unchecked
            )
            item.setData(Qt.UserRole, template["id"])
            self.template_list.addItem(item)
        self.template_warnings = warnings
        self.update_template_availability()
        # A refresh rebuilds the list from scratch, so the filter
        # text the user already typed applies to the new items too.
        self._apply_template_filter()
        return warnings

    def refresh_models(self):
        # Checked state is kept by target label (stored on each item),
        # not by the visible text — the text carries capability brackets.
        checked = set()
        for row in range(self.model_list.count()):
            item = self.model_list.item(row)
            target = item.data(Qt.UserRole)
            if item.checkState() == Qt.Checked and target is not None:
                checked.add(target["label"])
        self.refresh_templates()
        self.model_list.clear()
        ollama_error = None
        ollama_names = []
        api_up = False
        try:
            ollama_names, source = list_models()
            api_up = source == "api"
        except OllamaError as exc:
            ollama_error = exc
        providers, provider_warnings = load_providers()
        self.providers = providers
        self.provider_warnings = provider_warnings
        # Screening: cached capabilities first; models missing from the
        # cache are screened only while the API is answering. Never
        # while Ollama is down or the list came from the CLI — a
        # /api/show call per model against a dead server would stall
        # the window for seconds each.
        cache = load_capabilities()
        if api_up:
            changed = False
            for name in ollama_names:
                if name not in cache:
                    cache[name] = screen_ollama_model(name, cache)
                    changed = True
            if changed:
                save_capabilities(cache)
        targets = [
            ollama_target(name, cache.get(name)) for name in ollama_names
        ]
        for provider in providers:
            for model_entry in provider["models"]:
                targets.append(provider_target(provider, model_entry))
        if not targets:
            if ollama_error is not None:
                self.status_label.setText(str(ollama_error))
            else:
                self.status_label.setText(NO_MODELS_MESSAGE)
            self.run_button.setEnabled(False)
            self.update_template_availability()
            return
        for target in targets:
            item = QListWidgetItem(
                target["label"]
                + format_capabilities(target["capabilities"])
            )
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(
                Qt.Checked if target["label"] in checked else Qt.Unchecked
            )
            item.setData(Qt.UserRole, target)
            self.model_list.addItem(item)
        # A refresh rebuilds the list from scratch, so the filter
        # text the user already typed applies to the new items too.
        self._apply_model_filter()
        if not self.templates:
            self.status_label.setText(NO_TEMPLATES_MESSAGE)
            self.run_button.setEnabled(False)
            self.update_template_availability()
            return
        self.run_button.setEnabled(True)
        if ollama_error is not None:
            # Ollama is down, but provider models do not need it:
            # they can still be tested.
            status = (
                f"Ollama is not running, but {len(targets)} provider "
                f"model(s) are ready to test."
            )
        else:
            status = f"{len(targets)} models found, {len(self.templates)} templates loaded. Ready."
        warnings = list(self.template_warnings) + list(provider_warnings)
        if warnings:
            status += " " + "; ".join(warnings[:2])
            if len(warnings) > 2:
                status += f" (+{len(warnings) - 2} more)"
        self.status_label.setText(status)
        self.update_template_availability()

    def screen_models(self):
        """Re-screen every Ollama model from scratch: drop the cached
        capabilities and refresh the list, which screens any model
        missing from the cache."""
        save_capabilities({})
        self.refresh_models()

    def _apply_list_filter(self, filter_box, list_widget):
        """Hide the items that do not match the filter text. Hidden,
        never removed: removing an item would lose its tick, and the
        grey-out logic iterates every item either way, hidden or not.
        The match is against the item text — model labels carry their
        capability brackets in the text, so filtering "vision" finds
        every vision model."""
        term = filter_box.text().strip().lower()
        for row in range(list_widget.count()):
            item = list_widget.item(row)
            item.setHidden(bool(term) and term not in item.text().lower())

    def _apply_model_filter(self, _text=""):
        self._apply_list_filter(self.model_filter, self.model_list)

    def _apply_template_filter(self, _text=""):
        self._apply_list_filter(self.template_filter, self.template_list)

    def update_template_availability(self):
        """Grey out the templates no ticked model can run.

        A template is enabled when at least one TICKED model is
        eligible for it — or when nothing is ticked yet, in which case
        everything shows enabled. A template that greys out is
        unticked as it goes, so a ticked template is always one some
        ticked model can run. Skipped while a run is in progress: the
        run's template list is already fixed.
        """
        if self.worker is not None:
            return
        ticked = self.selected_targets()
        by_id = {t["id"]: t for t in self.templates}
        self.template_list.blockSignals(True)
        try:
            for row in range(self.template_list.count()):
                item = self.template_list.item(row)
                template = by_id.get(item.data(Qt.UserRole))
                if template is None:
                    continue
                enabled = not ticked or any(
                    template_eligible(template, target) for target in ticked
                )
                if enabled:
                    item.setFlags(
                        item.flags() | Qt.ItemIsEnabled | Qt.ItemIsUserCheckable
                    )
                else:
                    item.setFlags(item.flags() & ~Qt.ItemIsEnabled)
                    item.setCheckState(Qt.Unchecked)
        finally:
            self.template_list.blockSignals(False)

    def _set_all_checked(self, widget, state):
        # Greyed-out templates stay untouched: Select all must never
        # tick a template no ticked model can run.
        for row in range(widget.count()):
            item = widget.item(row)
            if not item.flags() & Qt.ItemIsEnabled:
                continue
            item.setCheckState(state)

    def select_all_models(self):
        self._set_all_checked(self.model_list, Qt.Checked)

    def clear_models(self):
        self._set_all_checked(self.model_list, Qt.Unchecked)

    def select_all_templates(self):
        self._set_all_checked(self.template_list, Qt.Checked)

    def clear_templates(self):
        self._set_all_checked(self.template_list, Qt.Unchecked)

    def selected_targets(self):
        """Ticked models as benchmark targets (Ollama and provider)."""
        targets = []
        for row in range(self.model_list.count()):
            item = self.model_list.item(row)
            if item.checkState() == Qt.Checked:
                target = item.data(Qt.UserRole)
                if target is not None:
                    targets.append(target)
        return targets

    def selected_templates(self):
        by_id = {t["id"]: t for t in self.templates}
        chosen = []
        for row in range(self.template_list.count()):
            item = self.template_list.item(row)
            # A greyed-out template is never selected, even if it was
            # checked programmatically — the run only ever sees
            # templates a ticked model can run.
            if item.checkState() == Qt.Checked and item.flags() & Qt.ItemIsEnabled:
                template = by_id.get(item.data(Qt.UserRole))
                if template is not None:
                    chosen.append(template)
        return chosen

    # -- benchmark run ---------------------------------------------------

    def start_run(self):
        targets = self.selected_targets()
        templates = self.selected_templates()
        if not targets:
            self.status_label.setText("Tick at least one model first.")
            return
        if not templates:
            self.status_label.setText("Tick at least one test template first.")
            return
        # The list may have come from the CLI fallback while the API was
        # down, or Ollama may have stopped since the window opened: check
        # it answers before starting, or every local model fails one by
        # one. Provider-only runs skip this check — they do not need
        # Ollama at all.
        needs_ollama = any(t["kind"] == "ollama" for t in targets)
        if needs_ollama and not ollama_reachable():
            self.status_label.setText(OLLAMA_DOWN_RUN_MESSAGE)
            return
        # The cost guard: a provider run shows its estimated cost
        # first and asks to confirm when it is over the threshold
        # (or when no prices are saved, so the cost is unknown).
        if not self._confirm_provider_cost(targets, templates):
            return
        # Sorting stays OFF while the run builds the table: a live sort
        # moves rows mid-update and later setItem calls land on the wrong
        # model. It goes back on when the run finishes.
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        self.results = {}
        self.result_order = []
        self.detail_view.setPlainText("Run in progress...")
        self.save_button.setEnabled(False)
        self.save_report_action.setEnabled(False)
        self.export_csv_button.setEnabled(False)
        self.export_html_button.setEnabled(False)
        self.run_button.setEnabled(False)
        self.refresh_button.setEnabled(False)
        self.select_all_button.setEnabled(False)
        self.clear_button.setEnabled(False)
        self.select_all_templates_button.setEnabled(False)
        self.clear_templates_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.progress_bar.setValue(0)
        self.worker = BenchWorker(
            targets, templates, self.sys_id, self.machine, get_results_dir(),
            time_budget_seconds=self._time_budget_seconds(),
        )
        self._update_delete_button()
        self.worker.model_started.connect(self.on_model_started)
        self.worker.template_done.connect(self.on_template_done)
        self.worker.model_done.connect(self.on_model_done)
        self.worker.progress.connect(self.on_progress)
        self.worker.run_finished.connect(self.on_run_finished)
        self.worker.start()

    def cancel_run(self):
        if self.worker is not None:
            self.worker.request_cancel()
            self.cancel_button.setEnabled(False)
            self.status_label.setText("Stopping after the current task...")

    def _time_budget_seconds(self):
        """The per-template time budget in seconds, or None when the
        control is 0 (no limit — the default)."""
        minutes = self.budget_spin.value()
        return minutes * 60 if minutes else None

    def _confirm_provider_cost(self, targets, templates):
        """The cost guard: a run with provider models shows its
        estimated cost before it starts. Over the $1.00 threshold —
        or when no prices are saved, so the cost is unknown — it asks
        to confirm, naming the estimate and the models; under the
        threshold it starts with no extra step and the estimate shows
        in the status line instead. Returns False when the user says
        no (or when there is nothing to confirm, True)."""
        provider_targets = [t for t in targets if t["kind"] == "provider"]
        if not provider_targets:
            return True
        estimate = estimate_run_cost(provider_targets, templates)
        lines = ["This run will use provider models, which cost real money:"]
        for entry in estimate["per_model"]:
            if entry["has_prices"]:
                lines.append(
                    f"  {entry['label']}: about {fmt_cost(entry['cost'])} "
                    f"(about {fmt_tokens(entry['prompt_tokens'])} tokens in, "
                    f"{fmt_tokens(entry['completion_tokens'])} out)"
                )
            else:
                lines.append(
                    f"  {entry['label']}: no prices saved — cost unknown"
                )
        if estimate["unknown"]:
            lines.append("")
            lines.append(
                "No prices are saved for "
                + ", ".join(estimate["unknown"])
                + ", so the total cost is unknown."
            )
            needs_confirm = True
        elif estimate["total"] > COST_CONFIRM_THRESHOLD_USD:
            lines.append("")
            lines.append(
                f"Estimated total: about {fmt_cost(estimate['total'])} — "
                f"over the {fmt_cost(COST_CONFIRM_THRESHOLD_USD)} "
                "confirmation threshold."
            )
            needs_confirm = True
        else:
            needs_confirm = False
        if not needs_confirm:
            if estimate["total"] < 0.01:
                cost_text = "less than a cent"
            else:
                cost_text = f"about {fmt_cost(estimate['total'])}"
            self.status_label.setText(
                f"Estimated provider cost: {cost_text} — starting the run."
            )
            return True
        lines.append("")
        lines.append("Run anyway?")
        answer = QMessageBox.question(
            self, "Confirm provider run", "\n".join(lines),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            self.status_label.setText("Run cancelled.")
            return False
        return True

    # -- start Ollama ----------------------------------------------------

    def start_ollama(self):
        if self.starter is not None:
            return
        self.start_ollama_button.setEnabled(False)
        self.status_label.setText("Starting Ollama...")
        self.starter = OllamaStarter()
        self.starter.started_ok.connect(self.on_ollama_started)
        self.starter.failed.connect(self.on_ollama_start_failed)
        self.starter.start()

    def on_ollama_started(self):
        self.starter = None
        self.start_ollama_button.setEnabled(True)
        self.refresh_models()
        self.status_label.setText("Ollama is running.")

    def on_ollama_start_failed(self, message):
        self.starter = None
        self.start_ollama_button.setEnabled(True)
        self.status_label.setText(message)

    # -- pull a model ----------------------------------------------------

    def start_pull(self):
        name = self.pull_input.text().strip()
        if not name:
            self.pull_status.setText("Type a model name first.")
            return
        if self.pull_worker is not None:
            return
        self.pull_button.setEnabled(False)
        self.pull_bar.setRange(0, 100)
        self.pull_bar.setValue(0)
        self.pull_status.setText(f"Pulling {name}...")
        self.pull_worker = PullWorker(name)
        self.pull_worker.progress.connect(self.on_pull_progress)
        self.pull_worker.finished_ok.connect(self.on_pull_done)
        self.pull_worker.failed.connect(self.on_pull_failed)
        self.pull_worker.start()

    def on_pull_progress(self, percent, text):
        if percent < 0:
            self.pull_bar.setRange(0, 0)
        else:
            self.pull_bar.setRange(0, 100)
            self.pull_bar.setValue(percent)
        self.pull_status.setText(text)

    def on_pull_done(self, name):
        self.pull_worker = None
        self.pull_button.setEnabled(True)
        self.pull_bar.setRange(0, 100)
        self.pull_bar.setValue(100)
        self.pull_status.setText(f"Pulled {name}.")
        self.pull_input.clear()
        self.refresh_models()

    def on_pull_failed(self, message):
        self.pull_worker = None
        self.pull_button.setEnabled(True)
        self.pull_bar.setRange(0, 100)
        self.pull_bar.setValue(0)
        self.pull_status.setText(f"Pull failed: {message}")

    def on_progress(self, text, percent):
        self.status_label.setText(text)
        self.progress_bar.setValue(percent)

    def on_model_started(self, name):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(name))
        self.table.setItem(row, 1, NumericItem(None, "—"))
        self.table.setItem(row, 2, NumericItem(None, "—"))
        self.table.setItem(row, 3, QTableWidgetItem("running"))

    def _row_for(self, name):
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None and item.text() == name:
                return row
        return None

    def on_template_done(self, name, run_data, path):
        if name not in self.results:
            self.results[name] = {"name": name, "status": "running", "templates": {}}
        self.results[name]["templates"][run_data["template"]] = run_data

    def on_model_done(self, name, result):
        self.results[name] = result
        self.result_order.append(name)
        row = self._row_for(name)
        if row is None:
            return
        self.table.setItem(
            row, 1, NumericItem(result.get("overall_score"), fmt_score(result.get("overall_score")))
        )
        self.table.setItem(
            row, 2, NumericItem(result.get("avg_decode_tok_s"), fmt_toks(result.get("avg_decode_tok_s")))
        )
        self.table.setItem(row, 3, QTableWidgetItem(result["status"]))

    def on_run_finished(self, reason):
        self.run_button.setEnabled(True)
        self.refresh_button.setEnabled(True)
        self.select_all_button.setEnabled(True)
        self.clear_button.setEnabled(True)
        self.select_all_templates_button.setEnabled(True)
        self.clear_templates_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.save_button.setEnabled(bool(self.results))
        self.save_report_action.setEnabled(bool(self.results))
        self.export_csv_button.setEnabled(bool(self.results))
        self.export_html_button.setEnabled(bool(self.results))
        self.table.setSortingEnabled(True)
        if reason == "done":
            self.status_label.setText("Done. Press Save report to keep the results.")
            self.table.sortItems(1, Qt.DescendingOrder)
            if self.table.rowCount():
                self.table.selectRow(0)
                first = self.table.item(0, 0)
                if first is not None:
                    self.show_detail(first.text())
        elif reason == "ollama_down":
            self.status_label.setText(OLLAMA_DIED_MESSAGE)
            self.table.sortItems(1, Qt.DescendingOrder)
            if self.table.rowCount():
                self.table.selectRow(0)
                first = self.table.item(0, 0)
                if first is not None:
                    self.show_detail(first.text())
        else:
            self.status_label.setText("Cancelled. Results so far are still in the table.")
        self.worker = None
        self.refresh_history()
        self.refresh_profiles_for_run()
        self.refresh_compare()
        self._update_delete_button()

    def refresh_profiles_for_run(self):
        """Rebuild the profiles of every model this run touched."""
        results_dir = get_results_dir()
        for name in self.result_order:
            write_profile(results_dir, slugify_model(name))
        self.refresh_profiles()

    def on_table_clicked(self, row, _column):
        item = self.table.item(row, 0)
        if item is not None:
            self.show_detail(item.text())

    def show_detail(self, name):
        result = self.results.get(name)
        if result is None:
            return
        lines = [f"{name} — overall {fmt_score(result.get('overall_score'))}, "
                 f"avg {fmt_toks(result.get('avg_decode_tok_s'))} tok/s"]
        if result["status"] != "done":
            lines.append(result["status"])
            self.detail_view.setPlainText("\n".join(lines))
            return
        if result.get("total_tokens") is not None:
            lines.append(
                f"Tokens: {fmt_tokens(result.get('prompt_tokens'))} in, "
                f"{fmt_tokens(result.get('completion_tokens'))} out "
                f"(includes warmup)"
            )
            if result.get("estimated_cost_usd") is not None:
                lines.append(
                    f"Estimated cost: {fmt_cost(result.get('estimated_cost_usd'))}"
                )
        for template_id, run in result.get("templates", {}).items():
            if run.get("skipped"):
                lines.append(
                    f"\n{run.get('template_name', template_id)}: "
                    f"not eligible for this model"
                )
                continue
            score = run.get("template_score")
            not_run = run.get("not_run", 0) or 0
            budget_stopped = run.get("budget_stopped", 0) or 0
            if score is None and not_run:
                header = (
                    f"\n{run.get('template_name', template_id)}: not run "
                    f"({not_run} {'task' if not_run == 1 else 'tasks'} "
                    "not run — test file missing)"
                )
            elif score is None and budget_stopped:
                header = (
                    f"\n{run.get('template_name', template_id)}: "
                    f"time budget reached "
                    f"({budget_stopped} "
                    f"{'task' if budget_stopped == 1 else 'tasks'} stopped "
                    "before they ran)"
                )
            elif score is None:
                header = (
                    f"\n{run.get('template_name', template_id)}: no scoring, "
                    f"{fmt_toks(run.get('avg_decode_tok_s'))} tok/s"
                )
            else:
                header = (
                    f"\n{run.get('template_name', template_id)}: {score:.0f}% "
                    f"({run.get('passed')}/{run.get('total')}"
                    f"{_not_run_suffix(not_run)}), "
                    f"{fmt_toks(run.get('avg_decode_tok_s'))} tok/s"
                )
                if budget_stopped:
                    header += (
                        f" — time budget reached "
                        f"({budget_stopped} "
                        f"{'task' if budget_stopped == 1 else 'tasks'} stopped)"
                    )
            if run.get("total_tokens") is not None:
                header += (
                    f", {fmt_tokens(run.get('prompt_tokens'))} in / "
                    f"{fmt_tokens(run.get('completion_tokens'))} out"
                )
                if run.get("estimated_cost_usd") is not None:
                    header += f", {fmt_cost(run.get('estimated_cost_usd'))}"
            lines.append(header)
            for task in run.get("tasks", []):
                passed = task.get("passed")
                if passed is True:
                    mark = "pass"
                elif passed is False:
                    mark = "fail"
                elif is_not_run(task):
                    mark = "not run"
                elif is_budget_stopped(task):
                    mark = "time budget reached"
                else:
                    mark = "run"
                note = ""
                if task.get("image_file"):
                    note = " (image saved)"
                elif task.get("audio_file_saved"):
                    note = " (audio saved)"
                lines.append(
                    f"  {mark}  {task.get('id', '?')} "
                    f"({fmt_toks(task.get('decode_tok_s'))} tok/s){note}"
                )
                detail = task.get("detail") or ""
                if (passed is False or is_not_run(task)
                        or is_budget_stopped(task)) and detail:
                    lines.append(f"       {detail}")
                for turn in task.get("turns", []):
                    turn_passed = turn.get("passed")
                    turn_mark = (
                        "pass" if turn_passed is True
                        else ("fail" if turn_passed is False else "run")
                    )
                    lines.append(
                        f"       {turn_mark}  {turn.get('prompt', '?')}"
                    )
                    if turn_passed is False and turn.get("detail"):
                        lines.append(f"           {turn['detail']}")
        self.detail_view.setPlainText("\n".join(lines))

    # -- history ---------------------------------------------------------

    def on_tab_changed(self, index):
        if self.tabs.tabText(index) == "Past runs":
            self.refresh_history()
        elif self.tabs.tabText(index) == "Profiles":
            self.refresh_profiles()
        elif self.tabs.tabText(index) == "Compare":
            self.refresh_compare()
        elif self.tabs.tabText(index) == "Providers":
            # Pick up outside edits to providers.json (a hand edit, or
            # another copy of the app), but only when the file actually
            # changed — otherwise an edit in progress would be wiped.
            if self._providers_file_bytes() != self._providers_snapshot:
                self.refresh_providers()

    def refresh_history(self):
        results_dir = get_results_dir()
        current = None
        if self.history_model_combo.count():
            current = self.history_model_combo.currentData()
        self.history_model_combo.blockSignals(True)
        self.history_model_combo.clear()
        models = list_history_models(results_dir)
        for slug in models:
            runs = list_history_runs(results_dir, slug)
            name = slug
            if runs:
                data = load_run_file(runs[0]["path"])
                if data and data.get("model"):
                    name = data["model"]
            self.history_model_combo.addItem(name, slug)
        self.history_model_combo.blockSignals(False)
        if current in models:
            self.history_model_combo.setCurrentIndex(models.index(current))
        elif models:
            self.history_model_combo.setCurrentIndex(0)
        self.on_history_model_changed()
        self._update_store_size()
        self._update_delete_button()

    def _update_store_size(self):
        """The size of the results store, on the Past runs tab."""
        self.store_size_label.setText(
            f"Results use {fmt_store_size(store_size(get_results_dir()))}"
        )

    def _update_delete_button(self, _current=None, _previous=None):
        """Delete is enabled only when a run is selected and no run
        is in progress — a run's files are never deleted under it."""
        selected = self.history_list.currentItem() is not None
        self.delete_run_button.setEnabled(
            selected and self.worker is None
        )

    def delete_selected_run(self):
        """Delete the selected run: its file and its media folder go,
        then the model's profile rebuilds from the runs that are
        left. Asks to confirm first — deletion cannot be undone."""
        item = self.history_list.currentItem()
        if item is None:
            return
        if self.worker is not None:
            return
        path = item.data(Qt.UserRole)
        slug = self.history_model_combo.currentData()
        answer = QMessageBox.question(
            self, "Delete this run?",
            "Delete this run? Its file and any images or audio it saved "
            "will be removed, and the model's profile will be rebuilt "
            "from the runs that are left. This cannot be undone.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        delete_run_file(path)
        if slug:
            write_profile(get_results_dir(), slug)
        self.refresh_history()
        self.refresh_profiles()
        self.history_view.setPlainText("Run deleted.")
        self.status_label.setText("Run deleted.")

    def on_history_model_changed(self, _index=None):
        slug = self.history_model_combo.currentData()
        self.history_list.clear()
        self.history_view.setPlainText("Pick a run to see its scores.")
        if not slug:
            return
        runs = list_history_runs(get_results_dir(), slug)
        for run in runs:
            score = run["score"]
            not_run = run.get("not_run", 0) or 0
            budget_stopped = run.get("budget_stopped", 0) or 0
            if score is None and not_run:
                score_text = "not run"
            elif score is None and budget_stopped:
                score_text = "time budget reached"
            elif score is None:
                score_text = "no scoring"
            else:
                score_text = (
                    f"{score:.0f}% ({run.get('passed')}/{run.get('total')}"
                    f"{_not_run_suffix(not_run)})"
                )
                if budget_stopped:
                    score_text += ", time budget reached"
            stamp = (run["timestamp"] or "").replace("T", " ")
            item = QListWidgetItem(
                f"{run['system_id']} — {stamp} — {run['template_name']} ({score_text})"
            )
            item.setData(Qt.UserRole, run["path"])
            self.history_list.addItem(item)

    def on_history_run_clicked(self, item):
        path = item.data(Qt.UserRole)
        data = load_run_file(path)
        if data is None:
            self.history_view.setPlainText("That run file could not be read.")
            return
        self.history_view.setPlainText(render_run_summary(data))

    # -- profiles --------------------------------------------------------

    def refresh_profiles(self):
        results_dir = get_results_dir()
        current = None
        if self.profile_model_combo.count():
            current = self.profile_model_combo.currentData()
        self.profile_model_combo.blockSignals(True)
        self.profile_model_combo.clear()
        models = list_history_models(results_dir)
        for slug in models:
            runs = list_history_runs(results_dir, slug)
            name = slug
            if runs:
                data = load_run_file(runs[0]["path"])
                if data and data.get("model"):
                    name = data["model"]
            self.profile_model_combo.addItem(name, slug)
        self.profile_model_combo.blockSignals(False)
        if current in models:
            self.profile_model_combo.setCurrentIndex(models.index(current))
        elif models:
            self.profile_model_combo.setCurrentIndex(0)
        self.on_profile_model_changed()

    def on_profile_model_changed(self, _index=None):
        slug = self.profile_model_combo.currentData()
        if not slug:
            self.profile_view.setPlainText("No profiles yet. Run a test first.")
            return
        path = os.path.join(get_results_dir(), slug, "PROFILE.md")
        try:
            with open(path, encoding="utf-8") as handle:
                self.profile_view.setPlainText(handle.read())
        except OSError:
            self.profile_view.setPlainText(
                "No profile yet for this model. Press Regenerate to build one."
            )

    def regenerate_profile(self):
        slug = self.profile_model_combo.currentData()
        if not slug:
            return
        path = write_profile(get_results_dir(), slug)
        if path is None:
            self.profile_view.setPlainText("No runs yet for this model.")
            return
        self.on_profile_model_changed()

    # -- compare ---------------------------------------------------------

    def refresh_compare(self):
        """Rebuild the Compare tab's model dropdowns from the models
        that have runs, keeping the current picks when they still
        exist. Both dropdowns start unpicked — nothing is selected
        by default."""
        results_dir = get_results_dir()
        current_a = self.compare_model_a.currentData()
        current_b = self.compare_model_b.currentData()
        models = list_history_models(results_dir)
        names = {}
        for slug in models:
            runs = list_history_runs(results_dir, slug)
            name = slug
            if runs:
                data = load_run_file(runs[0]["path"])
                if data and data.get("model"):
                    name = data["model"]
            names[slug] = name
        for combo, current in ((self.compare_model_a, current_a),
                               (self.compare_model_b, current_b)):
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("— pick a model —", None)
            for slug in models:
                combo.addItem(names[slug], slug)
            combo.blockSignals(False)
            if current in models:
                combo.setCurrentIndex(models.index(current) + 1)
        self._update_compare()

    def _update_compare(self, _index=None):
        """Fill the compare table from the two picked models, or show
        a plain message when there is nothing to compare yet. Picking
        the same model twice shows a message, never a duplicated
        table."""
        slug_a = self.compare_model_a.currentData()
        slug_b = self.compare_model_b.currentData()
        self.compare_table.setRowCount(0)
        if not slug_a or not slug_b:
            if list_history_models(get_results_dir()):
                self.compare_message.setText("Pick two models to compare.")
            else:
                self.compare_message.setText(
                    "No models have run yet. Run a test first."
                )
            return
        if slug_a == slug_b:
            self.compare_message.setText(
                "Pick two different models to compare."
            )
            return
        compared = compare_models(get_results_dir(), slug_a, slug_b)
        if compared is None:
            self.compare_message.setText(
                "One of these models has no runs yet."
            )
            return
        self.compare_message.setText("")
        self.compare_table.setHorizontalHeaderLabels(
            ["Template", compared["name_a"], compared["name_b"]]
        )
        rows = compared["rows"]
        self.compare_table.setRowCount(len(rows) + 2)
        for row_index, row in enumerate(rows):
            self.compare_table.setItem(
                row_index, 0, QTableWidgetItem(row["name"])
            )
            for col, key, not_run_key, missing_key in (
                (1, "score_a", "not_run_a", "missing_a"),
                (2, "score_b", "not_run_b", "missing_b"),
            ):
                if row[missing_key]:
                    text = "—"
                    value = None
                elif row[not_run_key]:
                    text = "not run"
                    value = None
                else:
                    text = fmt_score(row[key])
                    value = row[key]
                self.compare_table.setItem(
                    row_index, col, NumericItem(value, text)
                )
        overall_row = len(rows)
        self.compare_table.setItem(
            overall_row, 0, QTableWidgetItem("Overall score")
        )
        self.compare_table.setItem(
            overall_row, 1,
            NumericItem(compared["overall_a"],
                        fmt_score(compared["overall_a"])),
        )
        self.compare_table.setItem(
            overall_row, 2,
            NumericItem(compared["overall_b"],
                        fmt_score(compared["overall_b"])),
        )
        speed_row = len(rows) + 1
        self.compare_table.setItem(
            speed_row, 0, QTableWidgetItem("Avg decode tok/s")
        )
        self.compare_table.setItem(
            speed_row, 1,
            NumericItem(compared["speed_a"],
                        fmt_toks(compared["speed_a"])),
        )
        self.compare_table.setItem(
            speed_row, 2,
            NumericItem(compared["speed_b"],
                        fmt_toks(compared["speed_b"])),
        )

    # -- providers -------------------------------------------------------

    def _providers_file_bytes(self):
        """Raw bytes of providers.json, or None when it cannot be read."""
        try:
            with open(get_providers_file(), "rb") as handle:
                return handle.read()
        except OSError:
            return None

    def refresh_providers(self):
        """Reload providers.json and rebuild the Providers tab list.

        Keeps the provider currently being edited selected when it
        still exists; otherwise shows the first provider, or a blank
        form when there are no providers yet.
        """
        providers, _warnings = load_providers()
        self.providers = providers
        self._providers_snapshot = self._providers_file_bytes()
        current_name = None
        if self.editing_provider is not None:
            current_name = self.editing_provider.get("name")
        self.provider_list.clear()
        for provider in providers:
            item = QListWidgetItem(provider["name"])
            item.setData(Qt.UserRole, provider["name"])
            self.provider_list.addItem(item)
        keep = None
        for provider in providers:
            if provider["name"] == current_name:
                keep = provider
                break
        if keep is not None:
            self._load_provider_into_form(keep, is_new=False)
            for row in range(self.provider_list.count()):
                if self.provider_list.item(row).text() == keep["name"]:
                    self.provider_list.setCurrentRow(row)
                    break
        elif providers:
            self._load_provider_into_form(providers[0], is_new=False)
            self.provider_list.setCurrentRow(0)
        else:
            self._load_provider_into_form(
                {"name": "", "base_url": "", "api_key": "", "models": []},
                is_new=True,
            )

    def _load_provider_into_form(self, provider, is_new):
        """Fill the edit form from a provider dict.

        The key field is always left EMPTY: the stored key is kept in
        editing_kept_key and reused on save unless he types a new one.
        The key is never shown on screen.
        """
        self.editing_provider = {
            "name": provider.get("name", ""),
            "base_url": provider.get("base_url", ""),
            "api_key": provider.get("api_key", ""),
            "models": [dict(m) for m in provider.get("models", [])],
        }
        self.editing_is_new = is_new
        self.editing_original_name = provider.get("name", "")
        self.editing_kept_key = provider.get("api_key", "")
        self.provider_name_input.setText(self.editing_provider["name"])
        self.provider_url_input.setText(self.editing_provider["base_url"])
        self.provider_key_input.setText("")
        if self.editing_kept_key:
            self.provider_key_status.setText("Key saved")
            self.provider_key_input.setPlaceholderText(
                "Leave empty to keep the saved key"
            )
        else:
            self.provider_key_status.setText("No key saved")
            self.provider_key_input.setPlaceholderText(
                "API key (leave empty if the server needs none)"
            )
        self.provider_model_id_input.setText("")
        self.provider_model_name_input.setText("")
        self.provider_price_in_input.setText("")
        self.provider_price_out_input.setText("")
        self.editing_model_index = None
        self._set_caps_form(["text"])
        self._refresh_provider_models_list()

    def _caps_from_form(self):
        """Capabilities from the checkboxes (Text on its own when
        nothing is ticked — a model with no capability at all would
        run nothing)."""
        caps = [
            capability for capability in CAPABILITIES
            if self.provider_cap_boxes[capability].isChecked()
        ]
        return caps or ["text"]

    def _set_caps_form(self, capabilities):
        wanted = set(capabilities or ["text"])
        for capability, box in self.provider_cap_boxes.items():
            box.setChecked(capability in wanted)

    def _refresh_provider_models_list(self):
        self.provider_models_list.clear()
        if self.editing_provider is None:
            return
        for index, model in enumerate(self.editing_provider["models"]):
            text = model["id"]
            if model.get("display_name"):
                text += f" — {model['display_name']}"
            text += format_capabilities(model.get("capabilities") or ["text"])
            price_in = model.get("price_input_per_1m")
            price_out = model.get("price_output_per_1m")
            if price_in is not None or price_out is not None:
                in_text = f"${price_in}" if price_in is not None else "?"
                out_text = f"${price_out}" if price_out is not None else "?"
                text += f" ({in_text} in / {out_text} out per 1M)"
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, index)
            self.provider_models_list.addItem(item)

    def on_provider_model_selected(self, item):
        """Clicking a model loads it into the form for editing (its
        capabilities included); Update model writes the form back."""
        if self.editing_provider is None:
            return
        index = item.data(Qt.UserRole)
        models = self.editing_provider["models"]
        if not (0 <= index < len(models)):
            return
        model = models[index]
        self.editing_model_index = index
        self.provider_model_id_input.setText(model.get("id", ""))
        self.provider_model_name_input.setText(model.get("display_name", ""))
        price_in = model.get("price_input_per_1m")
        price_out = model.get("price_output_per_1m")
        self.provider_price_in_input.setText(
            "" if price_in is None else str(price_in)
        )
        self.provider_price_out_input.setText(
            "" if price_out is None else str(price_out)
        )
        self._set_caps_form(model.get("capabilities") or ["text"])
        self.provider_status.setText(
            f"Editing {model['id']}. Press Update model, then Save provider."
        )

    def _provider_from_form(self):
        """Provider dict from the form fields, keeping the stored key
        when the key field was left empty."""
        key = self.provider_key_input.text()
        if not key:
            key = self.editing_kept_key
        models = []
        if self.editing_provider is not None:
            models = [dict(m) for m in self.editing_provider["models"]]
        return {
            "name": self.provider_name_input.text().strip(),
            "base_url": self.provider_url_input.text().strip().rstrip("/"),
            "api_key": key,
            "models": models,
        }

    def on_provider_selected(self, item):
        name = item.data(Qt.UserRole)
        for provider in self.providers:
            if provider["name"] == name:
                self._load_provider_into_form(provider, is_new=False)
                return

    def new_provider(self):
        self.provider_list.clearSelection()
        self._load_provider_into_form(
            {"name": "", "base_url": "", "api_key": "", "models": []},
            is_new=True,
        )
        self.provider_status.setText("New provider. Fill in the details and press Save provider.")

    def remove_provider(self):
        item = self.provider_list.currentItem()
        if item is None:
            self.provider_status.setText("Pick a provider to remove first.")
            return
        name = item.data(Qt.UserRole)
        providers, _warnings = load_providers()
        providers = [p for p in providers if p["name"] != name]
        save_providers(providers)
        self.provider_status.setText(f"Removed {name}.")
        self.refresh_providers()
        self.refresh_models()

    def save_provider(self):
        if self.editing_provider is None:
            return
        provider = self._provider_from_form()
        if not provider["name"]:
            self.provider_status.setText("Give the provider a name first.")
            return
        if not provider["base_url"]:
            self.provider_status.setText("Give the provider a base URL first.")
            return
        if not provider["base_url"].startswith(("http://", "https://")):
            self.provider_status.setText("The base URL must start with http:// or https://")
            return
        providers, _warnings = load_providers()
        if self.editing_is_new:
            if any(p["name"] == provider["name"] for p in providers):
                self.provider_status.setText(
                    f"A provider named {provider['name']} already exists."
                )
                return
        else:
            providers = [
                p for p in providers
                if p["name"] != self.editing_original_name
            ]
            if any(p["name"] == provider["name"] for p in providers):
                self.provider_status.setText(
                    f"A provider named {provider['name']} already exists."
                )
                return
        providers.append(provider)
        providers.sort(key=lambda p: p["name"])
        save_providers(providers)
        self.provider_status.setText(f"Saved {provider['name']}.")
        self.refresh_providers()
        self.refresh_models()

    def test_provider_connection(self):
        if self.provider_test_worker is not None:
            return
        provider = self._provider_from_form()
        if not provider["base_url"]:
            self.provider_test_status.setText("Type a base URL first.")
            return
        if not provider["name"]:
            provider["name"] = "provider"
        self.provider_test_button.setEnabled(False)
        self.provider_test_status.setText("Testing...")
        self.provider_test_worker = ProviderTestWorker(provider)
        self.provider_test_worker.tested.connect(self.on_provider_tested)
        self.provider_test_worker.start()

    def on_provider_tested(self, ok, message):
        self.provider_test_worker = None
        self.provider_test_button.setEnabled(True)
        self.provider_test_status.setText(message)

    def add_provider_model(self):
        if self.editing_provider is None:
            self.provider_status.setText("Pick a provider first.")
            return
        model_id = self.provider_model_id_input.text().strip()
        if not model_id:
            self.provider_status.setText("Type a model id first.")
            return
        if any(m["id"] == model_id for m in self.editing_provider["models"]):
            self.provider_status.setText("That model is already on this provider.")
            return
        price_in = self._parse_price(self.provider_price_in_input.text())
        price_out = self._parse_price(self.provider_price_out_input.text())
        if price_in is False or price_out is False:
            self.provider_status.setText("Price must be a number, e.g. 0.15")
            return
        self.editing_provider["models"].append({
            "id": model_id,
            "display_name": self.provider_model_name_input.text().strip(),
            "price_input_per_1m": price_in,
            "price_output_per_1m": price_out,
            "capabilities": self._caps_from_form(),
        })
        self.provider_model_id_input.setText("")
        self.provider_model_name_input.setText("")
        self.provider_price_in_input.setText("")
        self.provider_price_out_input.setText("")
        self.editing_model_index = None
        self._set_caps_form(["text"])
        self._refresh_provider_models_list()
        self.provider_status.setText(
            f"Added {model_id}. Press Save provider to keep it."
        )

    def update_provider_model(self):
        """Write the model form back onto the model picked in the list
        (click a model first). The id may change, but not onto another
        model already on this provider."""
        if self.editing_provider is None:
            self.provider_status.setText("Pick a provider first.")
            return
        index = self.editing_model_index
        models = self.editing_provider["models"]
        if index is None or not (0 <= index < len(models)):
            self.provider_status.setText("Click a model in the list first.")
            return
        model_id = self.provider_model_id_input.text().strip()
        if not model_id:
            self.provider_status.setText("Type a model id first.")
            return
        if any(
            m["id"] == model_id for i, m in enumerate(models) if i != index
        ):
            self.provider_status.setText("That model is already on this provider.")
            return
        price_in = self._parse_price(self.provider_price_in_input.text())
        price_out = self._parse_price(self.provider_price_out_input.text())
        if price_in is False or price_out is False:
            self.provider_status.setText("Price must be a number, e.g. 0.15")
            return
        models[index] = {
            "id": model_id,
            "display_name": self.provider_model_name_input.text().strip(),
            "price_input_per_1m": price_in,
            "price_output_per_1m": price_out,
            "capabilities": self._caps_from_form(),
        }
        self.provider_model_id_input.setText("")
        self.provider_model_name_input.setText("")
        self.provider_price_in_input.setText("")
        self.provider_price_out_input.setText("")
        self.editing_model_index = None
        self._set_caps_form(["text"])
        self._refresh_provider_models_list()
        self.provider_status.setText(
            f"Updated {model_id}. Press Save provider to keep it."
        )

    def _parse_price(self, text):
        """Price text to float, None when empty, False when not a number."""
        text = (text or "").strip().lstrip("$").strip()
        if not text:
            return None
        try:
            value = float(text)
        except ValueError:
            return False
        if value < 0:
            return False
        return value

    def remove_provider_model(self):
        if self.editing_provider is None:
            return
        item = self.provider_models_list.currentItem()
        if item is None:
            self.provider_status.setText("Pick a model to remove first.")
            return
        index = item.data(Qt.UserRole)
        models = self.editing_provider["models"]
        if 0 <= index < len(models):
            removed = models.pop(index)
            self.editing_model_index = None
            self._refresh_provider_models_list()
            self.provider_status.setText(
                f"Removed {removed['id']}. Press Save provider to keep it."
            )

    # -- report ----------------------------------------------------------

    def save_report(self):
        if not self.results:
            return
        folder = get_results_dir()
        os.makedirs(folder, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d-%H%M")
        txt_path = os.path.join(folder, f"bench-results-{stamp}.txt")
        json_path = os.path.join(folder, f"bench-results-{stamp}.json")
        results = [self.results[name] for name in self.result_order]
        with open(txt_path, "w", encoding="utf-8") as handle:
            handle.write(render_text_report(results))
        with open(json_path, "w", encoding="utf-8") as handle:
            json.dump(build_report_data(results), handle, indent=2)
        self.status_label.setText(f"Report saved: {txt_path}")

    def export_csv(self):
        """Export the current results to a CSV file, via a file
        dialog. Plain message when there is nothing to export."""
        if not self.results:
            self.status_label.setText(
                "Nothing to export yet — run a test first."
            )
            return
        folder = get_results_dir()
        os.makedirs(folder, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d-%H%M")
        default = os.path.join(folder, f"bench-results-{stamp}.csv")
        path, _selected = QFileDialog.getSaveFileName(
            self, "Export results as CSV", default, "CSV files (*.csv)"
        )
        if not path:
            return
        results = [self.results[name] for name in self.result_order]
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(render_export_csv(results))
        self.status_label.setText(f"Exported: {path}")

    def export_html(self):
        """Export the current results to an HTML file, via a file
        dialog. Plain message when there is nothing to export."""
        if not self.results:
            self.status_label.setText(
                "Nothing to export yet — run a test first."
            )
            return
        folder = get_results_dir()
        os.makedirs(folder, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d-%H%M")
        default = os.path.join(folder, f"bench-results-{stamp}.html")
        path, _selected = QFileDialog.getSaveFileName(
            self, "Export results as HTML", default, "HTML files (*.html)"
        )
        if not path:
            return
        results = [self.results[name] for name in self.result_order]
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(render_export_html(results))
        self.status_label.setText(f"Exported: {path}")


# ---------------------------------------------------------------------------
# Headless mode: the same worker with no window, for scripts and cron
# ---------------------------------------------------------------------------

def _split_names(value):
    """A comma-separated flag value into a list of names."""
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def resolve_headless_targets(names):
    """Turn --models names into benchmark targets.

    A name matching a saved provider model ("provider / model-id") is
    a provider target; anything else is a local Ollama model, screened
    for its capabilities the same way the window does. Returns
    (targets, error): error is a plain message when a name matches
    nothing or Ollama is down, else None.
    """
    providers, _warnings = load_providers()
    provider_targets = {}
    for provider in providers:
        for model_entry in provider.get("models", []):
            label = provider_label(provider["name"], model_entry["id"])
            provider_targets[label] = provider_target(provider, model_entry)
    targets = {}
    ollama_names = []
    for name in names:
        if name in provider_targets:
            targets[name] = provider_targets[name]
            continue
        ollama_names.append(name)
    if ollama_names:
        # Provider-only runs skip this check — they do not need
        # Ollama at all, same as the window.
        if not ollama_reachable():
            return None, OLLAMA_DOWN_RUN_MESSAGE
        available, _source = list_models()
        caps_cache = load_capabilities()
        screened = False
        for name in ollama_names:
            if name not in available:
                known = sorted(set(available)) + sorted(provider_targets)
                if known:
                    detail = "Models I know about: " + ", ".join(known)
                else:
                    detail = ("No models found — is Ollama running, and "
                              "are any providers saved?")
                return None, f"Model not found: {name}.\n{detail}"
            caps = screen_ollama_model(name, caps_cache)
            screened = True
            targets[name] = ollama_target(name, caps)
        if screened:
            save_capabilities(caps_cache)
    # Back in the order the user named them.
    return [targets[name] for name in names], None


def run_headless(args):
    """Run a benchmark with no window. Returns the exit code: 0 when
    the run completed, 1 when it did not (bad names, Ollama down, or
    the run was stopped). Prints a plain summary to stdout.

    No QApplication is created here — only a QCoreApplication, which
    needs no window and no display, to run the worker's event loop.
    """
    model_names = _split_names(args.models)
    template_ids = _split_names(args.templates)
    if not model_names:
        print("Headless mode needs --models: the models to test, "
              "e.g. --models qwen3:0.6b")
        return 1
    if not template_ids:
        print("Headless mode needs --templates: the test templates "
              "to run, e.g. --templates coding,math")
        return 1
    templates, warnings = load_templates()
    for warning in warnings:
        print(f"Warning: {warning}")
    by_id = {t["id"]: t for t in templates}
    chosen = []
    for template_id in template_ids:
        template = by_id.get(template_id)
        if template is None:
            print(f"Template not found: {template_id}.")
            print("Templates I know about: " + ", ".join(sorted(by_id)))
            return 1
        chosen.append(template)
    targets, error = resolve_headless_targets(model_names)
    if error is not None:
        print(error)
        return 1
    results_dir = get_results_dir()
    machine = machine_info()
    sys_id = system_id(machine)
    results = {}
    order = []
    run_files = []

    core_app = QCoreApplication(sys.argv)
    # A no-op timer keeps the event loop waking up, so Ctrl+C reaches
    # Python instead of waiting behind Qt.
    keepalive = QTimer()
    keepalive.setInterval(500)
    keepalive.timeout.connect(lambda: None)
    keepalive.start()

    worker = BenchWorker(targets, chosen, sys_id, machine, results_dir)

    def on_model_started(name):
        print(f"Testing {name}...")

    def on_template_done(name, run_data, path):
        run_files.append(path)
        score = run_data.get("template_score")
        template_name = run_data.get("template_name", name)
        if score is None and run_data.get("not_run"):
            print(f"  {template_name}: not run (test file missing)")
        elif score is None:
            print(f"  {template_name}: no score")
        else:
            print(f"  {template_name}: {score:.0f}%")

    def on_model_done(name, result):
        results[name] = result
        order.append(name)

    state = {"reason": None}

    def on_run_finished(reason):
        state["reason"] = reason
        core_app.quit()

    worker.model_started.connect(on_model_started)
    worker.template_done.connect(on_template_done)
    worker.model_done.connect(on_model_done)
    worker.run_finished.connect(on_run_finished)

    def on_interrupt(_signum, _frame):
        # Ctrl+C stops after the current task, like the Cancel button.
        worker.request_cancel()

    signal.signal(signal.SIGINT, on_interrupt)
    worker.start()
    core_app.exec()
    worker.wait()

    reason = state["reason"]
    # Profiles rebuild after a run, same as the window does.
    for name in order:
        write_profile(results_dir, slugify_model(name))
    print()
    if reason == "ollama_down":
        print(OLLAMA_DIED_MESSAGE)
        return 1
    if reason != "done":
        print("Run stopped before it finished.")
        return 1
    for name in order:
        result = results[name]
        if result["status"] != "done":
            print(f"{name}: {result['status']}")
            continue
        print(f"{name}: {fmt_score(result.get('overall_score'))} overall, "
              f"{fmt_toks(result.get('avg_decode_tok_s'))} tok/s")
    print(f"Run files saved under {results_dir}")
    return 0


def main():
    parser = argparse.ArgumentParser(
        prog="ollama_system_test.py",
        description=(
            "Benchmark the Ollama models on this PC, and cloud "
            "providers' models too. With no flags it opens the window; "
            "with --headless it runs a benchmark with no window, for "
            "scripts and cron."
        ),
    )
    parser.add_argument(
        "--headless", action="store_true",
        help="run with no window: the same benchmark, with a plain "
        "summary printed to stdout",
    )
    parser.add_argument(
        "--models",
        help="comma-separated models to test in headless mode, "
        "e.g. qwen3:0.6b — a provider model is written "
        "\"provider / model-id\"",
    )
    parser.add_argument(
        "--templates",
        help="comma-separated template ids to run in headless mode, "
        "e.g. coding,math",
    )
    args = parser.parse_args()
    # The media folders exist from the first launch, window or not.
    ensure_media_dirs()
    if args.headless:
        sys.exit(run_headless(args))
    if args.models or args.templates:
        print("--models and --templates only apply with --headless; "
              "opening the window.", file=sys.stderr)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    # The saved theme applies before the window is built, so a
    # dark-mode user never sees a light flash.
    settings = QSettings("BrokeNBored", APP_NAME)
    apply_theme(settings.value("dark_mode", False, type=bool))
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
