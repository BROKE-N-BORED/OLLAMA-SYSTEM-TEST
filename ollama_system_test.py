#!/usr/bin/env python3
"""OLLAMA SYSTEM TEST v4 - benchmark the Ollama models on this PC,
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

DURABLE RULE: nothing is ever ticked by default. Every model and every
template starts unchecked; the user ticks what they want to test.

KEY RULE: an API key lives only in providers.json and in memory while
a request is made. It is never written to a run file, a report, a
profile, or the status line, and it is never shown on screen.
"""

import ast
import json
import os
import platform
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

APP_NAME = "OLLAMA SYSTEM TEST"
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
RETRY_DELAY = 3.0            # seconds before retrying a failed generate once
START_OLLAMA_TIMEOUT = 30.0  # seconds to wait for `ollama serve` to answer
START_OLLAMA_POLL = 1.0      # seconds between checks while waiting
PROVIDER_READ_TIMEOUT = 120.0   # seconds between provider chunks (providers are slow)
PROVIDER_STALL_TIMEOUT = 300.0  # seconds with no provider chunk at all before giving up
PROVIDER_TEST_TIMEOUT = 15.0    # seconds for the Test connection call
PROVIDER_LABEL_SEP = " / "      # "provider / model-id", used everywhere
KNOWN_RULES = ("contains", "numeric", "json", "code", "count_lines", "max_words")


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
            models.append({
                "id": model_id,
                "display_name": display_name,
                "price_input_per_1m": price_in,
                "price_output_per_1m": price_out,
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
    """Write providers.json. The file holds API keys, so it is written
    readable only by its owner (best effort on systems that allow it)."""
    if path is None:
        path = get_providers_file()
    data = {"providers": providers}
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


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


def provider_generate_once(provider, model_id, prompt, num_predict,
                           temperature=0, should_stop=None):
    """One streaming /v1/chat/completions call. Returns the same metrics
    shape as generate_once, plus prompt/completion/total tokens when the
    provider reports usage.

    Raises ProviderUnreachable when the provider did not answer at all
    (retriable), ProviderError for anything else (a bad key, a bad
    model, a provider error message), Cancelled if should_stop() fires.
    """
    name = provider.get("name") or "provider"
    base_url = (provider.get("base_url") or "").rstrip("/")
    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": num_predict,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
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
            if text:
                now = time.monotonic()
                if t_first is None:
                    t_first = now
                t_last = now
                answer_parts.append(text)
    finally:
        resp.close()
    if not answer_parts:
        # The stream ended before the provider sent any answer text:
        # it went away, so this is unreachable too.
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
        "prompt": prompt,
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
    }


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
    """Score one task. Returns (passed, detail)."""
    rule = task.get("rule")
    if rule == "contains":
        return score_contains(answer, task.get("expected", ""))
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
    return False, f"unknown scoring rule {rule!r}"


# ---------------------------------------------------------------------------
# Templates (data-driven, JSON files in templates/)
# ---------------------------------------------------------------------------

def _validate_task(task, template_id, warnings, filename):
    """Validate one task dict. Returns the task or None (with a warning)."""
    if not isinstance(task, dict):
        warnings.append(f"{filename}: a task in {template_id} is not an object, skipped")
        return None
    task_id = task.get("id")
    prompt = task.get("prompt")
    rule = task.get("rule")
    if not task_id or not isinstance(task_id, str):
        warnings.append(f"{filename}: a task in {template_id} has no id, skipped")
        return None
    if not prompt or not isinstance(prompt, str):
        warnings.append(f"{filename}: task {task_id} has no prompt, skipped")
        return None
    if rule not in KNOWN_RULES:
        warnings.append(
            f"{filename}: task {task_id} has unknown rule {rule!r}, skipped"
        )
        return None
    expected = task.get("expected")
    if rule == "contains" and not isinstance(expected, str):
        warnings.append(f"{filename}: task {task_id} needs a text expected value, skipped")
        return None
    if rule == "numeric" and (
        not isinstance(expected, (int, float)) or isinstance(expected, bool)
    ):
        warnings.append(f"{filename}: task {task_id} needs a numeric expected value, skipped")
        return None
    if rule == "json" and not isinstance(expected, dict):
        warnings.append(f"{filename}: task {task_id} needs an object expected value, skipped")
        return None
    if rule in ("count_lines", "max_words") and (
        not isinstance(expected, int) or isinstance(expected, bool)
    ):
        warnings.append(f"{filename}: task {task_id} needs a whole-number expected value, skipped")
        return None
    if rule == "code":
        function = task.get("function")
        tests = task.get("tests")
        if not function or not isinstance(function, str):
            warnings.append(f"{filename}: task {task_id} needs a function name, skipped")
            return None
        if not isinstance(tests, list) or not tests:
            warnings.append(f"{filename}: task {task_id} needs test cases, skipped")
            return None
        for case in tests:
            if not isinstance(case, dict) or "args" not in case or "expected" not in case:
                warnings.append(
                    f"{filename}: task {task_id} has a bad test case, skipped"
                )
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
        template = {
            "id": template_id,
            "name": name,
            "description": data.get("description", ""),
            "num_predict": num_predict,
            "scored": bool(scored),
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
            prompts = data.get("prompts")
            if not isinstance(prompts, list) or not prompts:
                warnings.append(f"{filename}: skipped, no prompts")
                continue
            prompts = [p for p in prompts if isinstance(p, str) and p]
            if not prompts:
                warnings.append(f"{filename}: skipped, no usable prompts")
                continue
            template["prompts"] = prompts
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

def save_run_file(results_dir, model, sys_id, run_data):
    """Write one template run to results/<model>/<system>/<ts>-<template>.json.

    Never overwrites: if the name exists, -2, -3 is appended. Returns the
    path written. Stamps the run data with its timestamp.
    """
    stamp = datetime.now()
    run_data["timestamp"] = stamp.isoformat(timespec="seconds")
    run_data["model"] = model
    run_data["model_slug"] = slugify_model(model)
    run_data["system_id"] = sys_id
    folder = os.path.join(results_dir, run_data["model_slug"], sys_id)
    os.makedirs(folder, exist_ok=True)
    base = f"{stamp.strftime('%Y-%m-%d-%H%M%S')}-{run_data['template']}"
    path = os.path.join(folder, base + ".json")
    suffix = 2
    while os.path.exists(path):
        path = os.path.join(folder, f"{base}-{suffix}.json")
        suffix += 1
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


def render_run_summary(run_data):
    """Plain-text summary of one run file, for the history view."""
    lines = [
        f"{run_data.get('model', '?')} — {run_data.get('template_name', run_data.get('template', '?'))}",
        f"Run: {run_data.get('timestamp', '?')}",
        f"System: {run_data.get('system_id', '?')}",
        f"Provider: {run_data.get('provider', 'ollama')}",
    ]
    score = run_data.get("template_score")
    if score is None:
        lines.append("Score: no scoring (speed only)")
    else:
        lines.append(
            f"Score: {score:.0f}% ({run_data.get('passed', '?')}/{run_data.get('total', '?')} tasks)"
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
        mark = "pass" if passed is True else ("fail" if passed is False else "run")
        lines.append(
            f"{mark}  {task.get('id', '?')} ({fmt_toks(task.get('decode_tok_s'))} tok/s)"
        )
        detail = task.get("detail") or ""
        if passed is False and detail:
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
            "templates (Coding, Math, Reasoning, Instruction, Knowledge) "
            "to find this model's strengths and weaknesses."
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
        if scored:
            latest = scored[-1]["template_score"]
            best = max(r["template_score"] for r in scored)
            latest_scores.append(latest)
            best_scores.append(best)
            template_rows.append((name, latest, best, len(t_runs)))
        else:
            template_rows.append((name, None, None, len(t_runs)))
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
        name for name, latest, _best, _n in template_rows
        if latest is not None and latest >= STRENGTH_AT
    ]
    weaknesses = [
        name for name, latest, _best, _n in template_rows
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
    for name, latest, best, count in template_rows:
        if latest is None:
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
            score = run.get("template_score")
            if score is None:
                lines.append(
                    f"  {run.get('template_name', template_id)}: no scoring, "
                    f"{fmt_toks(run.get('avg_decode_tok_s'))} tok/s"
                )
            else:
                lines.append(
                    f"  {run.get('template_name', template_id)}: {score:.0f}% "
                    f"({run.get('passed')}/{run.get('total')}), "
                    f"{fmt_toks(run.get('avg_decode_tok_s'))} tok/s"
                )
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Benchmark worker (runs in its own thread, one model at a time)
# ---------------------------------------------------------------------------

def _truncate_answer(answer):
    if len(answer) > ANSWER_TRUNCATE:
        return answer[:ANSWER_TRUNCATE] + "… (truncated)"
    return answer


def ollama_target(model_name):
    """A benchmark target for a local Ollama model."""
    return {
        "label": model_name,
        "kind": "ollama",
        "model": model_name,
        "provider": None,
        "provider_name": "ollama",
        "price_input_per_1m": None,
        "price_output_per_1m": None,
    }


def provider_target(provider, model_entry):
    """A benchmark target for a provider model.

    The label is "provider / model-id" everywhere: the model list, the
    results table, and the run files.
    """
    return {
        "label": provider_label(provider["name"], model_entry["id"]),
        "kind": "provider",
        "model": model_entry["id"],
        "provider": provider,
        "provider_name": provider["name"],
        "price_input_per_1m": model_entry.get("price_input_per_1m"),
        "price_output_per_1m": model_entry.get("price_output_per_1m"),
    }


class BenchWorker(QThread):
    model_started = Signal(str)
    template_done = Signal(str, dict, str)  # model, run data, file path
    model_done = Signal(str, dict)
    progress = Signal(str, int)
    # "done", "cancelled", or "ollama_down"
    run_finished = Signal(str)

    def __init__(self, targets, templates, sys_id, machine, results_dir):
        super().__init__()
        self.targets = targets
        self.templates = templates
        self.sys_id = sys_id
        self.machine = machine
        self.results_dir = results_dir
        self._stop = False

    def request_cancel(self):
        self._stop = True

    def _should_stop(self):
        return self._stop

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

    def _count_steps(self):
        steps = 0
        for _target in self.targets:
            steps += 1  # warmup
            for template in self.templates:
                if template["scored"]:
                    steps += len(template["tasks"])
                else:
                    steps += len(template["prompts"])
        return max(steps, 1)

    def _run_template(self, target, template):
        """Run one template for one target. Returns the run data dict."""
        label = target["label"]
        num_predict = template["num_predict"]
        task_results = []
        if template["scored"]:
            jobs = [
                (task["id"], task["prompt"], task) for task in template["tasks"]
            ]
        else:
            jobs = [
                (f"run-{i + 1}", prompt, None)
                for i, prompt in enumerate(template["prompts"])
            ]
        for job_id, prompt, task in jobs:
            if self._stop:
                raise Cancelled()
            try:
                metrics = self._generate(target, prompt, num_predict)
            except OllamaError as exc:
                if is_unreachable(exc):
                    # Ollama itself went away: stop the whole run
                    # instead of failing every remaining task and model.
                    raise OllamaUnreachable(describe_error(exc))
                task_results.append({
                    "id": job_id,
                    "prompt": prompt,
                    "answer": "",
                    "passed": False if task is not None else None,
                    "decode_tok_s": None,
                    "prompt_tok_s": None,
                    "ttft_s": None,
                    "prompt_tokens": None,
                    "completion_tokens": None,
                    "detail": str(exc),
                })
                self._bump_progress(label, template, job_id)
                continue
            except ProviderError as exc:
                # A provider failing never stops the run: this task
                # fails plainly and the rest carry on, including the
                # local Ollama models still to come.
                task_results.append({
                    "id": job_id,
                    "prompt": prompt,
                    "answer": "",
                    "passed": False if task is not None else None,
                    "decode_tok_s": None,
                    "prompt_tok_s": None,
                    "ttft_s": None,
                    "prompt_tokens": None,
                    "completion_tokens": None,
                    "detail": str(exc),
                })
                self._bump_progress(label, template, job_id)
                continue
            if task is None:
                passed, detail = None, ""
            else:
                passed, detail = score_task(task, metrics["answer"])
            task_results.append({
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
            })
            self._bump_progress(label, template, job_id)
        if template["scored"]:
            passed_count = sum(1 for t in task_results if t["passed"] is True)
            total = len(task_results)
            score = 100.0 * passed_count / total if total else 0.0
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
        run_data = {
            "template": template["id"],
            "template_name": template["name"],
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
        path = save_run_file(self.results_dir, label, self.sys_id, run_data)
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
                warmup = self._generate(target, WARMUP_PROMPT, WARMUP_PREDICT)
                warmup_prompt_tokens = warmup.get("prompt_tokens")
                warmup_completion_tokens = warmup.get("completion_tokens")
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
        self.setWindowTitle(APP_NAME)
        self.resize(980, 720)
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
            "and nothing is ever overwritten."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        pick_row = QHBoxLayout()
        model_box = QVBoxLayout()
        model_box.addWidget(QLabel("Models (local and provider):"))
        self.model_list = QListWidget()
        self.model_list.setMaximumHeight(180)
        model_box.addWidget(self.model_list)
        model_buttons = QHBoxLayout()
        self.select_all_button = QPushButton("Select all")
        self.select_all_button.clicked.connect(self.select_all_models)
        self.clear_button = QPushButton("Clear")
        self.clear_button.clicked.connect(self.clear_models)
        model_buttons.addWidget(self.select_all_button)
        model_buttons.addWidget(self.clear_button)
        model_buttons.addStretch()
        model_box.addLayout(model_buttons)
        pick_row.addLayout(model_box)

        template_box = QVBoxLayout()
        template_box.addWidget(QLabel("Test templates:"))
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
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh_models)
        self.start_ollama_button = QPushButton("Start Ollama")
        self.start_ollama_button.clicked.connect(self.start_ollama)
        run_buttons.addWidget(self.run_button)
        run_buttons.addWidget(self.cancel_button)
        run_buttons.addWidget(self.save_button)
        run_buttons.addWidget(self.refresh_button)
        run_buttons.addWidget(self.start_ollama_button)
        run_buttons.addStretch()
        layout.addLayout(run_buttons)

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
        history_layout.addWidget(self.history_list)
        self.history_view = QTextEdit()
        self.history_view.setReadOnly(True)
        self.history_view.setPlainText("Pick a run to see its scores.")
        history_layout.addWidget(self.history_view)

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
        model_buttons = QHBoxLayout()
        self.provider_add_model_button = QPushButton("Add model")
        self.provider_add_model_button.clicked.connect(self.add_provider_model)
        self.provider_remove_model_button = QPushButton("Remove model")
        self.provider_remove_model_button.clicked.connect(self.remove_provider_model)
        model_buttons.addWidget(self.provider_add_model_button)
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
        self.refresh_providers()

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
            item = QListWidgetItem(label)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(
                Qt.Checked if template["id"] in checked else Qt.Unchecked
            )
            item.setData(Qt.UserRole, template["id"])
            self.template_list.addItem(item)
        self.template_warnings = warnings
        return warnings

    def refresh_models(self):
        checked = {
            self.model_list.item(row).text()
            for row in range(self.model_list.count())
            if self.model_list.item(row).checkState() == Qt.Checked
        }
        self.refresh_templates()
        self.model_list.clear()
        ollama_error = None
        ollama_names = []
        try:
            ollama_names, _source = list_models()
        except OllamaError as exc:
            ollama_error = exc
        providers, provider_warnings = load_providers()
        self.providers = providers
        self.provider_warnings = provider_warnings
        targets = [ollama_target(name) for name in ollama_names]
        for provider in providers:
            for model_entry in provider["models"]:
                targets.append(provider_target(provider, model_entry))
        if not targets:
            if ollama_error is not None:
                self.status_label.setText(str(ollama_error))
            else:
                self.status_label.setText(NO_MODELS_MESSAGE)
            self.run_button.setEnabled(False)
            return
        for target in targets:
            item = QListWidgetItem(target["label"])
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(
                Qt.Checked if target["label"] in checked else Qt.Unchecked
            )
            item.setData(Qt.UserRole, target)
            self.model_list.addItem(item)
        if not self.templates:
            self.status_label.setText(NO_TEMPLATES_MESSAGE)
            self.run_button.setEnabled(False)
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

    def _set_all_checked(self, widget, state):
        for row in range(widget.count()):
            widget.item(row).setCheckState(state)

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
            if item.checkState() == Qt.Checked:
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
        # Sorting stays OFF while the run builds the table: a live sort
        # moves rows mid-update and later setItem calls land on the wrong
        # model. It goes back on when the run finishes.
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        self.results = {}
        self.result_order = []
        self.detail_view.setPlainText("Run in progress...")
        self.save_button.setEnabled(False)
        self.run_button.setEnabled(False)
        self.refresh_button.setEnabled(False)
        self.select_all_button.setEnabled(False)
        self.clear_button.setEnabled(False)
        self.select_all_templates_button.setEnabled(False)
        self.clear_templates_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.progress_bar.setValue(0)
        self.worker = BenchWorker(
            targets, templates, self.sys_id, self.machine, get_results_dir()
        )
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
            score = run.get("template_score")
            if score is None:
                header = (
                    f"\n{run.get('template_name', template_id)}: no scoring, "
                    f"{fmt_toks(run.get('avg_decode_tok_s'))} tok/s"
                )
            else:
                header = (
                    f"\n{run.get('template_name', template_id)}: {score:.0f}% "
                    f"({run.get('passed')}/{run.get('total')}), "
                    f"{fmt_toks(run.get('avg_decode_tok_s'))} tok/s"
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
                mark = "pass" if passed is True else ("fail" if passed is False else "run")
                lines.append(
                    f"  {mark}  {task.get('id', '?')} "
                    f"({fmt_toks(task.get('decode_tok_s'))} tok/s)"
                )
                detail = task.get("detail") or ""
                if passed is False and detail:
                    lines.append(f"       {detail}")
        self.detail_view.setPlainText("\n".join(lines))

    # -- history ---------------------------------------------------------

    def on_tab_changed(self, index):
        if self.tabs.tabText(index) == "Past runs":
            self.refresh_history()
        elif self.tabs.tabText(index) == "Profiles":
            self.refresh_profiles()
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

    def on_history_model_changed(self, _index=None):
        slug = self.history_model_combo.currentData()
        self.history_list.clear()
        self.history_view.setPlainText("Pick a run to see its scores.")
        if not slug:
            return
        runs = list_history_runs(get_results_dir(), slug)
        for run in runs:
            score = run["score"]
            if score is None:
                score_text = "no scoring"
            else:
                score_text = f"{score:.0f}% ({run.get('passed')}/{run.get('total')})"
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
        self._refresh_provider_models_list()

    def _refresh_provider_models_list(self):
        self.provider_models_list.clear()
        if self.editing_provider is None:
            return
        for index, model in enumerate(self.editing_provider["models"]):
            text = model["id"]
            if model.get("display_name"):
                text += f" — {model['display_name']}"
            price_in = model.get("price_input_per_1m")
            price_out = model.get("price_output_per_1m")
            if price_in is not None or price_out is not None:
                in_text = f"${price_in}" if price_in is not None else "?"
                out_text = f"${price_out}" if price_out is not None else "?"
                text += f" ({in_text} in / {out_text} out per 1M)"
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, index)
            self.provider_models_list.addItem(item)

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
        })
        self.provider_model_id_input.setText("")
        self.provider_model_name_input.setText("")
        self.provider_price_in_input.setText("")
        self.provider_price_out_input.setText("")
        self._refresh_provider_models_list()
        self.provider_status.setText(
            f"Added {model_id}. Press Save provider to keep it."
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


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
