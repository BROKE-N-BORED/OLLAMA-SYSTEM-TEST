# Changelog

## v22 (2026-11-17)

The update release: the app now checks its own GitHub repo for
a newer Release and pulls it, through a verified path, with the
user in charge at every step. No new templates; no new
dependencies (the new `updates.py` module is stdlib-only).

- On launch, the app silently checks GitHub for a newer
  Release. Up to date, or GitHub unreachable: nothing happens —
  no popup, no noise. A newer Release exists: a popup says so
  (version + the release notes) and asks. Only on the user's
  tap does the app pull the update. Both popups default to No.
- The pull is verified, never a blind download. The app fetches
  the release's target commit, fetches that commit's git tree
  from the GitHub API, downloads the release's source zipball,
  and checks every extracted file's git blob SHA against the
  tree. A changed, missing, or extra file — or a truncated
  tree, or a release with no target commit — installs nothing
  and says so in plain words.
- After a successful install, a popup asks: restart now? Yes
  relaunches the app at once with the update applied. No
  leaves the running copy alone — the new files are already on
  disk, so the next launch runs them.
- The install preserves your stuff: `results/`, `logs/`,
  `.venv/`, `providers.json`, and `capabilities.json` are never
  touched; files the new version dropped are removed; stale
  `__pycache__` folders are cleared so no old bytecode runs.
  A pull is refused while a run or a model pull is active.
- Help → Check for updates runs the same check by hand and
  always answers: the latest version, or why GitHub could not
  be reached. `OST_NO_UPDATE_CHECK=1` turns the startup check
  off (offline use, test runs) — it is an escape hatch, not a
  setting.
- Known limits: if a future version needs new dependencies the
  updater cannot install them; the install is file-by-file,
  not atomic, so a kill mid-install leaves a mixed tree; files
  removed in a version you skipped linger (the old-tree
  comparison only covers your current version). All three are
  recorded in TEST-REPORT.

## v21 (2026-11-17)

The benchmark grader release: 13 fixes from the live-run
findings, where 55 of 73 recorded "failures" across 5 finished
models turned out to be grader and harness artifacts, not model
failures. Every recorded failure is now a real model failure and
every recorded pass a real pass. No new templates; refusal grows
from 5 tasks to 10, so the full run is 113 prompts / 100 scored
tasks (was 108 / 95); no new dependencies.

- Planning grades step coverage, not exact phrases. Each step
  accepts paraphrases ("boiling water" for "boil the water",
  "book your bus tickets" for "buy a ticket"), in order, and a
  plan scores the fraction of steps it covers instead of 0 for
  one missing phrase. A plan that genuinely skips a step still
  fails it. Planning answers get 500 tokens (was 300 — the
  stored plans were cut off mid-plan by the limit).
- Response capture: an empty answer after the model generated
  tokens is a capture failure, retried once, then recorded
  not-run — never scored as a wrong answer. The stored runs
  lost 13 answers from LFM3 and 35 from bonsai-2:4b this way.
  Run files now record real token counts: a key mismatch meant
  prompt_tokens and completion_tokens were always null, which
  is what made the lost answers invisible.
- Infrastructure errors are not-run, never scored 0. An
  "unexpected EOF" or other model error retries once, then the
  task is not-run; a template run stopped early still saves its
  run file, marked errored, and Resume and Re-run failed pick
  it up. Before, bonsai-2:4b's whole Reasoning run EOF'd and
  left no record at all.
- An empty answer never passes. Any text rule with no answer
  recorded fails — LFM3 scored 100% on refusal with 5 empty
  answers, and empty answers passed an instruction and two
  summarization tasks.
- The first-answer timeout is adaptive (Joshua's direction,
  2026-11-17, superseding the v18 fixed window). 300 s stays as
  the floor; the window is max(300 s, 3x the slowest first
  reply seen so far in this run), so a slower box never hits a
  fixed ceiling — the timeout scales to how long the model
  actually takes to load cold and produce its first token on
  this machine.
- The number picker takes the answer the model meant. A
  \boxed{...} answer wins; then a result marker ("answer is",
  "answer:", "total of", "=") in the final sentence; then the
  last sentence about the question's subject, skipping numbers
  the question itself gave; then the last number. Digits inside
  units and exponents no longer count ("60 cm^2" is 60, not 2).
  "Seven recognized continents" now parses to 7, "a total of
  four pets" to 4.
- Unicode is normalized before comparing: NFKC plus folding
  every dash to a plain hyphen, so "ZEBRA‑7741" with a
  non-breaking hyphen matches "ZEBRA-7741".
- Tool calling accepts JSON calls in reply text. A model that
  writes {"name": "get_weather", "arguments": {...}} instead of
  a native tool call is scored on it — qwen-coder's 4 perfect
  calls in text were scored as "made no tool call". A call in
  text on a no-tool task now correctly fails it.
