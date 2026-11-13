# TEST-REPORT — OLLAMA SYSTEM TEST v9

Verified 2026-11-12 in a Linux sandbox (x86_64, PySide6 6.12.0, Python 3.12.3)
under the Qt test env from AGENTS.md: QT_QPA_PLATFORM=offscreen,
LD_LIBRARY_PATH=~/workspace/qt-libs/root/usr/lib/x86_64-linux-gnu,
venv ~/workspace/forge-v2-testenv/bin/python, default HOME (overriding
HOME breaks Qt plugin init). Plain widgets only, no OpenGL. The
geometry round-trip ran under Xvfb instead (see the v7 report §2 —
offscreen's 800x800 screen is smaller than the window's minimum
width).

v9 is a repo-audit polish release (tasks T13–T15) — the last version
in the queue. No new templates: filter boxes above the model and
template lists on the Benchmark tab (ticked items keep their ticks
while filtered, grey-outs keep working); a dark theme on the View
menu, remembered across restarts; the README's prompt count fixed
to 103 with a screenshot of the window; and the app creates the
`images` and `audio` folders on its first launch.

v8 is a repo-audit management release (tasks T9–T11): a run can be
deleted from the Past runs tab (its file and its media folder go,
the profile rebuilds from the runs that are left) and the tab shows
how much disk the results store uses; pressing Run with provider
models ticked shows the estimated cost first and asks to confirm
when it is over $1.00 (or when no prices are saved, so the cost is
unknown); an opt-in per-template time budget stops a template's jobs
at the limit with a plain "time budget reached" status, default off;
and the current results export to CSV and HTML, with a Compare tab
putting two models' per-template scores side by side. No new
templates, no template changes.

The sandbox has no Ollama, no models, and no real provider accounts, so
all runtime tests ran against the fake servers from v5/v6 (fake Ollama
with /api/show, /api/chat with images and tools; fake OpenAI-compatible
provider with image_url parts, tool-call SSE deltas, image generation,
transcription, and speech). Test suite:
tests/run_tests.py in this tree — **1005 passed, 0 failed** (final
run; the v8 suite's 968 checks are all carried over and green inside
that count, plus 37 new v9 checks).

## What was verified (v9)

### 1. Search/filter on the model + template lists (T13)

- Filter boxes sit above both pick lists on the Benchmark tab, with
  "Filter models..." and "Filter templates..." placeholders.
  Typing "smart" shows only fake-smart:2b; "vision" finds both
  vision models through the capability brackets in their labels;
  the match is case-insensitive; "math" shows only the Math
  template and "needs audio" only the Audio template.
- Filtering hides items, never removes them: both lists keep their
  full count (4 models, 19 templates) while filtered.
- A ticked model filtered out of view keeps its tick, and clearing
  the filter restores every item with ticks intact.
- Grey-outs keep working with a filter on: ticking the text-only
  model while the Vision template is filtered out of view greys it
  out anyway, and it stays greyed when the filter clears; a
  filtered-in ineligible template shows greyed.
- A refresh rebuilds both lists from scratch and the filter text
  the user already typed still applies to the new items.

### 2. Dark mode (T14)

- View > Dark mode is a checkable menu item; the menu bar is now
  File, View, Help. Dark mode starts off — light is the default
  and the application stylesheet is empty until the user toggles.
