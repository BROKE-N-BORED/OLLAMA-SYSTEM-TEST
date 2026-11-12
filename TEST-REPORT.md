# TEST-REPORT — OLLAMA SYSTEM TEST v4

Verified 2026-11-12 in a Linux sandbox (x86_64, PySide6 6.12.0, Python 3.12.3)
under the Qt test env from AGENTS.md: QT_QPA_PLATFORM=offscreen,
LD_LIBRARY_PATH=~/workspace/qt-libs/root/usr/lib/x86_64-linux-gnu,
venv ~/workspace/forge-v2-testenv/bin/python. Plain widgets only, no OpenGL.

v4 adds PROVIDERS: any OpenAI-compatible provider (OpenAI, OpenRouter,
Groq, Together, DeepSeek, xAI, Mistral, or a local OpenAI-compatible
server) can be added on a new Providers tab, and its models are tested
with the same templates, scoring, profiles, and immutable run files as
local Ollama models. Provider models are listed as "provider / model-id".
Run files record tokens used; when prices per 1M tokens are set, the
details view shows the estimated cost. install.sh / run.sh / templates
are unchanged from v3 (venv + PySide6-Essentials, the PEP 668 fix from
the v1 report still stands).

The sandbox has no Ollama, no models, and no real provider accounts, so
all runtime tests ran against a fake Ollama HTTP server (as in v3) and a
new fake OpenAI-compatible provider server (GET /v1/models, streaming
POST /v1/chat/completions with SSE chunks and usage, canned
right/wrong/partial answers per model, a bad-key mode returning HTTP 401
with an OpenAI-shaped error body, and a dead-provider mode). Test suite:
~/workspace/ollama-system-test-v4-verify/run_tests.py — **355 passed,
0 failed** (final run; the v3 suite's 233 checks are all carried over
and green inside that count, with the "three tabs" check updated to
four for the new Providers tab).

## What was verified

### 1. Provider config (providers.json)

- A missing file means no providers, not an error. A corrupt file, a
  wrong-shaped file, a duplicate provider name, a provider with no name
  or no base URL, a model with no id, and a model with a bad price are
  all skipped with warnings; the good entries still load.
- Round-trip: save then load keeps the provider, the key, the models,
  and the prices; a trailing slash on the base URL is stripped.
- The file is written owner-only (mode 0600).
- Label format: `provider_label("fake-provider", "fake-gpt-smart")` is
  "fake-provider / fake-gpt-smart", and it slugifies to
  "fake-provider_fake-gpt-smart" for the result folder.

### 2. Cost math

- estimate_cost(560, 5600, 0.15, 0.60) is exactly 0.003444.
- No cost is invented: estimate_cost returns None when either price is
  missing or either token count is unknown.
- fmt_cost: $0.0034 for fractions of a cent (4 places), $1.50 for real
  money (2 places), $0.00 for zero, dash for None.

### 3. Test connection

Proven four ways against the fake provider (direct calls):
- Good key: "Connected — fake-provider answered, 3 models listed."
- Bad key: fails plainly with "rejected the key".
- Dead provider (unused port): fails plainly with "did not answer".
- Empty base URL: fails plainly asking for a base URL.
No tokens are spent: the call is GET /v1/models only.

### 4. Chat completions path

- provider_generate_once returns the answer text, prompt_tokens 20,
  completion_tokens 200, total_tokens 220, a measured TTFT, and a
  measured decode speed from the streamed chunks and the usage object.
- Slow mode (content chunks 1.0 s apart, 2.0 s span) measures 100.0
  tok/s, as designed (200 tokens / 2.0 s).
- Bad key raises exactly "provider fake-provider rejected the key:
  Incorrect API key provided" — the provider's own error message is
  used — and is NOT unreachable, so it is never retried.
- Dead provider raises exactly "provider fake-provider did not
  answer" and IS unreachable, so it is retried once.
- A broken model (HTTP 500 with an OpenAI-shaped error body) raises
  exactly "provider fake-provider: model exploded".
- Both error-body shapes are parsed: {"error": "text"} and the OpenAI
  shape {"error": {"message": "text", ...}}.

### 5. Provider scoring exactness

Through the real HTTP path (provider_generate_once + score_task):
all 15 combinations (5 scored templates x smart/dumb/partial brains)
score exactly 100% / 0% / 60%, the same as the Ollama path.

### 6. Providers tab (GUI)

- The key field is a password field. The tab opens with a blank form
  when there are no providers.
- Adding a provider through the form, testing the connection in the
  UI ("Connected — fake-provider answered, 3 models listed."), adding
  3 models (one with display name and prices, shown as
  "fake-gpt-smart — Smart ($0.15 in / $0.60 out per 1M)"), and saving
  all work; providers.json on disk holds the provider and its models.
