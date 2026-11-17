# TEST-REPORT — OLLAMA SYSTEM TEST v20

Verified 2026-11-17 in a Linux sandbox (x86_64, PySide6 6.12.0, Python 3.12.3)
under the Qt test env from AGENTS.md: QT_QPA_PLATFORM=offscreen,
LD_LIBRARY_PATH=~/workspace/qt-libs/root/usr/lib/x86_64-linux-gnu,
venv ~/workspace/forge-v2-testenv/bin/python, default HOME (overriding
HOME breaks Qt plugin init). Plain widgets only, no OpenGL. The
geometry round-trip ran under Xvfb instead (see the v7 report §2 —
offscreen's 800x800 screen is smaller than the window's minimum
width).

v20 fixes two text-size defects found during v19 verification,
both proven by probe before fixing. No new templates; no new
dependencies. Bigger text now applies live: changing the size in
Settings used to move the application font but no open window's
widgets — the text only grew after a restart, so the live preview
was dead (even window.setFont did not move existing widgets in
this PySide6 build). The size is now set on every widget
directly, and the bento tile captions and values keep their
relative sizes (caption one point smaller, values bigger and
bold) at any size. And the Profiles tab scrolls: at 14 pt with
runs saved, the tiles and fixed-height charts forced the window
minimum to 1313x822 — taller than a 768 px screen. The tab now
lives in a scroll area (the same treatment the Providers tab got
in v18) and its intro line wraps, so the window minimum at 14 pt
with runs is 1293x578 and the window stays on a 1366x768 screen.

v19 is the Providers tab simplification + Settings auto-config
release. No new templates; no new dependencies; providers.json
format unchanged. The Providers tab was a wall of form shown
before he had any provider at all — it now starts as a calm
empty screen with one "Add a cloud provider" button, and adding
a provider is four steps (pick the service, paste the key, test
the connection, tick the models) with one save at the end.
Prices and capabilities moved under Advanced, collapsed, with
edits staging at once and the provider's single Save keeping
them. Settings detect the Ollama address and the models folder
and show what was found; the fields only appear under "Use a
different address/folder". The wizard enforces nothing-selected-
by-default twice: the name field takes focus on open (Qt would
otherwise make the first list row current and pre-pick OpenAI),
and a pick only counts after a real click or key.

