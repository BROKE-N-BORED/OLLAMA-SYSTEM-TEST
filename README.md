# OLLAMA SYSTEM TEST v4

A desktop window that tests your Ollama models on this PC — and cloud
providers' models too — with the same test templates, the same scoring,
and the same profiles. It lists the models you have pulled, plus the
models on any provider you add, runs each one through test templates,
scores them, and builds a profile for each model — its strengths,
weaknesses, and a plain-language verdict from the numbers.

## Install (one time)

Open a terminal in this folder, then type:

    sh install.sh

It creates a private folder (.venv) for the one package the app needs
(PySide6-Essentials). If it says python3-venv is missing, type this first:

    sudo apt install python3-venv

Then run `sh install.sh` again.

## Start it

    sh run.sh

## How to use it

1. Tick the models you want to test. Nothing is ticked to start with —
   tick only what you want. Select all is there if you want everything.
   Local Ollama models are listed by name; provider models are listed
   as "provider / model", for example `openai / gpt-4o-mini`.
2. Tick the test templates you want to run. Same rule: nothing is
   ticked to start with.
3. Press Run. Each model runs each template in turn, one at a time.
   A full run (all templates, all models) takes a while — each model
   answers 27 prompts — so start with one model or one template to get
   a feel for the speed, then run the rest.
4. The table shows, for each model: overall score, average decode speed,
   and status. Click a model to see its per-template scores, which
   tasks passed or failed, and — for provider models — the tokens used
   and the estimated cost.
5. Press Save report to keep a summary of the session. It writes two
   files into the `results` folder next to the app: a plain text report
   and a JSON file.
6. The Past runs tab lets you browse every run you have ever done,
   grouped by model and by system.
7. The Profiles tab shows each model's profile: latest and best scores,
   strengths and weaknesses, and a verdict written from its numbers.
   Profiles rebuild themselves after every run. Press Regenerate to
   rebuild one from the run files on disk at any time.

## If Ollama is not running

The app checks before every run that touches a local model. If Ollama
is down, the run will not start — press Start Ollama and the app will
start it for you, wait for it to answer (up to 30 seconds), then refresh
the model list. If it cannot start Ollama, it will say so: run
`ollama serve` in a terminal to see the error.

If Ollama stops answering in the middle of a run, the run stops right
there — it will not plough on through the rest of the models. Start
Ollama again, press Refresh, and Run again.

Provider-only runs do not need Ollama at all: if you tick only provider
models, the run goes ahead even while Ollama is down.

## Providers

The Providers tab adds cloud providers — OpenAI, OpenRouter, Groq,
Together, DeepSeek, xAI, Mistral, or a local OpenAI-compatible server
such as LM Studio. Any provider with an OpenAI-compatible API works:
the app talks to `{base URL}/chat/completions` and
`{base URL}/models`.

To add one: press New provider, fill in the name (your own label, e.g.
`openai`), the base URL (e.g. `https://api.openai.com/v1`), and the
API key, then press Test connection to check it answers (that costs
no tokens). Add the models you want to test by their id (e.g.
`gpt-4o-mini`), with a display name and prices per 1M tokens if you
want cost shown. Press Save provider when you are done — the models
appear in the Benchmark list, unticked like everything else.

Your API keys live in `providers.json` next to the app. That file
holds your keys: never share it, never upload it, never send it to
anyone. The app never writes a key anywhere else — not into a run
file, a report, a profile, or the status line — and the key is never
shown on screen. When you edit a provider, the key field is left
empty on purpose: leave it empty to keep the saved key, or type a new
one to replace it.

Provider tests cost real API money. The details view shows the tokens
each model used (prompt and completion, including the warmup call)
and, when you set prices for the model, the estimated cost of the run.
No prices set means tokens only — the app never invents a cost. Run
files record the tokens and the prices used, so the cost stays
attached to the run even if prices change later.

