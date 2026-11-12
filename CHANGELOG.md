# Changelog

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
