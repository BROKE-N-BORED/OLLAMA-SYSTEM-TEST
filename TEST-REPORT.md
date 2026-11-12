# TEST-REPORT — OLLAMA SYSTEM TEST v2

Verified 2026-11-12 in a Linux sandbox (x86_64, PySide6 6.12.0, Python 3.12.3)
under the Qt test env from AGENTS.md: QT_QPA_PLATFORM=offscreen,
LD_LIBRARY_PATH=~/workspace/qt-libs/root/usr/lib/x86_64-linux-gnu,
venv ~/workspace/forge-v2-testenv/bin/python. Plain widgets only, no OpenGL.

v2 adds to v1: data-driven test templates (JSON), per-task scoring, and
immutable per-model result folders organized per system. v1's speed test is
now the "Speed" template. install.sh / run.sh are unchanged from v1 (venv +
PySide6-Essentials, the PEP 668 fix from the v1 report still stands).

The sandbox has no Ollama and no models, so all runtime tests ran against a
fake Ollama HTTP server (Python stdlib http.server) serving /api/tags,
/api/ps and streaming /api/generate with KNOWN numbers and CANNED ANSWERS
per model. Test suite: ~/workspace/ollama-system-test-v2-verify/run_tests.py
— **151 passed, 0 failed** (final run; see run-log.txt in the verify tree).

## What was verified

### 1. Scoring, every rule type (canned answers, exact)

The fake server answers every task three ways:
- fake-smart:2b answers everything correctly → **100%** on every template.
- fake-dumb:1b answers everything wrong → **0%** on every template.
- fake-partial:3b answers the 1st, 3rd and 5th task of each template
  correctly → **60%** on every template.

All 15 combinations (5 scored templates x 3 brains) came out exact.
Rule types proven individually as well: contains (pass, fail,
case-insensitive, and the word-boundary trap — "I know." does NOT match
expected "no"), numeric (21 in "He has 21 apples.", float 150.0 matches
150, wrong number fails, no number fails), json (plain, fenced, and
prose-wrapped JSON pass; wrong value, missing key, and invalid JSON fail;
numeric values compare by number), count_lines (exactly 3, blanks ignored),
max_words (under the limit passes, 12 words against a 10-word limit fails).
Two extra rule types were added beyond the original spec because the
instruction examples need them: count_lines and max_words.

### 2. Code tasks run for real (subprocess)