v18 is the slow-machine review release (tasks RV1–RV13 from a
review of v17 run as a user on a slow machine). No new templates;
no new dependencies. The headline fix: every generate call used
to get 10 seconds for its first answer, and a cold model on a slow
machine takes far longer — the run died in warmup blaming Ollama,
though Ollama never stopped. The first reply now waits 5 minutes
and a slow model fails its own tasks while the run carries on,
with "slow" and "down" as different errors. Stopping a pull no
longer wedges pulling until restart: the worker always reports
back and the window clears a dead worker's slot. A bare Ollama
address (no http://) now works, a failed screening is no longer
cached as text-only, pull progress names the model, the Run tab's
elapsed time climbs during a long call, the Providers tab scrolls,
the try-it box starts blank, idle bars show no %, Settings has
plain-words copy and fits its default size, and an empty model
list says Ollama is starting instead of showing a blank box.
The RV4 resume test expectation was corrected (1 call, not 2 —
a model that cannot warm up fails its pairs without running a
task, by design).

v17 is the Ollama-starts-itself release, ordered in one morning
after Ollama died on his PC mid-run three times: the app starts
Ollama itself now, under the hood, whenever it needs it — at
startup, when Run is pressed, when a pull starts — and never asks
him to start it (the Start Ollama button stays as a manual
fallback). When Ollama dies mid-run, the worker restarts it once
and retries the failed generate; before each model loads, a RAM
check (model size x 1.2 against free memory) skips models the PC
cannot hold, plainly, never as a failure. Speed finally gets a
real score: 100% is 20 tokens/sec or faster on his machine,
scored per context length. And an interrupted run can resume
where it left off: the worker writes a manifest after every pair,
a Resume button offers the unfinished run, and --resume does the
same headless. Every message that used to send him hunting for a
button that was not on his screen is reworded. No new templates;
speed.json gains score_by and a rewritten description.

v16 is the math-scoring release (tasks M1–M6 from the math list):
the Math template grows from 5 tasks to 10, from one-step sums
to multi-step problems, a fraction answer, algebra, and a unit
conversion — and every numeric answer in the app is scored by
stricter rules. The last number in the answer is the answer, so
a number mentioned in passing no longer passes; answers must
match exactly unless the task gives a tolerance or says
approximate; written numbers ("twenty-one") and fractions
("1/2") score like digits; and a task with the right setup and
the wrong final answer scores partial credit instead of 0, marked
"partial (0.5)" in the detail view. No UI changes, no new
templates.

v15 is the pulls-and-polish release (tasks R19, R20, R21, R22, R23,
R24 from the redesign list): a pull can be stopped and resumed,
queued behind another, and watched with a real speed and time-left
readout; the Past runs tab gains a timeline scrubber; bigger text
is one tap on the first-run checklist and in Settings, with a
Reduce motion switch beside it; the focus ring is a visible 2 px
border, Tab moves in visual order, and focus comes back where it
was when the tab changes; every tick says what is picked now, and
the finish headline has a tick and a short fade; the Models tab can
delete a model and ask one model one question with no run; the Run
tab can re-run only the pairs that failed; and any template's tasks
can be read before running it, from the Preview button or a
double-click. No new templates, no template changes.

v14 is the results-and-settings release (tasks R14, R16, R17, R18
from the redesign list): the Profiles tab is a dashboard — one hero
tile for the latest overall score, four small tiles, score bars per
template, and a speed-across-runs line, each chart titled with its
takeaway in plain words — with the full profile text beside them;
Compare gets the same tiles for two models side by side and one
grouped-bar chart; the Providers tab is rebuilt into Connection and
Models sections with every field explained underneath it, and Test
connection offers the provider's own model list as a pick-list; and
File > Settings holds the models folder, the Ollama address, and the
default time budget, while each model remembers its usual templates.
No new templates, no template changes.

v13 is the run release (tasks R11, R12, R13, R15 from the redesign
list): pressing Run shows a preview first — what will run, how many
steps, an upper-bound time estimate, and the provider cost when there
is one — with Run and Adjust; while a run goes, a line under the
progress bar shows the current step, the step count, the elapsed
time, and a remaining-time estimate from the real rate so far, and a
small status chip says Ready, Running, Done, Stopped, or Ollama
stopped; when a run completes, one headline names the best model, its
score and speed, and the run's totals; and the results table gains
two columns (average time to first token, average prompt speed) while
the detail view now shows what each model actually wrote under each
task. The old cost guard is folded into the preview: a provider run's
cost is named there instead of in a separate confirm, and the $1.00
confirmation threshold is gone. No new templates, no template changes.

v12 is the layout release (tasks R1, R3, R6, R7 from the redesign
list): the one crowded Benchmark tab is now three tabs (Run setup,
Run, Models), one job each, with the status line under the tabs; a
How to Use index on the Help menu; single instance, so a second
launch brings the running copy forward; and a first-run setup
checklist that goes away once its three steps are done and never
comes back. No new templates, no template changes.

v11 is the first UI-redesign release (tasks R2, R4, R5, R8, R9, R10
from the redesign list): a debug log layer (scrubbed log files, a
debug report on the Help menu, and a crash handler that writes a
scrubbed report instead of dying silently); profiles render as
formatted markdown instead of raw text; removing a provider asks
first and defaults to No; saving twice in one minute no longer
overwrites the first report; the pick lists lock while a run is
going; and every visible em-dash is gone (n/a, periods, and plain
words in their place). No new templates, no template changes.

v10 is the real-speech release (task T12) — the last open repo-audit
task. No new templates: the three transcribe tasks finally have
speech to transcribe (real spoken clips replace the 1-second sine
tones), transcripts are normalized before scoring (digits spelled
out as words, punctuation dropped) so a correct transcript passes
however the provider writes it, and the fixture generator no longer
overwrites the clips with tones.

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

v20 suite: 1556 passed, 0 failed (run 7, log
~/workspace/ost-v20-verify/v20-run7.log). The v19 suite's 1538
checks are carried over inside that count, plus 18 new v20
checks (live apply on the open window's widgets, tile relative
sizes live and on restore, the Settings spin path, Cancel
restoring the size — including the size it opened with, after
the drive found Cancel restoring the startup default instead
—, the Profiles scroll area and the window minimum at 14 pt
with runs). The road: run 1 died at 384 checks with no
summary — the process aborted on "QThread: Destroyed while
thread is still running" in the v17 run-queue section, a load
flake (the VM was at load 9.4 with other threads' suites
running; the v18/v19 green logs show no such line, and run 2
passed the same section). Run 2 from a fresh copy went 1554/0
green with zero QThread lines — but that tree predated the
Cancel fix and its 2 checks, so a green run on the final tree
was still owed. Run 3 hit a load-timing cascade (the full fake
run missed its 300 s wait under load 16, 24 failures from that
one cause) and then died at check 790 on an uncaught
TimeoutExpired from a headless subprocess (120 s, load 14).
Run 4 reached 1193/0 before a VM restart killed it. Run 5 went
1555/1 — the single failure was the Xvfb geometry probe, which
failed only because the restart had wiped the /tmp/ost-v20
directory the launch used (a launch error, not the app; the
probe passed in run 6 with the directory in place). Run 6 went
1550/6, all 6 the full-run wait again under load 15-18. Run 7
from a fresh copy of the final tree went 1556/0 green with
zero QThread lines. The GUI drive went 14/14
(~/workspace/ost-v20-verify/gui-drive-v20c.log, Xvfb
1366x768): both fixes verified by hand and by eye, plus the
Cancel fix the drive itself found.

v19 suite: 1538 passed, 0 failed (run 3, log
~/workspace/ost-v19-verify/v19-run3.log). The v18 suite's 1481
checks are carried over inside that count, with the provider-UI
tests rewritten for the wizard and the detected Settings, plus
the new v19 section (wizard flow, empty state, ONE Save,
Advanced, detection helpers, detected Settings) and two extra
RV3 Settings checks. The road: run 1 reached 1532 of 1537 with
5 failures — 1 em-dash sweep (the wizard's strings used
em-dashes; the suite only shows the first 3 hits, and the scan
found 6 in total across the wizard, the budget copy, and the
Advanced copy, all reworded) and 4 from one cascade with a REAL
bug behind it: focusing the wizard's list makes row 0 current,
so clicking row 0 changed no row and picked nothing — the click
itself now picks (itemClicked/itemActivated), and the suite's
focus-then-click sequence is the regression test. Run 2 went
1537/0 green. Two executable fixes landed after run 2 — a None
guard in on_provider_model_selected (a click on an empty model
list used to crash the slot, found by the GUI drive) and the
wizard's address row hidden until "Other" is picked (found by
reviewing the drive screenshot) — plus one new suite check that
the address field starts hidden, so run 3 re-ran the final tree
and went 1538/0 green. Before run 1, the home quota blocked the
run-tree copy (Errno 122); two stale v6 install-sim venvs
(~276 MB) were removed and a 500 MB probe write confirmed the
headroom.