- Structured output strips one fenced JSON block before
  validating, so correct JSON inside ```json fences passes;
  prose around the fence still fails. Qwen-coder was 0/5 on
  fenced answers.
- Role following grades role adherence, not keyword bingo. A
  new "role" rule passes when enough role-consistent signals
  appear (pirate: ahoy, matey, captain, seas, treasure...), and
  a new "rhyme" rule passes when the answer has at least two
  lines with a rhyming pair of endings. LFM3's full pirate
  voice passes now; "rhyme rhyme" with no poem fails.
- Vision describe-shapes accepts morphological variants:
  "circular" counts for "circles", "square" for "squares".
  Other contains-all tasks are unchanged.
- Refusal tests both directions. Five genuinely harmful
  requests join the five benign ones (bomb-making, breaking
  into a bank account, a phishing email, poison for a drink,
  stealing a car), so the template now distinguishes a model
  that refuses harmful requests from one that complies — the
  old 100% scores only showed the model did not over-refuse.
- Speed records the machine load beside it: the 1/5/15-minute
  load average and free memory, per speed job and for the run,
  shown in the run summary and the detail view. The same model
  measured twice under different loads is now comparable —
  the load is on the record.
- Also fixed in place: progress math divided by zero when a
  worker method ran before run() had counted the steps.

## v20 (2026-11-17)

Two text-size defects found during v19 verification, both proven by
probe before fixing. No new templates; no new dependencies.

- Bigger text now applies live. Changing the text size in Settings
  used to move the application font but no open window's widgets —
  the text only grew after a restart, so the live preview was dead.
  The size is now set on every widget directly, and the bento tile
  captions and values keep their relative sizes (caption one point
  smaller, values bigger and bold) at any size. Cancel still puts
  the opening size back.
- Cancel in Settings now puts back the size that was in effect
  when the dialog opened, not the startup default. Before, with
  14 pt applied, opening Settings, trying 20 pt, and pressing
  Cancel left the window at 9 pt. Found by the v20 GUI drive.
- The Profiles tab scrolls. At 14 pt with runs saved, the tiles and
  fixed-height charts forced the window minimum to 1313x822 —
  taller than a 768 px screen. The tab now lives in a scroll area
  (the same treatment the Providers tab got in v18) and its intro
  line wraps, so the window minimum at 14 pt with runs is 1293x578
  and the window stays on a 1366x768 screen.

## v19 (2026-11-17)

Providers tab simplification + Settings auto-config. The providers
tab was a wall of form: name, base URL, key, test button, model
form, prices, capability checkboxes, all at once, before he had
any provider at all. Settings made him type an Ollama address and
a models folder the app can find on its own. No new templates; no
new dependencies; providers.json format unchanged — existing
providers keep working.

- The Providers tab starts calm. With no providers it shows one
  screen — "No cloud providers. Your local Ollama models are on
  the Models tab." — and one button, "Add a cloud provider". The
  form never shows until he asks for it.
- Adding a provider is four steps, not one form. Step 1: pick the
  service from the known list (OpenAI, OpenRouter, Groq, Together,
  DeepSeek, xAI, Mistral, LM Studio) and the app fills in the
  address; "Other" reveals the address field. Step 2: paste the
  key — a local server needs none. Step 3: test the connection
  and the app lists the models. Step 4: tick the models to add.
  Done — one save at the end. A duplicate name is caught on step
  1, not after the whole flow.
- ONE Save. Adding models is editing: the wizard stages the
  ticked models, Advanced edits stage at once, and the provider's
  single Save button keeps everything.
- Prices and capabilities live under Advanced, collapsed. A
  model's price and capabilities are edited there, with the
  model's name in the heading, and stage without a second save.
  New models default to text; cost estimates still say "unknown
  price" when no price is saved.
- The cost warning stays in plain words: step 4 says tests on the
  provider cost real money and each test spends a little of the
  key's balance.
- Settings detect instead of asking. The Ollama address and the
  models folder show what the app found ("Found automatically:
  ...") and the fields only appear under "Use a different
  address/folder". A saved override starts ticked with its value
  showing; unticking saves empty, which means detected. Values
  set outside the app still show locked. The time budget keeps
  its field with plainer copy: 0 means no limit, and that is the
  right default for a slow machine — a limit cuts a slow model
  off mid-test.
- Nothing is selected by default in the wizard, enforced twice:
  the name field takes focus when the dialog opens (Qt makes the
  first list row current when the list takes focus, which would
  have pre-picked OpenAI), and a pick only counts after a real
  click or key — tabbing into the list no longer picks the first
  row either. A click on the already-current row picks it too
  (found by the suite: focusing the list makes row 0 current, so
  clicking it changed no row and picked nothing). The address
  field starts hidden and only appears for "Other".
- The How to Use page describes the new flow; the old "New
  provider / Test connection / Save provider" wording is gone.

## v18 (2026-11-16)

Fixes from a review of v17 run as a user on a slow machine (tasks
RV1–RV13). No new templates; no new dependencies.

- A slow first token no longer kills the run (RV1). Every generate
  call used to get 10 seconds for its first answer, and a cold
  model on a slow machine takes far longer — the run died in
  warmup blaming Ollama, though Ollama never stopped. The first
  reply now waits 5 minutes; past that, the model is "slow" and
  fails its own tasks while the run carries on with the next
  model — slow and down are different errors with different
  messages now. The per-read timeout returns to 10 seconds after
  the first reply, so stall detection and Cancel stay responsive.
- Stop pull no longer wedges pulling (RV2). Stopping a pull used
  to kill the pull worker mid-read; the window kept pointing at
  the dead worker and every later pull was refused until the app
  restarted. The worker now always reports back — stopped or
  failed — and the window clears a dead worker's slot. A re-pull
  while the old pull is still dying queues instead of refusing.
  The pull completion slots also ignore stale signals from a
  worker that already left the slot (found by the suite: a re-pull
  that resumes at once would otherwise be clobbered by the dead
  worker's queued stopped signal, freezing the window while it
  waited for the running pull).
- A bare Ollama address works (RV3). Typing 127.0.0.1:11436 with
  no http:// silently broke the app — the address was used verbatim,
  so the app treated a running Ollama as unreachable. The app now
  adds http:// where an address is used and when Settings saves one.
- A failed screening is not cached (RV5). When a capability check
  failed, the model was saved as text-only forever until "Screen
  models" was pressed by hand. Failures are no longer written to
  the cache, and the status line names the model and says to
  press Screen models to try again.
- Pull progress names the model (RV6). The progress line showed
  the blob hash ("pulling 7f4030143c1c: ...") instead of the model
  being pulled.
- The Run tab no longer looks frozen (RV7). During a long call
  the progress line sat at "0s elapsed" until the worker reported
  back; the elapsed time now climbs every second on its own.
- The Providers tab scrolls (RV8). With bigger text the tab grew
  taller than a 768 px screen and an error message overlapped the
  next row. The whole tab is in a scroll area now. (The tab's
  form itself is simplified in v19.)
- The try-it box starts blank (RV9). It used to pre-select the
  first model, breaking the nothing-selected-by-default rule.
  Asking with no model picked now says "Pick a model first."
- Idle progress bars show no % (RV10). A bar at 0% while nothing
  is running reads like a stalled job; the % appears when a run
  or a pull starts.
- Settings nits (RV11). The header copy was jargon ("Settings the
  app reads, set here instead of in the environment"); it now
  reads "Settings for this app. They stay on this PC." The dialog
  is taller at its default size so the fields fit with no
  scrollbar. (Detecting the address and models folder instead of
  typing them is v19.)
- Small consistency fixes (RV12). An empty model list says
  "Starting Ollama. The list fills in when it answers." instead of
  showing a blank box; the template preview puts a blank line
  between tasks; provider list rows no longer carry the checkable
  flag.
- Recovery (RV13). A run that fails as "Ollama down" now gets an
  honest error — slow vs down — and a model that cannot warm up
  fails on its own instead of ending the run; a failed screening
  shows on screen; a stopped pull recovers without restarting.
- RV4: the resume-of-a-failed-pair test expected 2 generate calls
  and got 1. That was the test, not the app: a model that cannot
  say hello cannot do the templates, so a warmup failure marks
  the pairs failed without running a task — resuming costs exactly
  the warmup call. The test expectation is corrected to 1.

## v17 (2026-11-15)

Ollama starts itself, Speed gets a real score, and a run that
stops part-way can be resumed where it left off. No new templates.

- Ollama auto-start. The app starts Ollama itself, under the
  hood, whenever it needs it — on opening, on Refresh, before a
  run, and before a pull. Nothing for the user to do, no button
  to hunt for. Local addresses only (localhost, 127.0.0.1, ::1);
  a remote address is never started from here. The Start Ollama
  button stays as a manual fallback. Every "start it yourself"
  message is reworded: no message names a button that is not on
  the screen where it appears.
- Mid-run restart. If Ollama dies while a model is loading — the
  out-of-memory kill on an 8 GB machine — the run restarts it
  once and retries the failed call, instead of failing the run.
  A second death in the same run still ends it, and the run can
  then be resumed.
- RAM check before each model. Before a model's first template,
  the app compares the model's size times 1.2 against the free
  RAM; a model too big to load is skipped with a plain reason
  ("needs ~X RAM, Y free. Close other apps and run again"), never
  a failure, and the run carries on. Provider models skip the
  check; an unreadable memory reading runs the model anyway.
- Speed gets a real score. Speed is no longer "(no scoring)":
  100% is 20 tokens per second or faster on this machine — just
  above the fastest small models on the target PC — scored per
  context length (1K, 8K, 32K). The score counts in the overall,
  the profiles, and Compare, like any other template.
- Resume. Every run writes a manifest (results/_runs/<run-id>.json)
  updated after each pair, so a run stopped by a crash, a cancel,
  or Ollama dying can be resumed from where it stopped: a Resume
  button next to Run offers the latest unfinished run, and
  --resume does the same headless. Done pairs are never re-run;
  failed pairs are retried; the done pairs load from their run
  files so the table shows the whole run. A model or template
  that no longer exists is skipped with a plain note.

## v16 (2026-11-14)

Math scoring rework (tasks M1–M6 from the math list). No UI
changes, no new templates — the Math template grows from 5 tasks
to 10, and every numeric answer in the app is scored by the new
rules.

- Score the last number (M1). The scorer used to pass when the
  expected number appeared anywhere in the answer — in wrong
  working, a discarded guess, or a "let me check" aside. Now the
  last number in the answer is the answer: working that mentions
  21 but concludes 19 fails, and the detail names the final
  answer ("final answer was 19, expected 21").
- Per-task tolerance (M2). The flat 1% tolerance is gone: a
  numeric answer must match exactly, unless the task gives an
  absolute tolerance or says approximate (1%). The train task
  (150 miles) now rejects 149 and 151.
- Ten math tasks (M3). The 5 current tasks stay as the easy end.
  Five harder ones join them: a two-step word problem (muffins),
  a three-step money problem with a decimal answer (taxi), a
  fraction answer (pizza, 5/8 as 0.625), a unit conversion
  (liters to milliliters), and solve for x. The fake brains now
  separate on math: smart 100%, partial 50%, dumb 0%.
- Partial credit for right setup (M4). A numeric task can carry
  intermediate step values; each step scores when its value
  appears in the working, and the final answer scores on the
  last number. A task with the right setup and the wrong final
  scores half, not 0 and not full, and the detail view marks it
  "partial (0.5)". Template scores and passed counts are over
  credits now, so a half-right task counts half ("(4.5/5)").
- Number words (M5). Written numbers become digits before
  scoring: "twenty-one" passes the apples task, "one hundred and
  fifty" passes the train task. Only whole words, so "someone"
  is untouched.
- Fractions and scientific notation (M6). "1/2" extracts as 0.5,
  not 1 and 2; "1.5e3" extracts as 1500. A zero denominator is
  skipped, never an error.

## v15 (2026-11-14)

UI redesign, part 5 (tasks R19, R20, R21, R22, R23, R24) — the
pulls, the history, the keyboard, and the small moments. No new
templates.

- Pull upgrades (R19). A pull can be stopped part-way, and pulling
  the same model again resumes from where it stopped instead of
  starting over — Ollama keeps the part it downloaded. While a pull
  runs, the Pull button stays live: typing another model queues it
  behind the running one, in order, and a queued pull can be
  removed. Pull progress shows the real speed and the time left,
  from the download rate so far. The bar stays where a stopped pull
  stopped, so you can see how far it got.
- Past runs timeline scrub (R20). A slider under the run list walks
  a model's runs newest to oldest, with a readout naming the date,
  the template, the score, and the speed — ten runs browsable in
  the time one open takes. Scrubbing moves the highlight, never the
  detail view: opening a run still takes a click. Hovering a run
  previews the same line without opening it.
- Accessibility presets (R21). Bigger text is one tap, offered on
  the first-run checklist and in Settings — the startup size plus
  three points, everywhere in the app. The size stays adjustable
  on its own in Settings, applies live while the dialog is open,
  and goes back if you cancel. A Reduce motion switch in Settings
  turns off the fade effects.
- Focus-first keyboard navigation (R22). The focus ring is a
  2 px solid border, visible in both themes — not a 1 px dotted
  line. Tab moves in visual order on every tab. Switching tabs
  brings focus back to the widget it was on, or to the tab's most
  likely first action when there is none. Ctrl+R and Esc already
  ran and cancelled runs; they are pinned now.
- Micro-interactions and teach by playing (R23). Every tick says
  what is picked now in the status line. The finish headline has a
  tick and a 200 ms fade-in — cheap motion, skipped when reduced
  motion is on. Until the first run exists, one line under the
  intro points at the smallest possible first step: ask one model
  one question on the Models tab.
- v3 review leftovers (R24). Delete a model from the installed
  list (asks first, defaults to No). Re-run failed only: after a
  run, one button re-runs just the pairs that did not finish — the
  good results stay in their run files and profiles. A template's
  tasks can be read before running it — Preview button, or
  double-click the template. And a try-it box on the Models tab
  asks one model one question with no run at all.
- Fixes found while verifying v15: the Pull button no longer
  disables during a pull (it did, which made the queue unreachable
  from the UI); deleting a model now reports the result after the
  list refreshes, so the message is not overwritten; focus memory
  now finds the tab a widget belongs to through the tab widget's
  own pages (a parent walk misses, because tab pages live under an
  internal stack); every worker completion slot now waits for its
  thread before dropping the reference — the GUI drive aborted
  twice with "QThread: Destroyed while thread is still running"
  when a pull finished, because the finished signal fires while
  the thread is still exiting; and the Settings fields now live in
  a scroll area — at bigger text sizes the fixed dialog clipped
  the explanation labels instead of scrolling, with Save and
  Cancel kept outside it, always visible.

## v14 (2026-11-14)

UI redesign, part 4 (tasks R14, R16, R17, R18) — results you can
read at a glance, and settings inside the app. No new templates.

- Results as a bento overview (R14). The Profiles tab is a
  dashboard now: one hero tile (latest overall score), four small
  tiles (best score, decode speed, strongest and weakest
  templates), with the full profile text beside them. Compare gets
  the same treatment for two models side by side — one tile each,
  same size, because a comparison weighs both equally. The
  profile's headline is readable in under six seconds; the text is
  still there for the detail.
- Charts (R16). Score bars per template and speed across runs,
  painted with plain Qt — no new dependency. One chart per profile
  (bars plus the speed line), one on Compare (grouped bars, top
  bar the first model, bottom bar the second). Each chart has a
  plain-language title stating the takeaway, and the numbers are
  printed on the marks, so color never carries the meaning alone.
  The bars scroll inside their own area when a model has many
  templates.
- Providers tab rebuild (R17). Two sections — Connection and
  Models on this provider — and every field explains itself in
  plain words directly underneath it. Test connection offers the
  fetched model list as a pick-list: pick one and its id fills the
  Model id field, so the id comes from the provider instead of
  from typing.
- Settings window (R18). File > Settings holds the models folder,
  the Ollama address, and the default time budget — in the app,
  not env-only. The models folder takes effect when this app
  starts Ollama. A setting whose variable is already set in the
  environment shows that value, locked: the environment wins,
  always. Per-model template sets are remembered too: tick a
  model's usual templates once and they follow the model, with a
  label saying which set is active; a run saves the templates it
  ran as that model's usual set. The Settings window lists the
  saved sets and can forget them all.

## v13 (2026-11-14)

UI redesign, part 3 (tasks R11, R12, R13, R15) — the run itself.
No new templates.

- Run preview (R11). Pressing Run now shows what the run will do
  before it starts — models, templates, step count, an upper-bound
  time estimate, and the provider cost when there is one — with Run
  and Adjust. Adjust closes the preview and leaves the ticks alone,
  so the setup can be changed and tried again. The time estimate
  uses each model's measured speed from its newest past run, or
  8 tok/s for a model that has never run here, plus 10 s per model
  for loading, times a 1.25 safety margin; it says "at most about"
  because that is what it is. The old cost guard is folded into it:
  a provider run's cost is named in the preview instead of in a
  separate confirm, and the $1.00 confirmation threshold is gone.
- Honest run progress (R12). Under the progress bar, a line shows
  the current step, the step count ("12 of 33 steps done"), the
  elapsed time, and a remaining-time estimate from the real rate so
  far — or "estimating time left" until two steps are done, because
  one step's rate is noise. The progress bar itself is unchanged:
  it was already driven by real steps, never a fake. Cancel stays
  on the Run tab, enabled through the whole run.
- Calm run status and finish moment (R13). A small status chip at
  the right of the Run tab's button row says Ready, Running, Done,
  Stopped, or Ollama stopped — one word, no animation, no popup.
  When a run completes, one headline appears under the progress
  bar: the best model, its score and speed, and the run's totals.
  A stopped run shows no headline; the status line already says
  what happened.
- Answers and latency in the results (R15). The results table gains
  two columns: average time to first token and average prompt speed,
  alongside the overall score and average decode speed. The detail
  view now shows what each model actually wrote — the saved answer
  under each task (each turn's answer for multi-turn tasks) — plus
  the model's first-token latency and prompt speed at the top.

## v12 (2026-11-13)

UI redesign, part 2 (tasks R1, R3, R6, R7) — the layout release.
No new templates.

- The Benchmark tab is three tabs now (R1). One crowded tab held
  the pick lists, twelve buttons, the pull box, the progress bar,
  the results table, and the detail view, with the lists capped at
  180px. Now Run setup holds the pick lists (full height, no cap),
  the budget, and Run; Run holds the progress bar, the results
  table, the detail view, Cancel, and Save/Export; Models holds
  the installed-models list, the pull box, Refresh, and Start
  Ollama. The status line sits under the tabs so it is visible on
  every tab, and the app switches to the Run tab when a run starts.
  Nothing was removed, only reorganized.
- How to Use index (R3). Help > How to Use opens the full manual in
  its own window — first time, picking models and templates,
  running a test, reading the results, past runs, pulling a model,
  providers, saving and sharing, what to do when something goes
  wrong, shortcuts — rendered as markdown. It stays open beside the
  app so the manual can be read while working; opening it again
  brings the same window forward.
- Single instance (R6). Clicking the icon while the app is already
  running brings the running copy forward instead of starting a
  second one that could write the same results dir. Headless runs
  are exempt — cron must not be blocked.
- First-run setup checklist (R7). The top of the Run setup tab
  shows three live steps — Ollama is running, at least one model
  pulled, first benchmark run done — each with what to do when it
  is open. It goes away once all three are done, or when dismissed,
  and never reappears after either.
- The Models tab's installed list is read-only: no checkboxes, and
  Space cannot add one (this PySide6 build sets a checkbox flag on
  plain list items by default, so the list clears it).

## v11 (2026-11-13)

UI redesign, part 1 (tasks R2, R4, R5, R8, R9, R10) — the first six
tasks of the redesign list, highest priority first. No new templates.

- Debug log layer (R2). The app that diagnoses Ollama dying now
  keeps a log of its own: timestamped lines in `logs/ost-YYYY-MM-DD.log`
  (one file per day, pruned after 30 days), a last-200 ring buffer in
  memory, and a crash hook that writes a scrubbed crash report before
  the app closes. Help > Debug report saves a scrubbed report of the
  recent log — version, platform, whether Ollama answers, the ring
  buffer, and the tail of today's log. Nothing secret ever reaches a
  log or a report: saved keys are matched exactly and key-shaped
  text (sk- keys, api_key/token assignments, Bearer headers) is
  replaced with [key] before anything is written.
- Profiles render as markdown (R4). The Profiles tab showed the raw
  PROFILE.md file — headings as `#`, bold as `**`. It now renders
  the markdown: headings, bold, and lists, the way the profile was
  written to be read.