- Toggling sets a dark application stylesheet (dark backgrounds
  #1e1e1e / #2b2b2b with light text) covering lists, tables, text
  views, tabs, buttons, combo boxes, menus, progress bars, and
  scrollbars; the choice is saved to QSettings.
- A new window opened with the same settings comes up dark — the
  choice survives a restart — and toggling back restores the light
  theme and saves that too.
- Eyeballed from a real grab (tests/screenshot-dark.png, saved by
  the suite): the whole window is dark with readable light text —
  no white text-edit boxes, no unreadable labels. One defect found
  and fixed in place during the eyeball: the scrollbar arrow
  buttons were unstyled and showed as a white square at the bottom
  of the template list in dark mode; they are now styled dark.
- The suite's dark-mode test saves the prior QSettings values and
  restores them afterwards, so the run leaves no state behind.

### 3. README fixes + repo hygiene (T15)

- The prompt count was verified from the templates before it was
  written: 90 scored tasks (18 templates x 5) + 5 multi-turn extra
  turns + 5 role-following extra turns + 3 speed runs
  (context_lengths 1024, 8192, 32768) = 103 prompts on a full run.
  The README states 103 and no longer says "roughly 80".
- The README shows a screenshot of the window: screenshot.png is a
  real 980x720 PNG grabbed from the app itself (Xvfb, fake Ollama
  running so the model list is populated — 4 models, 19 templates,
  5 tabs, filter boxes visible). NOTE FOR THE LEAD: the PNG is
  binary and cannot ride the snapshot push — upload it via the web
  UI at ship time; the README image link is broken until that
  upload lands.
- The app creates `images/` and `audio/` on launch: with both dirs
  pointed at scratch folders that did not exist, opening a window
  created them.
- Description + topics are GitHub repo SETTINGS, not files — the
  lead sets them via the web UI at ship time. Suggested
  description: "Benchmark the Ollama models on your PC — and cloud
  providers' models too — through 19 test templates with per-model
  profiles." Suggested topics: ollama, benchmarking, llm, pyside6,
  desktop-app.

## What was verified (v8)

### 1. Run deletion + store-size display (T9)

- The Past runs tab has a Delete run button and a store-size label.
  The label reads "Results use <size>" (one decimal for MB, e.g.
  "12.3 MB") and matches an independent walk of the results folder
  byte for byte; it updates after a run and after a deletion.
- A real provider run (image_generation + audio) wrote run files
  plus a 5-image `-images` folder and a 2-clip `-audio` folder.
  Selecting the image run enables Delete; answering No to the
  confirm dialog leaves the run file and the media folder in place.
- Answering Yes removes the run file AND its media folder, leaves
  the model's other runs (and their media) alone, leaves other
  models' runs alone, drops the run from the history list, and
  shrinks the store size. The model's PROFILE.md regenerates from
  the remaining runs: the Image Generation row is gone and the run
  count drops (3 → 2).
- Delete starts disabled with no run selected, and is disabled while
  a run is in progress (checked mid-run against the slow fake, then
  re-enabled after the run is cancelled and a run is selected).
- Unit level: delete_run_file removes a run file and both media
  folders (3 paths returned), leaves a neighbouring run alone, and
  deleting a missing file is not an error; store_size counts run
  files and media.

### 2. Cost guard + per-template time budget (T10)

- Token estimate math (unit level): math predicts 5 × num_predict
  completions and a small positive prompt count; speed predicts the
  exact context lengths (1024 + 8192 + 32768) with 3 × num_predict
  completions; long_context counts the exact context tokens;
  multi-turn counts 5 tasks × 2 turns × num_predict; image
  generation and audio predict zero text tokens, matching the run
  files, which record None for those tasks.
- Cost estimate math (unit level): a priced provider model on math
  at $1000/1M estimates $1.666 (warmup included: 102 prompt +
  1564 completion tokens), over the $1.00 threshold; no prices
  saved means no total and the model listed as unknown; local
  Ollama targets are never estimated; templates a model cannot run
  are not estimated.
- Cost guard (GUI, dialogs driven by a stubbed QMessageBox.question):
  an over-threshold provider run asks to confirm once, the dialog
  names the estimated cost ($1.67) and the model; answering No
  starts no run ("Run cancelled."); answering Yes runs it to 100%.
  A cheap run ($0.01/1M prices) asks nothing and completes (partial
  math 60%). A run with no prices saved asks to confirm and the
  dialog says "no prices saved — cost unknown" plainly. A
  local-only run never sees the guard.
- Time budget (a real run through BenchWorker, 1-second budget on
  math then coding): the math template stops partway — the run file
  carries time_budget_reached and budget_stopped ≥ 1, stopped tasks
  carry the plain "time budget reached" detail, and the score is
  100% over the tasks that ran only. The run carries on with coding
  in its own fresh budget window (it runs, scores 100% over the
  tasks that ran, and gets its own budget flag), and the run still
  finishes "done". The history summary says "Stopped early: time
  budget reached", the detail view says it and marks the stopped
  tasks, and the history list marks the run. With no budget the
  same template runs all 5 tasks to the end. The budget control
  defaults to 0 (no limit); 0 maps to no budget, 2 minutes maps to
  120 seconds.

### 3. Result export CSV/HTML + side-by-side comparison (T11)

- A two-model run (smart + partial, math + coding) enables both
  export buttons. Export CSV writes a file (via a stubbed file
  dialog) carrying both models, 100% / 60%, and the Math and Coding
  columns; it parses with csv.reader into the header row plus one
  row per model, and carries no API key. Export HTML writes a file
  carrying the same models and scores, parses as basic markup
  (table/tr/td present, closing </html>), has one row per model
  plus the header, and carries no API key.
- A fresh window's export buttons start disabled, and exporting
  with no results says "Nothing to export yet — run a test first."
  for both formats.
- Unit level: the CSV and HTML renderers mark a not-run template
  and a template one model never ran ("—"); the HTML escapes model
  names (a "<script>" name never appears raw).
- Compare tab: the window has 5 tabs with Compare after Profiles.
  Both dropdowns list the models with runs and start unpicked
  (nothing selected by default); the tab asks to "Pick two models
  to compare." Picking smart vs partial shows a 4-row table — Math
  100% vs 60%, Coding 100% vs 60%, Overall 100% vs 60%, speed 20.0
  vs 10.0 tok/s — with the two model names as column headers.
  Picking the same model twice shows "Pick two different models to
  compare." and no table. Unit level: compare_models returns the
  latest scores (overall 90.0 vs 60.0 on crafted runs), marks a
  template one model never ran, and returns None when a model has
  no runs.

### 4. Carried-over v7 checks (all green)

- All 853 v7 checks pass inside the new total, including the
  headless runs, the geometry round-trip probe, the shortcuts and
  menu bar, the .desktop launcher installs, both install.sh
  end-to-end runs, and the fixture "not run" run.
- Four carried checks were updated for v8, not weakened: the
  version checks now assert `__version__ == "8"` and the v8 title
  bar; the tab check now asserts five tabs in the new order
  (Benchmark, Past runs, Profiles, Compare, Providers); and the six
  provider-section tab switches that meant "Providers tab" now use
  its new index (4), since Compare sits at 3.
- The suite now stubs QMessageBox.question globally (answering Yes)
  for the carried sections: the cost guard is the app's first modal
  dialog in the run path, and the carried provider sections tick
  models with no prices saved, so the guard would ask on every run
  and a real dialog would hang the suite with nobody to click it.
  The v8 sections replace the stub with a controllable one that
  records the dialog text and answers Yes or No on demand, and the
  real dialog is restored at the end of the suite.

### 5. Launch smoke

- The window opens offscreen with no Ollama running, stays up with
  no traceback, shows the title "OLLAMA SYSTEM TEST v8" and the
  plain "Ollama is not running. Start it first, then open this
  again." status. Five tabs, budget default 0, export and delete
  buttons disabled with no results, store label "Results use 0 B",
  compare message "No models have run yet. Run a test first."

## Bugs found and fixed during this build

- The first full-suite run hung in the carried "mixed failure run":
  the dead provider's model has no prices saved, so the new cost
  guard opened a real confirm dialog and the suite waited out its
  300-second timeout. Fixed in the suite, not the app — the guard
  asking is the specified behaviour; the suite now stubs
  QMessageBox.question globally (see §4).
- A first version of the budget test expected the template AFTER a
  budget-stopped one to run to the end. It does not: the budget is
  per (model, template) pair, so with a 1-second budget every
  template stops partway in its own window. The app was right and
  the check was wrong — fixed in the test to assert the run carries
  on (the next template runs in its own window and the run
  finishes "done").
- The Compare tab insertion moved Providers from tab index 3 to 4,
  which the carried provider tests address by index. Fixed in the
  tests (six switches updated to 4); the app dispatches tab changes
  by tab name, so it needed no change.

## Not verified

- A real-PC run: the closer for everything above. In particular
  deleting a real run (and watching the profile rebuild), the cost
  guard against a real provider bill, opening the exported CSV and
  HTML on his PC, and the Compare tab on his screen are all
  unproven until his verdict.
- Carried from v7: the desktop icon on a real desktop (Nemo trust
  step, the icon showing in the menu, double-click install), the
  geometry restore on his 1366x768 screen, and a real headless run
  from cron.
- Carried from v6: real Ollama /api/show answers for real models,
  real provider image generation and audio calls with a real key,
  real vision quality, a real multi-GB pull, a real `ollama serve`
  from the Start Ollama button, long-context scoring on a real model
  at 32K, and the real time a 32K speed run takes on his Ryzen 3.
- The GitHub Actions workflow has not run on GitHub yet — it runs
  when the push lands. The suite it runs is the one verified here.

---

# TEST-REPORT — OLLAMA SYSTEM TEST v7

Verified 2026-11-12 in a Linux sandbox (x86_64, PySide6 6.12.0, Python 3.12.3)
under the Qt test env from AGENTS.md: QT_QPA_PLATFORM=offscreen,
LD_LIBRARY_PATH=~/workspace/qt-libs/root/usr/lib/x86_64-linux-gnu,
venv ~/workspace/forge-v2-testenv/bin/python, default HOME (overriding
HOME breaks Qt plugin init). Plain widgets only, no OpenGL. The
geometry round-trip ran under Xvfb instead (see §2 — offscreen's
800x800 screen is smaller than the window's minimum width).

v7 is a repo-audit usability release (tasks T5–T8): a headless mode
that runs the same worker with no window, for scripts and cron; the
version in the title bar from a single `__version__`, plus a window
icon; geometry persistence, keyboard shortcuts, and a menu bar; and
double-click desktop files (Install-OST + OST), with install.sh
putting the launcher in the app menu. No new templates, no template
changes.

The sandbox has no Ollama, no models, and no real provider accounts, so
all runtime tests ran against the fake servers from v5/v6 (fake Ollama
with /api/show, /api/chat with images and tools; fake OpenAI-compatible
provider with image_url parts, tool-call SSE deltas, image generation,
transcription, and speech). Test suite:
tests/run_tests.py in this tree — **853 passed, 0 failed** (final run,
re-run by the lead after the headless not-run fix below; the v6 suite's
778 checks are all carried over and green inside that count, plus 75
new v7 checks).

## What was verified (v7)

### 1. Headless mode (T5)

- `--help` exits 0 and documents --headless, --models, --templates.
- A full headless run as a subprocess with NO display at all (no
  DISPLAY, no QT_QPA_PLATFORM — creating a QApplication there would
  fail to load a platform plugin and die): `--headless --models
  fake-smart:2b,v7-provider / fake-gpt-smart --templates coding,math`
  exits 0, prints a plain summary ("100% overall", "Run files saved
  under ..."), prints no traceback, writes the same 4 immutable run
  files (2 models x 2 templates, GUI run-file shape, 100% scores)
  plus both profiles, and no API key appears in any run file or
  profile. The run order follows the --models order.
- Exit 1 with a plain message for an unknown model ("Model not
  found", listing the models it knows about), an unknown template
  ("Template not found", listing the template ids), no --models at
  all, and Ollama down ("Ollama is not running..."). Provider-only
  runs skip the Ollama check, same as the window.
- The GUI path is unchanged when --headless is not passed.

### 2. Version in window + window icon (T6)

- `__version__ == "7"`, and the title bar shows
  "OLLAMA SYSTEM TEST v7" built from it. The About box reads the
  same value (§3).
- ost-icon.png ships in the tree (256x256 PNG, 12,462 bytes, a gauge
  mark rendered with Qt offscreen for this release) and the window
  icon is set from it (not null). The icon is binary and rides the
  Release zip; it needs a web-UI upload to reach the repo, like the
  fixtures.

### 3. Geometry persistence + shortcuts + menu bar (T7)

- Closing the window saves a geometry blob to QSettings
  ("BrokeNBored" / "OLLAMA SYSTEM TEST").
- The full round-trip (resize to 1200x700, move to 100,80, close,
  reopen) restores the exact frame geometry, checked by
  tests/geometry_probe.py under Xvfb at 1400x900. It cannot be
  checked in-process under offscreen: that platform reports an
  800x800 screen, the window's minimum width is ~841, and Qt clamps
  any restore to the screen — the in-process check covers the save,
  the probe covers the restore.
- The menu bar has File (Save report, Quit) and Help (About). About
  shows "OLLAMA SYSTEM TEST v7" from `__version__`. The Save report
  menu item starts disabled, mirrors the button's enabled state, and
  its shortcut is Ctrl+S.
- Ctrl+R with nothing ticked asks for a model and starts no worker;
  Ctrl+R with a model and template ticked starts a real run that
  completes (math 100%); Ctrl+R during a run is a no-op (same
  worker, still running). Esc with no run running is a no-op; Esc
  during a slow run cancels it with the "Cancelled. Results so far
  are still in the table." status. The Save report action writes
  nothing with no results and writes both report files after a run.
  The Quit menu item closes the window.

### 4. .desktop launcher (T8)

- Install-OST.desktop and OST.desktop ship in the repo and parse
  strictly: [Desktop Entry] header, Type=Application, Name, and the
  right Terminal value each. Both Exec lines use the python3 -c
  pattern with %k as its own token, resolve the folder from
  sys.argv[1] with an os.getcwd() fallback, and carry no nested
  escaped quotes (GLib rejects those and the whole file fails).
  desktop-file-validate runs when installed; it is not installed in
  the sandbox, so the strict manual parse is the check there.
- The installer Exec logic, run against a stub install.sh: the
  folder resolves from %k (in a path with a space) and from the cwd
  when %k is empty (the gio case), printing "Install finished.".
- `gio launch OST.desktop` (cwd = the app folder, %k empty) exits 0
  and opens the app, which stays up until stopped.
- install.sh re-run on the v6 install-good tree with a test HOME
  exits 0, prints "Launcher installed: OST is now in your app
  menu.", and lands OST.desktop in
  ~/.local/share/applications/ and ost-icon.png in
  ~/.local/share/icons/ (byte-identical to the tree icon). The
  installed launcher has the app folder baked into its Exec line
  (quoted, so spaced paths work) — %k would point at the menu copy,
  not the app — and still parses with no nested escaped quotes.
  Launching the installed copy from a different folder opens the
  app: only the baked-in path can find it there.
- The desktop step never fails the install: if it has a problem it
  prints a note and the app still runs from run.sh.

### 5. Carried-over v6 checks (all green)

- All 778 v6 checks pass unchanged inside the new total, including
  the fixture "not run" run, atomic providers.json, the suite-in-repo
  + CI checks, the PySide6 guard, and both install.sh end-to-end
  runs (good install exits 0 with the pinned 6.12.0, broken install
  fails loudly with no "Done.").

### 6. Launch smoke

- The window opens offscreen with no Ollama running, stays up with
  no traceback, shows the title "OLLAMA SYSTEM TEST v7", and shows
  the plain "Ollama is not running. Start it first, then open this
  again." status.

## Bugs found and fixed during this build

- Headless mode ran provider models before local ones whatever order
  --models gave them (targets were collected provider-first). Fixed:
  targets are resolved by kind but assembled back in the order the
  user named them.
- The new "no nested escaped quotes" check tested `'\"' not in ...`,
  which in Python is just a plain double quote — it failed on every
  Exec line, since the -c argument is legitimately quoted. Fixed to
  test for the real two-character sequence `'\\"'`. The .desktop
  files themselves were correct throughout (gio launched them fine);
  only the test's literal was wrong.
- The headless summary printed "no score" for a not-run template,
  losing the v6 "not run" distinction (a missing fixture would have
  looked like an unscored run). Fixed in review: it prints
  "not run (test file missing)" when a template has not-run tasks and
  no score. Two checks pin it (headless vision run, fixtures missing).

## Not verified

- A real-PC run: the closer for everything above. In particular the
  desktop icon on a real desktop (Nemo trust step, the icon showing
  in the menu, double-click install), the geometry restore on his
  1366x768 screen, and a real headless run from cron are all
  unproven until his verdict.
- Carried from v6: real Ollama /api/show answers for real models,
  real provider image generation and audio calls with a real key,
  real vision quality, a real multi-GB pull, a real `ollama serve`
  from the Start Ollama button, long-context scoring on a real model
  at 32K, and the real time a 32K speed run takes on his Ryzen 3.
- The GitHub Actions workflow has not run on GitHub yet — it runs
  when the push lands. The suite it runs is the one verified here.

---

# TEST-REPORT — OLLAMA SYSTEM TEST v6

Verified 2026-11-12 in a Linux sandbox (x86_64, PySide6 6.12.0, Python 3.12.3)
under the Qt test env from AGENTS.md: QT_QPA_PLATFORM=offscreen,
LD_LIBRARY_PATH=~/workspace/qt-libs/root/usr/lib/x86_64-linux-gnu,
venv ~/workspace/forge-v2-testenv/bin/python, default HOME (overriding
HOME breaks Qt plugin init). Plain widgets only, no OpenGL.

v6 is a repo-audit fix release (tasks T1–T4): a missing test image or
audio clip records its tasks as "not run" instead of failing them;
providers.json is written atomically and owner-only from the first
byte; the verification suite lives in the repo under tests/ with a
GitHub Actions workflow; launching without PySide6 prints a plain
message naming install.sh; install.sh verifies the install before
reporting success; PySide6-Essentials is pinned to 6.12.0. No new
templates, no template changes.

The sandbox has no Ollama, no models, and no real provider accounts, so
all runtime tests ran against the fake servers from v5 (fake Ollama
with /api/show, /api/chat with images and tools; fake OpenAI-compatible
provider with image_url parts, tool-call SSE deltas, image generation,
transcription, and speech). Test suite:
tests/run_tests.py in this tree — **778 passed, 0 failed** (final run,
re-run by the lead after three fixes below; the v5 suite's 686 checks
are all carried over and green inside that count, plus 92 new v6
checks).

## What was verified (v6)

### 1. Fixture check + "not run" (T1)

- load_templates warns when a referenced fixture is missing (one
  warning per missing file, however many tasks name it) and still
  loads the template — a missing file never drops a template.
- _task_vision returns passed None with detail "not run: test image
  not found" when the image is missing; _task_audio returns passed
  None with "not run: test audio not found" when the clip is missing.
  Both records still name the missing file.
- Template scores count only the tasks that ran. A template where
  nothing ran has no score at all (template_score None, passed None,
  total 0, not_run 5) — never 0%.
- Full GUI run with the fixtures missing (empty images/audio folders):
  smart ran Vision only — 5/5 not run, no score, overall a dash;
  the provider model ran Audio — 2 speech tasks passed, 3 transcribe
  tasks not run, 100% (2/2, 3 not run). No vision image was sent and
  no transcribe call was made while the files were missing.
- The detail view shows "Vision: not run (5 tasks not run — test
  file missing)" with per-task "not run" marks and the reason, and
  "Audio: 100% (2/2, 3 not run)". The history list shows the vision
  run as "(not run)" and the audio run with its partial count; the
  history summary shows "Score: not run" for the vision run and
  "Score: 100% (2/2 tasks, 3 not run)" for the audio run. The profile
  shows "| Vision | not run | not run | 1 |". The saved session report
  renders "not run" the same way.
- is_not_run marks only real not-run tasks: not a passed task, not a
  failed task, and not an unscored (Speed) task.
- The shipped templates still load with zero warnings when the
  fixtures are present.

### 2. Atomic providers.json + .gitignore (T2)

- save_providers writes to a temp file in the same directory, chmods
  it 0o600 before writing any key bytes, then renames it over
  providers.json. The written file is mode 0o600 and round-trips.
- A failed write (an unserializable value) raises, leaves the old
  file byte-identical, leaves no temp file behind, and the old file
  still loads.
- .gitignore covers providers.json, capabilities.json, results/,
  __pycache__/, .venv/, and *.pyc, plus the suite's run artifacts.

### 3. Suite in the repo + CI (T3)

- The suite runs green from tests/ and locates the app at the repo
  root (parent of tests/), with no hardcoded tree name. The fake
  servers, fixture/template generators, test templates, and fake
  `ollama` CLIs are all in the repo; the generators point at the
  repo root.
- .github/workflows/test.yml exists, runs tests/run_tests.py with
  QT_QPA_PLATFORM=offscreen, installs from requirements.txt, and
  regenerates the fixtures first if a checkout is missing them.
- The README documents running the suite locally.

### 4. PySide6 guard + install verification + pin (T4)

- Launching with a Python that has no PySide6 (system python3)
  exits 1 and prints "This app needs PySide6 to open its window, and
  it is not installed. Run install.sh first — it installs PySide6
  for you." — no traceback.
- requirements.txt pins PySide6-Essentials==6.12.0; install.sh
  installs from it and verifies (PySide6 imports, the app
  py-compiles) before reporting success.
- install.sh run end to end in a scratch copy (workspace disk, not
  /tmp): exit 0, "PySide6 OK.", "Done.", and the installed PySide6
  reports 6.12.0. A scratch copy with requirements.txt removed fails
  loudly (exit 1, no "Done.").
- The README documents `chmod +x install.sh run.sh` after a clone.

### 5. Carried-over v5 checks (all green)

- All 686 v5 checks pass unchanged inside the new total, including
  capability screening, grey-outs, all scorer rules, media
  validation, the full GUI run (smart 100%, partial 60%, dumb 0%),
  the provider media run (5 images + 2 clips saved), profiles,
  history, pull, cancel, retry, and no API key in any run file,
  profile, report, status line, detail view, or cache.

### 6. Launch smoke

- The window opens offscreen with no Ollama running, stays up with
  no traceback, lists 19 templates, and shows the plain
  "Ollama is not running. Start it first, then open this again."
  status.

## Bugs found and fixed during this build

- The new v6 suite referenced `app.BenchmarkWorker` — the worker class
  is `BenchWorker`. The suite crashed after 689 checks (all 686 carried
  checks green, plus 3 new ones). Fixed the reference in the test.
- `_not_run_suffix` rendered ", 3 tasks not run", so score lines read
  "Score: 100% (2/2 tasks, 3 tasks not run)". Fixed to ", 3 not run",
  matching the intended format everywhere it appears.
- The v6 GUI test's provider entry used an API key the fake provider
  rejects, so the warmup got a 401 and the provider model ran nothing
  (7 checks failed on the missing audio run). The test now uses
  `fake_ollama.PROVIDER_API_KEY`, the key the fake accepts.

Note: the builder was cut off by a runtime restart-drain just before
its final suite run, so the counts above come from the lead's own
verification runs (three full runs: crash fix, suffix fix, key fix).

## Not verified

- A real-PC run: the closer for everything above. Real Ollama
  `/api/show` answers for real models (capabilities arrays and
  context lengths) are unproven until then; the fake's are invented.
- Real provider image generation and audio calls with a real key.
- Real vision quality: the fake answers from the prompt, and the
  bundled images are simple shapes and a sign. The app proves the
  plumbing and the scoring, not that a real vision model sees well.
- Image quality is deliberately never scored by the app — the
  generated pictures are saved for his eyes.
- Speech quality: the bundled clips are tone fixtures, not speech.
  A real transcription test needs a recording of someone speaking.
- The GitHub Actions workflow has not run on GitHub yet — it runs
  when the push lands. The suite it runs is the one verified here.
- Carried from v5: a real multi-GB pull, a real `ollama serve`
  started from the Start Ollama button, long-context scoring on a
  real model at 32K, and the real time a 32K speed run takes on his
  Ryzen 3 (minutes, by design).

---

# TEST-REPORT — OLLAMA SYSTEM TEST v5

Verified 2026-11-12 in a Linux sandbox (x86_64, PySide6 6.12.0, Python 3.12.3)
under the Qt test env from AGENTS.md: QT_QPA_PLATFORM=offscreen,
LD_LIBRARY_PATH=~/workspace/qt-libs/root/usr/lib/x86_64-linux-gnu,
venv ~/workspace/forge-v2-testenv/bin/python. Plain widgets only, no OpenGL.

v5 adds CAPABILITY SCREENING and 13 new templates (19 total). The app asks
Ollama what each model can do (via `/api/show`), shows the capabilities in
brackets after the model name, caches them in `capabilities.json`, and
greys out templates no ticked model can run. Provider models get
capability checkboxes on the Providers tab (default text-only). The new
templates are Vision, Image Generation, Tool Calling, Long Context,
Multi-turn, Code Repair, Planning, Grounded QA, Refusal, Summarisation,
Structured Output, Role Following, and Audio. The Speed template now
measures tok/s at 1K, 8K, and 32K context instead of two plain prompts.
install.sh / run.sh are unchanged from v4 (venv + PySide6-Essentials,
the PEP 668 fix from the v1 report still stands).

Note on the template count: the v5 task and spec both say "14 templates"
in their headers but enumerate 13 new template files on top of the 6
carried over — 19 files in all. The enumeration is the instruction and
the header count is a summary error, so all 19 were built.

The sandbox has no Ollama, no models, and no real provider accounts, so
all runtime tests ran against fake servers: the fake Ollama server from
v4, extended with `/api/show` (capabilities per model), `/api/chat` with
images and tools, and the fake OpenAI-compatible provider server,
extended with image_url chat parts, tool-call SSE deltas,
POST `/v1/images/generations`, and POST `/v1/audio/transcriptions` and
`/v1/audio/speech`. Test suite:
~/workspace/ollama-system-test-v5-verify/run_tests.py — **686 passed,
0 failed** (final run; the v4 suite's 355 checks are all carried over
and green inside that count, with the counts the v5 design intentionally
changes: 19 templates, 3 speed runs, 5 retry attempts, 14 provider run
files, 1,580/15,800 provider tokens).

The fake models are three brains — smart (right answers), partial
(right on tasks 1, 3, and 5), dumb (wrong on everything) — plus a
broken model that errors. The fake cannot truly see: for vision tasks
it records that images arrived and answers from the prompt text, which
is enough to prove the image path, the scoring, and the grey-outs, but
not vision quality on a real model.

## What was verified

### 1. Screening via /api/show

- ollama_show returns the capabilities array and the model info
  (llama.context_length) from the fake.
- capabilities_from_show: smart [completion, vision, tools] with a
  131072 context becomes {text, vision, tools, long_context}; partial
  [completion, vision] with 32768 becomes {text, vision,
  long_context}; dumb [completion] with 4096 is text-only.
- A model that reports no context length is treated as long-context
  capable (the attempt is made and the score says whether it coped);
  a reported length under 32768 blocks long context.
- screen_ollama_model returns the capability set and writes the
  sorted list into the cache; on failure it keeps the cached value,
  or text-only when there is none. Screening never breaks the list.

### 2. Capabilities cache (capabilities.json)

- A missing file means nothing screened yet, not an error; a corrupt
  file is empty, not a crash.
- Round-trip: save then load keeps every model and its capabilities
  (stored alphabetically sorted).
- Unknown capability names are dropped on load; known entries survive.
- No API key is ever in the cache file (checked after the media run).

### 3. Grey-outs in the GUI

- Model list labels: smart shows "[vision, tools, long_context]",
  partial "[vision, long_context]", dumb and broken show no bracket.
- Template labels say what they need ("Audio (5 tasks) — needs audio",
  "Image Generation (5 tasks) — needs image generation").
- With nothing ticked, all 19 templates are enabled.
- Tick the text-only dumb model: Vision, Tool Calling, Long Context,
  Image Generation, and Audio grey out; the text templates stay
  enabled. A greyed-out template cannot be selected, even when
  checked programmatically, and Select all skips disabled items.
- Tick the smart model too: Vision, Tool Calling, and Long Context
  come back; Image Generation and Audio stay greyed (no local model
  has them).
- Ticking a model that greys a template out also unticks that
  template; unticking all models re-enables everything.
- Screen models: deleting the cache and pressing the button rebuilds
  it and restores the labels.

### 4. New scorer rules (unit tests, positive and negative)

- contains_all: passes only when every required phrase is present.
- Refusal: is_refusal recognises "I'm sorry, I can't", "I can't help
  with that", "I'm unable to help", "I refuse to", "against my
  guidelines", and friends; score_refusal passes a "comply" task when
  the answer is not a refusal and a "refuse" task when it is.
- Schema: the WHOLE trimmed answer must parse as JSON (fenced or
  prose-wrapped JSON fails), then the subset validator checks type
  (a bool is not an integer), required, properties, items, and enum.
- Steps: every expected step must appear, in order, as a normalised
  substring.
- Tool call: the right tool with the right arguments passes (numeric
  arguments within 1%, strings case-insensitive, extra arguments
  ignored); the wrong tool, a missing or wrong argument, two calls,
  or no call fails; a no-tool-needed task passes only when no tool
  is called.
- score_task defers tool_call, image, and audio to their runners
  instead of scoring them from text.

### 5. Media validation

- image_info reads PNG, JPEG, and GIF headers (format, width,
  height) and returns None for garbage or empty bytes. The bundled
  shapes.png is (png, 400, 300) and sign.png is (png, 400, 200).
- is_audio_bytes recognises WAV, MP3 (with and without an ID3 tag),
  OGG, MP4, and FLAC magic, and rejects garbage and empty bytes.
- The bundled images are pixel-verified: shapes.png has 3 red
  circles (15,075 red pixels) and 2 blue squares (12,800 blue
  pixels); sign.png carries a white "42" (2,800 glyph pixels).
  The bundled audio clips are 1-second sine tones (440/660/880 Hz).

### 6. Long documents and speed padding

- build_long_document puts the needle in the document at the start,
  middle, and end, at the right length (4 chars per token), for
  1K, 8K, and 32K.
- The filler paragraph contains none of the fake's answer keys, so a
  long-context task can only pass by finding the needle.
- build_context_prompt pads the Speed base prompt to the requested
  context length.

### 7. Template validation

- A template naming an unknown capability is skipped with a warning,
  never silently treated as text.
- A chat template with no scored turn, a tools template with a text
  rule, an image template with a text rule, and a long-context
  template with a bad needle position are all skipped; the 19 good
  templates still load beside them.
- All 19 shipped templates load with zero warnings.

### 8. Chat, images, and tools over HTTP (direct calls)

- chat_once (Ollama): returns the answer and decode 20.0; with an
  image attached the fake records it; with tools it returns
  [{"name": "get_weather", "arguments": {"city": "Paris"}}]; with a
  pirate system prompt the answer stays in character; the fake
  records every chat history.
- provider_chat_once: image_url parts are counted by the fake;
  tool calls are parsed back out of the SSE deltas; role answers
  stay in character; token counts are reported.
- provider_generate_image: a valid 1x1 PNG for the smart brain, a
  valid PNG for the partial brain on its 1st/3rd/5th tasks, and a
  ProviderError ("returned bytes that are not an image") for the
  partial brain's other tasks and for the dumb brain.
- provider_transcribe: the right words by filename and brain
  ("hello world" for smart, wrong for dumb, partial right only on
  its 1st/3rd/5th tasks).
- provider_synthesize: real audio bytes for smart, audio for
  partial on its speech task, empty bytes otherwise.
- Image and audio calls do NOT retry (unlike generate/chat, which
  retry once on unreachable).

### 9. Full GUI run — scores and skips

One run, all 4 local models, all 19 templates (image_gen and audio
disabled, since no local model has those capabilities):

- Smart ran 17 templates, 100% on every scored one; 17 run files.
- Partial ran 16 templates, 60% on every scored one; tools skipped
  as "not eligible"; 16 run files, no tools run file.
- Dumb ran 14 templates, 0% on every scored one; vision, tools, and
  long context skipped as "not eligible"; 14 run files, no run file
  for any skipped pair.
- Overall scores: smart 100%, partial 60%, dumb 0%. Skipped pairs
  never count toward a score.
- The detail view shows "not eligible for this model" for the
  skipped pairs (checked for partial/tools and dumb/vision,
  dumb/tools, dumb/long_context).
- The fake saw 10 vision images (smart 5 + partial 5) and 5 tool
  calls (smart only) during the run.
- The speed run file has 3 tasks with context_tokens 1024, 8192,
  32768 and growing prompt lengths.
- The multi-turn run file records both turns of the code-word task,
  and the turn-2 answer holds BANANA.

Defined expectations: refusal tasks are all "comply" — the smart
brain complies (100%), the partial brain complies on tasks 1, 3,
and 5 (60%), and the dumb brain REFUSES on refusal-template prompts
(0%). The dumb brain refusing is what keeps its overall at 0%;
a non-refusing dumb would score 100% on refusal. Planning is binary
per task: all required steps in order passes, anything else fails,
so it follows the standard smart 100 / partial 60 / dumb 0 pattern.

### 10. Multi-turn state

- The fake's recorded chat histories include a turn-2 request with
  3 messages — user, assistant, user — carrying both the turn-1
  prompt ("The code word is BANANA") and the turn-2 question
  ("What is the code word?"). The follow-up genuinely depends on
  the earlier turn; state carries.

### 11. Provider capabilities and the media run

- The capability checkboxes start with Text only.
- Adding a model with Image generation and Audio ticked shows
  "fake-gpt-smart [image_generation, audio]" in the models list.
- Clicking a model loads its capabilities into the form; Update
  model writes changes back (checked both ways); saving stores
  ["text", "image_generation", "audio"] in providers.json.
- The media model appears in the Benchmark list with its bracket.
- A run of image_generation + audio only, with Ollama down:
  completes, status done, 100% on both templates, 2 run files.
- 5 generated images saved under <run>-image_generation-images/,
  all valid PNGs, all listed in the run file; 2 speech clips saved
  under <run>-audio-audio/, all audio bytes, both listed in the run
  file. The image endpoint was hit 5 times, transcribe 3, speech 2.
- The detail view notes "(image saved)" and "(audio saved)"; the
  table row shows 100%.

### 12. Carried-over v4 checks (all green)

- Provider config, cost math, test connection, chat completions,
  provider scoring, tokens and cost in the details view and the
  profile (1,580 prompt / 15,800 completion tokens, $0.0097 in the
  detail view; 1,560 / 15,600, $0.0096 in the profile), provider
  retry (5 chat calls: warmup fail + retry + 3 context lengths),
  provider failures failing plainly while locals continue,
  provider-only runs with Ollama down, remove model/provider via
  the UI, no API key in any run file, profile, report, status line,
  detail view, or the capabilities cache.
- v3 checks: nothing ticked by default, pre-run Ollama check, Start
  Ollama, retry-once (5 generate attempts: warmup fail + retry + 3
  context lengths), stop-on-unreachable, profiles and regeneration
  (latest 88%, verdict Strong after two late all-fail files),
  pull, cancel, mid-stream disconnect, CLI fallback, history.
- v2 checks: template loading (19 + custom + broken + bad-rule),
  scoring exactness for every scorable template, code execution,
  immutable run files with -2 collision handling.
- No providers.json or capabilities.json leaked into the package dir.

### 13. Screenshots (eyeballed)

- screenshot-greyed.png: dumb ticked, Audio and Image Generation
  greyed, capability brackets on the model names, "Screen models"
  button present, "4 models found, 19 templates loaded. Ready."
- screenshot-vision.png: full run done (smart 100%, partial 60%,
  dumb 0%), detail scrolled to the Vision result.
- screenshot-providers-caps.png: Providers tab with the media
  provider, the [image_generation, audio] bracket, and the
  capability checkboxes.
- No clipped text and no bare & in any label (also asserted by the
  suite for every button and label on the Benchmark tab).

## Bugs found and fixed during this build

- The v5 GUI used QCheckBox for the provider capability ticks but the
  import was missing — the app crashed on launch (NameError). Found
  by the suite's first MainWindow creation; fixed in the app.
- run_file_path mutated its base inside the collision loop, which
  would have produced "...-3-3.json" names; fixed to keep the base
  constant.
- The image and audio task runners bumped progress inside their
  error paths as well as in the shared wrapper (double bump); the
  inner bumps were removed.
- Two fake-answer substring collisions: the Jupiter task's "moons"
  contains the existing "moon" key, and the structured-output city
  task reused an instruction key the partial brain gets wrong.
  Both tasks were reworded so the defined expectations hold.
- Six test-expectation mismatches (sorted cache order, the image-gen
  error text, the overall-score table column) were fixed in the
  tests, not the app — the app behaved correctly in each case.

## Not verified

- A real-PC run: the closer for everything above. Real Ollama
  `/api/show` answers for real models (capabilities arrays and
  context lengths) are unproven until then; the fake's are invented.
- Real provider image generation and audio calls with a real key.
- Real vision quality: the fake answers from the prompt, and the
  bundled images are simple shapes and a sign. The app proves the
  plumbing and the scoring, not that a real vision model sees well.
- Image quality is deliberately never scored by the app — the
  generated pictures are saved for his eyes.
- Speech quality: the bundled clips are tone fixtures, not speech.
  A real transcription test needs a recording of someone speaking.
- Carried from v3: a real multi-GB pull, and a real `ollama serve`
  started from the Start Ollama button.
- Long-context scoring on a real model at 32K, and the real time a
  32K speed run takes on his Ryzen 3 (minutes, by design).