v18 suite: 1481 passed, 0 failed (run 3, log
~/workspace/ost-v18-verify/v18-run3.log). The v17 suite's 1436
checks are carried over inside that count, plus 45 new v18
checks (RV1–RV13 coverage, the fake server's headers_delay knob,
the RV4 expectation correction). This run supersedes the owed
v17 green re-run — it includes every v17 check. The road: run 1
reached 1466 of 1480 with 14 failures — 2 screening tests written
for the old cache-on-failure contract (rewritten for RV5), 3
server-dies tests (the killer left the socket open, so the new
300 s first-reply window waited it out — the killer closes the
socket now, like a real dead Ollama), 2 run-queue tests (load
timing — the flow passes standalone in ~5 s and passed in run 2),
4 version pins still saying 17, the Settings scrollbar (the
content needs 611 px; the dialog is 620 now), the re-pull test
(the old worker's thread finished before the click, so the
re-pull resumed instead of queuing — the test accepts either,
and a stub pins the queue branch), and the placeholder test
(the Models tab is not the opening tab, so the label's
isVisible stayed False until the test switched tabs). Run 1's
re-pull race also caught a REAL bug: the pull completion slots
took any worker's signal, so a re-pull that resumed at once was
clobbered by the dead worker's queued stopped signal — the slots
now ignore signals from a worker that already left the slot.
Run 2 reached 1469 of 1476 with 7 failures, all install.sh, all
one cause: pip hit Errno 122 Disk quota exceeded on the home
subvolume (environmental, not the app — the pip cache was purged
and a stale 282 MB v17 unzip removed before run 3, and a 300 MB
probe write confirmed the headroom). Run 3 is the green run.

