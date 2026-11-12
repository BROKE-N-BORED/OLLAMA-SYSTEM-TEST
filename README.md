# OLLAMA SYSTEM TEST v3

A desktop window that tests your Ollama models on this PC: how fast they
are AND how good they are at different jobs. It lists the models you have
pulled, runs each one through test templates, scores them, and builds a
profile for each model — its strengths, weaknesses, and a plain-language
verdict from the numbers.

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
2. Tick the test templates you want to run. Same rule: nothing is
   ticked to start with.
3. Press Run. Each model runs each template in turn, one at a time.
   A full run (all templates, all models) takes a while — each model
   answers 27 prompts — so start with one model or one template to get
   a feel for the speed, then run the rest.
4. The table shows, for each model: overall score, average decode speed,
   and status. Click a model to see its per-template scores and which
   tasks passed or failed.
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

The app checks before every run. If Ollama is down, the run will not
start — press Start Ollama and the app will start it for you, wait for
it to answer (up to 30 seconds), then refresh the model list. If it
cannot start Ollama, it will say so: run `ollama serve` in a terminal
to see the error.

If Ollama stops answering in the middle of a run, the run stops right
there — it will not plough on through the rest of the models. Start
Ollama again, press Refresh, and Run again.

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

Each model also gets a profile, rebuilt from all of its run files:

    results/<model>/PROFILE.md

The profile is a living summary — latest and best scores per template,
average speed, strengths (templates at 80% or better), weaknesses
(templates under 50%), and a short verdict written from the numbers.
It changes as you test more; the run files underneath never do.

## What the numbers mean

- Overall score: the average of the template scores, as a percent.
  A template score is the share of its tasks the model passed.
- Decode speed: how fast the model writes its answer, in tokens per
  second. Higher is faster.
- First token: how long until the first word appears (saved in each
  run file, per task).
- Size in RAM: how much memory the model uses while loaded.

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
- A model row says "failed": that model could not run. The test
  carries on with the rest. Try that model on its own with
  `ollama run <name>` to see the error.
- A task says "failed": that task did not pass. Click the model to see
  why in the details.
- Press Cancel any time to stop the run. Results so far stay in the table,
  and every finished template run is already saved.
