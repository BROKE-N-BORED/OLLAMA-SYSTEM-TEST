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

v17 stops making the user start Ollama: the app starts it itself
whenever it needs it (opening, running, pulling), restarts it once
mid-run if it dies loading a model, and checks free RAM before each
model so a model too big for the machine is skipped instead of
killing the run. The Speed template gets a real score at last:
decode speed graded against 20 tokens per second, per context length.

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
import traceback
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timedelta

try:
    from PySide6.QtCore import (
        QCoreApplication, QPropertyAnimation, QRect, QSettings, QSize, Qt,
        QThread, QTimer, Signal,
    )
    from PySide6.QtGui import (
        QAction, QFont, QIcon, QKeySequence, QPainter, QPalette, QPen,
        QShortcut,
    )
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    from PySide6.QtWidgets import (
        QApplication,
        QCheckBox,
        QComboBox,
        QDialog,
        QDialogButtonBox,
        QFileDialog,
        QFrame,
        QGraphicsOpacityEffect,
        QGridLayout,
        QGroupBox,
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
        QScrollArea,
        QSlider,
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
    print("Run install.sh first. It installs PySide6 for you.", file=sys.stderr)
    sys.exit(1)

APP_NAME = "OLLAMA SYSTEM TEST"
# The one source of truth for the version: the title bar, the About
# box, and anything else that names the version reads this.
__version__ = "18"
APP_DIR = os.path.dirname(os.path.abspath(__file__))
WARMUP_PROMPT = "Say hello in one sentence."
WARMUP_PREDICT = 64
# Try-it answers (R24): one question, one short answer — long enough
# to judge, short enough to stay quick.
TRYIT_PREDICT = 200
TEMPERATURE = 0
READ_TIMEOUT = 10.0       # seconds between streamed chunks before retrying
STALL_TIMEOUT = 120.0     # seconds with no chunk at all before giving up
FIRST_REPLY_TIMEOUT = 300.0  # seconds allowed for the first token
# (v18): a cold model load from disk plus prompt work on a CPU-only
# box can take minutes; fast models answer at once, so this is a
# ceiling, not a delay. The old 10-second window killed every
# benchmark on a slow machine and blamed Ollama for it.
CODE_TIMEOUT = 10.0       # seconds allowed for one code test case
ANSWER_TRUNCATE = 2000    # chars of raw answer kept in a run file
OLLAMA_DOWN_MESSAGE = "Ollama is not running."
OLLAMA_DOWN_RUN_MESSAGE = ("Ollama is not answering at the set address. "
                           "Check the address in Settings.")
OLLAMA_DIED_MESSAGE = "Ollama stopped responding. Press Run again and the app will start it."
UNREACHABLE = "could not reach Ollama"
UNREACHABLE_HINT = "could not reach Ollama. Try again and the app will start it."
SLOW_REPLY = "Ollama took too long to answer"
# (v18): the first token did not come in FIRST_REPLY_TIMEOUT. Slow,
# not down — it deliberately does not start with UNREACHABLE, so a
# slow model fails its own tasks and the run carries on instead of
# the whole run ending as if Ollama had died.
START_OLLAMA_TIMEOUT_MSG = "Ollama could not be started. Try again."
START_OLLAMA_MISSING_MSG = "Ollama is not installed. Install it first, then try again."
NO_MODELS_MESSAGE = "No models installed yet. Pull one with the pull box on the Models tab."
NO_TEMPLATES_MESSAGE = "No test templates found in the templates folder."
# The name the single-instance guard listens on (U27). A second copy
# of the app connects here, tells the running copy to show itself,
# and exits without opening a window.
SINGLE_INSTANCE_NAME = "ollama-system-test"
# The How to Use index (U22), shown from Help > How to Use in a
# dialog that renders it as markdown. Plain words, no em-dashes,
# no bare &.
HOW_TO_USE_TEXT = """\
# How to Use OLLAMA SYSTEM TEST

## What this app does

This app tests the AI models on your PC. It runs each model through
test templates (coding, math, reasoning, vision, and more), scores the
answers, measures the speed, and builds a profile for each model: its
strengths, its weaknesses, and a plain verdict from the numbers. It
tests Ollama models on this PC and cloud provider models the same way.

## First time

1. Ollama starts itself. The app starts Ollama on its own whenever
   it needs it. If Ollama is not installed, install it first.
2. Pull a model. On the Models tab, type a model name (for example
   `qwen3:0.6b`) and press Pull.
3. Pick what to run. On the Run setup tab, tick a model and a test
   template, then press Run.
4. Read the results on the Run tab. The table shows each model's
   overall score and speed. Click a model to see its details.

The setup checklist at the top of the Run setup tab walks through
these steps. It goes away once they are all done, and you can dismiss
it at any time.

## Picking models and templates

Nothing is ticked to start with. Tick the models and the test
templates you want, then press Run. Each model runs each template in
turn.

A template no ticked model can run is greyed out and cannot be
ticked. For example, a vision test needs a model that can see images.
Press Screen models to check what each installed model can do.

Use the filter boxes to find a model or a template by name. Ticked
items keep their ticks while filtered.

The "Stop a template after __ minutes" box sets a time budget for one
template. At 0 there is no limit. When a budget runs out, that
template stops with "time budget reached" and the run carries on.

## Running a test

Press Run. The app checks Ollama is answering first (provider-only
runs skip this check), then runs each model through each template in
turn. Every template run is saved to its own file under `results` as
soon as it finishes, and nothing is ever overwritten.

While a run is in progress the pick lists lock, so the ticks on
screen always match the run. Press Cancel (or Esc) to stop after the
current task. Results so far stay in the table.

A full run over many models and templates can take a long time. The
Run tab shows the progress and the current model and template.

## Reading the results

The Run tab table shows one row per model: overall score, average
decode speed in tokens per second, and status. Click a column header
to sort. Click a model to see its details below the table: the score
on each template, the tokens used, and (for provider models) the
estimated cost.

The Profiles tab shows each model's profile: latest and best scores,
strengths and weaknesses, and a verdict written from its numbers.
Profiles rebuild themselves after every run. Press Regenerate to
rebuild one from the run files on disk.

The Compare tab puts two models side by side: pick two models and it
shows their latest score on each template they ran, plus their
overall score and speed.

## Past runs

The Past runs tab holds every run you have ever done, grouped by
model and by system. Pick a model to browse its runs, and pick a run
to see its scores. The tab shows how much disk the results store
uses. To remove a run, pick it and press Delete run. Deleting asks
to confirm first, and it is disabled while a run is in progress.

## Pulling a model

On the Models tab, type a model name and press Pull. The bar shows
the download progress. Pulling a model does not run any test; it just
downloads it so you can test it. Press Refresh to re-read the models
Ollama has installed.

## Providers (cloud models)

The Providers tab adds cloud providers (OpenAI-compatible APIs).
Press New provider, fill in the name, the base URL, and the API key,
then press Save provider. Keys live in `providers.json` next to the
app. That file holds your keys: never share it or upload it anywhere.

Press Test connection to check a provider answers. Add the provider's
models with their prices (per million tokens) to get cost estimates
before a run. A run with provider models shows its estimated cost
first and asks to confirm when it is over $1.00, or when no prices
are saved and the cost is unknown.

Removing a provider asks to confirm first. Its key and saved models
go with it.

## Saving and sharing

Save report writes the current results to the `results` folder as a
text file and a JSON file. Export CSV and Export HTML write the same
results as a spreadsheet file or a web page, wherever you choose.

## If something goes wrong

- "Ollama is not running": the app starts Ollama on its own. If it
  keeps failing, check that Ollama is installed.
- "could not reach Ollama. Try again and the app will start it.":
  Ollama stopped answering. Press Run again and the app starts it.
- "provider X did not answer": the provider could not be reached.
  Check the base URL on the Providers tab, or try Test connection.
- "provider X rejected the key": the API key is wrong or revoked.
  Paste a fresh key on the Providers tab and press Save provider.
- A model row says "failed": that model could not run. The test
  carries on with the rest. Try that model on its own with
  `ollama run <name>` (local) or Test connection (provider).
- A task says "not run": the test image or audio clip that task needs
  is missing from the `images` or `audio` folder. Put the file back
  (or re-download the Release zip, which carries them) and run again.
  A "not run" task is not the model's fault and never counts toward
  its score.
- A template says "time budget reached": the per-template time budget
  ran out before that template's jobs finished. The jobs that ran
  still count toward the score. Raise the budget, or leave it at 0
  for no limit, and run again.
- A template shows "not eligible for this model": the model cannot
  run that test (no vision, no tools, and so on). It is not a
  failure. Tick a model that has the capability to run it.
- Debug report: Help > Debug report saves a scrubbed copy of the
  app's recent log. It holds the app's log only (no keys, no model
  answers), so it is safe to send to someone helping you. If the app
  crashes, it writes a crash report to the `logs` folder before it
  closes.

## Shortcuts

- Ctrl+R: start a run.
- Ctrl+S: save the report.
- Esc: cancel a running run.
"""
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
MAX_FETCHED_MODELS = 200       # most model ids the Test connection pick-list holds
PROVIDER_LABEL_SEP = " / "      # "provider / model-id", used everywhere
SHOW_TIMEOUT = 2.0              # seconds for one /api/show screening call
LONG_CONTEXT_MIN_TOKENS = 32768  # a context this long counts as long-context
# Speed's full score (v17): decode speed that earns 100%. The
# fastest small models on the target Ryzen 3 box measure about 8-18
# tokens per second, so 20 is just above the best — a fast 2B model
# scores near full marks, a slow 4B model scores low, honestly.
SPEED_FULL_SCORE_TOK_S = 20.0
# RAM check (v17): a model needs its file size times this much free
# RAM to load safely — the weights plus room for the context and the
# rest of the system. A model that does not fit is skipped, never
# run into an out-of-memory kill.
RAM_NEED_FACTOR = 1.2
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


def normalize_ollama_address(value):
    """An Ollama address with a scheme (v18): a bare host:port
    ("127.0.0.1:11436", Ollama's own usual form) silently broke
    every request, because urlopen needs a scheme. Empty stays
    empty (the default); an address that already has a scheme is
    untouched."""
    value = (value or "").strip().rstrip("/")
    if not value or "://" in value:
        return value
    return "http://" + value


def ollama_base():
    return normalize_ollama_address(
        os.environ.get("OLLAMA_HOST", "http://localhost:11434"))


# What the environment set before this process started. The Settings
# window locks a field when its variable is set here — the
# environment wins over the saved setting, always. Captured at
# import time, because the app itself writes OLLAMA_HOST into the
# environment when it applies a saved address, and that must not
# lock the field it came from.
_ENV_OLLAMA_HOST = os.environ.get("OLLAMA_HOST")
_ENV_OLLAMA_MODELS = os.environ.get("OLLAMA_MODELS")


def load_app_settings():
    """The app's own settings from QSettings (R18): the models
    folder, the Ollama address, the default time budget, and the
    per-model usual template sets. R21 adds the text size (0 means
    the system default) and the reduce-motion flag. Missing keys
    read as empty."""
    settings = QSettings("BrokeNBored", APP_NAME)
    sets_raw = settings.value("template_sets", "")
    try:
        template_sets = json.loads(sets_raw) if sets_raw else {}
    except ValueError:
        template_sets = {}
    if not isinstance(template_sets, dict):
        template_sets = {}
    return {
        "models_folder": settings.value("models_folder", "") or "",
        "ollama_host": settings.value("ollama_host", "") or "",
        "default_budget_minutes": settings.value(
            "default_budget_minutes", 0, type=int
        ),
        "template_sets": template_sets,
        "font_point_size": settings.value(
            "font_point_size", 0, type=int
        ),
        "reduce_motion": settings.value(
            "reduce_motion", False, type=bool
        ),
    }


def save_app_settings(values):
    """Write the app's own settings (R18) to QSettings."""
    settings = QSettings("BrokeNBored", APP_NAME)
    settings.setValue("models_folder", values.get("models_folder", ""))
    settings.setValue("ollama_host", values.get("ollama_host", ""))
    settings.setValue(
        "default_budget_minutes", values.get("default_budget_minutes", 0)
    )
    settings.setValue(
        "template_sets", json.dumps(values.get("template_sets", {}))
    )
    settings.setValue(
        "font_point_size", values.get("font_point_size", 0)
    )
    settings.setValue(
        "reduce_motion", values.get("reduce_motion", False)
    )


def apply_saved_settings_to_env():
    """Apply the saved Ollama address to this process, unless the
    environment already sets it — the environment wins, always.
    Called from main() before anything reads ollama_base(), window
    or headless."""
    if _ENV_OLLAMA_HOST is not None:
        return
    host = load_app_settings()["ollama_host"]
    if host:
        os.environ["OLLAMA_HOST"] = host


def get_templates_dir():
    return os.environ.get(
        "OST_TEMPLATES_DIR", os.path.join(APP_DIR, "templates")
    )


def get_results_dir():
    return os.environ.get(
        "OST_RESULTS_DIR", os.path.join(APP_DIR, "results")
    )


def any_run_exists():
    """True when at least one benchmark run has ever been saved: any
    .json file anywhere under the results folder. A saved report
    counts too — it can only exist after a run."""
    results_dir = get_results_dir()
    if not os.path.isdir(results_dir):
        return False
    for _root, _dirs, files in os.walk(results_dir):
        if any(name.endswith(".json") for name in files):
            return True
    return False


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


# -- debug log layer (R2) ------------------------------------------------
# The app that diagnoses Ollama dying keeps a log of its own:
# timestamped lines in logs/ost-YYYY-MM-DD.log (one file per day,
# pruned after 30 days), a last-200 ring buffer in memory, and a
# scrubbed debug report on demand. Logging never crashes the app and
# never writes a key: every line is scrubbed before it is stored or
# reported.

LOG_KEEP_DAYS = 30      # daily log files older than this are pruned
LOG_RING_SIZE = 200     # lines kept in memory for the debug report
LOG_TAIL_LINES = 100    # lines of today's log file in the debug report

# Secret-shaped text is replaced with [key] before anything is logged
# or reported. The patterns cover the shapes the app itself writes
# (sk- keys, api_key/token assignments, Bearer headers) plus every key
# actually saved in providers.json, matched exactly.
_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)(api[_-]?key\s*[:=]\s*)\S+"),
    re.compile(r"(?i)(token\s*[:=]\s*)\S+"),
    re.compile(r"(?i)(bearer\s+)\S+"),
    re.compile(r"(?i)(authorization:\s*)\S+"),
)


def get_logs_dir():
    return os.environ.get(
        "OST_LOGS_DIR", os.path.join(APP_DIR, "logs")
    )


_LOG_RING = deque(maxlen=LOG_RING_SIZE)


def _saved_keys():
    """Every API key saved in providers.json, best effort. A missing
    or broken file means no keys, never a crash."""
    try:
        providers, _warnings = load_providers()
    except Exception:
        return []
    keys = []
    for provider in providers:
        key = provider.get("api_key")
        if key:
            keys.append(key)
    return keys


def scrub_text(text):
    """Replace anything secret-shaped in text with [key]. Saved keys
    are matched exactly first; the patterns catch the rest."""
    if not text:
        return text
    for key in _saved_keys():
        if len(key) >= 8:
            text = text.replace(key, "[key]")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(
            lambda match: (
                match.group(1) + "[key]" if match.lastindex else "[key]"
            ),
            text,
        )
    return text


def log_event(message):
    """One timestamped line to the ring buffer and today's log file.
    Never raises: a logging failure must not break the run it logs."""
    line = (
        f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
        f"{scrub_text(str(message))}"
    )
    _LOG_RING.append(line)
    try:
        folder = get_logs_dir()
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(
            folder, f"ost-{datetime.now().strftime('%Y-%m-%d')}.log"
        )
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def prune_old_logs():
    """Delete daily log files older than LOG_KEEP_DAYS. Best effort."""
    try:
        folder = get_logs_dir()
        if not os.path.isdir(folder):
            return
        cutoff = datetime.now() - timedelta(days=LOG_KEEP_DAYS)
        for name in os.listdir(folder):
            if not (name.startswith("ost-") and name.endswith(".log")):
                continue
            try:
                stamp = datetime.strptime(name[4:-4], "%Y-%m-%d")
            except ValueError:
                continue
            if stamp < cutoff:
                os.remove(os.path.join(folder, name))
    except OSError:
        pass