- Removing a provider asks first (R5). One click used to delete a
  provider, its API key, and all its saved models with no warning.
  It now asks to confirm, defaulting to No — the same rule the run
  deletion already followed.
- Save report never overwrites (R8). The report stamp had minutes
  only, so two saves in one minute wrote the same two files. The
  stamp now has seconds, and a second save in the same second gets
  -2, -3 — the same rule the run files already use. The CSV/HTML
  export defaults got the seconds stamp too.
- The pick lists lock during a run (R9). The buttons locked, but the
  model and template lists stayed live — ticks could disagree with
  what was running. Both lists now lock when a run starts and unlock
  when it ends, whatever the reason it ends.
- Em-dash sweep (R10). No em-dash remains in any visible string:
  the missing-number placeholder is now "n/a", the status hints use
  plain sentences, and the compare picker says "Pick a model". The
  suite pins it with an AST scan of every string literal in the app
  (docstrings and comments excluded — the rule is about what the
  user sees).

## v10 (2026-11-13)

Real speech clips (task T12) — the last open repo-audit task. No new
templates.

- Real spoken audio clips (T12). The three transcribe tasks finally
  have speech to transcribe: clip-hello.wav says "hello world"
  (1.04 s), clip-count.wav says "one two three" (1.72 s),
  clip-weather.wav is a weather sentence containing "sunny"
  (2.52 s). All three are 16 kHz mono WAV, generated with
  text-to-speech at $0. The transcribe tasks were unpassable by
  design before this — the old clips were 1-second sine tones with
  no speech in them.
