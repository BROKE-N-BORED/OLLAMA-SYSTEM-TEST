# TEST-REPORT — OLLAMA SYSTEM TEST v3

Verified 2026-11-12 in a Linux sandbox (x86_64, PySide6 6.12.0, Python 3.12.3)
under the Qt test env from AGENTS.md: QT_QPA_PLATFORM=offscreen,
LD_LIBRARY_PATH=~/workspace/qt-libs/root/usr/lib/x86_64-linux-gnu,
venv ~/workspace/forge-v2-testenv/bin/python. Plain widgets only, no OpenGL.

v3 fixes the two failures Joshua reported from his real PC on 2026-11-12:
v2 started with every model ticked (a model below the fold ran by accident),
and v2's run failed with "could not reach Ollama" on every model because
Ollama had stopped answering by run time. v3 also adds per-model profiles
and in-app model pulling. install.sh / run.sh are unchanged from v1/v2
(venv + PySide6-Essentials, the PEP 668 fix from the v1 report still stands).

The sandbox has no Ollama and no models, so all runtime tests ran against a
fake Ollama HTTP server (Python stdlib http.server) serving /api/tags,
/api/ps, streaming /api/generate with KNOWN numbers and CANNED ANSWERS per
model, and streaming /api/pull with progress lines. Test suite:
~/workspace/ollama-system-test-v3-verify/run_tests.py — **233 passed,
0 failed** (final run; the v2 suite's 151 checks are all carried over and
green inside that count).

## What was verified

### 1. Nothing selected by default

The main window opens with no model ticked and no template ticked, proven
in the GUI (checked_names() empty for both lists). Select-all buttons still
work. This is the durable rule from Joshua: never default anything to
selected again.

### 2. Pre-run check blocks a run when Ollama is down

With the API dead and the model list filled in from a fake `ollama` CLI on
PATH (2 CLI models listed, Run enabled), pressing Run starts no worker,
leaves the table empty, and shows exactly:
"Ollama is not running. Press Start Ollama, then Run again."

### 3. Start Ollama

Proven three ways with fake `ollama` binaries on PATH:
- Success: `ollama serve` starts a tiny server on the OLLAMA_HOST port;
  the app polls /api/tags, reports "Ollama is running.", and refreshes the
  model list (2 served models, both UNCHECKED).
- Already running: a second press does not launch a second serve — the
  fake CLI's call log shows exactly 1 serve call after two clicks.
- Timeout: a serve that never brings a server up yields, after the
  (test-shortened) timeout, exactly: "Ollama could not be started. Run
  ollama serve in a terminal to see the error."
- Missing binary: with no ollama on PATH, exactly: "Ollama is not
  installed. Install it first, then try again."

### 4. Retry once

The fake server drops the first generate attempt (one-shot). The run still
completes with status done, and the server counted exactly 4 generate
attempts: warmup fail + warmup retry + 2 speed prompts. RETRY_DELAY was
monkeypatched to 0.1 s for the test; the shipped value is 3 s.

### 5. Stop the run when Ollama is unreachable

Two independent proofs, both ticking fake-smart:2b + fake-partial:3b with
the Speed template:
- Server dies mid-run: a killer thread waits for the first generate to
  complete, then kills the server. Status is exactly "Ollama stopped
  responding. Start Ollama, then press Refresh and Run again.", the smart
  row reads "failed: could not reach Ollama — start it, then press
  Refresh", and the partial model was never attempted (no row).
- Mid-stream close: the server RSTs the connection after the first chunk.
  Same status, same row text, partial never attempted, and the server
  counted exactly 2 mid-stream closes (warmup + its retry) — proving the
  OSError branch in the streaming read loop fires and the retry still
  applies before the abort. The window stayed responsive afterwards.

Model-specific failures still continue to the next model: in the full run,
fake-broken:1b (HTTP 500 "model exploded") failed alone and the other three
models completed.

### 6. Per-model profiles

- Unit tests with hand-crafted run files (coding 100% then 40%, math 40%
  then 100%, across two systems): latest overall 70%, best 100%, average
  speed 13.0 tok/s, per-template latest/best table exact, strength Math,
  weakness Coding, verdict "Solid on this PC" with "Best at: Math." /
  "Weakest at: Coding."