The sandbox has no Ollama, no models, and no real provider accounts, so
all runtime tests ran against the fake servers from v5/v6 (fake Ollama
with /api/show, /api/chat with images and tools; fake OpenAI-compatible
provider with image_url parts, tool-call SSE deltas, image generation,
transcription, and speech). Test suite:
tests/run_tests.py in this tree — no single full run went
green before shipping (see the road below): run 7 reached
1416 of 1433 with 3 failures, all fixed after it; run 8
reached 996 checks (974 passed, 22 failed) before the agent
running it was cancelled — all 22 trace to one timing
cascade, the full fake run not finishing inside the test's
wait window under sandbox load, with zero errors or
tracebacks and every v17 feature proving out (speed scores,
RAM check, run files, profiles). The v16 suite's 1359 checks
are carried over inside those counts, plus 75 new v17
checks, minus 1 rewritten. A full green re-run is owed
after shipping; any real failure it finds ships as a fix
release.
Verification fought the sandbox, not the app: eight runs
in total, killed by two VM restarts, two silent
deaths under load 23–34, a headless subprocess timeout while the
VM degraded before a restart, and a sandbox-wide write refusal
(EPERM flap) that came and went for hours. Run 1 died silently
at 1112 with three understood failures: ollama_reachable (a real
Ollama now answers on the sandbox's 11434 — the check points at
a dead port now), mid-stream closes 2→3 (the restart adds a
third attempt — the app is right, the expectation moved), and
window5's 501 (the fake serve has no POST handler, so POST
returns 501, not 404). Run 4 caught a real timing flake (two
saves straddling a second boundary never collide, so the -2
suffix check passes one stamp to both saves) and died when the
EPERM flap refused a file write. Run 5 ran 1009 checks with 0
failures and was killed by the 25-minute timeout — the suite is
slow under load, so the timeout went to 60 minutes. Run 6's 2
window5 failures were the fake serve dying on a pidfile write
during the flap — its bookkeeping writes now retry and never
kill the serve. Run 7 reached 1416 with 3 failures: the loader
test's fixture forgot score_by (test-only), and a REAL app
defect — resuming a run whose done model has nothing left to
run crashed with "dictionary changed size during iteration"
(the preload loop calls on_model_done, which pops from the
same dict; it iterates a snapshot now). Run 8 (after the
fixes) reached 996 checks — 974 passed, 22 failed — before
the agent running it was cancelled at 18:51 EST: all 22
trace to one timing cascade, the full fake run's slowest
model still on its 13th template when the test's wait
expired under load (smart 100% and partial 59% had finished
with correct scores, including the new speed scores of 20.0
and 10.0 tok/s). No traceback, no error, no hang — the run
was 95% done. The v16 suite's
own history follows: **1359 passed, 0 failed** (final run; the
v15 suite's 1323 checks are all carried over and green inside
that count, plus 36 new v16 checks). The road to green: the
first run crashed at the audio checks — the new number-word dicts
were named _ONES/_TENS, which the v10 transcript normalizer
already owns (digits to words), so the new dicts overwrote the old
ones and transcript_words died with KeyError: 1. A real app defect
(the suite caught it, not the probes — the probes never called
transcript_words). The v16 dicts are _WORD_ONES/_WORD_TENS now,
and the second run came back 1351/8 — all eight test-only, all
numbers the math growth moved that the first sweep missed: the
provider full run's detail tokens (1,680 in, 16,800 out) and its
cost, which crosses the $0.01 line now so fmt_cost shows $0.01
instead of four decimals (the profile totals moved the same
way: 1,660 in, 16,600 out, $0.01); the budget check's total
(10 minus stopped, not 5); and the template filter's label
("Math (10 tasks)" in two checks). The third run from a fresh
unzip on workspace disk, TMPDIR=/tmp/ost-v16 with a watchdog
keeping it alive, is the green 1359 above. The v15
suite's own history follows: the first run came back 1316/4, all four test-only. The v14 providers
sections check searched the whole window for group boxes, and the
v15 try-it box is a third one — the check now searches the
Providers tab page only. The v14 settings-accept check compared
against a dict without the two new v15 keys (font size, reduced
motion) — added. The try-it empty-question check read the status
line, but the message goes in the answer box, where the answer
would be — the check reads the right place now. And the
queue-duplicate check read the pull status after a processEvents,
which let a progress event from the running pull overwrite the
refusal message first — the check reads it right after the click,
which sets it synchronously. The second run from a fresh unzip on
workspace disk, TMPDIR=/tmp/ost-v15 with a watchdog keeping it
alive, is the green 1320. Then the GUI drive found two app defects
(the worker-thread abort and the Settings overflow, §7 below),
fixed in the app with three scroll checks added, and the third run
came back 1322/1: the focus-restore check failed because the new
20 pt dialog check shows and closes a modeless dialog, which under
offscreen leaves the application with no active window — and with
no active window, setFocus stops emitting focusChanged, so the
focus memory goes deaf (probed: 0 emissions until activateWindow
restored it; a real window manager re-activates the window when a
dialog closes, so this is harness-only). The suite re-activates
the window after the dialog closes, and the fourth run from a
fresh unzip is the green 1323 above. The v14 suite's own history
follows: the first run crashed at 1211 checks — the suite imported QAction from
QtWidgets, where it does not live (it is in QtGui), and one File-menu
check still expected the v7 menu without Settings (both test-only
fixes; the app was right). The second run came back 1227/4: the
geometry probe failed when something wiped the run's TMPDIR
(/tmp/ost-v14) mid-run, and three starter checks failed because the
suite regenerates tests/fakebin/ollama at startup from an embedded
template — and the v14 env-file block had been added to the shipped
fakebin but not to the template, so every run overwrote the new
fakebin with the old one (root-caused by diffing the generated file
against the zip; the template now matches the shipped file
byte-for-byte, and a check pins it). The third launch died on import
before a single check: a misplaced & in the launch command
backgrounded the whole build chain, so the suite started while its
own fresh unzip was still being built. Relaunched properly, with a
watchdog keeping the TMPDIR alive, it is the green 1232 above. The
v13 suite's own history follows: the first run from a fresh unzip came back 1139/7 — six status-column
reads in the suite still pointed at column 3, which is the TTFT
column now (test-only fix; the app was right), and one new preview
string carried an em-dash, which the v11 AST pin caught (fixed in
the app). The second run came back 1134/7 — all seven install.sh
checks failed when pip's temp ran out of room: the fresh unzip
lived on the 512 MB /tmp tmpfs, so the "workspace scratch dir" the
install tests point pip at was tmpfs too, and another thread's
suite had /tmp at 86% (the wheel unpack hit ENOSPC). The third run
from a fresh unzip on workspace disk is the green 1146 above. The
v12 suite's own history follows: the first two full runs never
finished — the coordinator running them
died on a transport error (run 1 reached 915 PASS, run 2 reached 218
PASS, neither printed a summary). The third run from a fresh unzip
came back 1075/8: seven install.sh checks failed when pip's temp
ran out of room on the shared 512 MB /tmp (the 84 MB wheel unpack
hit ENOSPC under load), and the installed-list check failed because
this PySide6 build sets ItemIsUserCheckable on a bare list item by
default. Both were fixed in place — the install tests now point
pip's temp at the workspace scratch dir, and the installed list
clears the checkbox flag on its items — and the fourth run from a
fresh unzip is the green 1088 above. The v11 suite's own history
follows: its first full run crashed at 347 on a test bug (a
profile-regenerate check split rendered text on "## Weaknesses",
which setMarkdown strips); the second run came back 1049/2 (the
Help-menu check still expected About alone, and an em-dash in a new
debug-report string tripped the v11 AST pin — both fixed, the
second in the app); the third run crashed at 829 on a load-flake
subprocess timeout (the headless unknown-model command runs in 2.6 s
on its own); the fourth run was killed by a VM restart at 218. The
fifth run from a fresh unzip is the green 1051 above. The v10
suite's own history follows: its first full run came back 1010/6 —
two profile-regenerate checks failed on a time-of-day bug in the
test (it stamped its run files 09:00, which is only "later" before
09:00) and four version pins still said v9. Both were fixed in the
test, not the app, and the re-run from a fresh unzip is the green
1016 that v11 carries.

## What was verified (v16)

No GUI drive: v16 changes scoring only, and the one visible
change — the template list reading "Math (10 tasks)" — is a
label built from the template file, covered by the suite's
template-filter checks (which read the visible label text) and
by the "math has 10 tasks" check on the file itself. What the
suite proves, section by section:

§1. Last-number scoring (M1). An answer whose working mentions
21 but concludes 19 fails; one concluding 21 passes; and the
detail names the final answer ("final answer was 19, expected
21"). The four existing numeric checks (21 passes, 150.0 passes,
20 fails, no numbers fail) still pass under the new rule.

§2. Per-task tolerance (M2). The train task rejects 149 and 151
and accepts 150 exactly. A task marked approximate still allows
1% (149 passes, 148 fails), and an absolute tolerance allows
what it says (151 passes with a tolerance of 2, 153 fails).

§3. Ten math tasks (M3). The template keeps its 5 easy tasks
first and adds muffins (two-step, step 48), taxi (three-step,
step 25, decimal answer 32.5), pizza (fraction 5/8 as 0.625),
milliliters (unit conversion, 2500), and solve-x (algebra, 5).
A real run through the window — smart, partial, and dumb on math
only — separates them: 100%, 50%, 0%, with 10 tasks and passed
counts of 10, 5, and 0. The fake server's partial brain gets 2
of the 5 new tasks (muffins, pizza).