def write_debug_report():
    """Build a scrubbed debug report in logs/ and return its path.
    Contents: version, platform, Python, whether Ollama answers, the
    in-memory ring buffer, and the tail of today's log file. No keys,
    no answers, no model output — only the app's own log."""
    folder = get_logs_dir()
    os.makedirs(folder, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    path = os.path.join(folder, f"debug-report-{stamp}.txt")
    lines = [
        f"OLLAMA SYSTEM TEST v{__version__} debug report",
        f"Made: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"System: {platform.system()} {platform.release()} "
        f"({platform.machine()})",
        f"Python: {platform.python_version()}",
    ]
    try:
        lines.append(
            "Ollama: " + ("answering" if ollama_reachable() else "not answering")
        )
    except Exception:
        lines.append("Ollama: check failed")
    lines.append("")
    lines.append("Recent events (newest last):")
    if _LOG_RING:
        lines.extend(_LOG_RING)
    else:
        lines.append("(no events logged yet)")
    lines.append("")
    lines.append("Today's log file (last lines):")
    log_path = os.path.join(
        folder, f"ost-{datetime.now().strftime('%Y-%m-%d')}.log"
    )
    try:
        with open(log_path, encoding="utf-8") as handle:
            tail = handle.readlines()[-LOG_TAIL_LINES:]
        if tail:
            lines.extend(line.rstrip("\n") for line in tail)
        else:
            lines.append("(no log lines yet)")
    except OSError:
        lines.append("(no log file yet)")
    text = scrub_text("\n".join(lines) + "\n")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def _ost_excepthook(exc_type, exc_value, exc_tb):
    """Crash hook: the traceback goes to the log and to a scrubbed
    crash file, then the normal hook runs (the app still exits the
    way it would have). KeyboardInterrupt is left alone."""
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    log_event("Crash:\n" + text)
    try:
        folder = get_logs_dir()
        os.makedirs(folder, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        path = os.path.join(folder, f"crash-{stamp}.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(scrub_text(text))
    except OSError:
        pass
    sys.__excepthook__(exc_type, exc_value, exc_tb)


def install_excepthook():
    """Install the crash hook, once. Called from main()."""
    if sys.excepthook is not _ost_excepthook:
        sys.excepthook = _ost_excepthook


# The dark theme (T14): dark backgrounds with light text throughout,
# covering every widget the window uses — lists, tables, text edits,
# tabs, buttons, combo boxes, menus, the progress bar. Light stays the
# default; this only applies when the user toggles View > Dark mode.
def _focus_rules(color):
    """R22: the focus ring (library 023) — a 2 px solid border on
    every focusable widget. NOT outline: this PySide6 build does not
    render outline at all (probed 2026-11-14: 0 pixels), while border
    paints (1782 pixels). The ring must be unmistakable, not a 1 px
    dotted line."""
    widgets = ("QPushButton", "QLineEdit", "QComboBox", "QSpinBox",
               "QListWidget", "QTableWidget", "QTextEdit", "QCheckBox",
               "QSlider")
    return "\n".join(
        f"{widget}:focus {{ border: 2px solid {color}; }}"
        for widget in widgets
    ) + "\n"


# The light theme is the native look plus the focus ring: light mode
# is not "no stylesheet" anymore, because the ring must show in both
# themes (light is the default).
LIGHT_STYLESHEET = _focus_rules("#0b5cad")


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


# The focus ring goes last, so it wins over the base 1 px borders
# above (same specificity, later rule wins).
DARK_STYLESHEET += _focus_rules("#7db8e8")


def apply_theme(dark):
    """Apply the dark stylesheet to the running application, or the
    light one (native look plus the focus ring) back. A no-op without
    a QApplication — headless mode runs under a QCoreApplication,
    which has no window to restyle."""
    app = QApplication.instance()
    if not isinstance(app, QApplication):
        return
    app.setStyleSheet(DARK_STYLESHEET if dark else LIGHT_STYLESHEET)


# The font the application started with, captured the first time the
# text size is changed — restoring the default means restoring this,
# not guessing at a size.
_DEFAULT_FONT = None

# The big-text preset (R21, library 015): the default size plus this
# many points. One tap, readable text throughout; the size stays
# adjustable on its own afterwards.
FONT_PRESET_BONUS_PT = 3


def default_font_point_size():
    """The point size of the font the application started with."""
    app = QApplication.instance()
    if not isinstance(app, QApplication):
        return 0
    if _DEFAULT_FONT is not None:
        return _DEFAULT_FONT.pointSize()
    return app.font().pointSize()


def apply_font_size(point_size):
    """Set the application's text size (R21). 0 restores the font
    the application started with. A no-op without a QApplication."""
    global _DEFAULT_FONT
    app = QApplication.instance()
    if not isinstance(app, QApplication):
        return
    if _DEFAULT_FONT is None:
        _DEFAULT_FONT = QFont(app.font())
    if not point_size:
        app.setFont(QFont(_DEFAULT_FONT))
        return
    font = QFont(_DEFAULT_FONT)
    font.setPointSize(max(8, min(20, int(point_size))))
    app.setFont(font)


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
        return "n/a"
    if size >= 1_000_000_000:
        return f"{size / 1_000_000_000:.1f} GB"
    if size >= 1_000_000:
        return f"{size / 1_000_000:.0f} MB"
    if size >= 1_000:
        return f"{size / 1_000:.0f} KB"
    return f"{size} B"


def fmt_duration(seconds):
    if seconds is None:
        return "n/a"
    if seconds < 1:
        return f"{seconds * 1000:.0f} ms"
    return f"{seconds:.2f} s"


def fmt_eta(seconds):
    """A wall-clock duration in words for a run estimate: "45s",
    "3m", "18m", "1h 5m". Whole units only — an estimate is not a
    stopwatch, and false precision is a lie."""
    if seconds is None:
        return "n/a"
    total = int(round(seconds))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m"
    hours = total // 3600
    minutes = (total % 3600) // 60
    if minutes:
        return f"{hours}h {minutes}m"
    return f"{hours}h"


def fmt_toks(value):
    if value is None:
        return "n/a"
    return f"{value:.1f}"


def fmt_score(value):
    if value is None:
        return "n/a"
    return f"{value:.0f}%"


def fmt_tokens(count):
    if count is None:
        return "n/a"
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
        return "n/a"
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


def estimate_run_cost(targets, templates, pairs=None):
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
    run are skipped, same as the worker. When pairs is given (a
    re-run of failed pairs, R24), only the pairs in the set count.
    """
    per_model = []
    total = 0.0
    unknown = []
    for target in targets:
        if target["kind"] != "provider":
            continue
        mine = [t for t in templates
                if pairs is None
                or (target["label"], t["id"]) in pairs]
        if pairs is not None and not mine:
            continue
        prompt_tokens = 0
        completion_tokens = 0
        if "text" in set(target.get("capabilities") or ["text"]):
            prompt_tokens += _chars_to_tokens(len(WARMUP_PROMPT))
            completion_tokens += WARMUP_PREDICT
        for template in mine:
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


def failed_run_pairs(results, targets, templates, pairs=None):
    """The (target, template) pairs a run did not finish (R24).

    A pair failed when the model never started (missing from the
    results), the model failed outright (its status is not "done"),
    the template never ran for that model, the template ran but
    nothing in it ran (a scored template with no score and zero
    tasks run), or the time budget stopped it. Skipped pairs (a
    model that cannot run a template) are not failures — nothing
    went wrong, and re-running would skip them again. When pairs is
    given, only pairs in that set are considered: a re-run checks
    only what it ran. Returns the pairs in run order."""
    wanted = None if pairs is None else set(pairs)
    failed = []
    for target in targets:
        label = target["label"]
        mine = [t for t in templates
                if wanted is None or (label, t["id"]) in wanted]
        if not mine:
            continue
        result = results.get(label)
        if result is None or result.get("status") != "done":
            failed.extend((target, t) for t in mine)
            continue
        ran = result.get("templates") or {}
        for template in mine:
            run = ran.get(template["id"])
            if run is None:
                failed.append((target, template))
                continue
            if run.get("skipped"):
                continue
            if run.get("time_budget_reached"):
                failed.append((target, template))
                continue
            if (template.get("scored", True)
                    and run.get("template_score") is None
                    and (run.get("total") or 0) == 0):
                failed.append((target, template))
    return failed


def count_run_steps(targets, templates, pairs=None):
    """Progress steps for a run: one warmup per target, then one step
    per model call on the eligible templates only. A multi-turn task
    counts its turns (each turn is a call); the Speed template counts
    its context lengths. Skipped pairs cost no steps. The worker and
    the run preview share this, so the preview's step count is the
    same count the run itself uses. When pairs is given (a re-run of
    failed pairs, R24), only the pairs in the set count — a target
    with no pairs costs no warmup either."""
    steps = 0
    for target in targets:
        mine = [t for t in templates
                if pairs is None
                or (target["label"], t["id"]) in pairs]
        if pairs is not None and not mine:
            continue
        steps += 1  # warmup
        for template in mine:
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


# The run preview's time estimate (R11): what a model with no past
# runs is assumed to do, how long a model load is allowed for, and
# the safety margin on top. The estimate is an UPPER BOUND: the run
# usually finishes sooner, because models usually stop well short
# of num_predict.
PREVIEW_DEFAULT_TOK_S = 8.0
PREVIEW_LOAD_ALLOWANCE_S = 10.0
PREVIEW_SAFETY_MARGIN = 1.25


def latest_decode_speed(results_dir, label):
    """A model's decode speed from its newest past run file, or None
    when it has never run here (or no run recorded a speed)."""
    runs = load_model_runs(results_dir, slugify_model(label))
    for run in reversed(runs):
        speed = run.get("avg_decode_tok_s")
        if speed:
            return speed
    return None


def build_run_preview(targets, templates, results_dir, pairs=None,
                      resume=None):
    """R11: the run preview (library 017) — what a run will do, in
    plain text, before it starts.

    Returns a dict: "text" is the preview itself; "total_steps" is
    the step count; "seconds_upper" is the time estimate (an upper
    bound, in seconds); "cost" is the estimate_run_cost result for
    the provider targets (or None for a local-only run).

    The time estimate is honest about what it is: per model, the
    tokens the run will ask for (estimate_template_tokens, an upper
    bound itself) divided by the model's measured speed from its
    newest past run — or 8 tok/s when it has never run here — plus
    10 s per model for loading, all times a 1.25 safety margin. It
    says "at most about" because that is what it is.

    When pairs is given (a re-run of failed pairs, R24), the preview
    counts and prices only the pairs in the set, and says so. When
    resume is given (v17: (manifest, notes)), it says it is a resume
    and lists the pairs skipped because their model or template is
    gone.
    """
    lines = []
    total_steps = count_run_steps(targets, templates, pairs)
    if resume is not None:
        manifest, notes = resume
        started = (manifest.get("started") or "").replace("T", " ")
        lines.append(
            f"Resuming the run from {started}: only the pairs below "
            "will run."
        )
        for note in notes:
            lines.append(f"  {note}")
    elif pairs is not None:
        models = {label for label, _tid in pairs}
        lines.append(
            f"Re-running {len(pairs)} "
            f"{'failed pair' if len(pairs) == 1 else 'failed pairs'} "
            f"({len(models)} "
            f"{'model' if len(models) == 1 else 'models'}), "
            f"{total_steps} steps in total."
        )
    else:
        lines.append(
            f"{len(targets)} {'model' if len(targets) == 1 else 'models'}, "
            f"{len(templates)} {'template' if len(templates) == 1 else 'templates'}, "
            f"{total_steps} steps in total."
        )
    lines.append("")
    seconds_upper = 0.0
    for target in targets:
        label = target["label"]
        mine = [t for t in templates
                if pairs is None or (label, t["id"]) in pairs]
        if pairs is not None and not mine:
            continue
        steps = count_run_steps([target], templates, pairs)
        eligible = [t for t in mine if template_eligible(t, target)]
        lines.append(
            f"  {label}: {len(eligible)} "
            f"{'template' if len(eligible) == 1 else 'templates'}, "
            f"{steps} steps"
        )
        speed = latest_decode_speed(results_dir, label)
        if speed is None:
            speed = PREVIEW_DEFAULT_TOK_S
        speed = min(max(speed, 1.0), 500.0)
        tokens = 0
        for template in eligible:
            prompt_tokens, completion_tokens = estimate_template_tokens(
                template
            )
            tokens += prompt_tokens + completion_tokens
        seconds_upper += tokens / speed + PREVIEW_LOAD_ALLOWANCE_S
    seconds_upper *= PREVIEW_SAFETY_MARGIN
    lines.append("")
    lines.append(
        f"Time: at most about {fmt_eta(seconds_upper)} (an upper "
        "estimate; the run usually finishes sooner)."
    )
    provider_targets = [t for t in targets if t["kind"] == "provider"]
    if pairs is not None:
        provider_targets = [
            t for t in provider_targets
            if any(label == t["label"] for label, _tid in pairs)
        ]
    cost = None
    if provider_targets:
        cost = estimate_run_cost(provider_targets, templates, pairs)
        lines.append("")
        lines.append("Cost (provider models cost real money):")
        for entry in cost["per_model"]:
            if entry["has_prices"]:
                lines.append(
                    f"  {entry['label']}: about {fmt_cost(entry['cost'])} "
                    f"(about {fmt_tokens(entry['prompt_tokens'])} tokens in, "
                    f"{fmt_tokens(entry['completion_tokens'])} out)"
                )
            else:
                lines.append(
                    f"  {entry['label']}: no prices saved: cost unknown"
                )
        if cost["unknown"]:
            lines.append(
                "No prices are saved for "
                + ", ".join(cost["unknown"])
                + ", so the total cost is unknown."
            )
        else:
            lines.append(f"Estimated total: about {fmt_cost(cost['total'])}.")
    else:
        lines.append("")
        lines.append("Cost: local models only, so nothing to pay.")
    return {
        "text": "\n".join(lines),
        "total_steps": total_steps,
        "seconds_upper": seconds_upper,
        "cost": cost,
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


def ollama_address_is_local():
    """True when the Ollama address points at this machine (v17):
    localhost, 127.0.0.1, ::1, or no host at all. Only a local
    address may be auto-started — a remote server is someone
    else's to start."""
    try:
        host = urllib.parse.urlparse(ollama_base()).hostname
    except ValueError:
        return False
    if host is None:
        return True
    return host.lower() in ("localhost", "127.0.0.1", "::1")


def list_model_sizes():
    """Map of model name -> file size in bytes, from /api/tags.

    {} when Ollama does not answer — the RAM check then stays off
    instead of blocking a run on a failed read."""
    try:
        data = http_get_json("/api/tags", timeout=10)
    except OllamaError:
        return {}
    sizes = {}
    for model in data.get("models", []) or []:
        if model.get("name") and isinstance(model.get("size"), int):
            sizes[model["name"]] = model["size"]
    return sizes


def get_meminfo_path():
    """Where mem_available_bytes reads: /proc/meminfo, or the
    OST_MEMINFO_FILE override the suite points at a fixture."""
    return os.environ.get("OST_MEMINFO_FILE", "/proc/meminfo")


def mem_available_bytes():
    """MemAvailable from /proc/meminfo, or None when it cannot be
    read (non-Linux, or a failed read) — the RAM check then stays
    off instead of blocking a run."""
    try:
        with open(get_meminfo_path(), encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1]) * 1024
    except (OSError, ValueError):
        pass
    return None


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


def delete_ollama_model(name):
    """DELETE /api/delete for one model (R24). Returns (ok, message):
    ok True when Ollama deleted it, False with a plain reason when
    it did not answer or refused."""
    payload = {"name": name}
    req = urllib.request.Request(
        ollama_base() + "/api/delete",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(req, timeout=READ_TIMEOUT):
            return True, ""
    except urllib.error.HTTPError as exc:
        return False, _http_error_reason(exc)
    except (urllib.error.URLError, socket.timeout, OSError):
        return False, "could not reach Ollama"


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

    Returns the capability set, or None when screening failed — a
    failure is never cached (v18): the old code cached it as
    text-only forever. The caller keeps the old value (or
    text-only when there is none) and retries on the next refresh.
    """
    try:
        data = ollama_show(model)
    except OllamaError:
        return None
    caps = capabilities_from_show(data)
    cache[model] = sorted(caps)
    return caps


def _set_response_read_timeout(resp, timeout):
    """Lower the socket timeout on an open response (v18).

    The first reply waits FIRST_REPLY_TIMEOUT; once it arrives, the
    per-read timeout goes back to READ_TIMEOUT so the stall loop
    still notices a stalled stream within STALL_TIMEOUT and Cancel
    stays responsive. Best effort: when the socket cannot be
    reached through this response shape, the long timeout simply
    stays and stall detection takes longer.
    """
    sock = None
    try:
        sock = resp.fp.raw._sock
    except AttributeError:
        try:
            sock = resp.fp._sock
        except AttributeError:
            return
    try:
        sock.settimeout(timeout)
    except OSError:
        pass


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
        resp = urllib.request.urlopen(req, timeout=FIRST_REPLY_TIMEOUT)
    except urllib.error.HTTPError as exc:
        raise OllamaError(_http_error_reason(exc))
    except socket.timeout:
        # The server took the connection but never answered: slow,
        # not down. Say so plainly — the old message blamed Ollama.
        raise OllamaError(SLOW_REPLY)
    except urllib.error.URLError as exc:
        if isinstance(getattr(exc, "reason", None), socket.timeout):
            raise OllamaError(SLOW_REPLY)
        raise OllamaError("could not reach Ollama")
    except OSError:
        raise OllamaError("could not reach Ollama")
    _set_response_read_timeout(resp, READ_TIMEOUT)
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
        resp = urllib.request.urlopen(req, timeout=FIRST_REPLY_TIMEOUT)
    except urllib.error.HTTPError as exc:
        raise OllamaError(_http_error_reason(exc))
    except socket.timeout:
        # The server took the connection but never answered: slow,
        # not down. Say so plainly — the old message blamed Ollama.
        raise OllamaError(SLOW_REPLY)
    except urllib.error.URLError as exc:
        if isinstance(getattr(exc, "reason", None), socket.timeout):
            raise OllamaError(SLOW_REPLY)
        raise OllamaError("could not reach Ollama")
    except OSError:
        raise OllamaError("could not reach Ollama")
    _set_response_read_timeout(resp, READ_TIMEOUT)
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


def provider_fetch_models(provider):
    """GET {base}/models. Returns (ok, model ids, plain message).
    Spends no tokens. The ids are the provider's own list, in the
    provider's order, at most MAX_FETCHED_MODELS of them — a provider
    can list hundreds, and the pick-list only needs the first page."""
    name = provider.get("name") or "provider"
    base_url = (provider.get("base_url") or "").rstrip("/")
    if not base_url:
        return False, [], "No base URL set."
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
                return False, [], f"provider {name} rejected the key."
            return False, [], f"provider {name} rejected the key: {reason}"
        return False, [], f"provider {name}: {_provider_error_reason(exc)}"
    except (urllib.error.URLError, socket.timeout, OSError):
        return False, [], f"provider {name} did not answer. Check the base URL."
    except ValueError:
        return False, [], f"provider {name} answered, but not with JSON."
    models = data.get("data") if isinstance(data, dict) else None
    if not isinstance(models, list):
        return True, [], f"Connected: {name} answered."
    ids = [m.get("id") for m in models if isinstance(m, dict)]
    ids = [model_id for model_id in ids if isinstance(model_id, str) and model_id]
    total = len(ids)
    if total > MAX_FETCHED_MODELS:
        ids = ids[:MAX_FETCHED_MODELS]
        return True, ids, (
            f"Connected: {name} answered, {total} models listed "
            f"(showing the first {MAX_FETCHED_MODELS})."
        )
    return True, ids, f"Connected: {name} answered, {total} models listed."


def provider_test_connection(provider):
    """GET {base}/models. Returns (ok, plain message). Spends no tokens."""
    ok, _ids, message = provider_fetch_models(provider)
    return ok, message


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


_ONES = ("zero", "one", "two", "three", "four", "five", "six", "seven",
         "eight", "nine", "ten", "eleven", "twelve", "thirteen",
         "fourteen", "fifteen", "sixteen", "seventeen", "eighteen",
         "nineteen")
_TENS = ("", "", "twenty", "thirty", "forty", "fifty", "sixty",
         "seventy", "eighty", "ninety")


def _spell_number(value):
    """0-99 as words; anything else stays digits."""
    if 0 <= value < 20:
        return _ONES[value]
    if value < 100:
        tens, ones = divmod(value, 10)
        if ones:
            return f"{_TENS[tens]} {_ONES[ones]}"
        return _TENS[tens]
    return str(value)


def transcript_words(text):
    """Normalize a transcript for scoring: spell digits out as words
    and drop punctuation. Transcription providers write spoken numbers
    as digits ("1, 2, 3") while the task expects words ("one two three")
    — without this, a correct transcript fails. Only the scored copy is
    normalized; the stored answer keeps the provider's own words."""
    spelled = re.sub(
        r"\b\d+\b",
        lambda match: _spell_number(int(match.group(0))),
        str(text),
    )
    cleaned = re.sub(r"[^a-z0-9 ]+", " ", spelled.lower())
    return " ".join(cleaned.split())


def score_contains(answer, expected):
    """Expected string appears in the answer (whole word/phrase, any case)."""
    pattern = r"\b" + re.escape(_normalize(expected)) + r"\b"
    if re.search(pattern, _normalize(answer)):
        return True, ""
    return False, f"expected '{expected}' not in the answer"


# Number words (M5/T5): zero to nineteen, the tens, and hundred.
# The normalizer turns written numbers into digits before
# extraction, so "twenty-one" and "one hundred and fifty" score
# like 21 and 150. Named _WORD_* because _ONES/_TENS already
# exist above (v10: digits to words, for transcripts).
_WORD_ONES = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_WORD_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
_NUMBER_WORDS = dict(_WORD_ONES)
_NUMBER_WORDS.update(_WORD_TENS)


def _spell_number_words(text):
    """Replace written numbers with digits (M5): zero to nineteen,
    tens, tens-and-ones ("twenty-one", "twenty one"), and hundreds
    ("one hundred", "one hundred and fifty"). Only whole words, so
    "someone" is untouched; "and" only joins inside a hundred run
    ("one hundred and fifty"), never starts or ends one. Case
    insensitive. Text without number words comes back unchanged."""
    tokens = re.findall(r"[A-Za-z]+|[^A-Za-z]+", str(text))
    out = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        lowered = token.lower()
        if lowered not in _NUMBER_WORDS and lowered != "hundred":
            out.append(token)
            index += 1
            continue
        # A run of number words: collect its value, then emit the
        # digits once for the whole run. Separators the run
        # consumed are dropped with it; a trailing separator the
        # run did not consume stays in the text.
        current = 0
        while index < len(tokens):
            word = tokens[index].lower()
            if word in _NUMBER_WORDS:
                current += _NUMBER_WORDS[word]
                index += 1
            elif word == "hundred":
                current = (current or 1) * 100
                index += 1
            elif word == "and" and current:
                # "and" joins only when a number word follows it.
                following = None
                look = index + 1
                while look < len(tokens):
                    if tokens[look].strip():
                        following = tokens[look].lower()
                        break
                    look += 1
                if following in _NUMBER_WORDS or following == "hundred":
                    index += 1
                else:
                    break
            elif not word.strip():
                # Whitespace inside a run (or a hyphen, which the
                # tokenizer splits out): skip it, the run continues
                # when the next word is a number word.
                following = None
                look = index + 1
                while look < len(tokens):
                    if tokens[look].strip():
                        following = tokens[look].lower()
                        break
                    look += 1
                if (following in _NUMBER_WORDS
                        or following == "hundred"
                        or (following == "and" and current)):
                    index += 1
                else:
                    break
            elif word == "-":
                index += 1
            else:
                break
        out.append(str(current))
    return "".join(out)


# One number token: a fraction first ("1/2"), then scientific
# notation ("1.5e3"), then a plain number. The order matters —
# "1/2" must match as a fraction, not as 1 and 2.
_NUMBER_RE = re.compile(
    r"-?\d+(?:\.\d+)?\s*/\s*-?\d+(?:\.\d+)?"
    r"|-?\d+(?:\.\d+)?[eE][+-]?\d+"
    r"|-?\d+(?:\.\d+)?"
)


def _extract_numbers(answer):
    """Numbers in an answer, left to right (M5/M6): written numbers
    become digits first ("twenty-one" is 21), fractions evaluate
    ("1/2" is 0.5, not 1 and 2), and scientific notation matches
    ("1.5e3" is 1500). Commas are stripped, so "1,000" is 1000.
    A zero denominator is skipped, never an error."""
    text = _spell_number_words(str(answer).replace(",", ""))
    numbers = []
    for match in _NUMBER_RE.finditer(text):
        token = match.group(0)
        if "/" in token:
            numerator, denominator = token.split("/")
            try:
                divisor = float(denominator)
                if divisor == 0:
                    continue
                numbers.append(float(numerator) / divisor)
            except ValueError:
                pass
        else:
            try:
                numbers.append(float(token))
            except ValueError:
                pass
    return numbers


def _numbers_match(value, expected, tolerance=None, approximate=False):
    """One number against one expected value, with the task's
    tolerance (M2): an absolute tolerance when the task gives one,
    1% when the task says approximate, exact otherwise."""
    expected = float(expected)
    if tolerance is not None:
        return abs(value - expected) <= float(tolerance)
    if approximate:
        return abs(value - expected) <= 0.01 * max(1.0, abs(expected))
    return value == expected


def score_numeric(answer, expected, tolerance=None, approximate=False):
    """The LAST number in the answer must match the expected value
    (M1) — a number mentioned in passing no longer passes. Exact
    unless the task gives a tolerance or says approximate (M2)."""
    numbers = _extract_numbers(answer)
    if not numbers:
        return False, f"expected the number {expected} in the answer"
    if _numbers_match(numbers[-1], expected, tolerance, approximate):
        return True, ""
    return False, (
        f"final answer was {numbers[-1]:g}, "
        f"expected {float(expected):g}"
    )


def _numeric_parts(answer, task):
    """A numeric task's parts (M4): (final_ok, steps_ok, numbers).
    The final answer scores on the last number; each intermediate
    step scores when its value appears anywhere in the working."""
    numbers = _extract_numbers(answer)
    tolerance = task.get("tolerance")
    approximate = bool(task.get("approximate", False))
    if not numbers:
        return False, [False] * len(task.get("steps") or []), numbers
    final_ok = _numbers_match(
        numbers[-1], task.get("expected"), tolerance, approximate)
    steps_ok = [
        any(_numbers_match(value, step, tolerance, approximate)
            for value in numbers)
        for step in (task.get("steps") or [])
    ]
    return final_ok, steps_ok, numbers


def score_numeric_task(answer, task):
    """A numeric task with its tolerance and steps (M2/M4). Passed
    means every part right — the final answer and every
    intermediate step. The detail names what was right, so a
    partial answer says so instead of just failing."""
    final_ok, steps_ok, numbers = _numeric_parts(answer, task)
    steps = task.get("steps") or []
    if not numbers:
        return False, (
            f"expected the number {task.get('expected')} in the answer"
        )
    if final_ok and all(steps_ok):
        return True, ""
    parts = []
    for step, ok in zip(steps, steps_ok):
        parts.append(
            f"step {float(step):g} {'right' if ok else 'not found'}")
    parts.append(
        "final answer right"
        if final_ok
        else f"final answer was {numbers[-1]:g}, "
             f"expected {float(task.get('expected')):g}"
    )
    return False, "; ".join(parts)


def task_credit(task, answer, passed):
    """Credit 0.0-1.0 for one task's answer (M4). A numeric task
    with a steps list earns a fraction per part right — the final
    answer plus each intermediate step — so right setup with a
    wrong final scores partial, not 0 and not full. Everything
    else earns 1.0 for a pass, 0.0 for a fail."""
    if task.get("rule") == "numeric" and task.get("steps"):
        final_ok, steps_ok, _numbers = _numeric_parts(answer, task)
        parts = len(steps_ok) + 1
        got = sum(steps_ok) + (1 if final_ok else 0)
        return got / parts if parts else 0.0
    return 1.0 if passed else 0.0


def speed_credit(decode_tok_s):
    """One speed job's credit (v17): decode speed as a fraction of
    full speed (SPEED_FULL_SCORE_TOK_S), capped at 1. A failed job
    (no speed recorded) scores 0. Rounded to two decimals, so the
    stamped credit reads clean in the run file."""
    if decode_tok_s is None:
        return 0.0
    return round(min(1.0, decode_tok_s / SPEED_FULL_SCORE_TOK_S), 2)


def task_mark(task):
    """One task's mark for the detail and summary views (M4):
    pass, fail, partial with its credit ("partial (0.5)"), not
    run, time budget reached, or run (unscored)."""
    passed = task.get("passed")
    if passed is True:
        return "pass"
    if passed is False:
        credit = task.get("credit")
        if credit is not None and 0 < credit < 1:
            return f"partial ({credit:g})"
        return "fail"
    if is_not_run(task):
        return "not run"
    if is_budget_stopped(task):
        return "time budget reached"
    return "run"


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
        return score_numeric_task(answer, task)
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
            # v17: an unscored template may still score, by speed —
            # the Speed template grades decode speed instead of
            # answers. An unknown value drops the template with a
            # warning, same as an unknown capability: a typo must
            # never silently unscore a template.
            score_by = data.get("score_by")
            if score_by is not None:
                if score_by != "speed":
                    warnings.append(
                        f"{filename}: skipped, unknown score_by {score_by!r}"
                    )
                    continue
                template["score_by"] = score_by
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


# ---------------------------------------------------------------------------
# Run manifests (v17): what a run was, and how far it got — so an
# interrupted run can resume where it left off, even after the app
# was closed. R24's re-run-failed is in-memory only; the manifest is
# the part that survives a restart.
# ---------------------------------------------------------------------------

def get_runs_dir(results_dir=None):
    """results/_runs: run manifests live here, one file per run."""
    if results_dir is None:
        results_dir = get_results_dir()
    return os.path.join(results_dir, "_runs")


def manifest_path(results_dir, run_id):
    return os.path.join(get_runs_dir(results_dir), f"{run_id}.json")


def new_run_id(results_dir):
    """A run id from the start time; -2, -3 when one already exists
    for this second (two runs started at once)."""
    base = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_id = base
    n = 2
    while os.path.exists(manifest_path(results_dir, run_id)):
        run_id = f"{base}-{n}"
        n += 1
    return run_id


def pair_key(label, template_id):
    return f"{label}|{template_id}"


def split_pair_key(key):
    """A pair key back into (label, template id). Split on the last
    bar: template ids never contain one, labels might."""
    label, _bar, template_id = key.rpartition("|")
    return label, template_id


def new_manifest(run_id, targets, templates, sys_id):
    """A fresh manifest: every pair pending, nothing finished. The
    targets carry label, kind, model, and provider name only — never
    the provider entry itself, which holds the API key."""
    pairs = {}
    for target in targets:
        for template in templates:
            pairs[pair_key(target["label"], template["id"])] = "pending"
    return {
        "run_id": run_id,
        "started": datetime.now().isoformat(timespec="seconds"),
        "finished_at": None,
        "status": "running",
        "system_id": sys_id,
        "targets": [
            {"label": t["label"], "kind": t["kind"], "model": t["model"],
             "provider_name": t.get("provider_name")}
            for t in targets
        ],
        "templates": [t["id"] for t in templates],
        "pairs": pairs,
        "run_files": {},
    }


def save_manifest(results_dir, manifest):
    """Atomic write: tmp file + replace, so a crash never leaves a
    half-written manifest."""
    folder = get_runs_dir(results_dir)
    os.makedirs(folder, exist_ok=True)
    path = manifest_path(results_dir, manifest["run_id"])
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
    os.replace(tmp, path)
    return path


def load_manifest(results_dir, run_id):
    """One manifest by id, or None when it is missing or unreadable."""
    try:
        with open(manifest_path(results_dir, run_id),
                  encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or "pairs" not in data:
        return None
    return data


def list_manifests(results_dir):
    """All manifests, newest first. Unreadable files are skipped."""
    folder = get_runs_dir(results_dir)
    manifests = []
    if not os.path.isdir(folder):
        return manifests
    try:
        filenames = sorted(os.listdir(folder), reverse=True)
    except OSError:
        return manifests
    for filename in filenames:
        if not filename.endswith(".json"):
            continue
        manifest = load_manifest(results_dir, filename[:-5])
        if manifest is not None:
            manifests.append(manifest)
    return manifests


def manifest_is_resumable(manifest):
    """A manifest with pairs left to run — pending or failed. A run
    that finished with everything done or skipped is not resumable,
    whatever its end status says. A manifest still marked running
    (the app died mid-run) is resumable too."""
    pairs = manifest.get("pairs") or {}
    return any(status in ("pending", "failed")
               for status in pairs.values())


def latest_resumable_manifest(results_dir):
    """The newest manifest with pairs left to run, or None."""
    for manifest in list_manifests(results_dir):
        if manifest_is_resumable(manifest):
            return manifest
    return None


def build_resume_plan(manifest, targets_by_label, templates_by_id,
                      results_dir):
    """Turn a manifest into what a resume runs (v17).

    targets_by_label: the targets available now, by label.
    templates_by_id: the templates available now, by id.
    Returns (targets, pair_set, preloaded, notes):
    - targets: the available targets that still have pairs to run.
    - pair_set: (label, template id) pairs to run — pending or
      failed, both ends still available.
    - preloaded: {label: {template id: run data}} for the done
      pairs, loaded from their run files. A done pair whose run
      file is gone goes back to pending and runs again.
    - notes: plain words for pairs skipped because their model or
      template is gone.

    The manifest is updated in place: pairs whose model or template
    is gone are marked "skipped" (so a resume can finish even when
    something was deleted), and a done pair whose run file is gone
    goes back to "pending". The caller saves the manifest — the
    worker does it when a run starts, and the no-pairs path does
    it before reporting nothing left.
    """
    notes = []
    preloaded = {}
    pair_set = set()
    pairs = manifest.get("pairs") or {}
    run_files = manifest.get("run_files") or {}
    missing_models = {}
    missing_templates = {}
    for key, status in pairs.items():
        label, template_id = split_pair_key(key)
        target = targets_by_label.get(label)
        template = templates_by_id.get(template_id)
        if status == "done":
            rel = run_files.get(key)
            run_data = None
            if rel:
                run_data = load_run_file(os.path.join(results_dir, rel))
            if run_data is not None:
                preloaded.setdefault(label, {})[template_id] = run_data
                continue
            # The run file is gone (deleted from Past runs): run
            # the pair again instead of showing a hole.
            status = "pending"
            pairs[key] = "pending"
        if status == "skipped":
            continue
        if target is None:
            missing_models[label] = missing_models.get(label, 0) + 1
            pairs[key] = "skipped"
            continue
        if template is None:
            missing_templates[template_id] = (
                missing_templates.get(template_id, 0) + 1)
            pairs[key] = "skipped"
            continue
        if status in ("pending", "failed"):
            pair_set.add((label, template_id))
    targets = []
    for entry in manifest.get("targets") or []:
        label = entry.get("label")
        target = targets_by_label.get(label)
        if target is None:
            continue
        if any(pair_label == label for pair_label, _tid in pair_set):
            targets.append(target)
    for label, count in sorted(missing_models.items()):
        notes.append(
            f"{label} is no longer installed, so its "
            f"{count} {'pair' if count == 1 else 'pairs'} "
            "were skipped."
        )
    for template_id, count in sorted(missing_templates.items()):
        template = templates_by_id.get(template_id)
        name = template["name"] if template else template_id
        notes.append(
            f"The {name} template is gone, so "
            f"{count} {'pair' if count == 1 else 'pairs'} "
            "were skipped."
        )
    return targets, pair_set, preloaded, notes


def merge_resume_result(result, extra_templates=None):
    """Fold a resumed run's done pairs into a model's result (v17):
    the templates dict gains the extra run files, and the overall
    score, average speeds, and token totals are recomputed over the
    whole run. The warmup tokens of the earlier part are not in any
    run file, so the token total covers the run files only. Mutates
    and returns the result."""
    templates = result.setdefault("templates", {})
    if extra_templates:
        for tid, run_data in extra_templates.items():
            templates.setdefault(tid, run_data)
    scores = [run["template_score"] for run in templates.values()
              if run.get("template_score") is not None]
    result["overall_score"] = (
        sum(scores) / len(scores) if scores else None)
    speeds = [task["decode_tok_s"] for run in templates.values()
              for task in run.get("tasks", [])
              if task.get("decode_tok_s") is not None]
    result["avg_decode_tok_s"] = (
        sum(speeds) / len(speeds) if speeds else None)
    ttfts = [task["ttft_s"] for run in templates.values()
             for task in run.get("tasks", [])
             if task.get("ttft_s") is not None]
    result["avg_ttft_s"] = sum(ttfts) / len(ttfts) if ttfts else None
    prompt_speeds = [task["prompt_tok_s"] for run in templates.values()
                     for task in run.get("tasks", [])
                     if task.get("prompt_tok_s") is not None]
    result["avg_prompt_tok_s"] = (
        sum(prompt_speeds) / len(prompt_speeds) if prompt_speeds else None)
    prompt_total = 0
    completion_total = 0
    saw_tokens = False
    for run_data in templates.values():
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
            result.get("price_input_per_1m"),
            result.get("price_output_per_1m"),
        )
    return result


def list_history_models(results_dir):
    """Model slugs that have result folders, sorted."""
    try:
        entries = os.listdir(results_dir)
    except OSError:
        return []
    return sorted(
        name for name in entries
        # v17: the _runs folder holds run manifests, not a model's
        # runs — it must never appear as a model in Past runs,
        # Profiles, or Compare.
        if os.path.isdir(os.path.join(results_dir, name))
        and name != "_runs"
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
                "speed": data.get("avg_decode_tok_s"),
            })
    runs.sort(key=lambda r: (r["timestamp"], r["filename"]), reverse=True)
    return runs


def format_run_headline(run):
    """R20: one run as one line — date, template, score, speed. The
    timeline scrub's readout and the hover preview share it, so the
    glance and the hover never disagree."""
    stamp = (run.get("timestamp") or "").replace("T", " ")
    score = run.get("score")
    if score is None:
        score_text = "no scoring"
    else:
        score_text = f"{score:.0f}%"
        if run.get("passed") is not None and run.get("total"):
            score_text += f" ({run['passed']}/{run['total']})"
    text = f"{stamp} - {run.get('template_name', '')}: {score_text}"
    speed = run.get("speed")
    if speed:
        text += f" at {speed:.1f} tok/s"
    return text


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
        f"{run_data.get('model', '?')}: {run_data.get('template_name', run_data.get('template', '?'))}",
        f"Run: {run_data.get('timestamp', '?')}",
        f"System: {run_data.get('system_id', '?')}",
        f"Provider: {run_data.get('provider', 'ollama')}",
    ]
    score = run_data.get("template_score")
    not_run = run_data.get("not_run", 0) or 0
    if score is None and not_run:
        lines.append(
            f"Score: not run ({not_run} "
            f"{'task' if not_run == 1 else 'tasks'} not run: "
            "test file missing)"
        )
    elif score is None:
        lines.append("Score: no scoring (speed only)")
    else:
        # v17: a speed-scored run counts lengths, not tasks.
        noun = "lengths" if run_data.get("score_by") == "speed" else "tasks"
        lines.append(
            f"Score: {score:.0f}% ({run_data.get('passed', '?')}/{run_data.get('total', '?')} {noun}"
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
        mark = task_mark(task)
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


def profile_numbers(results_dir, model_slug):
    """Every number a profile is built from, in one dict.

    Returns None when the model has no runs. The markdown profile,
    the bento tiles (R14), and the charts (R16) all read from this,
    so the three can never disagree about a model. Keys: model_name,
    provider, systems, last_tested, runs_count, token_prompt,
    token_completion, has_tokens, total_cost, has_cost,
    template_rows (name, latest, best, count, not_run per template),
    overall_latest, overall_best, avg_speed, strengths, weaknesses,
    template_scores (name, latest) for scored templates only, best
    first, and speed_points (timestamp, decode tok/s) per run, oldest
    first.
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
    template_scores = sorted(
        [(name, latest) for name, latest, _b, _n, _nr in template_rows
         if latest is not None],
        key=lambda pair: pair[1],
        reverse=True,
    )
    speed_points = [
        (r.get("timestamp", "?"), r["avg_decode_tok_s"])
        for r in runs
        if r.get("avg_decode_tok_s") is not None
    ]
    return {
        "model_name": model_name,
        "provider": provider,
        "systems": systems,
        "last_tested": last_tested,
        "runs_count": len(runs),
        "token_prompt": token_prompt,
        "token_completion": token_completion,
        "has_tokens": has_tokens,
        "total_cost": total_cost,
        "has_cost": has_cost,
        "template_rows": template_rows,
        "overall_latest": overall_latest,
        "overall_best": overall_best,
        "avg_speed": avg_speed,
        "strengths": strengths,
        "weaknesses": weaknesses,
        "template_scores": template_scores,
        "speed_points": speed_points,
    }


def build_profile(results_dir, model_slug):
    """Markdown profile for one model, built from all its run files.

    Returns (model_name, text), or None when the model has no runs.
    The numbers come from profile_numbers; this only renders them.
    """
    numbers = profile_numbers(results_dir, model_slug)
    if numbers is None:
        return None
    model_name = numbers["model_name"]
    provider = numbers["provider"]
    systems = numbers["systems"]
    last_tested = numbers["last_tested"]
    token_prompt = numbers["token_prompt"]
    token_completion = numbers["token_completion"]
    has_tokens = numbers["has_tokens"]
    total_cost = numbers["total_cost"]
    has_cost = numbers["has_cost"]
    template_rows = numbers["template_rows"]
    overall_latest = numbers["overall_latest"]
    overall_best = numbers["overall_best"]
    avg_speed = numbers["avg_speed"]
    strengths = numbers["strengths"]
    weaknesses = numbers["weaknesses"]

    lines = [
        f"# {model_name}",
        "",
        f"Profile updated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"Last tested: {last_tested}",
        f"Runs: {numbers['runs_count']}",
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
        lines.append("None yet: no template at 80% or better.")
    lines.append("")
    lines.append("## Weaknesses")
    lines.append("")
    if weaknesses:
        for name in weaknesses:
            lines.append(f"- {name}")
    else:
        lines.append("None yet: no template under 50%.")
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
                    "not run: test file missing)"
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
                        f", time budget reached "
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
    shows "n/a" in that column. No answers and no API keys — only the
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
            row.append(template_score_text(run) if run is not None else "n/a")
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
        "<title>OLLAMA SYSTEM TEST results</title>",
        "<style>",
        "body { font-family: sans-serif; }",
        "table { border-collapse: collapse; }",
        "th, td { border: 1px solid #999; padding: 4px 8px; "
        "text-align: left; }",
        "th { background: #eee; }",
        "</style>",
        "</head>",
        "<body>",
        "<h1>OLLAMA SYSTEM TEST results</h1>",
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
    # R12: the run's total step count when it starts, and the step
    # count so far as each step finishes. The window turns these into
    # an honest "N of M steps, about X left" line — never a fake
    # progress bar.
    run_started = Signal(int)
    step_progress = Signal(int, int)
    # "done", "cancelled", or "ollama_down"
    run_finished = Signal(str)

    def __init__(self, targets, templates, sys_id, machine, results_dir,
                 time_budget_seconds=None, pairs=None, models_folder=None,
                 manifest=None):
        super().__init__()
        # R24: a re-run of failed pairs only runs the pairs in the
        # set — (model label, template id). A target with no pairs
        # never starts (no warmup, no row).
        self.pairs = None if pairs is None else set(pairs)
        if self.pairs is not None:
            targets = [t for t in targets
                       if any(label == t["label"]
                              for label, _tid in self.pairs)]
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
        # v17: the saved models folder, passed to the Ollama a
        # mid-run restart starts; whether this run has already used
        # its one restart; and the model sizes for the RAM check,
        # fetched once when the run starts.
        self._models_folder = models_folder
        self._ollama_restarted = False
        self._model_sizes = {}
        self._total_steps = 0
        self._done_steps = 0
        # v17: this run's manifest (created by the window, or by the
        # worker itself when none was passed). The worker owns it
        # from here: every pair updates it, every ending finishes it.
        self._manifest = manifest

    def _save_manifest(self):
        """Write the manifest now, when there is one. A failed write
        never stops a run — the manifest is a convenience, not the
        run itself."""
        if self._manifest is None:
            return
        try:
            save_manifest(self.results_dir, self._manifest)
        except OSError:
            pass

    def _finish_manifest(self, status):
        """Mark the manifest finished with its end status. A manifest
        with pending or failed pairs left stays resumable, whatever
        the end status says."""
        if self._manifest is None:
            return
        self._manifest["status"] = status
        self._manifest["finished_at"] = (
            datetime.now().isoformat(timespec="seconds"))
        self._save_manifest()

    def _set_pair_status(self, label, template_id, status, run_file=None):
        """Mark one pair in the manifest (v17) and save it at once:
        a crash after this pair still leaves the manifest true."""
        if self._manifest is None:
            return
        self._manifest.setdefault("pairs", {})[
            pair_key(label, template_id)] = status
        if run_file is not None:
            self._manifest.setdefault("run_files", {})[
                pair_key(label, template_id)] = run_file
        self._save_manifest()

    def _fail_model_pairs(self, label):
        """Mark one model's unfinished pairs failed (v17): the model
        itself errored, so a resume retries them. Done pairs stay
        done; skipped pairs stay skipped."""
        if self._manifest is None:
            return
        pairs = self._manifest.get("pairs") or {}
        changed = False
        for key, status in pairs.items():
            if status in ("pending", "failed"):
                pair_label, _tid = split_pair_key(key)
                if pair_label == label:
                    pairs[key] = "failed"
                    changed = True
        if changed:
            self._save_manifest()

    def _end_run(self, reason):
        """End the run: mark the manifest finished and tell the window."""
        self._finish_manifest(reason)
        self.run_finished.emit(reason)

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

    def _try_restart_ollama(self, target):
        """Restart a dead local Ollama once per run (v17). Returns
        True when a restart was attempted and Ollama answers again.
        Provider targets and remote addresses never trigger it, and
        a second death in the same run fails without another restart.
        Runs on the worker thread, so the up-to-30-second wait does
        not freeze the window; the progress line says what is
        happening."""
        if target.get("kind") != "ollama":
            return False
        if not ollama_address_is_local():
            return False
        if self._ollama_restarted:
            return False
        self._ollama_restarted = True
        percent = (int(100 * self._done_steps / self._total_steps)
                   if self._total_steps else 0)
        self.progress.emit("Ollama stopped, restarting it...", percent)
        log_event("Ollama stopped during the run; restarting it once.")
        ok, _message = start_ollama_process(self._models_folder)
        return ok

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
            try:
                return self._call_once(target, prompt, num_predict)
            except OllamaError as exc2:
                # v17: Ollama may have died loading the model (out of
                # memory on a small box). Restart it once per run and
                # try once more; if that fails, fail as before.
                if (is_unreachable(exc2)
                        and self._try_restart_ollama(target)):
                    return self._call_once(target, prompt, num_predict)
                raise

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
            try:
                return self._call_chat(target, messages, num_predict, tools)
            except OllamaError as exc2:
                # v17: same mid-run restart as _generate — Ollama may
                # have died loading the model. Once per run, then fail.
                if (is_unreachable(exc2)
                        and self._try_restart_ollama(target)):
                    return self._call_chat(target, messages, num_predict,
                                           tools)
                raise

    def _count_steps(self):
        """Progress steps: one warmup per target, then one step per
        model call on the eligible templates only. See
        count_run_steps — the worker and the run preview share it."""
        return count_run_steps(self.targets, self.templates, self.pairs)

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
            # M4: partial credit — 1.0 for a pass, 0.0 for a fail,
            # a fraction for a numeric task with steps.
            "credit": (
                task_credit(task, metrics["answer"], passed)
                if task is not None else 0.0
            ),
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
            passed, detail = score_task(task, transcript_words(transcript))
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
        if template["scored"] or template.get("score_by") == "speed":
            ran = [t for t in task_results
                   if not is_not_run(t) and not is_budget_stopped(t)]
            if template.get("score_by") == "speed":
                # v17: Speed scores decode speed, not answers — each
                # context length earns a credit from its own tok/s,
                # so a fast short run cannot mask a slow long one.
                # Failed jobs earn 0; jobs that never ran stay out.
                for t in ran:
                    t["credit"] = speed_credit(t.get("decode_tok_s"))
            # M4: the score is over credits, not passes — a task
            # with steps earns a fraction per part right, so a
            # half-right task counts half. Tasks without a credit
            # field (older builders) count 1.0 for a pass, 0.0
            # for a fail. "Passed" is the credit sum, whole when
            # nothing was partial, so "(4.5/5)" reads true.
            credit_sum = sum(
                t.get("credit", 1.0 if t["passed"] is True else 0.0)
                for t in ran
            )
            total = len(ran)
            score = 100.0 * credit_sum / total if total else None
            if total == 0:
                passed_count = None
            elif credit_sum == int(credit_sum):
                passed_count = int(credit_sum)
            else:
                passed_count = credit_sum
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
        # v17: a speed-scored run says so, so the summary can call
        # its items lengths instead of tasks.
        if template.get("score_by"):
            run_data["score_by"] = template["score_by"]
        path = save_run_file(self.results_dir, label, self.sys_id, run_data, stamp=stamp)
        return run_data, path

    def _bump_progress(self, label, template, job_id):
        self._done_steps += 1
        percent = int(100 * self._done_steps / self._total_steps)
        self.progress.emit(
            f"{label}: {template['name']} - {job_id}", min(percent, 100)
        )
        self.step_progress.emit(self._done_steps, self._total_steps)

    def _ram_skip_reason(self, target):
        """v17: why a model must not load, or None when it fits (or
        the check cannot run). Need is the file size times
        RAM_NEED_FACTOR, against MemAvailable read fresh for this
        model. Provider targets never reach here — the caller checks
        the kind first. An unreadable size or meminfo means proceed
        without the check, never block a run on a failed read."""
        size = self._model_sizes.get(target["model"])
        if size is None:
            return None
        available = mem_available_bytes()
        if available is None:
            return None
        need = int(size * RAM_NEED_FACTOR)
        if need <= available:
            return None
        return (f"skipped: needs ~{fmt_bytes(need)} RAM, "
                f"{fmt_bytes(available)} free. "
                "Close other apps and run again.")

    def run(self):
        self._total_steps = self._count_steps()
        self._done_steps = 0
        # v17: model sizes for the RAM check, once per run. {} when
        # Ollama does not answer — the check then stays off.
        self._model_sizes = list_model_sizes()
        # v17: the manifest for this run — the one passed in (a
        # resume or a re-run), or a fresh one. A resumed manifest
        # goes back to running with its pairs as they stood.
        if self._manifest is None:
            self._manifest = new_manifest(
                new_run_id(self.results_dir), self.targets,
                self.templates, self.sys_id)
        else:
            self._manifest["status"] = "running"
            self._manifest["finished_at"] = None
        self._save_manifest()
        self.run_started.emit(self._total_steps)
        for target in self.targets:
            label = target["label"]
            if self._stop:
                self._end_run("cancelled")
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
                "avg_ttft_s": None,
                "avg_prompt_tok_s": None,
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
            # v17: a model too big for the free RAM is skipped, not
            # run into an out-of-memory kill that takes Ollama down
            # with it. No run files, no scores — the run carries on
            # with the next model. The steps still count, so the
            # progress line stays honest.
            if target["kind"] == "ollama":
                skip_reason = self._ram_skip_reason(target)
                if skip_reason is not None:
                    result["status"] = skip_reason
                    log_event(f"{label}: {skip_reason}")
                    self._done_steps += count_run_steps(
                        [target], self.templates, self.pairs)
                    self.progress.emit(
                        f"{label}: skipped, not enough free RAM",
                        int(100 * self._done_steps / self._total_steps),
                    )
                    self.step_progress.emit(
                        self._done_steps, self._total_steps)
                    self.model_done.emit(label, result)
                    continue
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
                # The warmup is a counted step too: report it, so the
                # "N of M steps" line is right from the first step.
                self.step_progress.emit(self._done_steps, self._total_steps)
            except Cancelled:
                self._end_run("cancelled")
                return
            except OllamaError as exc:
                if is_unreachable(exc):
                    # The warmup retry already failed: Ollama is gone.
                    result["status"] = f"failed: {describe_error(exc)}"
                    self.model_done.emit(label, result)
                    self._end_run("ollama_down")
                    return
                # The model itself failed: its pairs failed too, so a
                # resume retries them.
                self._fail_model_pairs(label)
                result["status"] = f"failed: {exc}"
                self.model_done.emit(label, result)
                continue
            except ProviderError as exc:
                # The provider did not answer or rejected the key: this
                # model fails plainly and the run carries on with the
                # rest, including the local Ollama models.
                self._fail_model_pairs(label)
                result["status"] = f"failed: {exc}"
                self.model_done.emit(label, result)
                continue
            try:
                mine = [t for t in self.templates
                        if self.pairs is None
                        or (label, t["id"]) in self.pairs]
                for template in mine:
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
                        # v17: resolved, never to run — a resume must
                        # not offer it again.
                        self._set_pair_status(
                            label, template["id"], "skipped")
                        continue
                    run_data, path = self._run_template(target, template)
                    result["templates"][template["id"]] = run_data
                    result["templates"][template["id"]]["run_file"] = path
                    # v17: done, with the run file recorded so a
                    # resume loads it instead of running it again.
                    self._set_pair_status(label, template["id"], "done",
                                          run_file=os.path.relpath(
                                              path, self.results_dir))
                    self.template_done.emit(label, run_data, path)
            except Cancelled:
                self._end_run("cancelled")
                return
            except OllamaUnreachable as exc:
                log_event(f"Ollama stopped responding during the run: {exc}")
                result["status"] = f"failed: {exc}"
                self.model_done.emit(label, result)
                self._end_run("ollama_down")
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
            # R15: the same average, for first-token latency and prompt
            # speed, so the results table can show them. Multi-turn
            # tasks record None for both (they are several calls), so
            # they simply drop out of the average.
            ttfts = [
                task["ttft_s"]
                for run in result["templates"].values()
                for task in run["tasks"]
                if task.get("ttft_s") is not None
            ]
            if ttfts:
                result["avg_ttft_s"] = sum(ttfts) / len(ttfts)
            prompt_speeds = [
                task["prompt_tok_s"]
                for run in result["templates"].values()
                for task in run["tasks"]
                if task.get("prompt_tok_s") is not None
            ]
            if prompt_speeds:
                result["avg_prompt_tok_s"] = (
                    sum(prompt_speeds) / len(prompt_speeds)
                )
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
        self._end_run("done")


# ---------------------------------------------------------------------------
# Ollama starter (runs `ollama serve` detached, then waits for it)
# ---------------------------------------------------------------------------

def start_ollama_process(models_folder=None):
    """Start `ollama serve` detached and wait for it to answer.

    Returns (True, "") when Ollama answers, else (False, message).
    The OllamaStarter thread, the mid-run restart, and headless mode
    all share this. Already-running returns True at once, without
    launching a second server. The saved models folder goes to the
    child as OLLAMA_MODELS (R18) — unless the environment already
    sets it, in which case the child inherits it and this stays out.
    """
    if ollama_reachable():
        return True, ""
    env = None
    if models_folder and "OLLAMA_MODELS" not in os.environ:
        env = dict(os.environ)
        env["OLLAMA_MODELS"] = models_folder
    try:
        subprocess.Popen(
            ["ollama", "serve"],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
    except FileNotFoundError:
        return False, START_OLLAMA_MISSING_MSG
    except OSError as exc:
        return False, f"Could not start Ollama: {exc}"
    deadline = time.monotonic() + START_OLLAMA_TIMEOUT
    while time.monotonic() < deadline:
        if ollama_reachable():
            return True, ""
        time.sleep(START_OLLAMA_POLL)
    return False, START_OLLAMA_TIMEOUT_MSG


class OllamaStarter(QThread):
    started_ok = Signal()
    failed = Signal(str)  # plain message

    def __init__(self, models_folder=None):
        super().__init__()
        # R18: the saved models folder, passed to the Ollama this app
        # starts — unless the environment already sets OLLAMA_MODELS,
        # in which case the child inherits it and this stays out.
        self.models_folder = models_folder

    def run(self):
        ok, message = start_ollama_process(self.models_folder)
        if ok:
            self.started_ok.emit()
        else:
            self.failed.emit(message)


# ---------------------------------------------------------------------------
# Provider connection tester (GET /v1/models in its own thread)
# ---------------------------------------------------------------------------

class ProviderTestWorker(QThread):
    tested = Signal(bool, str, list)  # ok, plain message, fetched model ids

    def __init__(self, provider):
        super().__init__()
        self.provider = provider

    def run(self):
        ok, model_ids, message = provider_fetch_models(self.provider)
        self.tested.emit(ok, message, model_ids)


# ---------------------------------------------------------------------------
# Model puller (streams /api/pull progress in its own thread)
# ---------------------------------------------------------------------------

class PullWorker(QThread):
    progress = Signal(int, str)  # percent 0-100, or -1 when unknown; status text
    finished_ok = Signal(str)    # model name
    failed = Signal(str)         # plain message
    cancelled = Signal(str)      # model name

    def __init__(self, name):
        super().__init__()
        self.name = name
        self._cancelled = False
        self._resp = None
        # The last progress Ollama reported — the window keeps it so
        # a stopped pull can be resumed (R19) and named honestly.
        self.last_completed = 0
        self.last_total = 0

    def cancel(self):
        """Stop the pull (R19). The flag ends the read loop at the
        next line or the next socket timeout; closing the response
        ends a stalled read at once."""
        self._cancelled = True
        resp = self._resp
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass

    def is_stopping(self):
        """True once cancel() was called (v18): the worker is on its
        way out, and a re-pull should queue behind it, not be
        refused as already pulling."""
        return self._cancelled

    def _rate_text(self, samples, completed, total):
        """Speed and remaining time from the recent progress samples:
        (monotonic time, completed bytes). Needs at least half a
        second of movement, or there is no rate worth showing."""
        if len(samples) < 2:
            return ""
        first_time, first_done = samples[0]
        last_time, last_done = samples[-1]
        span = last_time - first_time
        moved = last_done - first_done
        if span < 0.5 or moved <= 0:
            return ""
        rate = moved / span
        eta = (total - completed) / rate if total > completed else 0
        return (
            f", {rate / 1_000_000:.1f} MB/s, "
            f"about {fmt_eta(eta)} left"
        )

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
        self._resp = resp
        last_chunk = time.monotonic()
        # Recent (time, completed) samples for the speed estimate —
        # the last 10 seconds of movement, oldest first.
        samples = deque(maxlen=40)
        try:
            while True:
                if self._cancelled:
                    self.cancelled.emit(self.name)
                    return
                try:
                    line = resp.readline()
                except socket.timeout:
                    if self._cancelled:
                        self.cancelled.emit(self.name)
                        return
                    if time.monotonic() - last_chunk > STALL_TIMEOUT:
                        self.failed.emit("Ollama stopped responding")
                        return
                    continue
                except (OSError, ValueError, AttributeError):
                    # ValueError: the response was closed under us by
                    # cancel() — a stop, not a failure. AttributeError:
                    # the same close racing the read ('NoneType' has
                    # no 'peek' once the buffer is gone) — also a stop.
                    if self._cancelled:
                        self.cancelled.emit(self.name)
                    else:
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
                    self.last_completed = completed or 0
                    self.last_total = total
                    now = time.monotonic()
                    samples.append((now, self.last_completed))
                    while samples and now - samples[0][0] > 10.0:
                        samples.popleft()
                    percent = int(100 * self.last_completed / total)
                    # v18 (RV6): the status Ollama sends here is the
                    # layer's blob hash ("pulling 7f4030143c1c") —
                    # show the model name instead.
                    text = (
                        f"Pulling {self.name}: "
                        f"{self.last_completed / 1_000_000:.1f} MB "
                        f"of {total / 1_000_000:.1f} MB"
                        f"{self._rate_text(samples, self.last_completed, total)}"
                    )
                else:
                    percent = -1
                    text = status
                self.progress.emit(percent, text)
        except Exception:
            # v18 (RV2): never die silent. The window clears the
            # pull slot only on a signal, so an unreported death
            # wedges pulling until the app restarts.
            if self._cancelled:
                self.cancelled.emit(self.name)
            else:
                self.failed.emit(
                    "The pull stopped unexpectedly. Try again.")
            return
        finally:
            self._resp = None
            try:
                resp.close()
            except Exception:
                pass
        if self._cancelled:
            self.cancelled.emit(self.name)
            return
        self.failed.emit("The pull ended without finishing. Try again.")


class TryItWorker(QThread):
    """One question to one model (R24): a single generate call in its
    own thread, so the window never waits on it. Emits (ok, text):
    the model's answer, or a plain reason when it did not answer."""

    answered = Signal(bool, str)

    def __init__(self, model, question):
        super().__init__()
        self.model = model
        self.question = question

    def run(self):
        try:
            metrics = generate_once(
                self.model, self.question, TRYIT_PREDICT, TEMPERATURE
            )
        except Cancelled:
            return
        except OllamaError as exc:
            self.answered.emit(
                False, f"Could not ask {self.model}: {describe_error(exc)}"
            )
            return
        answer = (metrics.get("answer") or "").strip()
        if not answer:
            answer = f"{self.model} answered with nothing."
        self.answered.emit(True, answer)


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
    "Avg TTFT",
    "Avg prompt tok/s",
    "Status",
)


class RunPreviewDialog(QDialog):
    """R11: the run preview (library 017 — see it before you commit).

    Shows the run's plan in plain text — models, templates, steps,
    an upper-bound time estimate, and the provider cost when there
    is one — with Run (the default) and Adjust. Adjust closes the
    dialog and leaves the setup lists untouched, so the user can
    change the ticks and try again."""

    def __init__(self, parent, preview_text):
        super().__init__(parent)
        self.setWindowTitle("Before you run")
        self.resize(520, 420)
        layout = QVBoxLayout(self)
        intro = QLabel(
            "This is what the run will do. Check it, then press Run."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.text_view = QTextEdit()
        self.text_view.setReadOnly(True)
        self.text_view.setPlainText(preview_text)
        layout.addWidget(self.text_view)
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        self.run_button = buttons.button(QDialogButtonBox.Ok)
        self.run_button.setText("Run")
        self.run_button.setDefault(True)
        self.adjust_button = buttons.button(QDialogButtonBox.Cancel)
        self.adjust_button.setText("Adjust")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


def render_template_preview(template):
    """R24: a template's tasks as plain text, for the preview dialog
    — what the template asks, before the user runs it. Prompts are
    cut at 300 characters: enough to judge the task, short enough
    to read. Chat tasks show their turn count; every task shows its
    expected answer when it has one."""
    lines = [template.get("name", template.get("id", "template"))]
    description = template.get("description") or ""
    if description:
        lines.append(description)
    capability = template.get("capability", "text")
    scored = "scored" if template.get("scored", True) else "not scored"
    lines.append(
        f"Needs: {capability}. {scored}. "
        f"Up to {template.get('num_predict', 200)} tokens per answer."
    )
    tasks = template.get("tasks") or []
    lines.append("")
    lines.append(
        f"{len(tasks)} {'task' if len(tasks) == 1 else 'tasks'}:"
    )
    for index, task in enumerate(tasks):
        kind = task.get("kind", "generate")
        head = f"  {task.get('id', '?')}"
        if kind != "generate":
            head += f" ({kind})"
        lines.append(head)
        prompt = task.get("prompt") or ""
        if not prompt and task.get("turns"):
            first = task["turns"][0] or {}
            prompt = first.get("prompt") or first.get("content", "")
            lines.append(f"    {len(task['turns'])} turns")
        if prompt:
            shown = prompt if len(prompt) <= 300 else prompt[:300] + "..."
            lines.append(f"    {shown}")
        if "expected" in task:
            lines.append(f"    Expected: {task['expected']}")
        if index < len(tasks) - 1:
            # v18 (RV12): a blank line between tasks, so they scan.
            lines.append("")
    return "\n".join(lines)


class TemplatePreviewDialog(QDialog):
    """R24: read a template's tasks before running it. Read-only —
    the preview is for judging, not editing."""

    def __init__(self, parent, template):
        super().__init__(parent)
        self.setWindowTitle(
            f"Template: {template.get('name', template.get('id', ''))}"
        )
        self.resize(520, 420)
        layout = QVBoxLayout(self)
        self.text_view = QTextEdit()
        self.text_view.setReadOnly(True)
        self.text_view.setPlainText(render_template_preview(template))
        layout.addWidget(self.text_view)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


# ---------------------------------------------------------------------------
# Charts (R16, library 037) — plain Qt painting, no new dependency.
#
# One chart, one message, readable at a glance: bars for comparing
# amounts (per-template scores, 0-based axis, direct value labels),
# a line for change over time (decode speed across runs). Colors come
# from the widget palette, so dark mode works; the numbers are always
# printed next to the marks, so color never carries meaning alone.
# ---------------------------------------------------------------------------

def _nice_step(max_value, steps=4):
    """Grid step for a chart axis: max_value split into about four
    intuitive steps (1, 2, 5, or 10 times a power of ten)."""
    if max_value <= 0:
        return 1.0
    raw = max_value / steps
    power = 1.0
    while power * 10 <= raw:
        power *= 10
    while power > raw:
        power /= 10
    for mult in (1, 2, 5, 10):
        if mult * power >= raw:
            return mult * power
    return 10 * power


def _short_date(timestamp):
    """An ISO timestamp as a short chart label ("Nov 13")."""
    try:
        return datetime.fromisoformat(str(timestamp)).strftime("%b %d")
    except ValueError:
        return str(timestamp)[:10]


class BarChartWidget(QWidget):
    """Horizontal bars for comparing amounts (library 037).

    set_data takes rows of (label, value) for one series, or
    (label, value_a, value_b) for two series side by side (the
    Compare tab: top bar is the first model, bottom bar the second).
    Values are 0 to 100 scores; the axis is fixed at 0 so a bar twice
    as long really means twice as much. takeaway_title states the
    answer in plain words, above the chart.
    """

    ROW_HEIGHT = 26
    BAR_HEIGHT = 14

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = []
        self.pair_names = None
        self.setMinimumHeight(80)

    def set_data(self, rows, pair_names=None):
        self.rows = list(rows)
        self.pair_names = pair_names
        self.updateGeometry()
        self.update()

    def takeaway_title(self):
        if not self.rows:
            return "No scored templates yet."
        if self.pair_names is None:
            scored = [(label, value) for label, value in self.rows]
            best_label, best = max(scored, key=lambda pair: pair[1])
            worst_label, worst = min(scored, key=lambda pair: pair[1])
            if best == worst:
                return f"Every template at {best:.0f}%."
            return (
                f"Strongest: {best_label} {best:.0f}%. "
                f"Weakest: {worst_label} {worst:.0f}%."
            )
        name_a, name_b = self.pair_names
        wins_a = wins_b = 0
        for _label, value_a, value_b in self.rows:
            if value_a is None or value_b is None:
                continue
            if value_a > value_b:
                wins_a += 1
            elif value_b > value_a:
                wins_b += 1
        if wins_a == wins_b:
            return f"{name_a} and {name_b} are even across the templates."
        leader, lead = (
            (name_a, wins_a) if wins_a > wins_b else (name_b, wins_b)
        )
        noun = "template" if lead == 1 else "templates"
        return f"{leader} leads on {lead} {noun}."

    def sizeHint(self):
        per_row = self.ROW_HEIGHT * (2 if self.pair_names else 1)
        height = len(self.rows) * per_row + 34
        return QSize(320, max(80, height))

    def paintEvent(self, _event):
        painter = QPainter(self)
        palette = self.palette()
        painter.fillRect(self.rect(), palette.color(QPalette.Window))
        if not self.rows:
            painter.setPen(palette.color(QPalette.Text))
            painter.drawText(self.rect(), Qt.AlignCenter,
                             "No scored templates yet.")
            return
        text_color = palette.color(QPalette.Text)
        grid_color = palette.color(QPalette.Mid)
        bar_color = palette.color(QPalette.Highlight)
        bar_b_color = palette.color(QPalette.Dark)
        metrics = painter.fontMetrics()
        label_width = 0
        for row in self.rows:
            label_width = max(
                label_width, metrics.horizontalAdvance(row[0] + "  ")
            )
        label_width = min(label_width, self.width() // 3)
        value_width = metrics.horizontalAdvance("100%  ")
        left = 4 + label_width
        right = self.width() - 4 - value_width
        bar_span = max(1, right - left)
        axis_y = self.height() - 20
        # Grid: 0, 25, 50, 75, 100 — intuitive steps for a percent
        # scale, drawn first so the bars sit on top of them.
        painter.setPen(QPen(grid_color))
        for percent in (0, 25, 50, 75, 100):
            x = left + bar_span * percent // 100
            painter.drawLine(x, 4, x, axis_y)
            label = f"{percent}%" if percent == 100 else str(percent)
            painter.setPen(text_color)
            painter.drawText(
                QRect(x - 14, axis_y + 2, 28, 16),
                Qt.AlignHCenter | Qt.AlignTop, label,
            )
            painter.setPen(QPen(grid_color))
        per_row = self.ROW_HEIGHT * (2 if self.pair_names else 1)
        for index, row in enumerate(self.rows):
            label = row[0]
            top = 6 + index * per_row
            painter.setPen(text_color)
            painter.drawText(
                QRect(4, top, label_width - 4, per_row),
                Qt.AlignLeft | Qt.AlignVCenter,
                metrics.elidedText(
                    label, Qt.ElideRight, label_width - 4),
            )
            if self.pair_names is None:
                value = row[1]
                if value is None:
                    continue
                width = int(bar_span * max(0.0, min(100.0, value)) / 100)
                painter.setPen(Qt.NoPen)
                painter.setBrush(bar_color)
                painter.drawRect(
                    QRect(left, top + (self.ROW_HEIGHT - self.BAR_HEIGHT) // 2,
                          max(1, width), self.BAR_HEIGHT)
                )
                painter.setPen(text_color)
                painter.drawText(
                    QRect(left + width + 4, top, value_width,
                          self.ROW_HEIGHT),
                    Qt.AlignLeft | Qt.AlignVCenter, f"{value:.0f}%",
                )
            else:
                for series, value in ((0, row[1]), (1, row[2])):
                    if value is None:
                        continue
                    width = int(
                        bar_span * max(0.0, min(100.0, value)) / 100
                    )
                    painter.setPen(Qt.NoPen)
                    painter.setBrush(
                        bar_color if series == 0 else bar_b_color
                    )
                    row_top = top + series * self.ROW_HEIGHT
                    painter.drawRect(
                        QRect(
                            left,
                            row_top + (self.ROW_HEIGHT - self.BAR_HEIGHT) // 2,
                            max(1, width), self.BAR_HEIGHT,
                        )
                    )
                    painter.setPen(text_color)
                    painter.drawText(
                        QRect(left + width + 4, row_top, value_width,
                              self.ROW_HEIGHT),
                        Qt.AlignLeft | Qt.AlignVCenter, f"{value:.0f}%",
                    )


class LineChartWidget(QWidget):
    """A line for change over time (library 037): decode speed across
    runs, oldest first. The vertical axis starts at 0, grid lines sit
    at intuitive steps, and the takeaway title states the trend in
    plain words."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.points = []
        self.setMinimumHeight(120)

    def set_data(self, points):
        self.points = list(points)
        self.update()

    def takeaway_title(self):
        if not self.points:
            return "No runs with speed yet."
        if len(self.points) == 1:
            return f"One run so far: {self.points[0][1]:.1f} tok/s."
        first = self.points[0][1]
        last = self.points[-1][1]
        if first > 0 and abs(last - first) / first <= 0.10:
            return f"Speed is steady around {last:.1f} tok/s."
        if last > first:
            return f"Speed rose from {first:.1f} to {last:.1f} tok/s."
        return f"Speed fell from {first:.1f} to {last:.1f} tok/s."

    def sizeHint(self):
        return QSize(320, 150)

    def paintEvent(self, _event):
        painter = QPainter(self)
        palette = self.palette()
        painter.fillRect(self.rect(), palette.color(QPalette.Window))
        if not self.points:
            painter.setPen(palette.color(QPalette.Text))
            painter.drawText(self.rect(), Qt.AlignCenter,
                             "No runs with speed yet.")
            return
        text_color = palette.color(QPalette.Text)
        grid_color = palette.color(QPalette.Mid)
        line_color = palette.color(QPalette.Highlight)
        metrics = painter.fontMetrics()
        max_value = max(value for _label, value in self.points)
        step = _nice_step(max_value)
        top_value = step * 4
        while top_value < max_value:
            top_value += step
        label_width = metrics.horizontalAdvance(f"{top_value:.0f}  ")
        left = 4 + label_width
        right = self.width() - 8
        top = 16
        bottom = self.height() - 22
        plot_height = max(1, bottom - top)
        # Grid lines at intuitive steps, labelled on the left.
        painter.setPen(QPen(grid_color))
        grid_value = 0.0
        while grid_value <= top_value + 0.001:
            y = bottom - int(plot_height * grid_value / top_value)
            painter.drawLine(left, y, right, y)
            painter.setPen(text_color)
            painter.drawText(
                QRect(4, y - 8, label_width - 2, 16),
                Qt.AlignRight | Qt.AlignVCenter,
                f"{grid_value:.0f}",
            )
            painter.setPen(QPen(grid_color))
            grid_value += step
        count = len(self.points)
        xs = []
        for index in range(count):
            if count == 1:
                xs.append((left + right) // 2)
            else:
                xs.append(
                    left + (right - left) * index // (count - 1)
                )
        ys = [
            bottom - int(plot_height * value / top_value)
            for _label, value in self.points
        ]
        painter.setPen(QPen(line_color, 2))
        for index in range(count - 1):
            painter.drawLine(xs[index], ys[index], xs[index + 1], ys[index + 1])
        painter.setBrush(line_color)
        for index in range(count):
            painter.drawEllipse(xs[index] - 3, ys[index] - 3, 6, 6)
        # Value labels on every dot when there are few, first and last
        # only when there are many — clutter helps nobody.
        label_at = set(range(count)) if count <= 8 else {0, count - 1}
        painter.setPen(text_color)
        for index in label_at:
            value = self.points[index][1]
            text = f"{value:.1f}"
            width = metrics.horizontalAdvance(text)
            x = min(max(xs[index] - width // 2, left), right - width)
            painter.drawText(QRect(x, ys[index] - 16, width, 14),
                             Qt.AlignLeft, text)
        # Date labels: first, middle, last (all of them when few).
        date_at = set(range(count)) if count <= 5 else {
            0, count // 2, count - 1,
        }
        for index in sorted(date_at):
            text = self.points[index][0]
            width = metrics.horizontalAdvance(text)
            x = min(max(xs[index] - width // 2, left), right - width)
            painter.drawText(
                QRect(x, bottom + 4, width, 16), Qt.AlignLeft, text)


def make_bento_tile(caption, hero=False):
    """One bento tile (library 005): a rounded frame with a small
    caption and one big value. Returns (frame, caption_label,
    value_label); the caller writes the value (and, on Compare, the
    caption, which is the model name). Sizes mean something: the hero
    tile's value is bigger, and the caller gives it more grid space."""
    frame = QFrame()
    frame.setObjectName("bentoTile")
    frame.setStyleSheet(
        "QFrame#bentoTile { border: 1px solid gray; "
        "border-radius: 12px; padding: 6px; }"
    )
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(8, 6, 8, 6)
    caption_label = QLabel(caption)
    caption_font = caption_label.font()
    caption_font.setPointSize(max(8, caption_font.pointSize() - 1))
    caption_label.setFont(caption_font)
    caption_label.setWordWrap(True)
    layout.addWidget(caption_label)
    value_label = QLabel("n/a")
    value_label.setWordWrap(True)
    value_font = value_label.font()
    value_font.setBold(True)
    value_font.setPointSize(
        value_font.pointSize() + (8 if hero else 4)
    )
    value_label.setFont(value_font)
    layout.addWidget(value_label)
    layout.addStretch()
    return frame, caption_label, value_label


def _explain(text):
    """A small grey explanation line under a field (R17): plain words
    saying what the field is for. Disabled so the style paints it
    greyed in either theme — it takes no input, so disabling it
    changes nothing but the color."""
    label = QLabel(text)
    label.setWordWrap(True)
    label.setEnabled(False)
    return label


class SettingsDialog(QDialog):
    """Settings (R18, R21): the models folder, the Ollama address,
    the default time budget, the accessibility preset (bigger text,
    reduce motion), and the per-model usual template sets — in the
    app, not env-only. Built from a load_app_settings dict;
    result_values holds the new dict when the dialog is accepted.
    A field whose variable is set in the environment shows the
    environment's value, locked: the environment wins, always, and
    the stored setting is kept untouched for when it goes away."""

    def __init__(self, current, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        # v18 (RV11): tall enough that the fields fit at the default
        # size with no scrollbar (measured: the content needs 611 px
        # at the default font; 620 leaves a margin and still fits a
        # 768 px screen with its title bar); bigger text still scrolls.
        self.resize(520, 620)
        self._current = dict(current)
        self._template_sets = dict(current.get("template_sets") or {})
        self.result_values = None
        # R21: the fields live in a scroll area, not straight in the
        # dialog — at the bigger text sizes the content is taller
        # than the dialog, and a fixed stack clips the explanation
        # labels instead of scrolling. The Save/Cancel buttons stay
        # outside it, always visible.
        layout = QVBoxLayout(self)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        scroll = QScrollArea()
        scroll.setWidget(content)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.NoFrame)
        layout.addWidget(scroll)
        layout = content_layout
        layout.addWidget(QLabel(
            # v18 (RV11): plain words, no jargon about environments.
            "Settings for this app. They stay on this PC."
        ))
        layout.addWidget(QLabel("Models folder:"))
        folder_row = QHBoxLayout()
        self.models_folder_input = QLineEdit()
        self.models_folder_input.setPlaceholderText(
            "Leave empty for where Ollama keeps its models"
        )
        folder_row.addWidget(self.models_folder_input)
        self.browse_button = QPushButton("Browse")
        self.browse_button.clicked.connect(self._browse_folder)
        folder_row.addWidget(self.browse_button)
        layout.addLayout(folder_row)
        if _ENV_OLLAMA_MODELS is not None:
            self.models_folder_input.setText(_ENV_OLLAMA_MODELS)
            self.models_folder_input.setEnabled(False)
            self.browse_button.setEnabled(False)
            layout.addWidget(_explain(
                "OLLAMA_MODELS is set outside the app, so this "
                "setting is ignored while that is set."
            ))
        else:
            self.models_folder_input.setText(
                current.get("models_folder", "")
            )
            layout.addWidget(_explain(
                "Where Ollama keeps its models. Takes effect when "
                "this app starts Ollama; if Ollama is already "
                "running, restart it for this to apply."
            ))
        layout.addWidget(QLabel("Ollama address:"))
        self.ollama_host_input = QLineEdit()
        self.ollama_host_input.setPlaceholderText(
            "e.g. http://localhost:11434 (empty means the default)"
        )
        layout.addWidget(self.ollama_host_input)
        if _ENV_OLLAMA_HOST is not None:
            self.ollama_host_input.setText(_ENV_OLLAMA_HOST)
            self.ollama_host_input.setEnabled(False)
            layout.addWidget(_explain(
                "OLLAMA_HOST is set outside the app, so this "
                "setting is ignored while that is set."
            ))
        else:
            self.ollama_host_input.setText(current.get("ollama_host", ""))
            layout.addWidget(_explain(
                "Where Ollama answers. Applies to new connections "
                "right away."
            ))
        budget_row = QHBoxLayout()
        budget_row.addWidget(QLabel("Default time budget:"))
        self.budget_spin = QSpinBox()
        self.budget_spin.setRange(0, 600)
        self.budget_spin.setValue(current.get("default_budget_minutes", 0))
        self.budget_spin.setSuffix(" minutes")
        budget_row.addWidget(self.budget_spin)
        budget_row.addStretch()
        layout.addLayout(budget_row)
        layout.addWidget(_explain(
            "Minutes after which a template stops (0 means no "
            "limit). Applies to new runs."
        ))
        # R21: accessibility (library 015) — a bigger-text preset and
        # a reduce-motion switch, in Settings as well as at first
        # boot. The size applies live while the dialog is open, so
        # the user sees it before saving; Cancel puts it back.
        self._font_at_open = default_font_point_size()
        layout.addWidget(QLabel("Accessibility:"))
        bigger_row = QHBoxLayout()
        self.bigger_text_button = QPushButton("Bigger text")
        self.bigger_text_button.clicked.connect(self._apply_bigger_text)
        bigger_row.addWidget(self.bigger_text_button)
        bigger_row.addWidget(QLabel("Text size:"))
        self.font_spin = QSpinBox()
        self.font_spin.setRange(8, 20)
        saved_size = current.get("font_point_size") or 0
        self.font_spin.setValue(
            saved_size if saved_size else default_font_point_size()
        )
        self.font_spin.setSuffix(" pt")
        self.font_spin.valueChanged.connect(self._on_font_size_changed)
        bigger_row.addWidget(self.font_spin)
        bigger_row.addStretch()
        layout.addLayout(bigger_row)
        layout.addWidget(_explain(
            "Bigger text: one tap for larger text everywhere. The "
            "size above stays adjustable on its own afterwards."
        ))
        self.reduce_motion_check = QCheckBox("Reduce motion")
        self.reduce_motion_check.setChecked(
            bool(current.get("reduce_motion", False))
        )
        layout.addWidget(self.reduce_motion_check)
        layout.addWidget(_explain(
            "Reduce motion: no fade or slide effects anywhere in "
            "the app."
        ))
        layout.addWidget(QLabel("Usual templates per model:"))
        self.template_sets_label = QLabel()
        self.template_sets_label.setWordWrap(True)
        layout.addWidget(self.template_sets_label)
        self._refresh_template_sets_label()
        forget_row = QHBoxLayout()
        self.forget_sets_button = QPushButton("Forget all usual templates")
        self.forget_sets_button.clicked.connect(self._forget_template_sets)
        forget_row.addWidget(self.forget_sets_button)
        forget_row.addStretch()
        layout.addLayout(forget_row)
        layout.addWidget(_explain(
            "Ticking a model ticks its usual templates. A run saves "
            "the templates it ran as that model's usual set."
        ))
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.button(QDialogButtonBox.Ok).setText("Save")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        # The buttons stay in the dialog's own layout, below the
        # scroll area — always visible, never scrolled away.
        self.layout().addWidget(buttons)

    def _refresh_template_sets_label(self):
        if not self._template_sets:
            self.template_sets_label.setText(
                "No usual templates saved yet."
            )
            return
        templates, _warnings = load_templates()
        names = {t["id"]: t["name"] for t in templates}
        parts = []
        for label in sorted(self._template_sets):
            ids = self._template_sets[label]
            shown = ", ".join(names.get(tid, tid) for tid in ids)
            parts.append(f"{label}: {shown}")
        self.template_sets_label.setText("; ".join(parts))

    def _forget_template_sets(self):
        self._template_sets = {}
        self._refresh_template_sets_label()

    def _browse_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Pick the models folder",
            self.models_folder_input.text() or os.path.expanduser("~"),
        )
        if folder:
            self.models_folder_input.setText(folder)

    def _on_font_size_changed(self, value):
        """R21: the size applies live, so the user sees the change
        before saving it. Cancel puts the opening size back."""
        apply_font_size(value)

    def _apply_bigger_text(self):
        """R21: the bigger-text preset — the startup size plus the
        preset bonus. One tap, readable text throughout; the spin
        stays adjustable afterwards."""
        self.font_spin.setValue(min(
            20, default_font_point_size() + FONT_PRESET_BONUS_PT
        ))

    def reject(self):
        """R21: Cancel puts the font size back to what it was when
        the dialog opened."""
        apply_font_size(self._font_at_open)
        super().reject()

    def accept(self):
        values = dict(self._current)
        if self.models_folder_input.isEnabled():
            values["models_folder"] = (
                self.models_folder_input.text().strip()
            )
        if self.ollama_host_input.isEnabled():
            # v18 (RV3): a bare address is saved with its scheme,
            # so the field shows the working form back.
            values["ollama_host"] = normalize_ollama_address(
                self.ollama_host_input.text()
            )
        values["default_budget_minutes"] = self.budget_spin.value()
        values["font_point_size"] = self.font_spin.value()
        values["reduce_motion"] = self.reduce_motion_check.isChecked()
        values["template_sets"] = dict(self._template_sets)
        self.result_values = values
        super().accept()


class MainWindow(QMainWindow):
    # Tab order (R1): setup, run, models, then the v1-v9 tabs.
    TAB_SETUP = 0
    TAB_RUN = 1
    TAB_MODELS = 2
    TAB_HISTORY = 3
    TAB_PROFILES = 4
    TAB_COMPARE = 5
    TAB_PROVIDERS = 6

    def __init__(self, instance_server=None):
        super().__init__()
        # Single-instance guard (U27): the server main() acquired,
        # kept alive here; a second copy's knock raises this window.
        self._instance_server = instance_server
        if instance_server is not None:
            instance_server.newConnection.connect(self._on_instance_connection)
        # The How to Use window (U22), if open: one copy only.
        self._how_to_use_dialog = None
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
        # v17: a run or a pull waiting for Ollama to start — the
        # starter runs it when Ollama answers, or drops it when the
        # start fails. A run is (targets, templates, pairs); a pull
        # is the model name.
        self._pending_run = None
        self._pending_pull = None
        self.provider_test_worker = None
        # R19: pulls waiting behind the one running, and pulls that
        # stopped part-way (name -> (completed, total) bytes) so a
        # re-pull can say it is resuming instead of starting over.
        self.pull_queue = []
        self.pull_interrupted = {}
        self._pull_resuming = False
        # R24: the try-it answer worker, and the last run's inputs
        # for re-running only the pairs that failed.
        self.tryit_worker = None
        self._last_run_targets = []
        self._last_run_templates = []
        self._last_run_pairs = None
        # v17: the last run's manifest, so "Re-run failed only"
        # updates the same manifest instead of starting a new one;
        # the done pairs a resumed run preloads; and what the run
        # preview says when it is a resume.
        self._last_run_manifest = None
        self._resume_preloaded = {}
        self._preview_resume = None
        self._resumable_manifest = None
        self._history_runs = []
        self._reduce_motion = bool(
            load_app_settings().get("reduce_motion", False)
        )
        # R22: the last focused widget on each tab, so focus returns
        # where it was instead of getting lost on a tab change.
        self._tab_focus = {}
        # R23: the finish label's fade, kept alive while it runs.
        self._finish_fade = None
        # R12/R13: the running run's progress state. The start time
        # and step counts feed the honest progress line; the current
        # text is the latest progress signal.
        self._run_started_at = None
        self._run_total_steps = 0
        self._run_done_steps = 0
        self._run_current_text = ""
        # v18 (RV7): the progress line's heartbeat. It re-renders
        # the line every second while a run is active, so a long
        # call that reports nothing still shows its elapsed time
        # climbing instead of looking frozen at "0s elapsed".
        self._progress_tick_timer = QTimer(self)
        self._progress_tick_timer.setInterval(1000)
        self._progress_tick_timer.timeout.connect(
            self._refresh_progress_line)
        # R18: applying a usual template set ticks templates by code,
        # which must not clear the label the set just set.
        self._applying_template_set = False
        self._applied_template_set = None
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
        self.tabs = tabs

        self._build_setup_tab(tabs)
        self._build_run_tab(tabs)
        self._build_models_tab(tabs)

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
        self.history_list.currentItemChanged.connect(
            self._on_history_selection_changed)
        history_layout.addWidget(self.history_list)
        # R20: the timeline scrub (library 018). Drag across a
        # model's runs and watch the scores change in the readout —
        # ten runs browsable in the time one open takes. The slider
        # moves the highlight, never the detail view: opening a run
        # still takes a click.
        scrub_row = QHBoxLayout()
        scrub_row.addWidget(QLabel("Scrub runs:"))
        self.history_scrub_slider = QSlider(Qt.Horizontal)
        self.history_scrub_slider.setEnabled(False)
        self.history_scrub_slider.valueChanged.connect(
            self._on_history_scrub)
        scrub_row.addWidget(self.history_scrub_slider)
        history_layout.addLayout(scrub_row)
        self.history_scrub_label = QLabel("No runs for this model yet.")
        self.history_scrub_label.setWordWrap(True)
        history_layout.addWidget(self.history_scrub_label)
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
        self.store_size_label = QLabel("Results use n/a")
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
        # Two columns: the glance on the left (bento tiles and charts),
        # the full profile text on the right. The left column is the
        # answer in under six seconds; the right is the detail.
        profile_columns = QHBoxLayout()
        profile_left = QVBoxLayout()
        # R14: the bento overview (library 005). One hero tile (latest
        # overall score, two columns wide), four small tiles. Hidden
        # when the model has no runs: no numbers, no tiles.
        self.profile_tiles = QWidget()
        tiles_grid = QGridLayout(self.profile_tiles)
        tiles_grid.setSpacing(16)
        tiles_grid.setContentsMargins(0, 0, 0, 0)
        self.profile_hero_tile, _hero_cap, self.profile_hero_value = make_bento_tile(
            "Latest overall score", hero=True
        )
        tiles_grid.addWidget(self.profile_hero_tile, 0, 0, 1, 2)
        self.profile_best_tile, _best_cap, self.profile_best_value = make_bento_tile(
            "Best overall score"
        )
        tiles_grid.addWidget(self.profile_best_tile, 0, 2)
        self.profile_speed_tile, _speed_cap, self.profile_speed_value = make_bento_tile(
            "Average decode speed"
        )
        tiles_grid.addWidget(self.profile_speed_tile, 1, 0)
        self.profile_strong_tile, _strong_cap, self.profile_strong_value = (
            make_bento_tile("Strongest")
        )
        tiles_grid.addWidget(self.profile_strong_tile, 1, 1)
        self.profile_weak_tile, _weak_cap, self.profile_weak_value = make_bento_tile(
            "Weakest"
        )
        tiles_grid.addWidget(self.profile_weak_tile, 1, 2)
        profile_left.addWidget(self.profile_tiles)
        # R16: the profile's charts — score bars per template, speed
        # across runs. Each has its takeaway title above it. The bars
        # scroll inside their own area: a model with many templates
        # would otherwise push everything else off the tab.
        self.profile_bars_title = QLabel("")
        self.profile_bars_title.setWordWrap(True)
        profile_left.addWidget(self.profile_bars_title)
        self.profile_bars = BarChartWidget()
        bars_scroll = QScrollArea()
        bars_scroll.setWidgetResizable(True)
        bars_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        bars_scroll.setFixedHeight(200)
        bars_scroll.setWidget(self.profile_bars)
        profile_left.addWidget(bars_scroll)
        self.profile_speed_title = QLabel("")
        self.profile_speed_title.setWordWrap(True)
        profile_left.addWidget(self.profile_speed_title)
        self.profile_speed_chart = LineChartWidget()
        self.profile_speed_chart.setFixedHeight(150)
        profile_left.addWidget(self.profile_speed_chart)
        profile_left.addStretch()
        profile_columns.addLayout(profile_left, 1)
        self.profile_view = QTextEdit()
        self.profile_view.setReadOnly(True)
        self.profile_view.setPlainText("Pick a model to see its profile.")
        profile_columns.addWidget(self.profile_view, 1)
        profiles_layout.addLayout(profile_columns)

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
        # R14: the same bento treatment for two models side by side —
        # one tile each, same size, because a comparison weighs both
        # equally. Hidden until two models are picked.
        self.compare_tiles = QWidget()
        compare_tiles_row = QHBoxLayout(self.compare_tiles)
        compare_tiles_row.setSpacing(16)
        compare_tiles_row.setContentsMargins(0, 0, 0, 0)
        self.compare_tile_a, self.compare_caption_a, self.compare_value_a = make_bento_tile("")
        compare_tiles_row.addWidget(self.compare_tile_a)
        self.compare_tile_b, self.compare_caption_b, self.compare_value_b = make_bento_tile("")
        compare_tiles_row.addWidget(self.compare_tile_b)
        compare_layout.addWidget(self.compare_tiles)
        # R16: one chart on Compare — grouped bars, one pair per
        # template, top bar the first model, bottom bar the second.
        # The caption says which is which; the values are printed on
        # the bars, so color never carries the meaning alone.
        self.compare_chart_title = QLabel("")
        self.compare_chart_title.setWordWrap(True)
        compare_layout.addWidget(self.compare_chart_title)
        self.compare_chart_caption = QLabel("")
        self.compare_chart_caption.setWordWrap(True)
        compare_layout.addWidget(self.compare_chart_caption)
        self.compare_chart = BarChartWidget()
        compare_scroll = QScrollArea()
        compare_scroll.setWidgetResizable(True)
        compare_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        compare_scroll.setFixedHeight(200)
        compare_scroll.setWidget(self.compare_chart)
        compare_layout.addWidget(compare_scroll)
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
        # v18 (RV8): the tab scrolls. Without a scroll area the
        # content squeezed (a Test error drew over the next row)
        # and bigger text grew the window past the screen.
        providers_scroll = QScrollArea()
        providers_scroll.setWidgetResizable(True)
        providers_scroll.setFrameShape(QFrame.NoFrame)
        providers_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarAlwaysOff)
        providers_content = QWidget()
        providers_layout = QVBoxLayout(providers_content)
        providers_scroll.setWidget(providers_content)
        providers_tab_layout = QVBoxLayout(providers_tab)
        providers_tab_layout.addWidget(providers_scroll)
        providers_intro = QLabel(
            "Cloud providers, tested with the same templates as your local "
            "models. Providers and their API keys live in providers.json "
            "next to the app. That file holds your keys, never share it "
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

        # R17: the Providers tab rebuilt (library 012, 004; U20) —
        # two sections, each field explained in plain words directly
        # underneath it, and the model list comes from the provider.
        provider_right = QVBoxLayout()
        connection_box = QGroupBox("Connection")
        connection_layout = QVBoxLayout(connection_box)
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Name:"))
        self.provider_name_input = QLineEdit()
        self.provider_name_input.setPlaceholderText("e.g. openai")
        name_row.addWidget(self.provider_name_input)
        connection_layout.addLayout(name_row)
        connection_layout.addWidget(_explain(
            "A short name for this provider, e.g. openai. "
            "Shown in the model list."
        ))
        url_row = QHBoxLayout()
        url_row.addWidget(QLabel("Base URL:"))
        self.provider_url_input = QLineEdit()
        self.provider_url_input.setPlaceholderText("e.g. https://api.openai.com/v1")
        url_row.addWidget(self.provider_url_input)
        connection_layout.addLayout(url_row)
        connection_layout.addWidget(_explain(
            "The provider's API address. It must start with "
            "http:// or https://"
        ))
        key_row = QHBoxLayout()
        key_row.addWidget(QLabel("API key:"))
        self.provider_key_input = QLineEdit()
        self.provider_key_input.setEchoMode(QLineEdit.Password)
        self.provider_key_input.setPlaceholderText("API key")
        key_row.addWidget(self.provider_key_input)
        self.provider_key_status = QLabel("No key saved")
        key_row.addWidget(self.provider_key_status)
        connection_layout.addLayout(key_row)
        connection_layout.addWidget(_explain(
            "Your key for this provider. Saved in providers.json, "
            "never shown on screen."
        ))
        provider_test_row = QHBoxLayout()
        self.provider_test_button = QPushButton("Test connection")
        self.provider_test_button.clicked.connect(self.test_provider_connection)
        self.provider_test_status = QLabel("")
        self.provider_test_status.setWordWrap(True)
        provider_test_row.addWidget(self.provider_test_button)
        provider_test_row.addWidget(self.provider_test_status)
        provider_test_row.addStretch()
        connection_layout.addLayout(provider_test_row)
        fetched_row = QHBoxLayout()
        fetched_row.addWidget(QLabel("Models this provider lists:"))
        self.fetched_models_combo = QComboBox()
        self.fetched_models_combo.setSizeAdjustPolicy(
            QComboBox.AdjustToContents
        )
        self.fetched_models_combo.addItem(
            "Test the connection to list models", None
        )
        self.fetched_models_combo.currentIndexChanged.connect(
            self.on_fetched_model_picked
        )
        fetched_row.addWidget(self.fetched_models_combo)
        fetched_row.addStretch()
        connection_layout.addLayout(fetched_row)
        connection_layout.addWidget(_explain(
            "Pick one to fill the Model id below. The list comes from "
            "the provider, so the id cannot be mistyped."
        ))
        provider_right.addWidget(connection_box)
        models_box = QGroupBox("Models on this provider")
        models_layout = QVBoxLayout(models_box)
        self.provider_models_list = QListWidget()
        self.provider_models_list.itemClicked.connect(self.on_provider_model_selected)
        models_layout.addWidget(self.provider_models_list)
        model_row = QHBoxLayout()
        model_row.addWidget(QLabel("Model id:"))
        self.provider_model_id_input = QLineEdit()
        self.provider_model_id_input.setPlaceholderText("e.g. gpt-4o-mini")
        model_row.addWidget(self.provider_model_id_input)
        model_row.addWidget(QLabel("Display name:"))
        self.provider_model_name_input = QLineEdit()
        self.provider_model_name_input.setPlaceholderText("optional")
        model_row.addWidget(self.provider_model_name_input)
        models_layout.addLayout(model_row)
        models_layout.addWidget(_explain(
            "The model id exactly as the provider lists it. "
            "Display name is optional: a shorter name to show in the "
            "model list."
        ))
        price_row = QHBoxLayout()
        price_row.addWidget(QLabel("Price in per 1M:"))
        self.provider_price_in_input = QLineEdit()
        self.provider_price_in_input.setPlaceholderText("$ optional")
        price_row.addWidget(self.provider_price_in_input)
        price_row.addWidget(QLabel("Price out per 1M:"))
        self.provider_price_out_input = QLineEdit()
        self.provider_price_out_input.setPlaceholderText("$ optional")
        price_row.addWidget(self.provider_price_out_input)
        models_layout.addLayout(price_row)
        models_layout.addWidget(_explain(
            "Optional. Dollars per million tokens, used for cost "
            "estimates. Leave empty when you do not know it."
        ))
        caps_label = QLabel(
            "What this model can do (decides which templates it can run):"
        )
        caps_label.setWordWrap(True)
        models_layout.addWidget(caps_label)
        caps_row = QHBoxLayout()
        self.provider_cap_boxes = {}
        for capability in CAPABILITIES:
            box = QCheckBox(CAPABILITY_LABELS[capability])
            if capability == "text":
                box.setChecked(True)
            self.provider_cap_boxes[capability] = box
            caps_row.addWidget(box)
        caps_row.addStretch()
        models_layout.addLayout(caps_row)
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
        models_layout.addLayout(model_buttons)
        provider_right.addWidget(models_box)
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

        # R22: Tab in visual order on every tab, and focus that comes
        # back where it was when the tab changes.
        self._set_tab_orders()
        app = QApplication.instance()
        if isinstance(app, QApplication):
            app.focusChanged.connect(self._remember_focus)

        # The status line lives under the tabs, not inside one: it
        # is the one thing that must stay visible on every tab.
        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.addWidget(tabs)
        self.status_label = QLabel("Ready.")
        self.status_label.setWordWrap(True)
        self.status_label.setIndent(4)
        central_layout.addWidget(self.status_label)
        self.setCentralWidget(central)

        self.refresh_templates()
        self.refresh_models()
        self.refresh_history()
        self.refresh_profiles()
        self.refresh_compare()
        self.refresh_providers()
        self._build_menu_bar()
        self._build_shortcuts()

    def _build_setup_tab(self, tabs):
        """The Run setup tab (R1): pick models and templates, set the
        budget, press Run. One job only — the running run lives on
        the Run tab, pulling on the Models tab."""
        setup_tab = QWidget()
        tabs.addTab(setup_tab, "Run setup")
        layout = QVBoxLayout(setup_tab)

        # The first-run checklist (R7): three live steps, dismissed
        # or completed means gone for good.
        self.checklist_frame = QFrame()
        self.checklist_frame.setFrameStyle(QFrame.StyledPanel)
        checklist_layout = QVBoxLayout(self.checklist_frame)
        checklist_layout.addWidget(QLabel("Get set up:"))
        self.checklist_step1 = QLabel()
        self.checklist_step2 = QLabel()
        self.checklist_step3 = QLabel()
        for step in (self.checklist_step1, self.checklist_step2,
                     self.checklist_step3):
            step.setWordWrap(True)
            checklist_layout.addWidget(step)
        dismiss_row = QHBoxLayout()
        self.checklist_dismiss = QPushButton("Hide this checklist")
        self.checklist_dismiss.clicked.connect(self._dismiss_checklist)
        dismiss_row.addWidget(self.checklist_dismiss)
        # R21: the bigger-text preset at first boot (library 015) —
        # one tap, readable text throughout, saved right away. It
        # lives on the checklist, so it goes away with it.
        self.checklist_bigger_text = QPushButton("Bigger text")
        self.checklist_bigger_text.clicked.connect(
            self._apply_bigger_text_preset)
        dismiss_row.addWidget(self.checklist_bigger_text)
        dismiss_row.addStretch()
        checklist_layout.addLayout(dismiss_row)
        layout.addWidget(self.checklist_frame)

        intro = QLabel(
            "Nothing is ticked to start with. Tick the models and the test "
            "templates you want, then press Run. Each model runs each template "
            "in turn; every template run is saved to its own file under results, "
            "and nothing is ever overwritten. A template no ticked model can "
            "run is greyed out and cannot be ticked."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        # R23: the first run teaches the app by doing it (library
        # 014) — one line pointing at the smallest possible first
        # step. Visible only until the first run exists.
        self.first_run_hint = QLabel(
            "New here? Ask one model one question first: Models tab, "
            "Try one model."
        )
        self.first_run_hint.setWordWrap(True)
        layout.addWidget(self.first_run_hint)

        pick_row = QHBoxLayout()
        model_box = QVBoxLayout()
        model_box.addWidget(QLabel("Models (local and provider):"))
        self.model_filter = QLineEdit()
        self.model_filter.setPlaceholderText("Filter models...")
        self.model_filter.setClearButtonEnabled(True)
        self.model_filter.textChanged.connect(self._apply_model_filter)
        model_box.addWidget(self.model_filter)
        self.model_list = QListWidget()
        # Ticking or unticking a model changes which templates any
        # ticked model can run, so the grey-outs refresh on every tick.
        self.model_list.itemChanged.connect(self.update_template_availability)
        # R18: ticking a model with a saved usual set ticks those
        # templates. Connected after the grey-outs, so the set only
        # ever ticks templates a ticked model can run.
        self.model_list.itemChanged.connect(self._on_model_ticked)
        # R23: every tick says what is picked now, in the status line.
        self.model_list.itemChanged.connect(self._update_pick_counts)
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
        # R18: a hand-changed template tick means the applied usual
        # set no longer matches, so its label goes away.
        self.template_list.itemChanged.connect(self._on_template_ticked)
        # R23: every tick says what is picked now, in the status line.
        self.template_list.itemChanged.connect(self._update_pick_counts)
        # R24: double-click a template to read its tasks first.
        self.template_list.itemDoubleClicked.connect(
            self._preview_template)
        template_box.addWidget(self.template_list)
        # R18: which model's usual templates are ticked, when a set
        # was applied. Empty the rest of the time.
        self.template_set_label = QLabel("")
        self.template_set_label.setWordWrap(True)
        template_box.addWidget(self.template_set_label)
        template_buttons = QHBoxLayout()
        self.select_all_templates_button = QPushButton("Select all")
        self.select_all_templates_button.clicked.connect(self.select_all_templates)
        self.clear_templates_button = QPushButton("Clear")
        self.clear_templates_button.clicked.connect(self.clear_templates)
        # R24: read the selected template's tasks before running it.
        self.preview_template_button = QPushButton("Preview")
        self.preview_template_button.clicked.connect(self._preview_template)
        template_buttons.addWidget(self.select_all_templates_button)
        template_buttons.addWidget(self.clear_templates_button)
        template_buttons.addWidget(self.preview_template_button)
        template_buttons.addStretch()
        template_box.addLayout(template_buttons)
        pick_row.addLayout(template_box)
        layout.addLayout(pick_row)

        run_row = QHBoxLayout()
        self.run_button = QPushButton("Run")
        self.run_button.clicked.connect(self.start_run)
        run_row.addWidget(self.run_button)
        # v17: resume the latest run that did not finish. It sits
        # next to Run, where the run happens — never on another tab.
        self.resume_button = QPushButton("Resume last run")
        self.resume_button.clicked.connect(self.start_resume)
        self.resume_button.setEnabled(False)
        run_row.addWidget(self.resume_button)
        run_row.addStretch()
        layout.addLayout(run_row)

        # Opt-in per-template time budget. Default 0 = no limit: a
        # limit needs a concrete reason, and here the user sets it.
        # When set and a template's jobs run past it, that template's
        # jobs stop with "time budget reached" and the run carries on
        # with the next template.
        budget_row = QHBoxLayout()
        budget_row.addWidget(QLabel("Stop a template after"))
        self.budget_spin = QSpinBox()
        self.budget_spin.setRange(0, 600)
        # R18: the default time budget comes from Settings (0 means
        # no limit, which is also what a fresh install gets).
        self.budget_spin.setValue(
            load_app_settings()["default_budget_minutes"]
        )
        self.budget_spin.setSuffix(" minutes")
        budget_row.addWidget(self.budget_spin)
        budget_row.addWidget(QLabel("(0 = no limit)"))
        budget_row.addStretch()
        layout.addLayout(budget_row)

    def _build_run_tab(self, tabs):
        """The Run tab (R1): the running run and its results. The app
        switches here when a run starts."""
        run_tab = QWidget()
        tabs.addTab(run_tab, "Run")
        layout = QVBoxLayout(run_tab)

        run_buttons = QHBoxLayout()
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
        # R24: re-run only the pairs the last run did not finish.
        # Enabled when a finished run left failed pairs behind.
        self.rerun_failed_button = QPushButton("Re-run failed only")
        self.rerun_failed_button.clicked.connect(self._rerun_failed)
        self.rerun_failed_button.setEnabled(False)
        run_buttons.addWidget(self.cancel_button)
        run_buttons.addWidget(self.save_button)
        run_buttons.addWidget(self.export_csv_button)
        run_buttons.addWidget(self.export_html_button)
        run_buttons.addWidget(self.rerun_failed_button)
        run_buttons.addStretch()
        # R13: the calm status chip (library 003/021). One word — the
        # run's state — at the right end of the button row, where a
        # glance lands. No animation, no popup: it just changes state.
        self.run_chip = QLabel("Ready")
        self.run_chip.setObjectName("runChip")
        self.run_chip.setStyleSheet(
            "QLabel#runChip { border: 1px solid gray; "
            "border-radius: 8px; padding: 2px 10px; }"
        )
        run_buttons.addWidget(self.run_chip)
        layout.addLayout(run_buttons)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        # v18 (RV10): no % text while idle — "0%" reads like a
        # stalled job. The % comes back when a run starts.
        self.progress_bar.setFormat("")
        layout.addWidget(self.progress_bar)

        # R12: the honest progress line — current step, step count,
        # elapsed time, and a remaining-time estimate from the real
        # rate so far. Empty when no run is going.
        self.progress_detail = QLabel("")
        layout.addWidget(self.progress_detail)

        # R13: the finish moment — one headline when a run completes
        # (best model, totals, elapsed). Hidden until a run finishes.
        self.finish_label = QLabel("")
        self.finish_label.setObjectName("finishLabel")
        self.finish_label.setStyleSheet(
            "QLabel#finishLabel { font-weight: bold; }"
        )
        self.finish_label.setWordWrap(True)
        self.finish_label.hide()
        layout.addWidget(self.finish_label)

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
        layout.addWidget(self.table, stretch=2)

        layout.addWidget(QLabel("Details for the selected model:"))
        self.detail_view = QTextEdit()
        self.detail_view.setReadOnly(True)
        self.detail_view.setPlainText("Run a test, then click a model to see its scores.")
        layout.addWidget(self.detail_view, stretch=1)

    def _build_models_tab(self, tabs):
        """The Models tab (R1): what is installed, pulling a new
        model, Refresh, and Start Ollama. R19 adds stopping a pull
        and queueing pulls behind the one running; R24 adds deleting
        a model and a try-it box (ask one model one question, no
        run)."""
        models_tab = QWidget()
        tabs.addTab(models_tab, "Models")
        layout = QVBoxLayout(models_tab)

        intro = QLabel(
            "Models installed on this PC. Pull a new one below, or "
            "press Refresh to re-read the list."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        layout.addWidget(QLabel("Installed on this PC:"))
        # Read-only: no checkboxes, nothing to tick here — ticking
        # happens on the Run setup tab.
        self.installed_list = QListWidget()
        self.installed_list.currentItemChanged.connect(
            self._update_delete_model_button)
        layout.addWidget(self.installed_list)
        # v18 (RV12): while the list is empty, say why — starting
        # Ollama, or nothing installed yet — instead of a blank box.
        self.installed_empty_label = QLabel("")
        self.installed_empty_label.setWordWrap(True)
        layout.addWidget(self.installed_empty_label)

        model_buttons = QHBoxLayout()
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh_models)
        self.start_ollama_button = QPushButton("Start Ollama")
        self.start_ollama_button.clicked.connect(self.start_ollama)
        self.delete_model_button = QPushButton("Delete selected model")
        self.delete_model_button.clicked.connect(self.delete_selected_model)
        self.delete_model_button.setEnabled(False)
        model_buttons.addWidget(self.refresh_button)
        model_buttons.addWidget(self.start_ollama_button)
        model_buttons.addWidget(self.delete_model_button)
        model_buttons.addStretch()
        layout.addLayout(model_buttons)

        pull_row = QHBoxLayout()
        pull_row.addWidget(QLabel("Pull a model:"))
        self.pull_input = QLineEdit()
        self.pull_input.setPlaceholderText("model name, e.g. qwen3:4b")
        self.pull_input.returnPressed.connect(self.start_pull)
        self.pull_button = QPushButton("Pull")
        self.pull_button.clicked.connect(self.start_pull)
        self.pull_cancel_button = QPushButton("Stop pull")
        self.pull_cancel_button.clicked.connect(self.cancel_pull)
        self.pull_cancel_button.setEnabled(False)
        pull_row.addWidget(self.pull_input)
        pull_row.addWidget(self.pull_button)
        pull_row.addWidget(self.pull_cancel_button)
        layout.addLayout(pull_row)
        self.pull_bar = QProgressBar()
        self.pull_bar.setRange(0, 100)
        self.pull_bar.setValue(0)
        # v18 (RV10): no % text while idle — "0%" reads like a
        # stalled pull. The % comes back when a pull starts.
        self.pull_bar.setFormat("")
        layout.addWidget(self.pull_bar)
        self.pull_status = QLabel("")
        self.pull_status.setWordWrap(True)
        layout.addWidget(self.pull_status)
        # R19: pulls waiting behind the one running. The list stays
        # hidden until something is queued — an empty queue is not
        # a control the user needs to see.
        self.pull_queue_label = QLabel("Pull queue:")
        self.pull_queue_label.hide()
        layout.addWidget(self.pull_queue_label)
        self.pull_queue_list = QListWidget()
        self.pull_queue_list.setMaximumHeight(70)
        self.pull_queue_list.hide()
        layout.addWidget(self.pull_queue_list)
        queue_row = QHBoxLayout()
        self.pull_queue_remove_button = QPushButton(
            "Remove from queue"
        )
        self.pull_queue_remove_button.clicked.connect(
            self._remove_queued_pull)
        self.pull_queue_remove_button.hide()
        queue_row.addWidget(self.pull_queue_remove_button)
        queue_row.addStretch()
        layout.addLayout(queue_row)

        # R24: try one model — ask one question, get one answer, no
        # run and no scoring. Also the first thing a new user can do
        # (R23): the doing that teaches the app.
        tryit_box = QGroupBox("Try one model")
        tryit_layout = QVBoxLayout(tryit_box)
        tryit_row = QHBoxLayout()
        self.tryit_model_combo = QComboBox()
        self.tryit_model_combo.setSizeAdjustPolicy(
            QComboBox.AdjustToContents)
        self.tryit_input = QLineEdit()
        self.tryit_input.setPlaceholderText("Ask one question...")
        self.tryit_input.returnPressed.connect(self.ask_tryit)
        self.tryit_ask_button = QPushButton("Ask")
        self.tryit_ask_button.clicked.connect(self.ask_tryit)
        tryit_row.addWidget(self.tryit_model_combo)
        tryit_row.addWidget(self.tryit_input)
        tryit_row.addWidget(self.tryit_ask_button)
        tryit_layout.addLayout(tryit_row)
        self.tryit_answer = QTextEdit()
        self.tryit_answer.setReadOnly(True)
        self.tryit_answer.setMaximumHeight(110)
        self.tryit_answer.setPlainText(
            "Pick a model, ask one question, press Ask.")
        tryit_layout.addWidget(self.tryit_answer)
        layout.addWidget(tryit_box)

    def refresh_checklist(self):
        """The first-run checklist (R7): three live steps. Dismissed
        or completed means gone for good — it never reappears."""
        # R23: the first-run hint goes away with the first run, even
        # when the checklist itself was dismissed.
        self.first_run_hint.setVisible(not any_run_exists())
        settings = QSettings("BrokeNBored", APP_NAME)
        if settings.value("setup_dismissed", False, type=bool):
            self.checklist_frame.hide()
            return
        if settings.value("setup_completed", False, type=bool):
            self.checklist_frame.hide()
            return
        ollama_up = ollama_reachable()
        have_model = self.installed_list.count() > 0
        have_run = any_run_exists()
        if ollama_up and have_model and have_run:
            settings.setValue("setup_completed", True)
            self.checklist_frame.hide()
            return
        self.checklist_step1.setText(
            "✓ Ollama is running." if ollama_up
            else "○ Ollama is not running yet: the app starts it itself."
        )
        self.checklist_step2.setText(
            "✓ At least one model is pulled." if have_model
            else "○ Pull a model: type a model name on the Models tab "
                 "and press Pull."
        )
        self.checklist_step3.setText(
            "✓ First benchmark run done." if have_run
            else "○ Run your first test: tick a model and a template "
                 "below, then press Run."
        )
        self.checklist_frame.show()

    def _dismiss_checklist(self):
        """Hide the setup checklist for good (R7)."""
        settings = QSettings("BrokeNBored", APP_NAME)
        settings.setValue("setup_dismissed", True)
        self.checklist_frame.hide()

    def _apply_bigger_text_preset(self):
        """R21: the bigger-text preset from the first-run checklist
        (library 015) — the startup size plus the preset bonus,
        applied and saved at once."""
        size = min(20, default_font_point_size() + FONT_PRESET_BONUS_PT)
        values = load_app_settings()
        values["font_point_size"] = size
        save_app_settings(values)
        apply_font_size(size)

    def _on_instance_connection(self):
        """A second copy knocked (U27): bring this window forward."""
        while self._instance_server.hasPendingConnections():
            client = self._instance_server.nextPendingConnection()
            client.disconnectFromServer()
            client.deleteLater()
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()

    def show_how_to_use(self):
        """The How to Use index (U22): the full manual in its own
        window, rendered as markdown. It stays open beside the app
        so the manual can be read while working; opening it again
        brings the same window forward."""
        if self._how_to_use_dialog is not None:
            self._how_to_use_dialog.show()
            self._how_to_use_dialog.raise_()
            self._how_to_use_dialog.activateWindow()
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("How to Use")
        dialog.resize(640, 520)
        layout = QVBoxLayout(dialog)
        text = QTextEdit()
        text.setReadOnly(True)
        text.setMarkdown(HOW_TO_USE_TEXT)
        layout.addWidget(text)
        close_button = QPushButton("Close")
        close_button.clicked.connect(dialog.accept)
        button_row = QHBoxLayout()
        button_row.addStretch()
        button_row.addWidget(close_button)
        layout.addLayout(button_row)
        dialog.destroyed.connect(self._clear_how_to_use_dialog)
        self._how_to_use_dialog = dialog
        dialog.show()

    def _clear_how_to_use_dialog(self):
        self._how_to_use_dialog = None

    def closeEvent(self, event):
        # Save the window's size and position so the next launch
        # opens where this one left off.
        settings = QSettings("BrokeNBored", APP_NAME)
        settings.setValue("geometry", self.saveGeometry())
        super().closeEvent(event)

    def _build_menu_bar(self):
        """A minimal menu bar: File (Save report, Quit), View (Dark
        mode), Help (How to Use, About, Debug report)."""
        menu_bar = self.menuBar()
        file_menu = menu_bar.addMenu("File")
        self.save_report_action = QAction("Save report", self)
        self.save_report_action.setShortcut(QKeySequence("Ctrl+S"))
        self.save_report_action.triggered.connect(self.save_report)
        self.save_report_action.setEnabled(False)
        file_menu.addAction(self.save_report_action)
        settings_action = QAction("Settings", self)
        settings_action.triggered.connect(self.show_settings)
        file_menu.addAction(settings_action)
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
        how_to_use_action = QAction("How to Use", self)
        how_to_use_action.triggered.connect(self.show_how_to_use)
        help_menu.addAction(how_to_use_action)
        about_action = QAction("About", self)
        about_action.triggered.connect(self.show_about)
        help_menu.addAction(about_action)
        debug_action = QAction("Debug report", self)
        debug_action.triggered.connect(self.show_debug_report)
        help_menu.addAction(debug_action)

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

    def show_debug_report(self):
        """Build the scrubbed debug report and tell the user where it
        is. The report holds the app's own log only — no keys, no
        answers, no model output."""
        path = write_debug_report()
        QMessageBox.information(
            self, "Debug report",
            f"Debug report saved:\n{path}\n\n"
            "It holds the app's recent log only. No API keys and no "
            "model answers. You can send it to someone helping you.",
        )

    def show_settings(self):
        """The Settings window (R18): models folder, Ollama address,
        default time budget, usual template sets. Saving writes the
        settings and applies them to this window."""
        dialog = SettingsDialog(load_app_settings(), self)
        if dialog.exec() != QDialog.Accepted:
            return
        values = dialog.result_values
        save_app_settings(values)
        self._apply_settings(values)

    def _apply_settings(self, values):
        """Apply saved settings to the running window: the default
        time budget, the text size and the motion setting (R21), and
        the Ollama address when the environment did not set it at
        startup. Clearing a saved address removes the address this
        app set, so Ollama answers at the default again."""
        self.budget_spin.setValue(values["default_budget_minutes"])
        apply_font_size(values.get("font_point_size") or 0)
        self._reduce_motion = bool(values.get("reduce_motion", False))
        if _ENV_OLLAMA_HOST is not None:
            return
        if values["ollama_host"]:
            os.environ["OLLAMA_HOST"] = values["ollama_host"]
        else:
            os.environ.pop("OLLAMA_HOST", None)

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
            elif (template.get("score_by") == "speed"
                    and template.get("context_lengths")):
                # v17: Speed scores per context length, so the list
                # counts lengths instead of saying "no scoring".
                label = (f"{template['name']} "
                         f"({len(template['context_lengths'])} lengths)")
            else:
                label = f"{template['name']} (no scoring)"
            capability = template.get("capability", "text")
            if capability != "text":
                label += f" (needs {capability.replace('_', ' ')})"
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
        # R18: the ticks were rebuilt, not applied — any usual-set
        # label no longer describes how they got there.
        self._applied_template_set = None
        self.template_set_label.setText("")
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
        screen_failed = []
        if api_up:
            changed = False
            for name in ollama_names:
                if name not in cache:
                    screened = screen_ollama_model(name, cache)
                    if screened is None:
                        # v18 (RV5): a failed screening is not
                        # cached — the model lists as text-only
                        # for now and is re-screened next refresh.
                        screen_failed.append(name)
                    else:
                        changed = True
            if changed:
                save_capabilities(cache)
        targets = [
            ollama_target(name, cache.get(name)) for name in ollama_names
        ]
        # The Models tab's read-only list: Ollama models only, with
        # their capabilities. Provider models live on the Providers tab.
        # This PySide6 build sets ItemIsUserCheckable on a bare item by
        # default, and Space would sprout a checkbox on a read-only
        # list — clear the flag so the list stays read-only.
        self.installed_list.clear()
        for name in ollama_names:
            installed_item = QListWidgetItem(
                name + format_capabilities(cache.get(name) or set())
            )
            installed_item.setFlags(
                installed_item.flags() & ~Qt.ItemIsUserCheckable
            )
            # The plain name rides along: the visible text carries
            # capability brackets, and delete needs the bare name.
            installed_item.setData(Qt.UserRole, name)
            self.installed_list.addItem(installed_item)
        # v18 (RV12): the empty-list placeholder — visible only
        # while the list is empty, and it says which empty it is.
        if self.installed_list.count() == 0:
            if ollama_error is not None:
                self.installed_empty_label.setText(
                    "Starting Ollama. The list fills in when it answers."
                )
            else:
                self.installed_empty_label.setText(
                    "No models installed yet. "
                    "Pull one with the pull box below."
                )
            self.installed_empty_label.setVisible(True)
        else:
            self.installed_empty_label.setVisible(False)
        # R24: the try-it box offers the installed Ollama models,
        # keeping the current pick when it is still installed.
        # v18 (RV9): nothing selected by default — when there is no
        # pick to keep, the box starts blank instead of the first
        # model, so Ask never questions a model nobody chose.
        current_tryit = self.tryit_model_combo.currentText()
        self.tryit_model_combo.clear()
        for name in ollama_names:
            self.tryit_model_combo.addItem(name)
        if current_tryit in ollama_names:
            self.tryit_model_combo.setCurrentText(current_tryit)
        else:
            self.tryit_model_combo.setCurrentIndex(-1)
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
            self.refresh_checklist()
            self.refresh_resume_button()
            # v17: Ollama is down and nothing lists without it — start
            # it instead of leaving the user to. Local addresses
            # only, and never a second starter while one is already
            # running. Once Ollama answers, the starter's refresh
            # lists the models; no recursion, because a reachable
            # Ollama never reaches this branch.
            if (ollama_error is not None and self.starter is None
                    and ollama_address_is_local()):
                self.start_ollama()
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
            self.refresh_checklist()
            self.refresh_resume_button()
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
        if screen_failed:
            # v18 (RV5): a failed screening says so, instead of
            # silently leaving the model text-only.
            shown = ", ".join(screen_failed[:2])
            if len(screen_failed) > 2:
                shown += f" (+{len(screen_failed) - 2} more)"
            status += (
                f" Could not check what {shown} can do. "
                f"Press Screen models to try again."
            )
        self.status_label.setText(status)
        self.update_template_availability()
        self.refresh_checklist()
        self.refresh_resume_button()

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

    def _update_pick_counts(self, _item=None):
        """R23: tick feedback (library 001) — every tick says what
        is picked now, in the status line. Silent during a run: the
        progress line owns the status then."""
        if self.worker is not None:
            return
        models = sum(
            1 for row in range(self.model_list.count())
            if self.model_list.item(row).checkState() == Qt.Checked
        )
        templates = sum(
            1 for row in range(self.template_list.count())
            if self.template_list.item(row).checkState() == Qt.Checked
        )
        self.status_label.setText(
            f"{models} {'model' if models == 1 else 'models'}, "
            f"{templates} {'template' if templates == 1 else 'templates'} "
            "picked."
        )

    def _on_model_ticked(self, item):
        """R18: ticking a model with a saved usual set ticks exactly
        those templates and says so. Only on a tick, never on an
        untick — unticking must not touch the templates. Greyed-out
        templates stay unticked: the set never ticks a template no
        ticked model can run."""
        if self.worker is not None:
            return
        if item.checkState() != Qt.Checked:
            return
        target = item.data(Qt.UserRole)
        if not isinstance(target, dict):
            return
        template_sets = load_app_settings()["template_sets"]
        wanted = template_sets.get(target["label"])
        if not wanted:
            return
        wanted = set(wanted)
        self._applying_template_set = True
        try:
            for row in range(self.template_list.count()):
                template_item = self.template_list.item(row)
                if not template_item.flags() & Qt.ItemIsEnabled:
                    continue
                template_item.setCheckState(
                    Qt.Checked
                    if template_item.data(Qt.UserRole) in wanted
                    else Qt.Unchecked
                )
        finally:
            self._applying_template_set = False
        self._applied_template_set = (target["label"], wanted)
        self.template_set_label.setText(
            f"Usual templates for {target['label']}."
        )

    def _on_template_ticked(self, _item):
        """R18: a hand-changed template tick means the applied usual
        set no longer matches, so its label goes away. Ticks made
        while applying a set do not count."""
        if self._applying_template_set:
            return
        self._applied_template_set = None
        self.template_set_label.setText("")

    def _save_template_sets(self, targets, templates):
        """R18: remember this run's templates as each ticked model's
        usual set — ticking the model later ticks its usual templates.
        Saved when a run actually starts, never for a cancelled
        preview."""
        values = load_app_settings()
        template_sets = dict(values["template_sets"])
        ids = [t["id"] for t in templates]
        for target in targets:
            template_sets[target["label"]] = ids
        values["template_sets"] = template_sets
        save_app_settings(values)

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

    def _preview_template(self, _item=None):
        """R24: show the selected template's tasks in their own
        window. Nothing happens with no selection — the button is
        only useful when a template is picked."""
        item = self.template_list.currentItem()
        if item is None:
            return
        template_id = item.data(Qt.UserRole)
        template = next(
            (t for t in self.templates if t["id"] == template_id), None
        )
        if template is None:
            return
        dialog = TemplatePreviewDialog(self, template)
        dialog.exec()

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

    def refresh_resume_button(self):
        """v17: the Resume button offers the latest run that did not
        finish, or sits disabled when there is none. Checked on
        startup, on refresh, and when a run ends."""
        manifest = latest_resumable_manifest(get_results_dir())
        self._resumable_manifest = manifest
        if manifest is None:
            self.resume_button.setText("Resume last run")
            self.resume_button.setEnabled(False)
            return
        pairs = manifest.get("pairs") or {}
        left = sum(1 for s in pairs.values() if s in ("pending", "failed"))
        total = len(pairs)
        self.resume_button.setText(
            f"Resume last run ({left} of {total} pairs left)")
        self.resume_button.setEnabled(left > 0 and self.worker is None)

    def start_resume(self):
        """v17: resume the latest run that did not finish — only the
        pairs it did not finish. Done pairs load from their run
        files, so the table shows the whole run."""
        self.refresh_models()
        manifest = latest_resumable_manifest(get_results_dir())
        if manifest is None:
            self.status_label.setText("No run to resume.")
            self.refresh_resume_button()
            return
        targets_by_label = {}
        for row in range(self.model_list.count()):
            item = self.model_list.item(row)
            target = item.data(Qt.UserRole)
            if isinstance(target, dict):
                targets_by_label[target["label"]] = target
        templates_by_id = {t["id"]: t for t in self.templates}
        targets, pair_set, preloaded, notes = build_resume_plan(
            manifest, targets_by_label, templates_by_id, get_results_dir())
        if not pair_set:
            # The plan may have marked pairs skipped (a model or
            # template gone): save that, so the run stops offering
            # a resume with nothing in it.
            save_manifest(get_results_dir(), manifest)
            text = "Nothing left to resume in that run."
            if notes:
                text += " " + " ".join(notes)
            self.status_label.setText(text)
            self.refresh_resume_button()
            return
        if notes:
            log_event("Resume skips: " + " ".join(notes))
        self._start_run_with(targets, self.templates, pair_set,
                             resume=(manifest, preloaded, notes))

    def _preload_resume_results(self, preloaded, targets):
        """v17: the done pairs of a resumed run, loaded into the
        results before the worker starts. Models with nothing left
        to run get their row now, from their run files alone."""
        running_labels = {t["label"] for t in targets}
        # Snapshot: a fully-done model goes through on_model_done,
        # which pops its own entry out of this same dict — iterating
        # the live dict dies with "dictionary changed size".
        for label, runs in list(preloaded.items()):
            if label in running_labels:
                self.results[label] = {
                    "name": label, "status": "running",
                    "templates": dict(runs),
                }
            else:
                result = {
                    "name": label, "status": "done",
                    "templates": dict(runs),
                    "overall_score": None,
                    "avg_decode_tok_s": None,
                    "avg_ttft_s": None,
                    "avg_prompt_tok_s": None,
                    "size_ram": None,
                    "prompt_tokens": None,
                    "completion_tokens": None,
                    "total_tokens": None,
                    "estimated_cost_usd": None,
                    "price_input_per_1m": None,
                    "price_output_per_1m": None,
                }
                merge_resume_result(result)
                self.on_model_started(label)
                self.on_model_done(label, result)

    def start_run(self):
        targets = self.selected_targets()
        templates = self.selected_templates()
        if not targets:
            self.status_label.setText("Tick at least one model first.")
            return
        if not templates:
            self.status_label.setText("Tick at least one test template first.")
            return
        self._start_run_with(targets, templates)

    def _rerun_failed(self):
        """Re-run only the pairs the last run did not finish (R24):
        models that never started or failed, templates that never
        ran, and templates the time budget stopped. The good results
        stay in their run files and profiles; the table shows this
        re-run's results."""
        pairs = failed_run_pairs(
            self.results, self._last_run_targets,
            self._last_run_templates, self._last_run_pairs,
        )
        if not pairs:
            return
        pair_set = {(t["label"], x["id"]) for t, x in pairs}
        targets = [t for t in self._last_run_targets
                   if any(label == t["label"] for label, _tid in pair_set)]
        # v17: the re-run updates the same manifest the run wrote,
        # so Resume sees the pairs it finished.
        self._start_run_with(
            targets, self._last_run_templates, pair_set,
            manifest=self._last_run_manifest)

    def _start_run_with(self, targets, templates, pairs=None, resume=None,
                        manifest=None):
        """Start a run over the given targets and templates — the
        whole run, or only the pairs in the set (a re-run, R24, or a
        resume, v17). resume is (manifest, preloaded, notes) for a
        resumed run; manifest reuses one run's manifest for a re-run.
        """
        # v17: a resumed run's done pairs, folded into each model's
        # result as it finishes. A fresh run or a re-run has none.
        self._resume_preloaded = resume[1] if resume is not None else {}
        # The list may have come from the CLI fallback while the API was
        # down, or Ollama may have stopped since the window opened: check
        # it answers before starting, or every local model fails one by
        # one. Provider-only runs skip this check — they do not need
        # Ollama at all.
        needs_ollama = any(t["kind"] == "ollama" for t in targets)
        if needs_ollama and not ollama_reachable():
            # v17: a local Ollama starts itself and the run waits
            # behind it — the user never starts anything by hand. A
            # remote address cannot be started from here, so that
            # run still waits on the user.
            if ollama_address_is_local():
                self._pending_run = (
                    targets, templates, pairs, resume, manifest)
                if self.starter is None:
                    self.status_label.setText(
                        "Ollama is not running. Starting it now; "
                        "your run will begin when it answers.")
                    self.start_ollama()
                else:
                    self.status_label.setText(
                        "Ollama is starting. Your run will begin "
                        "when it answers.")
                return
            self._resume_preloaded = {}
            self.status_label.setText(OLLAMA_DOWN_RUN_MESSAGE)
            return
        # R11: every run shows its preview first — what will run, how
        # long it may take, what it will cost — and asks to go ahead.
        # The old cost guard is folded into it: a provider run's cost
        # is part of the preview, named there.
        self._preview_resume = (
            (resume[0], resume[2]) if resume is not None else None)
        try:
            preview_ok = self._show_run_preview(targets, templates, pairs)
        finally:
            self._preview_resume = None
        if not preview_ok:
            self.status_label.setText("Run cancelled.")
            self._resume_preloaded = {}
            return
        # v17: this run's manifest — the resumed or re-run one, or a
        # fresh one. Made here, after the preview was accepted, so a
        # cancelled preview leaves no manifest behind.
        if manifest is None:
            if resume is not None:
                manifest = resume[0]
            else:
                manifest = new_manifest(
                    new_run_id(get_results_dir()), targets, templates,
                    self.sys_id)
        self._last_run_manifest = manifest
        # R18: this run's templates become each ticked model's usual
        # set. Saved here, after the preview was accepted — a
        # cancelled preview saves nothing. A re-run saves nothing:
        # its subset must not clobber the model's real usual set.
        if pairs is None:
            self._save_template_sets(targets, templates)
        # R24: the last run's inputs, so "Re-run failed only" knows
        # what to re-run — and which pairs this run itself ran.
        self._last_run_targets = list(targets)
        self._last_run_templates = list(templates)
        self._last_run_pairs = None if pairs is None else set(pairs)
        # The run lives on the Run tab: switch there so the user
        # watches it instead of the setup lists.
        self.tabs.setCurrentIndex(self.TAB_RUN)
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
        self.rerun_failed_button.setEnabled(False)
        self.run_button.setEnabled(False)
        self.resume_button.setEnabled(False)
        self.refresh_button.setEnabled(False)
        self.select_all_button.setEnabled(False)
        self.clear_button.setEnabled(False)
        self.select_all_templates_button.setEnabled(False)
        self.clear_templates_button.setEnabled(False)
        # The pick lists lock too: ticks must not change under a run,
        # or the screen disagrees with what ran. They unlock in
        # on_run_finished, whatever the reason the run ends.
        self.model_list.setEnabled(False)
        self.template_list.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.progress_bar.setValue(0)
        # v18 (RV10): the bar shows its % while a run is on.
        self.progress_bar.setFormat("%p%")
        # R12/R13: fresh progress state, a clean finish line, and the
        # status chip on Running.
        self._run_started_at = time.monotonic()
        self._run_total_steps = 0
        self._run_done_steps = 0
        self._run_current_text = ""
        self.progress_detail.setText("")
        self._progress_tick_timer.start()
        self.finish_label.hide()
        self.run_chip.setText("Running")
        self.worker = BenchWorker(
            targets, templates, self.sys_id, self.machine, get_results_dir(),
            time_budget_seconds=self._time_budget_seconds(),
            pairs=pairs,
            # v17: the mid-run restart starts Ollama with the saved
            # models folder, like the Start Ollama button does.
            models_folder=load_app_settings()["models_folder"] or None,
            manifest=manifest,
        )
        self._update_delete_button()
        self.worker.model_started.connect(self.on_model_started)
        self.worker.template_done.connect(self.on_template_done)
        self.worker.model_done.connect(self.on_model_done)
        self.worker.progress.connect(self.on_progress)
        self.worker.run_started.connect(self.on_run_started)
        self.worker.step_progress.connect(self.on_step_progress)
        self.worker.run_finished.connect(self.on_run_finished)
        # v17: a resumed run shows the whole run — the done pairs
        # load into the results before the worker starts.
        if resume is not None:
            self._preload_resume_results(resume[1], targets)
        self.worker.start()
        if pairs is None:
            log_event(
                f"Run started: {len(targets)} models, "
                f"{len(templates)} templates"
            )
        else:
            log_event(
                f"Re-run started: {len(pairs)} failed "
                f"{'pair' if len(pairs) == 1 else 'pairs'}"
            )

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

    def _show_run_preview(self, targets, templates, pairs=None):
        """R11: show the run preview dialog (library 017) and return
        True when the user presses Run, False on Adjust. A seam for
        the suite, which stubs it so existing run tests pass through
        without a dialog."""
        preview = build_run_preview(
            targets, templates, get_results_dir(), pairs,
            resume=getattr(self, "_preview_resume", None))
        dialog = RunPreviewDialog(self, preview["text"])
        return dialog.exec() == QDialog.Accepted

    # -- start Ollama ----------------------------------------------------

    def start_ollama(self):
        if self.starter is not None:
            return
        self.start_ollama_button.setEnabled(False)
        self.status_label.setText("Starting Ollama...")
        models_folder = load_app_settings()["models_folder"] or None
        self.starter = OllamaStarter(models_folder)
        self.starter.started_ok.connect(self.on_ollama_started)
        self.starter.failed.connect(self.on_ollama_start_failed)
        self.starter.start()

    def on_ollama_started(self):
        self.starter = None
        self.start_ollama_button.setEnabled(True)
        self.refresh_models()
        self.status_label.setText("Ollama is running.")
        # v17: a run or a pull that waited for the start goes now.
        # The run's preview still shows first — it happens inside
        # _start_run_with, after the reachable check.
        pending_run = self._pending_run
        self._pending_run = None
        pending_pull = self._pending_pull
        self._pending_pull = None
        if pending_run is not None:
            self._start_run_with(*pending_run)
        if pending_pull is not None:
            self._begin_pull(pending_pull)

    def on_ollama_start_failed(self, message):
        self.starter = None
        self.start_ollama_button.setEnabled(True)
        # v17: nothing waiting on the start can go now. A queued
        # pull's name goes back in the box, like a failed pull.
        self._pending_run = None
        pending_pull = self._pending_pull
        self._pending_pull = None
        if pending_pull is not None and not self.pull_input.text():
            self.pull_input.setText(pending_pull)
        self.status_label.setText(message)

    # -- pull a model ----------------------------------------------------

    def start_pull(self):
        """Pull the named model — or queue it behind the pull that is
        running (R19). A pull that stopped part-way resumes: Ollama
        keeps the part it downloaded, so the re-pull picks up where
        it left off and says so."""
        name = self.pull_input.text().strip()
        if not name:
            self.pull_status.setText("Type a model name first.")
            return
        if self.pull_worker is not None:
            if not self.pull_worker.isRunning():
                # v18 (RV2): the worker died without reporting (the
                # old wedge) — clear the slot and pull as normal.
                # Its partial progress stays, so the re-pull resumes.
                worker = self.pull_worker
                self.pull_worker = None
                worker.wait()
                if worker.last_total:
                    self.pull_interrupted[worker.name] = (
                        worker.last_completed, worker.last_total)
            else:
                if name in self.pull_queue:
                    self.pull_status.setText(
                        f"{name} is already pulling or queued.")
                    return
                if (name == self.pull_worker.name
                        and not self.pull_worker.is_stopping()):
                    self.pull_status.setText(
                        f"{name} is already pulling or queued.")
                    return
                # A different model while one pulls, or the same
                # model while its pull is stopping: queue it. The
                # queued pull starts when the slot clears.
                self.pull_queue.append(name)
                self._refresh_pull_queue()
                self.pull_input.clear()
                count = len(self.pull_queue)
                self.pull_status.setText(
                    f"Queued {name}. {count} "
                    f"{'pull' if count == 1 else 'pulls'} in the queue."
                )
                return
        self.pull_input.clear()
        self._begin_pull(name)

    def _begin_pull(self, name):
        """Start a pull worker for one model (R19). The Pull button
        stays enabled while a pull runs: pressing it with a new name
        queues that pull behind the running one."""
        # v17: a pull needs Ollama answering. When it is down and
        # local, start it and pull when it answers instead of
        # failing at once. A remote address still fails plainly —
        # it cannot be started from here.
        if not ollama_reachable() and ollama_address_is_local():
            self._pending_pull = name
            if self.starter is None:
                self.pull_status.setText(
                    "Ollama is not running. Starting it now; "
                    "your pull will begin when it answers.")
                self.start_ollama()
            else:
                self.pull_status.setText(
                    "Ollama is starting. Your pull will begin "
                    "when it answers.")
            return
        self._pull_resuming = name in self.pull_interrupted
        self.pull_cancel_button.setEnabled(True)
        self.pull_bar.setRange(0, 100)
        self.pull_bar.setValue(0)
        # v18 (RV10): the % shows while a pull is on.
        self.pull_bar.setFormat("%p%")
        if self._pull_resuming:
            done, total = self.pull_interrupted[name]
            if total:
                self.pull_bar.setValue(int(100 * done / total))
            self.pull_status.setText(f"Resuming {name}...")
        else:
            self.pull_status.setText(f"Pulling {name}...")
        self.pull_worker = PullWorker(name)
        self.pull_worker.progress.connect(self.on_pull_progress)
        self.pull_worker.finished_ok.connect(self.on_pull_done)
        self.pull_worker.failed.connect(self.on_pull_failed)
        self.pull_worker.cancelled.connect(self.on_pull_cancelled)
        self.pull_worker.start()

    def cancel_pull(self):
        """Stop the running pull (R19). What Ollama downloaded stays
        downloaded — pulling the same model again resumes it."""
        if self.pull_worker is not None:
            self.pull_cancel_button.setEnabled(False)
            self.pull_status.setText(
                f"Stopping the pull of {self.pull_worker.name}..."
            )
            self.pull_worker.cancel()

    def _refresh_pull_queue(self):
        """The queue list shows the waiting pulls, or hides when the
        queue is empty."""
        self.pull_queue_list.clear()
        for name in self.pull_queue:
            self.pull_queue_list.addItem(name)
        visible = bool(self.pull_queue)
        self.pull_queue_label.setVisible(visible)
        self.pull_queue_list.setVisible(visible)
        self.pull_queue_remove_button.setVisible(visible)

    def _remove_queued_pull(self):
        """Drop the selected pull from the queue (R19)."""
        item = self.pull_queue_list.currentItem()
        if item is None:
            return
        name = item.text()
        if name in self.pull_queue:
            self.pull_queue.remove(name)
        self._refresh_pull_queue()
        self.pull_status.setText(f"Removed {name} from the queue.")

    def _start_next_queued(self):
        """Start the next queued pull, if there is one (R19)."""
        if self.pull_worker is not None or not self.pull_queue:
            return
        self._begin_pull(self.pull_queue.pop(0))
        self._refresh_pull_queue()

    def on_pull_progress(self, percent, text):
        # v18 (RV2): ignore a stale signal from a worker that already
        # left the slot — a re-pull clears a dead worker's slot and
        # starts a new pull, and the old worker's queued signals
        # still arrive afterwards. Without this the old worker's
        # progress would overwrite the new pull's.
        if self.sender() is not self.pull_worker:
            return
        if percent < 0:
            self.pull_bar.setRange(0, 0)
        else:
            self.pull_bar.setRange(0, 100)
            self.pull_bar.setValue(percent)
        if self._pull_resuming:
            text = f"Resuming: {text}"
        self.pull_status.setText(text)

    def on_pull_done(self, name):
        # v18 (RV2): ignore a stale signal from a worker that already
        # left the slot (see on_pull_progress). Without this the old
        # worker's done would clear the new pull's slot and wait for
        # a running worker on the main thread — a UI freeze.
        if self.sender() is not self.pull_worker:
            return
        # The worker's finished signal fires while its thread is
        # still exiting: capture it, clear the attribute, and wait
        # for it before the last reference goes — dropping the
        # reference first aborts Qt ("QThread: Destroyed while
        # thread is still running"). The same wait guards every
        # worker slot in this window.
        worker = self.pull_worker
        self.pull_worker = None
        if worker is not None:
            worker.wait()
        self._pull_resuming = False
        self.pull_button.setEnabled(True)
        self.pull_cancel_button.setEnabled(False)
        self.pull_bar.setRange(0, 100)
        self.pull_bar.setValue(100)
        self.pull_status.setText(f"Pulled {name}.")
        log_event(f"Pulled {name}.")
        self.pull_interrupted.pop(name, None)
        self.refresh_models()
        self._start_next_queued()

    def on_pull_failed(self, message):
        # v18 (RV2): ignore a stale signal from a worker that already
        # left the slot (see on_pull_progress).
        if self.sender() is not self.pull_worker:
            return
        worker = self.pull_worker
        self.pull_worker = None
        if worker is not None:
            worker.wait()
        self._pull_resuming = False
        self.pull_button.setEnabled(True)
        self.pull_cancel_button.setEnabled(False)
        self.pull_bar.setRange(0, 100)
        self.pull_bar.setValue(0)
        # v18 (RV10): a failed pull leaves the bar idle, no "0%".
        self.pull_bar.setFormat("")
        self.pull_status.setText(f"Pull failed: {message}")
        log_event(f"Pull failed: {message}")
        # A pull that got part-way keeps its place: pulling the same
        # model again resumes from where it stopped (R19). The name
        # goes back in the box, so resuming is one press of Pull.
        if worker is not None:
            if not self.pull_input.text():
                self.pull_input.setText(worker.name)
            if worker.last_total:
                self.pull_interrupted[worker.name] = (
                    worker.last_completed, worker.last_total)
        self._start_next_queued()

    def on_pull_cancelled(self, name):
        """A stopped pull (R19): the bar stays where it stopped, so
        the user can see how far it got, and the model stays queued
        for a resume."""
        # v18 (RV2): ignore a stale signal from a worker that already
        # left the slot (see on_pull_progress).
        if self.sender() is not self.pull_worker:
            return
        worker = self.pull_worker
        self.pull_worker = None
        if worker is not None:
            worker.wait()
        self._pull_resuming = False
        self.pull_button.setEnabled(True)
        self.pull_cancel_button.setEnabled(False)
        if worker is not None and worker.last_total:
            self.pull_interrupted[name] = (
                worker.last_completed, worker.last_total)
            percent = int(100 * worker.last_completed / worker.last_total)
            self.pull_status.setText(
                f"Pull of {name} stopped at {percent}%. "
                f"Pull it again to resume."
            )
        else:
            self.pull_status.setText(f"Pull of {name} stopped.")
        if not self.pull_input.text():
            self.pull_input.setText(name)
        log_event(f"Pull of {name} stopped.")
        self._start_next_queued()

    # -- delete a model --------------------------------------------------

    def _update_delete_model_button(self, _current=None, _previous=None):
        """Delete is enabled only when a model is selected (R24)."""
        self.delete_model_button.setEnabled(
            self.installed_list.currentItem() is not None
        )

    def delete_selected_model(self):
        """Delete the selected installed model (R24). Asks to confirm
        first — deletion cannot be undone — and defaults to No."""
        item = self.installed_list.currentItem()
        if item is None:
            return
        name = item.data(Qt.UserRole) or item.text()
        answer = QMessageBox.question(
            self, "Delete this model?",
            f"Delete {name} from this PC? The model file will be "
            "removed. This cannot be undone.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        ok, message = delete_ollama_model(name)
        # The list refreshes first: it rewrites the status line with
        # its models-found message, so the delete result is set after
        # it, or the user never sees it.
        self.refresh_models()
        if ok:
            self.status_label.setText(f"Deleted {name}.")
            log_event(f"Deleted model {name}.")
        else:
            self.status_label.setText(f"Could not delete {name}: {message}")

    # -- try one model ---------------------------------------------------

    def ask_tryit(self):
        """Ask one model one question (R24): one call, one answer, no
        run and no scoring."""
        if self.tryit_worker is not None:
            return
        model = self.tryit_model_combo.currentText()
        question = self.tryit_input.text().strip()
        if not model:
            if self.tryit_model_combo.count() == 0:
                self.tryit_answer.setPlainText(
                    "No model to ask: pull one first.")
            else:
                self.tryit_answer.setPlainText(
                    "Pick a model first.")
            return
        if not question:
            self.tryit_answer.setPlainText(
                "Type a question first.")
            return
        self.tryit_ask_button.setEnabled(False)
        self.tryit_answer.setPlainText("Asking...")
        self.tryit_worker = TryItWorker(model, question)
        self.tryit_worker.answered.connect(self.on_tryit_answered)
        self.tryit_worker.start()

    def on_tryit_answered(self, ok, text):
        worker = self.tryit_worker
        self.tryit_worker = None
        if worker is not None:
            worker.wait()
        self.tryit_ask_button.setEnabled(True)
        self.tryit_answer.setPlainText(text)

    def on_progress(self, text, percent):
        self.status_label.setText(text)
        self.progress_bar.setValue(percent)
        self._run_current_text = text
        self._refresh_progress_line()

    def on_run_started(self, total_steps):
        """R12: the run's total step count — the denominator for the
        honest progress line."""
        self._run_total_steps = total_steps
        self._refresh_progress_line()

    def on_step_progress(self, done, total):
        """R12: one more step finished — update the count and the
        remaining-time estimate."""
        self._run_done_steps = done
        self._run_total_steps = total
        self._refresh_progress_line()

    def _refresh_progress_line(self):
        """R12: the honest progress line under the progress bar —
        current step, step count, elapsed time, and a remaining-time
        estimate from the real rate so far. No estimate until at
        least 2 steps are done: one step's rate is noise."""
        if self._run_started_at is None:
            self.progress_detail.setText("")
            return
        elapsed = time.monotonic() - self._run_started_at
        done = self._run_done_steps
        total = self._run_total_steps
        parts = []
        if self._run_current_text:
            parts.append(self._run_current_text)
        if total:
            parts.append(f"{done} of {total} steps done")
        parts.append(f"{fmt_eta(elapsed)} elapsed")
        if total and done >= 2 and done < total:
            rate = elapsed / done
            remaining = rate * (total - done)
            parts.append(f"about {fmt_eta(remaining)} left")
        elif total and done < total:
            parts.append("estimating time left")
        self.progress_detail.setText(" - ".join(parts))

    def on_model_started(self, name):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(name))
        self.table.setItem(row, 1, NumericItem(None, "n/a"))
        self.table.setItem(row, 2, NumericItem(None, "n/a"))
        self.table.setItem(row, 3, NumericItem(None, "n/a"))
        self.table.setItem(row, 4, NumericItem(None, "n/a"))
        self.table.setItem(row, 5, QTableWidgetItem("running"))

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
        # v17: a resumed run folds the model's done pairs back in,
        # so the table shows the whole run, not just the resumed
        # part. The preloaded entry itself is dropped either way.
        preloaded = self._resume_preloaded.pop(name, None)
        if preloaded:
            merge_resume_result(result, preloaded)
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
        self.table.setItem(
            row, 3, NumericItem(
                result.get("avg_ttft_s"),
                fmt_duration(result.get("avg_ttft_s")),
            )
        )
        self.table.setItem(
            row, 4, NumericItem(result.get("avg_prompt_tok_s"), fmt_toks(result.get("avg_prompt_tok_s")))
        )
        self.table.setItem(row, 5, QTableWidgetItem(result["status"]))

    def on_run_finished(self, reason):
        log_event(f"Run finished: {reason}")
        self.run_button.setEnabled(True)
        self.refresh_button.setEnabled(True)
        self.select_all_button.setEnabled(True)
        self.clear_button.setEnabled(True)
        self.select_all_templates_button.setEnabled(True)
        self.clear_templates_button.setEnabled(True)
        self.model_list.setEnabled(True)
        self.template_list.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.save_button.setEnabled(bool(self.results))
        self.save_report_action.setEnabled(bool(self.results))
        self.export_csv_button.setEnabled(bool(self.results))
        self.export_html_button.setEnabled(bool(self.results))
        # R24: "Re-run failed only" is there when the run left failed
        # pairs behind — and only then.
        self.rerun_failed_button.setEnabled(bool(failed_run_pairs(
            self.results, self._last_run_targets,
            self._last_run_templates, self._last_run_pairs,
        )))
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
        # R12/R13: the run's final state on the chip, the progress
        # line's final count, and the finish moment on a completed
        # run. A stopped run shows no finish headline — the status
        # line already says what happened.
        elapsed = None
        if self._run_started_at is not None:
            elapsed = time.monotonic() - self._run_started_at
        if reason == "done":
            self.run_chip.setText("Done")
            if self._run_total_steps:
                done_text = (
                    f"{self._run_done_steps} of {self._run_total_steps} "
                    "steps done"
                )
                if elapsed is not None:
                    done_text += f", took {fmt_eta(elapsed)}."
                self.progress_detail.setText(done_text)
            self._show_finish_label(elapsed)
        elif reason == "ollama_down":
            self.run_chip.setText("Ollama stopped")
        else:
            self.run_chip.setText("Stopped")
        self._run_started_at = None
        self._progress_tick_timer.stop()
        worker = self.worker
        self.worker = None
        if worker is not None:
            worker.wait()
        self.refresh_history()
        self.refresh_profiles_for_run()
        self.refresh_compare()
        self._update_delete_button()
        # A finished run completes the checklist's third step.
        self.refresh_checklist()
        # v17: a run that ended early leaves a resumable manifest,
        # so the Resume button is re-checked now; and the preloaded
        # pairs of a resumed run are spent.
        self._resume_preloaded = {}
        self.refresh_resume_button()

    def _show_finish_label(self, elapsed):
        """R13: the finish moment — one headline naming the best
        model, its score and speed, and the run's totals. Nothing
        shows when no model has a score (an unscored-only run)."""
        best = None
        for name in self.result_order:
            result = self.results.get(name)
            if result is None or result.get("overall_score") is None:
                continue
            if best is None or result["overall_score"] > best["overall_score"]:
                best = result
        if best is None:
            return
        models = len(self.result_order)
        templates = len(best.get("templates", {}))
        text = (
            f"✓ Done. Best: {best['name']}, "
            f"{fmt_score(best.get('overall_score'))} overall at "
            f"{fmt_toks(best.get('avg_decode_tok_s'))} tok/s. "
            f"{models} {'model' if models == 1 else 'models'}, "
            f"{templates} {'template' if templates == 1 else 'templates'}"
        )
        if elapsed is not None:
            text += f", {fmt_eta(elapsed)}."
        else:
            text += "."
        self.finish_label.setText(text)
        self.finish_label.show()
        # R23: the finish moment (library 008) — a 200 ms fade-in,
        # cheap motion, and only when the user has not asked for
        # reduced motion.
        if self._reduce_motion:
            self.finish_label.setGraphicsEffect(None)
            self._finish_fade = None
        else:
            effect = QGraphicsOpacityEffect(self.finish_label)
            self.finish_label.setGraphicsEffect(effect)
            fade = QPropertyAnimation(effect, b"opacity", self)
            fade.setDuration(200)
            fade.setStartValue(0.0)
            fade.setEndValue(1.0)
            fade.start()
            self._finish_fade = fade

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
        lines = [f"{name}: overall {fmt_score(result.get('overall_score'))}, "
                 f"avg {fmt_toks(result.get('avg_decode_tok_s'))} tok/s"]
        # R15: the latency numbers the table now shows, in words.
        if (result.get("avg_ttft_s") is not None
                or result.get("avg_prompt_tok_s") is not None):
            lines.append(
                f"First token {fmt_duration(result.get('avg_ttft_s'))}, "
                f"prompt {fmt_toks(result.get('avg_prompt_tok_s'))} tok/s"
            )
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
                    "not run: test file missing)"
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
                        f", time budget reached "
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
                mark = task_mark(task)
                note = ""
                if task.get("image_file"):
                    note = " (image saved)"
                elif task.get("audio_file_saved"):
                    note = " (audio saved)"
                lines.append(
                    f"  {mark}  {task.get('id', '?')} "
                    f"({fmt_toks(task.get('decode_tok_s'))} tok/s){note}"
                )
                # R15: the saved answer itself, so the detail view
                # shows what the model actually wrote. The answer was
                # already truncated at storage time (ANSWER_TRUNCATE).
                answer = task.get("answer") or ""
                if answer:
                    answer_lines = answer.splitlines() or [""]
                    lines.append("       Answer: " + answer_lines[0])
                    for extra in answer_lines[1:]:
                        lines.append("       " + extra)
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
                    turn_answer = turn.get("answer") or ""
                    if turn_answer:
                        turn_lines = turn_answer.splitlines() or [""]
                        lines.append("           Answer: " + turn_lines[0])
                        for extra in turn_lines[1:]:
                            lines.append("           " + extra)
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
        # R22: focus comes back where it was on this tab — after the
        # refreshes above, which rebuild lists and could move it.
        self._restore_tab_focus(index)

    def _set_tab_orders(self):
        """R22: Tab moves in visual order on every tab (library 023),
        instead of the order the widgets were built in."""
        chains = [
            [self.checklist_dismiss, self.checklist_bigger_text,
             self.model_filter, self.model_list,
             self.select_all_button, self.clear_button, self.screen_button,
             self.template_filter, self.template_list,
             self.select_all_templates_button, self.clear_templates_button,
             self.preview_template_button, self.budget_spin,
             self.run_button],
            [self.cancel_button, self.save_button,
             self.export_csv_button, self.export_html_button,
             self.rerun_failed_button, self.table, self.detail_view],
            [self.installed_list, self.refresh_button,
             self.start_ollama_button, self.delete_model_button,
             self.pull_input, self.pull_button, self.pull_cancel_button,
             self.pull_queue_list, self.pull_queue_remove_button,
             self.tryit_model_combo, self.tryit_input,
             self.tryit_ask_button, self.tryit_answer],
            [self.history_model_combo, self.history_refresh_button,
             self.history_list, self.history_scrub_slider,
             self.history_view, self.delete_run_button],
            [self.profile_model_combo, self.profile_regen_button,
             self.profile_view],
            [self.compare_model_a, self.compare_model_b,
             self.compare_table],
            [self.provider_list, self.provider_new_button,
             self.provider_remove_button, self.provider_name_input,
             self.provider_url_input, self.provider_key_input,
             self.provider_test_button, self.fetched_models_combo,
             self.provider_models_list, self.provider_model_id_input,
             self.provider_model_name_input, self.provider_price_in_input,
             self.provider_price_out_input]
            + [self.provider_cap_boxes[c] for c in CAPABILITIES]
            + [self.provider_add_model_button,
               self.provider_update_model_button,
               self.provider_remove_model_button,
               self.provider_save_button],
        ]
        for chain in chains:
            for first, second in zip(chain, chain[1:]):
                self.setTabOrder(first, second)

    def _remember_focus(self, _old, new):
        """R22: remember the focused widget per tab, so a tab change
        brings focus back where it was (library 023: focus is never
        lost). Widgets outside the tabs — the menu bar, a dialog —
        are not stored. (A tab page's parent is the tab widget's
        internal stack, not the tab widget, so the test is
        isAncestorOf, not a parent walk.)"""
        if new is None or not isinstance(new, QWidget):
            return
        for index in range(self.tabs.count()):
            page = self.tabs.widget(index)
            if page is new or page.isAncestorOf(new):
                self._tab_focus[index] = new
                return

    def _restore_tab_focus(self, index):
        """R22: on a tab change, focus goes back to the widget it was
        on last time — or the tab's most likely first action when
        there is none."""
        widget = self._tab_focus.get(index)
        if (widget is None or not widget.isVisibleTo(self)
                or not widget.isEnabled()):
            widget = self._default_focus_widget(index)
        if widget is not None:
            widget.setFocus()

    def _default_focus_widget(self, index):
        """R22: each tab's most likely first action."""
        if index == self.TAB_SETUP:
            return self.model_filter
        if index == self.TAB_RUN:
            if self.worker is not None:
                return self.cancel_button
            if self.results:
                return self.save_button
            return self.table
        if index == self.TAB_MODELS:
            return self.pull_input
        if index == self.TAB_HISTORY:
            return self.history_model_combo
        if index == self.TAB_PROFILES:
            return self.profile_model_combo
        if index == self.TAB_COMPARE:
            return self.compare_model_a
        if index == self.TAB_PROVIDERS:
            return self.provider_list
        return None

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
        self._history_runs = []
        self.history_scrub_slider.setRange(0, 0)
        self.history_scrub_slider.setEnabled(False)
        self.history_scrub_label.setText("No runs for this model yet.")
        if not slug:
            self._update_delete_button()
            return
        runs = list_history_runs(get_results_dir(), slug)
        self._history_runs = runs
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
                f"{run['system_id']} | {stamp} | {run['template_name']} ({score_text})"
            )
            item.setData(Qt.UserRole, run["path"])
            # R20: hover a run to preview it without opening it.
            item.setToolTip(format_run_headline(run))
            self.history_list.addItem(item)
        # R20: the scrub slider walks the same list, newest first.
        # One run needs no slider; zero runs needs no readout.
        count = len(runs)
        self.history_scrub_slider.setRange(0, max(0, count - 1))
        self.history_scrub_slider.setEnabled(count >= 2)
        self.history_scrub_slider.blockSignals(True)
        self.history_scrub_slider.setValue(0)
        self.history_scrub_slider.blockSignals(False)
        if count:
            self.history_scrub_label.setText(
                format_run_headline(runs[0]))
        self._update_delete_button()

    def _on_history_scrub(self, value):
        """R20: the slider moves the highlight and the readout. The
        detail view does not move — scrubbing is a preview, opening
        still takes a click."""
        if 0 <= value < len(self._history_runs):
            self.history_list.setCurrentRow(value)
            self.history_scrub_label.setText(
                format_run_headline(self._history_runs[value]))

    def _on_history_selection_changed(self, _current, _previous):
        """R20: picking a run in the list moves the slider and the
        readout to match, so the two never disagree."""
        row = self.history_list.currentRow()
        if 0 <= row < len(self._history_runs):
            self.history_scrub_slider.blockSignals(True)
            self.history_scrub_slider.setValue(row)
            self.history_scrub_slider.blockSignals(False)
            self.history_scrub_label.setText(
                format_run_headline(self._history_runs[row]))

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
            self._update_profile_glance(None)
            return
        path = os.path.join(get_results_dir(), slug, "PROFILE.md")
        try:
            with open(path, encoding="utf-8") as handle:
                self.profile_view.setMarkdown(handle.read())
        except OSError:
            self.profile_view.setPlainText(
                "No profile yet for this model. Press Regenerate to build one."
            )
        self._update_profile_glance(
            profile_numbers(get_results_dir(), slug)
        )

    def _update_profile_glance(self, numbers):
        """The Profiles tab's glance (R14/R16): bento tiles and charts
        from the profile numbers. Hidden when the model has no runs —
        no numbers, no tiles, no charts."""
        if numbers is None:
            self.profile_tiles.setVisible(False)
            self.profile_bars_title.setText("")
            self.profile_bars.set_data([])
            self.profile_speed_title.setText("")
            self.profile_speed_chart.set_data([])
            return
        self.profile_tiles.setVisible(True)
        self.profile_hero_value.setText(fmt_score(numbers["overall_latest"]))
        self.profile_best_value.setText(fmt_score(numbers["overall_best"]))
        speed = numbers["avg_speed"]
        self.profile_speed_value.setText(
            "n/a" if speed is None else f"{speed:.1f} tok/s"
        )
        self.profile_strong_value.setText(
            ", ".join(numbers["strengths"])
            if numbers["strengths"] else "None yet"
        )
        self.profile_weak_value.setText(
            ", ".join(numbers["weaknesses"])
            if numbers["weaknesses"] else "None yet"
        )
        self.profile_bars.set_data(numbers["template_scores"])
        self.profile_bars_title.setText(self.profile_bars.takeaway_title())
        speed_points = [
            (_short_date(stamp), value)
            for stamp, value in numbers["speed_points"]
        ]
        self.profile_speed_chart.set_data(speed_points)
        self.profile_speed_title.setText(
            self.profile_speed_chart.takeaway_title()
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
            combo.addItem("Pick a model", None)
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
            self._update_compare_glance(None)
            if list_history_models(get_results_dir()):
                self.compare_message.setText("Pick two models to compare.")
            else:
                self.compare_message.setText(
                    "No models have run yet. Run a test first."
                )
            return
        if slug_a == slug_b:
            self._update_compare_glance(None)
            self.compare_message.setText(
                "Pick two different models to compare."
            )
            return
        compared = compare_models(get_results_dir(), slug_a, slug_b)
        if compared is None:
            self._update_compare_glance(None)
            self.compare_message.setText(
                "One of these models has no runs yet."
            )
            return
        self.compare_message.setText("")
        self._update_compare_glance(compared)
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
                    text = "n/a"
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

    def _update_compare_glance(self, compared):
        """The Compare tab's glance (R14/R16): one tile per model and
        the grouped score bars. Hidden until two models compare."""
        if compared is None:
            self.compare_tiles.setVisible(False)
            self.compare_chart_title.setText("")
            self.compare_chart_caption.setText("")
            self.compare_chart.set_data([])
            return
        self.compare_tiles.setVisible(True)
        for caption, value, name, overall, speed, key in (
            (self.compare_caption_a, self.compare_value_a,
             compared["name_a"], compared["overall_a"], compared["speed_a"],
             "score_a"),
            (self.compare_caption_b, self.compare_value_b,
             compared["name_b"], compared["overall_b"], compared["speed_b"],
             "score_b"),
        ):
            caption.setText(name)
            best = None
            for row in compared["rows"]:
                score = row[key]
                if score is not None and (best is None or score > best[1]):
                    best = (row["name"], score)
            lines = [f"{fmt_score(overall)} overall"]
            lines.append(
                "n/a" if speed is None else f"{speed:.1f} tok/s average"
            )
            if best is not None:
                lines.append(f"Best: {best[0]} {best[1]:.0f}%")
            value.setText("\n".join(lines))
        rows = [
            (row["name"], row["score_a"], row["score_b"])
            for row in compared["rows"]
            if row["score_a"] is not None or row["score_b"] is not None
        ]
        self.compare_chart.set_data(
            rows, (compared["name_a"], compared["name_b"])
        )
        self.compare_chart_title.setText(
            self.compare_chart.takeaway_title()
        )
        self.compare_chart_caption.setText(
            f"Top bar: {compared['name_a']}. "
            f"Bottom bar: {compared['name_b']}."
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
            # v18 (RV12): read-only like the installed-models list —
            # this PySide6 build sets ItemIsUserCheckable on a bare
            # item by default, and Space would sprout a checkbox.
            item.setFlags(item.flags() & ~Qt.ItemIsUserCheckable)
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
        # The fetched list belongs to the previous provider: a new
        # form starts with no list until this provider is tested.
        self.fetched_models_combo.blockSignals(True)
        self.fetched_models_combo.clear()
        self.fetched_models_combo.addItem(
            "Test the connection to list models", None
        )
        self.fetched_models_combo.blockSignals(False)

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
                text += f" ({model['display_name']})"
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
        answer = QMessageBox.question(
            self, "Remove this provider?",
            f"Remove {name}? Its API key and all its saved models go "
            "with it. This cannot be undone.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
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

    def on_provider_tested(self, ok, message, model_ids):
        worker = self.provider_test_worker
        self.provider_test_worker = None
        if worker is not None:
            worker.wait()
        self.provider_test_button.setEnabled(True)
        self.provider_test_status.setText(message)
        # R17: the fetched model list becomes a pick-list — the model
        # list comes from the provider, not from typing. Nothing is
        # picked by default: the first item is a prompt, not a model.
        self.fetched_models_combo.blockSignals(True)
        self.fetched_models_combo.clear()
        if ok and model_ids:
            self.fetched_models_combo.addItem(
                "Pick a model to use its id", None
            )
            for model_id in model_ids:
                self.fetched_models_combo.addItem(model_id, model_id)
        else:
            self.fetched_models_combo.addItem(
                "Test the connection to list models", None
            )
        self.fetched_models_combo.blockSignals(False)

    def on_fetched_model_picked(self, index):
        """Picking a fetched model fills the Model id field with it —
        the id comes from the provider, so it cannot be mistyped."""
        model_id = self.fetched_models_combo.itemData(index)
        if model_id:
            self.provider_model_id_input.setText(model_id)

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
        stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        base = f"bench-results-{stamp}"
        txt_path = os.path.join(folder, base + ".txt")
        json_path = os.path.join(folder, base + ".json")
        # Never overwrites: two saves in the same second get -2, -3,
        # the same rule the run files use.
        suffix = 2
        while os.path.exists(txt_path) or os.path.exists(json_path):
            txt_path = os.path.join(folder, f"{base}-{suffix}.txt")
            json_path = os.path.join(folder, f"{base}-{suffix}.json")
            suffix += 1
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
                "Nothing to export yet. Run a test first."
            )
            return
        folder = get_results_dir()
        os.makedirs(folder, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
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
                "Nothing to export yet. Run a test first."
            )
            return
        folder = get_results_dir()
        os.makedirs(folder, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
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


def ensure_ollama_headless():
    """v17: make sure a local Ollama is answering before a headless
    run — starting it when it is down. Returns None when Ollama
    answers (or a start made it answer), else a plain error. A
    remote address is never started from here."""
    if ollama_reachable():
        return None
    if not ollama_address_is_local():
        return OLLAMA_DOWN_RUN_MESSAGE
    ok, message = start_ollama_process(
        load_app_settings()["models_folder"] or None)
    if not ok:
        return message
    if not ollama_reachable():
        return "Ollama started but stopped answering. Try again."
    return None


def headless_targets_for_labels(labels):
    """v17 (resume): the known targets for the given labels — Ollama
    models screened like the window does, provider models from the
    saved providers. Labels that match nothing come back as missing,
    for the caller to report; nothing here errors."""
    providers, _warnings = load_providers()
    provider_targets = {}
    for provider in providers:
        for model_entry in provider.get("models", []):
            label = provider_label(provider["name"], model_entry["id"])
            provider_targets[label] = provider_target(provider, model_entry)
    targets = {}
    missing = []
    for label in labels:
        if label in provider_targets:
            targets[label] = provider_targets[label]
    ollama_labels = [label for label in labels
                     if label not in provider_targets]
    if ollama_labels:
        available, _source = list_models()
        caps_cache = load_capabilities()
        screened = False
        for label in ollama_labels:
            if label not in available:
                missing.append(label)
                continue
            caps = screen_ollama_model(label, caps_cache)
            screened = True
            targets[label] = ollama_target(label, caps)
        if screened:
            save_capabilities(caps_cache)
    return targets, missing


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
            error = ensure_ollama_headless()
            if error is not None:
                return None, error
        available, _source = list_models()
        caps_cache = load_capabilities()
        screened = False
        for name in ollama_names:
            if name not in available:
                known = sorted(set(available)) + sorted(provider_targets)
                if known:
                    detail = "Models I know about: " + ", ".join(known)
                else:
                    detail = ("No models found. Is Ollama running, and "
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

    With --resume, the latest incomplete run (or the run-id given)
    picks up where it left off: only its pending and failed pairs
    run, and the done pairs load from their run files.
    """
    results_dir = get_results_dir()
    templates, warnings = load_templates()
    for warning in warnings:
        print(f"Warning: {warning}")
    by_id = {t["id"]: t for t in templates}
    resume = getattr(args, "resume", None)
    preloaded = {}
    manifest = None
    pairs = None
    if resume is not None:
        if resume == "__latest__":
            manifest = latest_resumable_manifest(results_dir)
            if manifest is None:
                print("No run to resume.")
                return 1
        else:
            manifest = load_manifest(results_dir, resume)
            if manifest is None:
                print(f"No run with id {resume}.")
                return 1
            if not manifest_is_resumable(manifest):
                print(f"Run {resume} already finished.")
                return 1
        # A resumed run may need Ollama: start it first, so the
        # models in the manifest can be listed and screened.
        if any(t.get("kind") == "ollama"
               for t in manifest.get("targets", [])):
            error = ensure_ollama_headless()
            if error is not None:
                print(error)
                return 1
        labels = [t["label"] for t in manifest.get("targets", [])]
        targets_by_label, _missing = headless_targets_for_labels(labels)
        targets, pair_set, preloaded, notes = build_resume_plan(
            manifest, targets_by_label, by_id, results_dir)
        if not pair_set:
            # The plan may have marked pairs skipped (a model or
            # template gone): save that, so the run stops offering
            # a resume with nothing in it.
            save_manifest(results_dir, manifest)
            print("Nothing left to resume in that run.")
            for note in notes:
                print(note)
            return 1
        started = (manifest.get("started") or "").replace("T", " ")
        print(f"Resuming the run from {started}...")
        for note in notes:
            print(note)
        # The worker filters by pairs, so it takes all the templates.
        chosen = templates
        pairs = pair_set
    else:
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
    machine = machine_info()
    sys_id = system_id(machine)
    results = {}
    order = []
    run_files = []
    # v17: models whose pairs are all done already get their rows
    # from their run files, before the worker starts.
    if preloaded:
        running_labels = {t["label"] for t in targets}
        for label, runs in preloaded.items():
            if label in running_labels:
                continue
            result = {
                "name": label, "status": "done",
                "templates": dict(runs),
                "overall_score": None,
                "avg_decode_tok_s": None,
                "avg_ttft_s": None,
                "avg_prompt_tok_s": None,
                "size_ram": None,
                "prompt_tokens": None,
                "completion_tokens": None,
                "total_tokens": None,
                "estimated_cost_usd": None,
                "price_input_per_1m": None,
                "price_output_per_1m": None,
            }
            merge_resume_result(result)
            results[label] = result
            order.append(label)

    core_app = QCoreApplication(sys.argv)
    # A no-op timer keeps the event loop waking up, so Ctrl+C reaches
    # Python instead of waiting behind Qt.
    keepalive = QTimer()
    keepalive.setInterval(500)
    keepalive.timeout.connect(lambda: None)
    keepalive.start()

    worker = BenchWorker(targets, chosen, sys_id, machine, results_dir,
                         # v17: the mid-run restart starts Ollama with
                         # the saved models folder, like the window.
                         models_folder=load_app_settings()["models_folder"] or None,
                         pairs=pairs, manifest=manifest)

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
        # v17: a resumed run folds the model's done pairs back in.
        extra = preloaded.get(name)
        if extra:
            merge_resume_result(result, extra)
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


def acquire_single_instance(name):
    """Single-instance guard (U27). Returns (server, is_first).

    First try to connect: if a copy is already listening, send it a
    "show" nudge and return (None, False) — the caller exits without
    opening a window. Otherwise take the name and return
    (server, True); the caller keeps the server alive on the window
    and raises it whenever a second copy knocks.

    A crashed run leaves its socket behind, so the name is removed
    before listening or the listen fails. If listening fails anyway,
    the app still starts (as the first copy, with no server) rather
    than refusing to open at all.
    """
    socket = QLocalSocket()
    socket.connectToServer(name)
    if socket.waitForConnected(500):
        socket.write(b"show")
        socket.flush()
        socket.waitForBytesWritten(500)
        socket.disconnectFromServer()
        return None, False
    QLocalServer.removeServer(name)
    server = QLocalServer()
    if server.listen(name):
        return server, True
    return None, True


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
        "e.g. qwen3:0.6b. A provider model is written "
        "\"provider / model-id\"",
    )
    parser.add_argument(
        "--templates",
        help="comma-separated template ids to run in headless mode, "
        "e.g. coding,math",
    )
    parser.add_argument(
        "--resume", nargs="?", const="__latest__", metavar="RUN-ID",
        help="resume an incomplete run in headless mode: the latest "
        "one, or the run-id given. --models and --templates are not "
        "needed with it",
    )
    args = parser.parse_args()
    # The media folders exist from the first launch, window or not.
    ensure_media_dirs()
    # The debug log starts with the app: old logs pruned, the crash
    # hook installed, the launch itself logged.
    prune_old_logs()
    install_excepthook()
    log_event(f"OLLAMA SYSTEM TEST v{__version__} started")
    # R18: the saved Ollama address applies to this process, unless
    # the environment already sets it — the environment wins, always.
    # Window or headless, before anything reads ollama_base().
    apply_saved_settings_to_env()
    if args.headless:
        sys.exit(run_headless(args))
    if args.models or args.templates or args.resume is not None:
        print("--models, --templates and --resume only apply with "
              "--headless; opening the window.", file=sys.stderr)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    # R21: the saved text size applies before the window is built,
    # so a bigger-text user never sees small text first.
    apply_font_size(load_app_settings().get("font_point_size") or 0)
    # Single instance (U27): a second copy nudges the running one
    # and exits. Headless runs skip this — cron must not be blocked.
    instance_server, is_first = acquire_single_instance(SINGLE_INSTANCE_NAME)
    if not is_first:
        return 0
    # The saved theme applies before the window is built, so a
    # dark-mode user never sees a light flash.
    settings = QSettings("BrokeNBored", APP_NAME)
    apply_theme(settings.value("dark_mode", False, type=bool))
    window = MainWindow(instance_server=instance_server)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