- A duplicate model id is rejected ("already on this provider"); a
  bad price is rejected ("must be a number").
- After saving, the key field is empty (the key is never shown) and
  the status reads "Key saved".
- Re-saving with the key field left empty keeps the stored key in the
  file. Testing a bad key typed in the form fails plainly and does
  NOT overwrite the saved key.
- Switching tabs keeps an unsaved edit: the tab only reloads from
  providers.json when the file actually changed on disk (see Bugs
  found).

### 7. Provider models in the main list and a full run

- After saving, the Benchmark list holds 7 models (4 local + 3
  provider), the provider models labelled "fake-provider / ...", all
  UNCHECKED — the durable nothing-ticked rule holds for providers too.
- A provider model run through all 6 templates scores 100% overall,
  status done, with a measured decode speed in the table.
- The details view shows exactly "Tokens: 560 in, 5,600 out (includes
  warmup)" and "Estimated cost: $0.0034" (28 calls: warmup + 27
  template prompts, 20 in / 200 out each, at $0.15 in / $0.60 out per
  1M). Per-template lines show tokens and cost too
  ("Coding: 100% (5/5), ..., 100 in / 1,000 out, $0.0006").
- The run files: 6 for the provider model, in
  results/fake-provider_fake-gpt-smart/<system>/. The coding file
  records provider "fake-provider", provider_model "fake-gpt-smart",
  model "fake-provider / fake-gpt-smart", prompt_tokens 100,
  completion_tokens 1000, total_tokens 1100, estimated_cost_usd
  exactly 0.000615, and the machine system id. Every task carries its
  token counts.
- PROFILE.md is written for the provider model: latest 100%,
  "Provider: fake-provider", "Tokens used in template runs: 540 in,
  5,400 out" (27 template calls; the warmup call is not part of any
  run file), "Estimated cost in template runs: $0.0033".

### 8. Provider retry once

With the fake provider dropping the next chat call, a Speed run still
completes with status done, and the server counted exactly 4 chat
calls: warmup fail + warmup retry + 2 speed prompts.

### 9. Provider failures never stop local tests

With a dead provider (unused port) and a bad-key provider added, a run
ticking one local model and both broken provider models finishes with
status "Done." (not ollama_down): the local model completes, the dead
provider model reads exactly "failed: provider dead-provider did not
answer", and the bad-key model reads exactly "failed: provider
badkey-provider rejected the key: Incorrect API key provided".

### 10. Ollama down, providers up

- With the Ollama server shut down, the model list holds only the 5
  provider models, Run stays enabled, and the status says Ollama is
  not running but the provider models are ready.
- A provider-only run completes with Ollama down, status Done.
- A mixed run (a CLI-fallback local model + a provider model) with
  Ollama down is still blocked by the pre-run check, with the exact
  Ollama-down run message and an empty table.

### 11. The API key appears nowhere in results

After runs and a saved report, every file under results/ was read and
checked: neither the good fake key nor the bad key appears in any run
file, profile, or report (txt or JSON). The status line and the detail
view are clean too. (providers.json itself holds the key, as it must —
the check is that nothing else does.) The package directory gained no
providers.json from the test run.

### 12. Remove model and provider (GUI)

- Removing a provider from the tab deletes it from providers.json and
  its models leave the Benchmark list.
- Removing a model from a provider, then saving, removes it from the
  file.

### 13. v3 checks re-run green

- Scoring: all 15 combinations (5 scored templates x smart/dumb/partial
  brains) exact — 100% / 0% / 60% — plus every rule type individually.
- Code execution: real subprocess runs — correct fib passes, wrong answer
  fails naming the test, syntax error fails, missing function fails, an
  infinite loop times out quickly.
- Retry once: the Ollama warmup fail + retry + 2 prompts is exactly 4
  generate attempts.
- Immutability: a second save in the same second makes a -2 file and the
  first file's SHA-256 is unchanged; a speed rerun through the GUI added
  a second speed file and left the first file's bytes untouched.
- History tab: 3 models, 7 smart runs listed, coding run summary shows
  model, 100%, and the system id.
- Profiles tab: smart profile 100%, regenerate after two hand-added
  all-fail runs rebuilds to latest 60% / best 100% with the run files'
  SHA-256 hashes unchanged.
- Save report: writes the .txt + .json pair; txt has the 100% line and
  the failed model; json has smart overall 100.0 with 6 templates.
- Pull: success (bar moves, "Pulled fake-new:1b.", new model unchecked)
  and failure (plain "pull exploded", button re-enabled).
