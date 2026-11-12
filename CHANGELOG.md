# Changelog

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