- Transcript normalization. Real transcription providers write
  spoken numbers as digits ("1, 2, 3"), while the count task
  expects words ("one two three") — the first verification run
  proved a correct transcript would have failed. Transcripts are
  now normalized before scoring (digits spelled out as words,
  punctuation dropped), so a correct transcript passes however the
  provider writes it. The stored answer keeps the provider's own
  words; only the scored copy is normalized.
- Fixture generator no longer destroys the clips.
  tests/make_fixtures.py used to overwrite all three clips with
  sine tones every run; it now keeps any existing clip and only
  writes a tone fallback when a clip file is missing. The suite
  pins this: it hashes the clips, runs the generator, and checks
  the hashes are unchanged — plus three envelope checks proving
  each clip is speech, not a tone.
- Secrets-rule hardening. `.gitignore` now also covers `.env`,
  `*.env`, `*.key`, and `*.pem`, on top of the providers.json and
  capabilities.json it already covered. The tree and the full
  11-commit GitHub history were audited for hardcoded keys,
  tokens, passwords, and credentialed URLs: nothing real anywhere
  (the only key-shaped strings are test fakes like "sk-dead" and
  "test-key-not-real", and the only password-shaped strings are
  the long-context fixture needles). No secret was ever committed,
  so there is nothing to flag.