- The full GUI run auto-wrote PROFILE.md for each tested model: smart
  profile latest 100%, verdict "Strong on this PC", all five scored
  templates listed as strengths, "None yet — no template under 50%."
  for weaknesses.
- Regenerate proven in the GUI: two later all-fail run files (coding 0%,
  math 0%) were added by hand, Regenerate was pressed, and the profile
  rebuilt to latest 60%, best still 100%, weaknesses Coding and Math,
  verdict "Solid on this PC" — while the original run files' SHA-256
  hashes were unchanged.
- The Profiles tab shows the profile read-only; the model picker and the
  Past runs picker both size to the longest model name (see Bugs found).

### 7. Pull models from the app

- Success: pulling fake-new:1b moved the progress bar (screenshot caught
  it at 25% with "downloading: 25.0 MB of 100.0 MB"), finished with
  "Pulled fake-new:1b.", bar at 100, input cleared, and the new model
  appeared in the list UNCHECKED.
- Failure: a server returning HTTP 500 on /api/pull produced a plain
  "pull exploded" error in the status line, the Pull button re-enabled,
  no crash, window still responsive.

### 8. v2 checks re-run green

- Scoring: all 15 combinations (5 scored templates x smart/dumb/partial
  brains) exact — 100% / 0% / 60% — plus every rule type individually
  (contains incl. word boundaries, numeric incl. floats, json incl.
  fenced/prose-wrapped, count_lines, max_words).
- Code execution: real subprocess runs — correct fib passes, wrong answer
  fails naming the test, syntax error fails, missing function fails, an
  infinite loop times out quickly.
- Immutability: a second save in the same second makes a -2 file and the
  first file's SHA-256 is unchanged; a speed rerun through the GUI added
  a second speed file and left the first file's bytes untouched.
- History tab: 3 models, 7 smart runs listed, coding run summary shows
  model, 100%, and the system id.
- Save report: writes the .txt + .json pair; txt has the 100% line and
  the failed model; json has smart overall 100.0 with 6 templates.
- Cancel: stops a slow run, status "Cancelled. Results so far are still
  in the table.", cancelled model has no saved run.
- CLI fallback: with the API dead and a fake `ollama` CLI on PATH, the
  window lists the 2 CLI models and Run is enabled.
- Ollama down: with no server and no binary, no models, the exact
  Ollama-down status, Run disabled, Start Ollama still enabled.
- Templates: the 6 shipped templates load with no warnings; a 7th
  template added as JSON loads in the GUI; corrupt and bad-rule template
  files are skipped with warnings, never a crash.
- Size in RAM recorded (fake-partial:3b = 3,000,000,000); /api/ps empty
  yields an empty size map.
- No bare `&` in any button or label text (checked programmatically).

### 9. Screenshots eyeballed

- screenshot-start.png: window opens with nothing ticked, Start Ollama
  button and pull section visible, no clipped text.
- screenshot-pull.png: pull at 25% with "downloading: 25.0 MB of 100.0 MB".
- screenshot-profiles.png: smart profile, full model name in the picker,
  scores table, strengths, weaknesses.
- screenshot-results.png: 100% / 60% / 0% / failed rows sorted best-first.
- screenshot-history.png: 7 smart runs grouped by system, coding summary.

## Bugs found and fixed in place

1. Fake-server race (test-side, not the app): the pull handler recorded
   the pulled model after its final sleep, so the app's list refresh
   (triggered by the "success" event) ran before the model existed. The
   handler now records the model the moment it writes the success event.
2. Model pickers clipped names (app bug, found by eyeballing the
   screenshot): the Past runs and Profiles pickers stayed at the width
   they had when empty, showing "fake-sm" for "fake-smart:2b". Both now
   use QComboBox.AdjustToContents. Two regression checks assert the
   picker is at least as wide as the longest model name in font pixels.
3. Refresh wiped ticks (v2 wart, fixed in v3): refresh_models /
   refresh_templates now preserve the user's existing check states; a
   fresh list starts with nothing ticked. This matters for the
   pull-refresh path, which must not disturb the user's selection.

## Not verified

- The real-PC run against real models (the closer for the v2 failures).
- System id on his Mint PC; real-model answer quirks vs the scorers.
- A real `ollama serve` start from the button (proven only with a fake
  CLI + tiny server); a real multi-GB pull (proven only against the fake
  server's 100 MB stream).
- Long real runs; code-exec against real model output.
