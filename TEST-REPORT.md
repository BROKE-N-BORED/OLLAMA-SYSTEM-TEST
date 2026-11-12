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