§4. Partial credit (M4). Right setup with a wrong final scores
0.5; fully wrong scores 0; all right scores 1. The task fails
unless every part is right, and the detail names the right step.
End to end through a real worker run, a canned answer with the
right step (25) and the wrong final (30) scores 50% with passed
0.5 and total 1, the task record carries credit 0.5, and both
the run summary and the detail view mark it "partial (0.5)".

§5. Number words and fractions (M5, M6). "twenty-one" is 21,
"one hundred and fifty" is 150, and "someone" is untouched;
written numbers pass the apples and train tasks. "1/2" extracts
as 0.5, "5/8" as 0.625, "1.5e3" as 1500, and a fraction answer
passes the pizza task; "1,000 and 2.5" still extracts as
1000.0 and 2.5, and a zero denominator is skipped.

§6. What the growth moved, and what did not. Growing math to 10
tasks moved the numbers that depend on it, all updated in the
suite and green: the full-run partial overall is 59% now (math
50% among fourteen 60% templates), the compare overall for the
math+coding export run is 55%, the v13 run's final count is
48 of 48 steps, the v8 preview cost is $3.29, and a full run is
108 prompts. Every other template still scores 100/60/0 across
the three brains under the new scorer (probed directly before
the suite ran), so the stricter rules changed math only.

## What was verified (v15)

The GUI drive (v15_gui_drive.py, Xvfb at 1366x768, fresh config,
fake Ollama server, fabricated runs for the scrub) ran all 17
steps in 21 s, exit 0, no unexpected dialogs. Screenshots in
~/workspace/ollama-system-test-audit/v15-gui-drive/.

### 1. Pull upgrades (R19)

- A slow pull (2 s between events) shows a real speed and time
  left: "downloading: 50.0 MB of 100.0 MB, 12.5 MB/s, about 4s
  left" (suite, against the slow pull server).
- Stop pull stops it where it is: "Pull of slow-a:1b stopped at
  75%. Pull it again to resume." The bar stays at 75, the name
  goes back in the box, and the interrupted record is kept
  (suite). Re-pulling resumes ("Resuming" in the status) and
  completes, clearing the record (suite).