Coding answers are extracted from ```python fences (fallback: the whole
answer) and run in a subprocess with a 10-second timeout per test case, in
a temp dir, using the system Python. Proven with real subprocess runs:
- Correct fib passes all its test cases.
- Wrong answer fails and names the failing test ("test 1 ...").
- Syntax error fails. Empty answer fails. Missing function fails.
- An infinite loop fails by timeout (measured, under 20 s for the check).

**Safety note:** generated code runs as the user, in a subprocess with a
10-second timeout per test case, in a temp dir. The harness imports the
generated file as a module, so module-level code runs too. These are his
own models on his own machine — the code being tested is what his model
wrote, not code from the internet.

### 3. Immutability and the per-system layout

- Layout proven: run files land in
  `results/<model-slug>/<system-id>/<timestamp>-<template>.json`.
  The system id is derived at runtime from /proc/cpuinfo, /proc/meminfo
  and /etc/os-release; in the sandbox it came out as
  `amd-epyc-9d25-126-core-processor_8gb_ubuntu-24-04-5-lts`.
- Same template saved twice → two files; the first file's SHA-256 is
  unchanged after the second save. The second file gets a `-2` suffix.
- Proven again through the real GUI: a Speed re-run after the full run
  created a second Speed file and the first file's hash was unchanged.
- Run files carry model, template, timestamp, system id, full machine
  details, the prompts used, and per-task results (prompt, raw answer,
  pass/fail, decode tok/s, failure detail) plus the template score.
  A real sample is in the verify tree: sample-run-file.json.
- The broken model (HTTP 500 at warmup) produced no run files at all.

### 4. Templates load from JSON

- All 6 shipped templates load, no warnings; 5 tasks each, Speed unscored
  with 2 prompts.
- A 7th template file added at test time was listed by the GUI (7
  templates). A corrupt JSON file and a file with an unknown rule type
  were skipped with warnings in the status line and the app kept going —
  proven at the loader level and in the GUI.

### 5. Full run through the real window

- Window constructs; 4 models from the fake /api/tags, 6 templates listed,
  all ticked by default. Run button runs 4 models x 6 templates in order
  (warmup + 27 generations per working model).
- Progress bar reaches 100%, status ends "Done. Press Save report to keep
  the results."
- Table sorts best score first, failed last: smart 100% / 20.0 tok/s,
  partial 60% / 10.0, dumb 0% / 5.0, broken "failed: model exploded"
  with "—" for numbers. The run carried on past the broken model.
- Detail pane: clicking a model shows per-template scores and per-task
  pass/fail with the reason (e.g. dumb's coding fails show the
  SyntaxError from running its nonsense answer).
- History tab: lists the 3 models with runs, 7 runs for the smart model
  (6 from the full run + the Speed re-run), grouped by system; clicking a
  run shows its summary (model, template, run time, system, score,
  per-task results). Summaries are computed at display time by reading
  the run files — there are no mutable summary files.
- No bare `&` in any button or label text (checked at runtime — the Qt
  mnemonic trap from AGENTS.md).

### 6. Save report

- Save report writes exactly 2 files into `results/`: bench-results-
  YYYY-MM-DD-HHMM.txt and .json.
- Text report checked: overall score, avg decode speed, size in RAM,
  per-template lines ("Coding: 100% (5/5), 20.0 tok/s"), failed model
  listed. JSON checked: parses, smart overall_score 100.0, 6 templates.
- Copies of a real saved pair: verify tree saved-report.txt /
  saved-report.json.

### 7. V1 checks still green

- Math: decode/prompt tok/s exact against the fake server's known numbers
  (20.0 / 40.0, 10.0 / 20.0, 5.0 / 10.0); time to first token near the
  fake 0.5 s first-chunk delay; load time and size in RAM recorded.
- Size in RAM is None (shown as "—") when /api/ps returns no models.
- Cancel: against a slow fake server (12 chunks at 1 s each), Cancel
  pressed 2 s in stops the run within seconds and the status reads
  "Cancelled. Results so far are still in the table."
- Ollama down: with no server and no `ollama` binary on PATH, the window
  shows "Ollama is not running. Start it first, then open this again."
  and Run is disabled.
- CLI fallback: a fake `ollama` executable on PATH printing canned
  `ollama list` output lists those 2 models and Run is enabled.

### 8. Screenshots (offscreen grabs, eyeballed)

- screenshot-start.png: intro text, 4 checked models, 6 checked
  templates, buttons, 0% bar, "4 models found, 6 templates loaded.
  Ready.", empty table with all 4 headers. No clipped text.
- screenshot-results.png: 100% bar, sorted table with all scores and
  speeds, "failed: model exploded" in full, detail pane with per-task
  results. No clipped text (see bug 2 below).
- screenshot-history.png: model picker, 7 runs grouped by system with
  scores, coding run summary with all 5 tasks passing. No clipped text.

## Bugs found and fixed during verification

1. **Leftover bad line in refresh_history.** An unused assignment read
   `runs[0]` before checking whether the list was empty — an IndexError
   waiting for a model with no runs. Removed before the first test run.
2. **"Avg decode tok/s" header clipped** in the results screenshot
   ("Avg decode tok/:"). Fix: the first three columns size to contents
   and Status stretches. Re-verified: 151/151 and the screenshot shows
   the header in full.
3. **Test-side fixes (app code untouched):** the generate spot-check used
   a prompt without the word "fibonacci" so the fake server returned its
   fallback answer; a QListWidget signal emit passed a column argument
   the signal does not take; the history check clicked the first listed
   run, which was the unscored Speed re-run. All three fixed in
   run_tests.py; the suite then passed 151/151 twice in a row.

## What was NOT verified

- **Nothing ran against real Ollama or real models.** The sandbox has
  neither. All numbers above come from the fake server's canned done
  events and canned answers. The first run on Joshua's PC, against his
  real models, is the closer.
- **The system id on his Mint PC.** The derivation is proven (it produced
  a correct id in the sandbox), but his id — expected to look like
  `ryzen-3-3250u_8gb_linux-mint` — is unproven until a real run.
- **Real model answers.** Scoring was proven against canned correct and
  wrong answers. Real models write messier answers (extra prose around
  code, numbers buried in sentences, JSON with commentary); the scorers
  tolerate fences and prose, but a real model's quirks are unproven.
- **The visible window on his desktop**: verified offscreen only. The
  window opening on his Mint desktop is his verdict.
- **Long real runs.** A full run is much longer than v1: each model
  answers 27 prompts (about 6,500 generated tokens). At ~10 tok/s that
  is roughly 11 minutes per model before load time, so 8 models x all
  templates is on the order of 1.5 hours. The README now says so and
  suggests starting with one model or one template. Cancel was proven
  on the fake server only.
- **Code-exec against real model output** was not exercised — only canned
  answers. A real model could write code that is slow, prints huge
  output, or loops; the 10-second timeout per test case bounds the
  damage, but it is unproven against a real answer.

## Test logs (in the verify tree, not the package)

`~/workspace/ollama-system-test-v2-verify/`: `run_tests.py`,
`run-log.txt`, `fake_ollama.py`, `screenshot-start.png`,
`screenshot-results.png`, `screenshot-history.png`,
`sample-run-file.json`, `saved-report.txt`, `saved-report.json`,
`results-test/` (the run files the suite produced), `fakebin/ollama`,
`templates-test/` (7th-template + corrupt-template fixtures).