If a provider is down or a key is wrong, those models fail plainly
("provider X did not answer" / "provider X rejected the key") and the
run carries on with the rest — a provider outage never stops your
local Ollama tests. A single dropped connection is retried once; a
rejected key is never retried.

The Pull a model box is Ollama-only: provider models are added by
typing their id on the Providers tab, not pulled.

## Pull a model from the app

Type a model name in the "Pull a model" box (for example `qwen3:4b`)
and press Pull. The download runs in the background with a live
progress bar and a status line showing what is downloading and how many
megabytes are in so far. When it finishes, the new model appears in the
list — unticked, like everything else.

Pulls go to Ollama's own models folder. If you want models stored
somewhere else, that is Ollama's own setting: set the OLLAMA_MODELS
environment variable before starting Ollama.

## The test templates

- Speed: tokens per second only, no scoring.
- Coding: write Python functions. Each answer is actually run against
  test cases, so a function only passes if it works.
- Math: word problems with known answers.
- Reasoning: short logic puzzles.
- Instruction: follow the format asked for (JSON, exact line counts,
  word limits).
- Knowledge: factual questions.

Templates are JSON files in the `templates` folder. You can add your own:
copy one, change the id, name, and tasks, and press Refresh. A broken
template file is skipped with a warning, it never crashes the app.

## Where results live

Every template run is saved to its own file as soon as it finishes:

    results/<model>/<system>/<time>-<template>.json

The system folder comes from your machine (CPU, RAM, OS), so when you
upgrade your PC one day, the new results go in a new folder and the old
ones stay untouched. Run files are never overwritten or edited. If two
runs ever land in the same second, the second one gets -2 on the name.

Each run file records the provider (`ollama` for local models, the
provider name otherwise), the model id, and — for provider runs — the
tokens used and the estimated cost. Provider models get their own
result folders, named after the "provider / model" label.

Each model also gets a profile, rebuilt from all of its run files:

    results/<model>/PROFILE.md

The profile is a living summary — latest and best scores per template,
average speed, strengths (templates at 80% or better), weaknesses
(templates under 50%), and a short verdict written from the numbers.
For provider models it also totals the tokens used and the estimated
cost across template runs. It changes as you test more; the run files
underneath never do.

## What the numbers mean

- Overall score: the average of the template scores, as a percent.
  A template score is the share of its tasks the model passed.
- Decode speed: how fast the model writes its answer, in tokens per
  second. Higher is faster. For provider models it is measured from
  the streamed answer and the token counts the provider reports; a
  provider that reports no usage shows no speed.
- First token: how long until the first word appears (saved in each
  run file, per task).
- Size in RAM: how much memory the model uses while loaded (local
  Ollama models only).
- Tokens: prompt and completion tokens the provider reports for a run.
  The model total includes the warmup call; template lines and profiles
  count template runs only.
- Estimated cost: tokens times the prices you set for the model, in
  dollars. Shown only when both prices are set.

## A note on the coding tests

For coding tasks, the code the model writes is run on your machine in a
separate process, with a 10-second limit per test case, to check it
actually works. It runs as you, the same as anything else you run. These
are your own models on your own machine.

## If something goes wrong

- "Ollama is not running": press Start Ollama in the app, or start
  Ollama yourself, then press Refresh.
- "could not reach Ollama — start it, then press Refresh": Ollama
  stopped answering. Start it again, press Refresh, and Run again.
- "provider X did not answer": the provider could not be reached.
  Check the base URL on the Providers tab, or try Test connection.
- "provider X rejected the key": the API key is wrong or revoked.
  Paste a fresh key on the Providers tab and press Save provider.
- A model row says "failed": that model could not run. The test
  carries on with the rest. Try that model on its own with
  `ollama run <name>` (local) or Test connection (provider) to see
  the error.
- A task says "failed": that task did not pass. Click the model to see
  why in the details.
- Press Cancel any time to stop the run. Results so far stay in the table,
  and every finished template run is already saved.