- While a pull runs, the Pull button stays live: two more pulls
  queue behind it in order, a duplicate is refused ("already
  pulling or queued"), a queued pull can be removed, and stopping
  the running pull starts the next queued one (suite).
- The drive pulled a model the plain way ("Pulled
  drive-model:1b.", in the installed list) — no abort, which is
  what §7 is about.

### 2. Timeline scrub (R20)

- Three fabricated runs for the smart model: the slider spans
  them, the readout names the newest ("2026-11-13 10:00:00 -
  Math: 80% (4/5) at 20.0 tok/s"), scrubbing to the oldest shows
  "2026-11-11 10:00:00 - Math: 40% (2/5) at 20.0 tok/s" with the
  highlight on row 2, and the detail view still says "Pick a run
  to see its scores." — scrubbing previews, it never opens
  (drive + suite). Hovering a run shows the same line as a
  tooltip (suite). Picking a run in the list moves the slider to
  match (suite).

### 3. Accessibility presets (R21)

- First boot: the checklist shows with steps 1-2 honestly ticked
  and step 3 open, and its Bigger text button takes the font from
  9 pt to 12 pt at one tap, saved right away (drive).
- Settings holds the same preset: the spin follows it, the font
  applies live while the dialog is open, and Cancel puts the
  opening size back; Save returns the size and the Reduce motion
  switch, and applying them sets both (suite).
- The Settings fields scroll: at 20 pt text no explanation label
  is clipped (height against heightForWidth at the label's own
  width — sizeHint overstates a wrapped label's height, because
  it is computed at a narrower width than the label gets), and
  Save stays visible below the scroll area (suite + a 12 pt
  screenshot read by eye, top and scrolled to the bottom).

### 4. Focus-first keyboard navigation (R22)

- Tab moves in visual order on the setup tab (filter, list,
  Select all) and focus returns where it was after a tab change
  (suite + drive: "focus after Tab: model_list").
- The focus ring is a 2 px solid border in both themes (the
  stylesheet rules are pinned in the suite; `outline` does not
  render in this PySide6 build — probed — so the ring uses
  `border`). In the drive screenshot the focused model list
  shows 2,354 pixels of the ring blue, against 132 before the
  window was activated — an inactive window's widgets report
  hasFocus() False, so the ring never paints there (see §7).
- Ctrl+R and Esc are pinned as the Run and Cancel shortcuts
  (suite; they already existed from v7).

### 5. Micro-interactions and teach by playing (R23)

- Every tick says what is picked now: "1 model, 0 templates
  picked." after ticking a model, "1 model, 1 template picked."
  after ticking a template (drive + suite).
- The finish headline has its tick and a 200 ms fade-in: "✓
  Done. Best: fake-smart:2b, 100% overall at 20.0 tok/s.
  2 models, 2 templates, 10s." Reduced motion skips the fade
  (suite checks the effect is absent then).
- The first-run hint shows with no runs ("New here? Ask one
  model one question first: Models tab, Try one model.") and is
  gone after a run (drive + suite).

### 6. v3 review leftovers (R24)

- Delete a model: the confirm asks ("Delete drive-model:1b from
  this PC? ... This cannot be undone."), Yes removes it from
  Ollama and the list ("Deleted drive-model:1b."), No keeps it
  (drive + suite).
- Re-run failed only: after a run with the broken model, the
  button is enabled; the re-run's preview says "Re-running 2
  failed pairs (1 model), 11 steps in total." and its table has
  one row, the broken model (drive + suite). The pair logic is
  unit-checked: missing models, failed models, unscored
  templates with no tasks run, and budget-stopped templates
  fail; skipped pairs and unscored-but-run templates do not.
- Template preview: the math dialog names the tasks and their
  expected answers, the multi-turn dialog shows the turn counts
  and the first prompt (suite).
- Try one model: "What is 2 plus 2?" to the smart model answers
  "A fine answer indeed." (drive + suite); no question says so
  in the answer box (suite).

### 7. What the verification caught

- The drive aborted twice at the first pull completion with
  "QThread: Destroyed while thread is still running" —
  faulthandler put the abort inside on_pull_done, which dropped
  the last reference to the PullWorker while its thread was
  still exiting. All six worker completion slots (pull done /
  failed / cancelled, try-it, run finished, provider tested)
  now capture the worker, clear the attribute, and wait() before
  the reference goes. The drive has completed cleanly three times
  since.
- The Pull button disabled itself during a pull, which made the
  queue unreachable from the UI — it stays enabled now.
- Deleting a model set the status before the list refresh, so
  the refresh overwrote it — the result is set after.
- Focus memory walked parents to find the tab, but tab pages
  live under the tab widget's internal stack, not the tab widget
  — it uses isAncestorOf now.
- The template preview read chat turns' "content" key; the
  template files use "prompt".
- The fake server's DELETE handler used urlparse, which the
  file never imported — it uses the file's plain path
  comparison now.
- The Settings dialog clipped its explanation labels at bigger
  text sizes (its fixed 420 px height is shorter than the
  content) — the fields live in a scroll area now, with
  Save/Cancel outside it.
- A modeless dialog closed under offscreen leaves the
  application with no active window, and setFocus then stops
  emitting focusChanged — the suite re-activates the window
  after the dialog closes. The same inactive-window effect
  hides the :focus ring in screenshots; the drive activates the
  window before the focus shot.

## What was verified (v14)

### 1. Bento results overview (R14)

- The Profiles tab is two columns: the glance on the left, the
  full profile text on the right. The glance is five tiles — one
  hero (latest overall score, two columns wide) and four small
  ones (best score, decode speed, strongest and weakest
  templates) — in a 3-column grid with 16 px gaps and 12 px
  rounded corners, per library entry 005. The Compare tab gets
  one tile per model, same size, because a comparison weighs both
  equally. Tiles and charts read from one new helper,
  profile_numbers, which build_profile also uses — the tiles, the
  charts, and the markdown can never disagree about a model. The
  suite pins the helper's numbers against fabricated runs and the
  tiles' contents in a real window; with no runs, the tiles hide
  and the charts clear.
- GUI-driven under Xvfb at 1366x768: the tiles read at a glance,
  the hero score is the biggest thing on the tab, and the Compare
  tiles name both models with their overall scores, speeds, and
  best templates.

### 2. Charts (R16)

- Two chart widgets, painted with plain Qt — no new dependency.
  The bar chart draws horizontal score bars on a 0-based axis
  with grid lines at 0, 25, 50, 75, and 100 and the value printed
  at the end of each bar; the line chart draws decode speed across
  runs with grid lines at intuitive steps (1, 2, 5, or 10 times a
  power of ten) and the value on each dot. Each chart has a
  takeaway title above it in plain words ("Strongest: Math 80%.
  Weakest: Coding 40%."; "Speed is steady around 22.0 tok/s.").
  Colors come from the widget palette, so dark mode works, and the
  numbers are always printed next to the marks, so color never
  carries the meaning alone. The Compare chart groups two bars
  per template (top bar the first model, bottom bar the second)
  with a caption saying which is which. The bars scroll inside
  their own area when a model has many templates. The suite pins
  the takeaway titles (empty, single, steady, rose, fell, even,
  leader), the axis step, and that all three chart shapes paint
  without error, with data and without.
- GUI-driven: the bars and the line render in the window, and the
  takeaway titles read above them.

### 3. Providers tab rebuild (R17)

- The tab is two sections now — Connection and Models on this
  provider — and every field explains itself in plain words
  directly underneath it. Test connection fetches the provider's
  model list and offers it as a pick-list: picking one fills the
  Model id field, so the id comes from the provider instead of
  from typing. The list is capped at 200 ids, and the status says
  so when the cap bites. The fetch is a new function,
  provider_fetch_models, returning (ok, ids, message);
  provider_test_connection keeps its (ok, message) shape by
  delegating to it, and the worker warmup path is untouched. The
  suite pins the fetch against the fake provider (list, bad key,
  no base URL), the group boxes, the explanations, the pick-list
  filling after a real test click, and the list resetting when a
  new provider is started.
- GUI-driven: the sections read as two groups, the explanations
  sit under their fields, and picking a fetched model fills the
  Model id.

### 4. Settings window (R18)

- File > Settings holds the models folder, the Ollama address,
  and the default time budget — in the app, not env-only. The
  models folder is passed to the Ollama this app starts; the
  address applies to new connections right away; the budget
  initializes the Run setup spin and applies to new runs. A field
  whose variable is already set in the environment shows that
  value, locked, and the stored setting is kept untouched for
  when the variable goes away. Per-model template sets are
  remembered: ticking a model ticks its usual templates, with a
  label saying which set is active; a hand-changed template clears
  the label; a run saves the templates it ran as each ticked
  model's usual set. The Settings window lists the saved sets and
  can forget them all. The suite pins the round-trip, the locked
  fields, the address applying (and clearing) in the process, the
  starter passing the models folder to a fake Ollama (and never
  overriding OLLAMA_MODELS), and the set applying, clearing, and
  saving through a real run.
- GUI-driven: the Settings window opens from the File menu, its
  fields show the saved values, and saving applies the budget to
  the Run setup spin.

### 5. QTest.qWait does not deliver worker signals here

- The v14 probe caught a second timer quirk in this sandbox (the
  first was the modal-dialog one in v13): QTest.qWait(2000) after
  clicking Test connection left the status on "Testing..." — the
  worker had finished, but its queued signal was never delivered
  inside qWait's local loop. A processEvents + sleep polling loop
  (the suite's wait_until) delivers it at once. The suite's
  provider checks use wait_until, and GUI drives poll the same
  way — never qWait for a worker thread's signal.

## What was verified (v13)

### 1. Run preview (R11)

- Pressing Run shows the preview dialog before anything runs:
  models, templates, step count, an upper-bound time estimate, and
  the provider cost when there is one — with Run (the default) and
  Adjust. Adjust closes the preview, leaves the ticks alone, and
  the status line says "Run cancelled." The time estimate uses each
  model's measured speed from its newest past run, or 8 tok/s for a
  model that has never run here, plus 10 s per model for loading,
  times a 1.25 safety margin; it says "at most about" because that
  is what it is. The step count comes from the same counter the
  worker uses, so the preview's number is the run's number. The
  suite checks the builder (counts, upper-bound label, cost lines,
  unknown-price wording, a fast past run shortening the estimate),
  the dialog's buttons, and the rewritten v8 cost-guard checks
  (the preview names the $1.67 estimate and the model; Adjust
  starts no run).
- GUI-driven under Xvfb at 1366x768: the dialog appears on Run,
  names both ticked models, shows the step total and the upper
  estimate, and says local runs cost nothing; Run starts the run,
  and a second preview's Adjust cancels it with no run started.

### 2. Honest run progress (R12)

- Under the progress bar, a line shows the current step, the step
  count ("12 of 33 steps done"), the elapsed time, and a
  remaining-time estimate from the real rate so far — or
  "estimating time left" until two steps are done, because one
  step's rate is noise. The worker reports its total step count
  when the run starts and each finished step as it goes (the
  warmup counts too). The progress bar itself is unchanged: it was
  already driven by real steps, never a fake. Cancel stays on the
  Run tab, enabled through the whole run. The suite pins the
  mid-run line, the step count, the elapsed time, and the estimate.
- GUI-driven: mid-run the line reads a real step count with
  elapsed time and an estimate, and Cancel is enabled; after the
  run it keeps its final count ("22 of 22 steps done" for the
  drive's two-model run).

### 3. Calm run status and finish moment (R13)

- A small status chip at the right of the Run tab's button row
  says Ready, Running, Done, Stopped, or Ollama stopped — one
  word, no animation, no popup. When a run completes, one headline
  appears under the progress bar: the best model, its score and
  speed, and the run's totals. A stopped run shows no headline;
  the status line already says what happened. The suite pins every
  chip state and the finish headline's contents, including the
  cancelled run (chip Stopped, no headline).
- GUI-driven: the chip reads Running mid-run and Done after, and
  the finish headline names the best model, 100%, 20.0 tok/s, and
  the run's totals.

### 4. Answers and latency in the results (R15)

- The results table has six columns now: Model, Overall score, Avg
  decode tok/s, Avg TTFT, Avg prompt tok/s, Status. The two new
  columns are real averages from the task records (multi-turn
  tasks record no TTFT or prompt speed, so they drop out of the
  average). The detail view shows the saved answer under each task
  — each turn's answer for multi-turn tasks — plus the model's
  first-token latency and prompt speed at the top. The suite pins
  the prompt speeds against the fake server's canned metrics
  (40.0 / 20.0 / 10.0 tok/s), the TTFT format, the status column's
  new position, and the answers in the detail view (including a
  fabricated multi-turn result for the turn answers).
- GUI-driven: the table shows the six headers with TTFT in ms and
  prompt speeds matching the fake models, and the detail view
  shows the model's code answer.

### 5. Fresh-unzip location (verification lesson)

- The v12 install-test fix points pip's temp at the workspace
  scratch dir — but a fresh unzip on /tmp puts that scratch dir on
  the 512 MB tmpfs, so the fix does nothing there: run 2's seven
  install failures were ENOSPC with /tmp at 86% under load. Full
  suites now run from a fresh unzip on workspace disk (with
  TMPDIR=/tmp/ost-v13, a tmpfs subdir, for the small tempdirs).
  No product change — install.sh is untouched.

## What was verified (v12)

### 1. The Benchmark tab is three tabs (R1)

- Run setup holds the pick lists (full height, no 180px cap), the
  time-budget control, and Run. Run holds the progress bar, the
  results table, the detail view, Cancel, and Save/Export. Models
  holds the installed-models list, the pull box, Refresh, and Start
  Ollama. The status line sits under the tabs and stays visible on
  every tab. The app switches to the Run tab when a run starts.
  Nothing was removed, only reorganized — the suite checks every
  control still exists on its new tab.
- The Models tab's installed list is read-only: its items carry no
  checkbox flag (this PySide6 build sets ItemIsUserCheckable on a
  bare QListWidgetItem by default, so the app clears it — otherwise
  Space would sprout a checkbox on a list that has none). Probed
  directly: a bare item's flags include the bit, and clearing it
  leaves the list plain. The suite pins it.
- GUI-driven under Xvfb at 1366x768: all seven tabs screenshotted
  and read — the setup lists show all 19 templates without
  scrolling, the Models list shows the four fake models with no
  checkboxes, the status line reads on every tab.

### 2. How to Use index (R3)

- Help > How to Use opens the full manual in its own window —
  first time, picking models and templates, running a test, reading
  the results, past runs, pulling a model, providers, saving and
  sharing, what to do when something goes wrong, shortcuts —
  rendered as markdown (headings, lists, code spans). It stays
  open beside the app; opening it again brings the same window
  forward instead of a second copy. The suite checks the menu
  action, the section list, and that no em-dash or bare & appears
  in the text; the GUI drive read the rendered window.

### 3. Single instance (R6)

- A second launch while the app is running brings the running
  copy forward instead of starting a second one that could write
  the same results dir. The suite proves the handshake: a second
  copy is refused while the window holds the lock, and the knock
  reaches the running window. Headless runs are exempt — cron
  must not be blocked.

### 4. First-run setup checklist (R7)

- The top of the Run setup tab shows three live steps — Ollama is
  running, at least one model pulled, first benchmark run done —
  each with what to do when it is open. The first two steps tick
  honestly from live state; the third ticks when a run finishes.
  It goes away once all three are done, or when dismissed, and
  never reappears after either. GUI-driven with fresh settings:
  the panel shows with steps 1-2 ticked and step 3 open, and the
  Hide button removes it. The suite covers the dismiss/completion
  persistence.

### 5. Install-test hardening (verification fix)

- The suite's install.sh end-to-end tests ran pip with TMPDIR=/tmp
  (a 512 MB tmpfs shared with every other thread's suite) and the
  84 MB wheel unpack died with ENOSPC under load, failing seven
  checks on an install.sh byte-identical to v11's green run. The
  tests now point pip's temp at the workspace scratch dir (the
  same reason the scratch copy itself lives under tests/). No
  product change — install.sh is untouched.

## What was verified (v10)

### 1. Real spoken clips (T12)

- The three clips in `audio/` are real speech, 16 kHz mono WAV,
  generated with text-to-speech at $0: clip-hello.wav says "hello
  world" (1.04 s, 33,358 B), clip-count.wav says "one two three"
  (1.72 s, 55,118 B), clip-weather.wav says "Today will be sunny
  with a high near seventy degrees" (2.52 s, 80,718 B). The TTS
  output needed cleaning before it could ship: the synthesizer
  prepended a 37-byte ID3 tag to the WAV, which made the file
  invalid (ffprobe rejected it), so the tag was stripped and the
  audio resampled to clean 16 kHz WAV.
- Each clip was transcribed back locally with faster-whisper
  (tiny.en, CPU int8) to prove it carries the expected words:
  "Hello world." (expected "hello world" — pass), "1, 2, 3."
  (expected "one two three" — FAIL as transcribed, see §2),
  "Today will be sunny with a high near 70 degrees." (expected
  "sunny" — pass).
- The suite pins the clips as speech, not tones: a pure sine tone
  has a near-constant amplitude envelope, speech varies a lot.
  Coefficient of variation of per-10 ms-window RMS measured
  0.005–0.008 on the old tones and 0.94–1.41 on the new clips; the
  suite checks each clip is above 0.25 (stdlib only — wave,
  struct, math).
- templates/audio.json no longer says the clips are test tones:
  the description names the real spoken content.

### 2. Transcript normalization

- The transcribe-back in §1 caught a real defect: Whisper writes
  spoken numbers as digits ("1, 2, 3"), while the count task
  expects words ("one two three") — and the contains scorer needs
  the exact phrase, so the commas alone would fail it too. A
  correct transcript would have failed against any real provider.
- Transcripts are now normalized before scoring
  (`transcript_words`): digit tokens are spelled out as words
  (0–99; larger numbers stay digits) and punctuation is dropped,
  so "1, 2, 3." scores as "one two three". Only the scored copy
  is normalized — the stored answer keeps the provider's own
  words.
- Five new suite checks pin it: digits spelled out, punctuation
  dropped, "near 70 degrees" becomes "near seventy degrees",
  numbers over 99 left alone, and the digit transcript passing
  the count task.

### 3. Fixture generator keeps the clips

- tests/make_fixtures.py used to overwrite all three clips with
  sine tones every run — running it once would have silently
  un-fixed T12. It now keeps any existing clip and only writes a
  tone fallback when a clip file is missing (so a fresh checkout
  with no clips still has something to send). The docstring says
  the real speech lives in the repo.
- The suite pins it: it hashes the three clips, runs
  `build_fixtures()`, and checks the hashes are unchanged.

### 4. Secrets-rule audit

- The tree and the full 11-commit GitHub history were audited for
  hardcoded keys, tokens, passwords, and credentialed URLs: no
  real secret anywhere. The only key-shaped strings in any
  revision are test fakes ("test", "sk-dead", "test-key",
  "sk-wrong", "test-key-not-real"); the only password-shaped
  strings are the long-context fixture needles ("The meeting
  password is ORANGE-2210"). No providers.json, .env, key, or
  credential file was ever committed, and neither file exists on
  main today (checked live).
- `.gitignore` hardened in this build: `.env`, `*.env`, `*.key`,
  `*.pem` added alongside the providers.json and capabilities.json
  it already covered.

### 5. Not verified (v10)

- No real provider transcription run yet — the sandbox has no
  provider key, so the local Whisper transcribe-back (§1) stands
  in for a provider. A first real run against a provider account
  is still the closer for the Audio template.
- The clips are a TTS voice, not his voice. T12's original block
  was "his recording or TTS" — TTS is what shipped, on the
  "finish it" order. His own recording can still replace the
  clips later; nothing in the app cares whose voice it is.

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