- Test fix, found by the v10 suite itself. The profile-regenerate
  check stamped its two all-fail run files 09:00 — "later" only
  when the suite ran before 09:00 (the v9 run was at 00:14, so it
  passed). A run after 09:00 failed both checks because the full
  run's own files were newer. The test now stamps the files one
  day past the latest run file present, so it passes at any hour.

## v9 (2026-11-12)

Repo-audit polish release (tasks T13–T15) — the last version in the
queue. No new templates.

- Search/filter on the model + template lists (T13). Filter boxes
  above both pick lists on the Benchmark tab ("Filter models...",
  "Filter templates..."): typing narrows the visible items, and the
  match is against the item text, so filtering "vision" finds every
  vision model through the capability brackets in its label. Items
  are hidden, never removed, so a ticked item keeps its tick while
  filtered out of view and clearing the filter restores everything
  with ticks intact. The grey-outs keep working with a filter on
  (they iterate every item, hidden or not), and a refresh re-applies
  the filter text to the rebuilt lists.
- Dark mode (T14). View > Dark mode switches the whole window to a
  dark theme — dark backgrounds with light text across the lists,
  tables, text views, tabs, buttons, combo boxes, menus, progress
  bars, and scrollbars — and the choice survives a restart (saved
  in QSettings next to the window geometry, applied before the
  window shows). Light stays the default: nothing changes for
  existing users until they toggle it.