- Cancel: stops a slow run, status "Cancelled. Results so far are still
  in the table.", cancelled model has no saved run.
- Mid-stream close and server-dies-mid-run: the run aborts with the
  exact Ollama-died message, the second model is never attempted, and
  the window stays responsive.
- Pre-run check: with the API dead and the model list from the CLI
  fallback, Run is blocked with the exact Ollama-down run message and
  the table stays empty.
- Start Ollama: success, already-running (exactly 1 serve call),
  timeout message, missing-binary message.
- Ollama down (no server, no binary, no providers): no models, the
  exact Ollama-down status, Run disabled, Start Ollama still enabled.
- CLI fallback: with the API dead and a fake `ollama` CLI on PATH, the
  window lists the 2 CLI models and Run is enabled.
- Templates: the 6 shipped templates load with no warnings; a 7th
  template added as JSON loads in the GUI; corrupt and bad-rule template
  files are skipped with warnings, never a crash.
- Size in RAM recorded (fake-partial:3b = 3,000,000,000); /api/ps empty
  yields an empty size map.
- No bare `&` in any button or label text (checked programmatically,
  including the new Providers tab widgets).

### 14. Screenshots eyeballed

- screenshot-start.png: window opens with 4 tabs, nothing ticked,
  "Models (local and provider):", no clipped text.
- screenshot-providers.png: Providers tab with a saved provider, the
  key field empty with "Leave empty to keep the saved key" and "Key
  saved", the Test connection result, 3 models with prices, "Saved
  fake-provider." No clipped text, no bare `&`.
- screenshot-provider-results.png: provider model in the results
  table at 100% with a measured speed; details show "Tokens: 560 in,
  5,600 out (includes warmup)", "Estimated cost: $0.0034", and
  per-template tokens and cost. No clipped text.
- screenshot-results.png, screenshot-history.png, screenshot-profiles.png,
  screenshot-pull.png: re-taken in the v4 run, same content as v3.

## Bugs found and fixed in place

1. Providers tab went stale (app bug, caught by the suite): providers
   added to providers.json outside the tab (a hand edit, or another
   copy of the app) never appeared in the tab's list, because the tab
   only rebuilt on save/remove. Fixed: switching to the Providers tab
   now reloads when the file's bytes changed since the tab last saw
   it — and only then, so an edit in progress is never wiped. Two
   regression checks cover both sides (outside edits appear; an
   unsaved edit survives a tab switch).
2. Providers intro label had no word wrap (app nit, caught by
   eyeballing the screenshot): it fit at the default window width but
   would clip in a narrower window. Now wraps like the Benchmark
   intro.
3. One test expectation was wrong (test-side, not the app): after
   removing a provider, the suite expected 6 models in the list, but
   Ollama was down and PATH was back to the original, so only the 4
   provider models remained — the app was right, the expectation was
   fixed.

## Notes on the numbers

- Provider decode speed is measured from the streamed answer: the
  provider's completion_tokens over the wall time between the first
  and last content chunks. It needs at least two content chunks spread
  in time and a usage object; a provider that reports no usage shows
  no speed rather than an invented one. Prompt speed is estimated as
  prompt_tokens over the wait for the first token.
- Model token totals include the warmup call (the provider charges
  for it). Template lines and profiles count template runs only,
  because the warmup is not part of any run file; the detail view
  says so ("includes warmup"), and the profile line reads "Tokens
  used in template runs".
- The fake provider reports fixed usage (20 prompt / 200 completion
  tokens per call), so the token and cost checks are exact. Real
  providers report real usage; the math is the same.
- The request sends stream_options {"include_usage": true} so usage
  arrives in the final chunk. Providers that ignore the option may
  still send usage (most do); ones that send none show tokens as
  unknown and no cost.

## Not verified

- Any real provider: no real API key exists in the sandbox, so
  OpenAI, OpenRouter, Groq, Together, DeepSeek, xAI, Mistral, and
  LM Studio are unproven against the real services. The fake
  provider implements the OpenAI wire shape (SSE `data:` chunks,
  `[DONE]`, usage object, OpenAI-shaped error bodies), but real
  providers vary in details (usage timing, reasoning_content fields,
  rate-limit 429s with Retry-After, non-streaming fallbacks).
- A real `ollama serve` start from the button (proven only with a
  fake CLI + tiny server); a real multi-GB pull (proven only against
  the fake server's 100 MB stream).
- His real-PC run against real models and real providers (the closer),
  system id on his Mint PC, real-model answer quirks vs the scorers.
- Long real runs; code-exec against real model output.
- Provider rate limiting (429): the app reports the provider's error
  message and fails the model; there is no backoff-and-retry for
  rate limits.
