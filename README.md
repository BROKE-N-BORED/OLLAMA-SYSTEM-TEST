# OLLAMA SYSTEM TEST v2

A desktop window that tests your Ollama models on this PC: how fast they
are AND how good they are at different jobs. It lists the models you have
pulled, runs each one through test templates, and scores them.

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

1. Tick the models you want to test (all are ticked to start with).
2. Tick the test templates you want to run (all are ticked to start with).
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

- "Ollama is not running": start Ollama first, then open this again.
- A model row says "failed": that model could not run. The test
  carries on with the rest. Try that model on its own with
  `ollama run <name>` to see the error.
- A task says "failed": that task did not pass. Click the model to see
  why in the details.
- Press Cancel any time to stop the run. Results so far stay in the table,
  and every finished template run is already saved.