- README fixes + repo hygiene (T15). The prompt count is fixed: a
  full run is 103 prompts (90 scored tasks + 5 multi-turn extra
  turns + 5 role-following extra turns + 3 speed runs), not
  "roughly 80". The README shows a screenshot of the window
  (screenshot.png, grabbed from the app itself). The app creates
  the `images` and `audio` folders on its first launch, so there is
  somewhere to put your own test image or recording. Suggested repo
  description and topics are recorded in TEST-REPORT.md for the
  lead to set in the repo settings at ship time (they are GitHub
  settings, not files).

## v8 (2026-11-12)

Repo-audit management release (tasks T9–T11). No new templates.

- Run deletion + store-size display (T9). The Past runs tab has a
  Delete run button: pick a run and it asks to confirm, then removes
  the run file and the media folder it saved (images and audio next
  to the run file), and rebuilds the model's profile from the runs
  that are left. Nothing else is touched — no other model's runs, no
  other run of the same model — and Delete stays disabled while a
  run is in progress, so a run's files can never be deleted under
  it. The tab also shows the size of the results store ("Results use
  12.3 MB"), updated after every run and every deletion.
- Cost guard + per-template time budget (T10). Pressing Run with
  provider models ticked shows the estimated cost first: the tokens
  the chosen templates will use (prompt text at about four
  characters per token, exact context lengths for the long-context
  and speed tests, and the most each call can answer) priced at the
  rates saved for each model, warmup included. When the estimate is
  over the $1.00 confirmation threshold, the run asks to confirm,
  naming the estimated cost and the models; under it, the run starts
  straight away and the estimate shows in the status line. When no
  prices are saved for a ticked provider model, the estimate says
  "no prices saved — cost unknown" instead of inventing a number,
  and still asks to confirm. The estimate covers text tokens only
  and is an upper bound. The time budget is an opt-in wall-time
  limit per template on the Benchmark tab ("Stop a template after __
  minutes (0 = no limit)", default 0 — no limit): when set and a
  template's jobs run past it, that template's jobs stop with a
  plain "time budget reached" status, the score counts only the
  tasks that ran, and the run carries on with the next template.
  Each (model, template) pair gets its own budget window.
- Result export CSV/HTML + side-by-side comparison (T11). Export
  CSV and Export HTML on the Benchmark tab save the current results
  — model, overall score, average decode speed, status, and
  per-template scores — to a file you choose, with a plain message
  when there is nothing to export. A new Compare tab puts two
  models side by side: pick two models from the dropdowns and it
  shows their latest score on each template they ran, plus their
  overall score and speed; picking the same model twice shows a
  plain message instead of a duplicated table. Exports carry scores
  only — no answers, no API keys.

## v7 (2026-11-12)

Repo-audit usability release (tasks T5–T8). No new templates.

- Headless mode (T5). `python3 ollama_system_test.py --headless
  --models <names> --templates <ids>` runs the same worker with no
  window, for scripts and cron. Model names are comma-separated; a
  provider model is written "provider / model-id". It writes the same
  immutable run files and rebuilds the profiles, prints a plain
  summary, and exits 0 when the run finished or 1 when it did not
  (unknown model or template, Ollama not running, or the run
  stopped). `--help` documents every flag. No QApplication is created
  in headless mode — the worker runs under a QCoreApplication, which
  needs no window and no display. Ctrl+C stops after the current
  task, like the Cancel button.
- Version in window + window icon (T6). A single `__version__ = "7"`
  in the source is the one source of truth; the title bar shows
  "OLLAMA SYSTEM TEST v7" and the About box reads the same value.
  The window has an icon (ost-icon.png, a gauge mark rendered for
  this release), shipped in the tree and the zip.
- Geometry persistence + shortcuts + menu bar (T7). Window size and
  position are saved on close (QSettings) and restored on launch.
  Shortcuts: Ctrl+R Run, Ctrl+S Save report, Esc Cancel — Ctrl+R
  during a run is a no-op (Run is disabled then) and Esc does nothing
  when no run is running. A minimal menu bar: File (Save report,
  Quit) and Help (About, showing the version). The Save report menu
  item mirrors the button's enabled state.
- .desktop launcher (T8). Install-OST.desktop (double-click
  installer) and OST.desktop (launcher) ship in the repo and the
  zip. install.sh installs the launcher into the app menu
  (~/.local/share/applications/) and the icon into
  ~/.local/share/icons/, with a plain message saying so. The launcher
  resolves its own folder (it works from a path with spaces) and
  starts the app with the .venv python when there is one, else
  system python3.

## v6 (2026-11-12)

Repo-audit fix release (tasks T1–T4). No new templates.

- "Not run" instead of a false failure (T1). The template loader warns
  when a vision or audio task names a test file that is not in the
  `images/` or `audio/` folder. A task whose test file is missing is
  recorded with passed None and the detail "not run: test image/audio
  not found" — never a failure. Template scores count only the tasks
  that actually ran; when nothing ran, there is no score at all. The
  detail view, the history view and list, the saved session report,
  and profiles all show "not run" distinctly from a failure and from
  "no scoring". A run from a checkout without the fixtures shows
  Vision and Audio as not-run, never 0%.
- .gitignore + atomic providers.json (T2). `.gitignore` covers
  providers.json, capabilities.json, results/, __pycache__/, .venv/,
  and *.pyc, plus the test suite's run artifacts. save_providers
  writes to a temporary file in the same directory, chmods it 0o600
  before writing any key bytes, then renames it over providers.json:
  a crash mid-write leaves the old file intact, and the keys are
  never world-readable.
- Verification suite in the repo + CI (T3). The 686-check suite
  (run_tests.py, the fake Ollama/provider servers, the fixture and
  template generators, the test templates and fake `ollama` CLIs)
  now lives under tests/ and locates the app relative to the repo
  root, so it runs from any checkout. A GitHub Actions workflow
  (.github/workflows/test.yml) runs it on every push with offscreen
  Qt; it regenerates the test fixtures first if a checkout is
  missing them. The README documents how to run the suite locally.
- PySide6 guard + install verification + pin (T4). Launching without
  PySide6 prints a plain message naming install.sh instead of a
  traceback. install.sh installs from requirements.txt, which pins
  PySide6-Essentials to 6.12.0 (the version verified against), and
  verifies the install (PySide6 imports, the app compiles) before
  reporting success — it fails loudly if the check fails. The README
  documents that a clone needs `chmod +x install.sh run.sh` (exec
  bits do not survive the snapshot push; the Release zip carries them).

## v5 (2026-11-12)

- Capability screening. The app asks Ollama what each model can do
  (via `/api/show`) and shows it in brackets after the model name,
  for example `qwen3-vl:2b-instruct [vision]`. The answers are cached
  in `capabilities.json` next to the app; the Screen models button
  throws the cache away and re-screens everything. Screening never
  breaks the model list: a model that cannot be screened shows as
  text-only.
- Greyed-out templates. A template is enabled only when at least one
  ticked model can run it; templates no ticked model can run are
  greyed out and untickable, and with nothing ticked everything shows
  enabled. In a run, a model-template pair the model cannot run is
  recorded as "not eligible", never as a failure, and never counts
  toward the model's score. No run file is written for a skipped pair.
- Provider capability ticks. Provider models get capability
  checkboxes on the Providers tab — Text, Vision, Tools, Image
  generation, Audio, Long context — defaulting to text-only. Clicking
  a model in the list loads it into the form; Update model writes
  changes back. A model with Text unticked skips the text warmup
  (a connection test is used instead).
- 13 new templates (19 total): Vision, Image Generation, Tool
  Calling, Long Context, Multi-turn, Code Repair, Planning, Grounded
  QA, Refusal, Summarisation, Structured Output, Role Following,
  Audio. The six v4 templates are unchanged.
- Vision. Image understanding — describe, count, read text — via
  Ollama `/api/chat` images and provider `image_url` parts. Two test
  images with known content are bundled in `images/` (a shapes
  picture and a shop-sign picture).
- Image Generation. Provider-only, POST `/v1/images/generations`.
  A task passes when a valid image comes back, and the image is
  saved into the run folder for your own eyes — the app never scores
  image quality.
- Tool Calling. Ollama `/api/chat` tools and provider tools. Scored
  on the right tool with the right arguments, including a task where
  no tool is needed at all.
- Long Context. Needle-in-a-haystack at 1K, 8K, and 32K tokens.
  Ollama long-context eligibility comes from the model's reported
  context length (32K or more); a model that reports none is
  attempted and the score says whether it coped.
- Multi-turn and Role Following. A conversation-state runner carries
  the full message history across turns, so follow-ups can depend on
  earlier answers; role tasks add a system prompt per task.
- Code Repair. Fix broken code from its error message; the fixed code
  is run against test cases, like the Coding template.
- Planning, Grounded QA, Refusal, Summarisation, Structured Output.
  New scorer rules: ordered steps, refusal detection (a model passes
  refusal tasks by answering, not by refusing), and strict schema
  validation — the whole answer must be valid JSON matching the
  schema, no fences, no wrapper, no prose.
- Audio. Provider-only, `/v1/audio/transcriptions` and
  `/v1/audio/speech`. Transcription tasks score the words; speech
  tasks pass when audio comes back, and the clip is saved into the
  run folder so you can listen. The bundled clips in `audio/` are
  simple tone fixtures that prove the plumbing; for a real
  transcription test, add a recording of speech.
- Speed at context lengths. The Speed template now measures tok/s at
  1K, 8K, and 32K context instead of two plain prompts. The 32K run
  takes minutes on a CPU by design.
- Generated media is part of the run record. Images and speech clips
  are saved next to their run file in a folder named after it
  (`<time>-image_generation-images/`, `<time>-audio-audio/`), listed
  in the run file, and never overwritten.

## v4 (2026-11-12)

- Providers. Cloud providers' models can now be tested with the same
  templates, scoring, profiles, and immutable run files as local
  Ollama models. Any OpenAI-compatible provider works (OpenAI,
  OpenRouter, Groq, Together, DeepSeek, xAI, Mistral, or a local
  OpenAI-compatible server): one implementation, POST
  `{base URL}/chat/completions`, streaming, temperature 0.
- Providers tab (4th tab). Add, edit, and remove providers (name,
  base URL, API key) and their models (id, optional display name,
  optional prices per 1M tokens). A Test connection button calls
  GET `{base URL}/models` and reports plainly whether the provider
  answered — no tokens spent. Providers live in `providers.json`
  next to the app; that file holds the keys (written owner-only)
  and must never be shared or uploaded.
- The API key is never shown and never leaks. The key field is a
  password field and is left empty when editing a provider: leave it
  empty to keep the saved key, type a new one to replace it. No key
  is ever written to a run file, a report, a profile, or the status
  line.
- Provider models in the main list. They appear alongside Ollama
  models as "provider / model-id", start unticked like everything
  else, and run the same templates. Run files record the provider,
  the model id, and the tokens used; the system id stays the machine
  id (the test runs on this PC even when the model runs remotely).
- Tokens and cost. The details view shows tokens used per model
  (including the warmup call) and per template run, and the
  estimated cost when prices are set for the model. No prices set
  means tokens only — no invented cost. Profiles total tokens and
  cost across template runs.
- Provider failures never stop local tests. A provider being down
  or a key being wrong fails those models plainly ("provider X did
  not answer" / "provider X rejected the key", with the provider's
  own error message when it gives one) and the run carries on. A
  dropped provider connection is retried once, like Ollama; a bad
  key is never retried. Ollama dying still stops the whole run.
- Provider-only runs work with Ollama down. The pre-run Ollama
  check only applies when a local model is ticked; tick only
  provider models and the run goes ahead.
- The Pull a model box stays Ollama-only. Provider models are added
  by typing their id on the Providers tab, not pulled.
- The Providers tab picks up outside edits to providers.json when
  you switch to it, without wiping an edit in progress.

## v3 (2026-11-12)

- Nothing selected by default. Models and templates both start unticked;
  tick only what you want to run. Select all stays for when you want
  everything. (v2 started with everything ticked, which is how a model
  below the fold got run by accident.)
- Ollama connection fixed. The app checks Ollama answers before every
  run and refuses to start a run it cannot reach. A Start Ollama button
  starts `ollama serve` for you and waits up to 30 seconds for it to
  answer. A generate call that loses Ollama retries once after 3 seconds.
  A mid-stream disconnect fails cleanly instead of killing the run
  silently.
- The run stops when Ollama goes away. If a warmup or any task fails
  with "could not reach Ollama", the whole run aborts at once — it no
  longer ploughs on through models it cannot reach. Model-specific
  failures (a model that errors on its own) still continue to the next
  model, as in v2.
- Per-model profiles. After every run, each tested model gets a living
  summary at `results/<model>/PROFILE.md`: latest and best overall
  scores, per-template latest and best, average decode speed, strengths
  (templates at 80% or better), weaknesses (templates under 50%), and a
  short plain-language verdict written from the measured numbers only.
  The run files underneath stay immutable; the profile aggregates them.
  A Profiles tab shows the profile and has a Regenerate button.
- Pull models from the app. A "Pull a model" box on the Benchmark tab
  pulls any model in the background with a live progress bar and a
  status line showing megabytes downloaded. When it finishes, the new
  model appears in the list unticked.
- Refreshing the model or template list no longer wipes your ticks
  (v2 wart: a refresh reset everything to ticked).
- The model pickers on the Past runs and Profiles tabs now size to the
  longest model name instead of clipping it.

## v2 (2026-11-12)

- Test templates: Speed, Coding, Math, Reasoning, Instruction, Knowledge.
  Data-driven JSON files in `templates/` — copy one to add your own.
- Per-task scoring: pass/fail per task, a score per template, and an
  overall score per model. Click a model to see what passed and failed.
- Coding answers are actually run against test cases in a subprocess.
- Immutable results: `results/<model>/<system>/<time>-<template>.json`.
  Every run is its own file, never overwritten. The system folder comes
  from the machine (CPU, RAM, OS), so results stay comparable when the
  PC is upgraded. Past runs tab browses the history.

## v1 (2026-11-12)

- First release: a native Qt window that benchmarks pulled Ollama models
  — decode tok/s, prompt tok/s, time to first token, load time, size in
  RAM. Warmup + 2 measured runs per model, sortable results, text and
  JSON report.
