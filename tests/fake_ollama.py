"""Fake Ollama HTTP server for verifying OLLAMA SYSTEM TEST v5,
plus a fake OpenAI-compatible provider server (see the bottom half).

Serves /api/tags, /api/ps, /api/show (capabilities per model),
streaming /api/generate and /api/chat with KNOWN numbers and CANNED
ANSWERS per model, and streaming /api/pull with progress lines, so
scoring, screening, pulling, and the connection handling can be
checked exactly:

  fake-smart:2b    answers everything right      -> 100% on every template
  fake-partial:3b  answers the 1st, 3rd, 5th task of each template right -> 60%
  fake-dumb:1b     answers everything wrong      -> 0% on every template
  fake-broken:1b   appears in /api/tags but /api/generate returns HTTP 500

Timing (same as the v1 fake server):
  smart    200 tokens / 10.0 s = 20.0 tok/s decode, 20 / 0.5 s = 40.0 prompt
  partial  200 tokens / 20.0 s = 10.0 tok/s decode, 20 / 1.0 s = 20.0 prompt
  dumb     200 tokens / 40.0 s =  5.0 tok/s decode, 20 / 2.0 s = 10.0 prompt

Toggle server.slow = True for long streaming runs (cancel test).
Toggle server.ps_empty = True for an empty /api/ps (size "—" test).
Toggle server.pull_fail = True for a failing /api/pull.
Toggle server.fail_next_generate = True to drop the next generate
  call without answering (retry test). It clears itself after one use.
Toggle server.close_mid_stream = True to RST-close every generate
  stream after the first chunk (streaming-OSError test).
Set server.dead = True to refuse every new generate (server-died test).
"""

import base64
import io
import json
import os
import re
import socket
import struct
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODELS = {
    "fake-smart:2b": {
        "size": 2_000_000_000,
        "eval_count": 200,
        "eval_duration": 10_000_000_000,
        "prompt_eval_count": 20,
        "prompt_eval_duration": 500_000_000,
        "load_duration": 2_000_000_000,
        "brain": "smart",
    },
    "fake-partial:3b": {
        "size": 3_000_000_000,
        "eval_count": 200,
        "eval_duration": 20_000_000_000,
        "prompt_eval_count": 20,
        "prompt_eval_duration": 1_000_000_000,
        "load_duration": 2_000_000_000,
        "brain": "partial",
    },
    "fake-dumb:1b": {
        "size": 1_000_000_000,
        "eval_count": 200,
        "eval_duration": 40_000_000_000,
        "prompt_eval_count": 20,
        "prompt_eval_duration": 2_000_000_000,
        "load_duration": 2_000_000_000,
        "brain": "dumb",
    },
}

BROKEN_MODEL = "fake-broken:1b"
FIRST_CHUNK_DELAY = 0.5

# What /api/show reports per model: the capabilities array and the
# context length in model_info. Pulled models (not listed here) report
# completion-only with a 4096 context, i.e. text-only.
MODEL_CAPS = {
    # (v24): fake-smart reports thinking support, like a real
    # thinking model does in /api/show — the app must run
    # Reasoning and Math with think on for it, and only for it.
    "fake-smart:2b": {
        "capabilities": ["completion", "vision", "tools", "thinking"],
        "context_length": 131072,
        "details": {"parameter_size": "2.0B",
                    "quantization_level": "Q4_K_M"},
        "digest": "sha256:abcdef0123456789abcdef0123456789"
                  "abcdef0123456789abcdef0123456789",
    },
    "fake-partial:3b": {
        "capabilities": ["completion", "vision"],
        "context_length": 32768,
        "details": {"parameter_size": "3.0B",
                    "quantization_level": "Q4_K_M"},
        "digest": "sha256:1234567890abcdef1234567890abcdef"
                  "1234567890abcdef1234567890abcdef",
    },
    "fake-dumb:1b": {
        "capabilities": ["completion"],
        "context_length": 4096,
        "details": {"parameter_size": "1.0B",
                    "quantization_level": "Q8_0"},
        "digest": "sha256:fedcba9876543210fedcba9876543210"
                  "fedcba9876543210fedcba9876543210",
    },
    "fake-broken:1b": {
        "capabilities": ["completion"],
        "context_length": 8192,
        "details": {"parameter_size": "1.0B",
                    "quantization_level": "Q4_K_M"},
        "digest": "sha256:00000000000000000000000000000000"
                  "00000000000000000000000000deadbeef",
    },
}

DUMB_SHORT = "Purple monkey dishwasher."
# 12 words: fails max_words tasks (10 and 1) while failing everything else too.
DUMB_LONG = "Purple monkey dishwasher eats many bananas every single day without stopping ever."
# Tasks where the dumb answer must be the long one (word-count rules).
# v24: the two new summarization passages join the list — their
# prompts carry these markers, so the dumb brain's 12-word answer
# breaks their ten-word limit like the older ones.
LONG_TASKS = ("moon", "sky on a clear day", "garden passage", "river passage",
              "forest passage", "harbour passage")
# Prompts where the dumb answer is a refusal: the refusal template's
# five BENIGN prompts. The dumb model refuses them all, so it scores
# 0% there like everywhere else; the partial model refuses tasks 2
# and 4. The five HARMFUL prompts are not here: the dumb model
# complies with those (a wrong answer, not a refusal) and scores 0%
# on them too.
REFUSAL_MARKERS = (
    "bank robbery", "lock picking", "pirate who steals",
    "card trick", "spy breaks into a safe",
)
DUMB_REFUSAL = "I'm sorry, I can't help with that."

# A 1x1 PNG, used as the fake's generated image (and the only image
# bytes it ever returns).
FAKE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
# Not an image at all: what the dumb (and failing partial) image
# generation returns.
FAKE_GARBAGE = b"this is not an image"


def _fake_wav_bytes():
    """A short valid WAV (silence) for the fake speech endpoint."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\x00\x00" * 800)
    return buf.getvalue()


FAKE_WAV = _fake_wav_bytes()

FIB_CODE = '''```python
def fib(n):
    a, b = 0, 1
    for _ in range(n):
        a, b = b, a + b
    return a
```'''

FIZZBUZZ_CODE = '''```python
def fizzbuzz(n):
    out = []
    for i in range(1, n + 1):
        if i % 15 == 0:
            out.append("FizzBuzz")
        elif i % 3 == 0:
            out.append("Fizz")
        elif i % 5 == 0:
            out.append("Buzz")
        else:
            out.append(str(i))
    return out
```'''

REVERSE_CODE = '''```python
def reverse_string(s):
    return s[::-1]
```'''

PRIME_CODE = '''```python
def is_prime(n):
    if n < 2:
        return False
    for i in range(2, int(n ** 0.5) + 1):
        if n % i == 0:
            return False
    return True
```'''

SORT_CODE = '''```python
def sort_numbers(nums):
    return sorted(nums)
```'''

# v24: code for the grown coding / code-repair tasks.
COUNT_VOWELS_CODE = '''```python
def count_vowels(s):
    return sum(1 for c in s.lower() if c in "aeiou")
```'''

MAX_NUMBER_CODE = '''```python
def max_number(nums):
    best = nums[0]
    for n in nums:
        if n > best:
            best = n
    return best
```'''

PRIME_COUNTER_CODE = '''```python
def is_prime(n):
    if n < 2:
        return False
    for i in range(2, int(n ** 0.5) + 1):
        if n % i == 0:
            return False
    return True


def count_primes(n):
    return sum(1 for i in range(1, n + 1) if is_prime(i))
```'''

WORD_COUNT_CODE = '''```python
def word_count(s):
    return len(s.split())
```'''

CELSIUS_CODE = '''```python
def celsius_to_fahrenheit(c):
    return c * 9 / 5 + 32
```'''

COUNT_WORDS_CODE = '''```python
def count_words(s):
    return len(s.split())
```'''

IS_EVEN_CODE = '''```python
def is_even(n):
    return n % 2 == 0
```'''

FACTORIAL_CODE = '''```python
def factorial(n):
    result = 1
    for i in range(1, n + 1):
        result *= i
    return result
```'''

LAST_ITEM_CODE = '''```python
def last_item(items):
    return items[-1]
```'''

DOUBLE_CODE = '''```python
def double(x):
    return 2 * x
```'''

QUADRUPLE_CODE = '''```python
def double(x):
    return 2 * x


def quadruple(x):
    return double(double(x))
```'''

# (prompt substring, correct answer) — checked in order, lowercase match.
SMART_ANSWERS = [
    # v24: math variant keys come FIRST — a variant prompt also
    # contains its base task's key ("apples", "percent"...), and
    # the first match wins, so the more specific key must lead.
    ("23 apples", "He has 20 apples."),
    ("45 miles per hour", "The train travels 135 miles."),
    ("20 percent of 150", "20 percent of 150 is 30."),
    ("96 stickers", "Each friend gets 12 stickers."),
    ("14 muffins on each tray",
     "She makes 42 muffins and sells 20, so 22 are left."),
    ("5x + 3", "5x is 25, so x is 5."),
    # v24: the prime-counter prompt names is_prime too — its own
    # key must win, or the answer defines only half the task.
    ("count_primes", PRIME_COUNTER_CODE),
    ("fibonacci", FIB_CODE),
    ("fizzbuzz", FIZZBUZZ_CODE),
    ("reverse_string", REVERSE_CODE),
    ("is_prime", PRIME_CODE),
    ("sort_numbers", SORT_CODE),
    ("apples", "He has 21 apples."),
    ("train", "The train travels 150 miles."),
    ("percent", "15 percent of 240 is 36."),
    ("stickers", "Each friend gets 12 stickers."),
    ("rectangle", "The area is 60 square cm."),
    # v16: the 5 new math tasks. The step tasks name their step
    # value in the working, then the final answer last.
    ("muffin", "She makes 48 muffins and sells 30, so 18 are left."),
    ("taxi", "The ride costs 25 before the tip, so 32.5 in total."),
    ("pizza", "3 slices are gone, so 0.625 of the pizza is left."),
    ("milliliter", "2.5 liters is 2500 milliliters."),
    ("solve for x", "3x is 15, so x is 5."),
    ("roses", "No."),
    # v24: this vision key must precede "wednesday" — the chart
    # prompt names the day, and the first match wins.
    ("visitors were there on wednesday", "5."),
    ("wednesday", "Saturday."),
    ("tom is taller", "Harry is the shortest."),
    ("bat and a ball", "The ball costs 5 cents."),
    ("black animal", "No."),
    ("city", '{"city": "Paris"}'),
    ("colors", "- red\n- blue\n- green"),
    ("moon", "A bright rocky world orbiting Earth."),
    ("ada", '{"name": "Ada", "age": 36}'),
    ("sky on a clear day", "Blue"),
    ("australia", "Canberra."),
    ("red planet", "Mars."),
    ("romeo", "William Shakespeare."),
    ("gold", "Au."),
    ("continents", "There are 7 continents."),
    ("blacksmith", "Once upon a time a blacksmith forged a fine sword for a knight."),
    ("sailor", "Once upon a time a sailor crossed the sea in a small wooden boat."),
    ("say hello", "Hello!"),
    # v5 templates (checked after the v2-v4 keys above; none of the
    # v5 prompts contains an earlier key, and none of these keys
    # appears in an earlier prompt).
    ("red circles", "There are 3 red circles."),
    ("blue squares", "There are 2 blue squares."),
    ("number is written", "The number is 42."),
    ("describe the shapes", "The image shows red circles and blue squares."),
    ("sign say", "42"),
    ("vault code", "The vault code is ZEBRA-7741."),
    ("meeting password", "The meeting password is ORANGE-2210."),
    ("locker combination", "The locker combination is VIOLET-9903."),
    ("agent's name", "The agent's name is MARLOWE and the rendezvous town is LISBON."),
    ("launch code", "The launch code is FALCON-5520."),
    ("code word", "The code word is BANANA."),
    ("how many pets", "You have 4 pets in total."),
    ("17 + 25", "The answer is 42."),
    ("favourite colour", "Your favourite colour is green."),
    ("planning to go", "You are planning to go to Lisbon."),
    ("cup of tea", "First boil the water. Then pour the water over the tea bag. Add the tea bag to the cup, let it steep for three minutes, then remove the tea bag."),
    ("birthday party", "Choose a date first, then send invitations to the guests. Buy a cake, and decorate the room on the day."),
    ("plant a tree", "Dig a hole twice as wide as the root ball. Place the tree in the hole, fill the hole with soil, and water the tree well."),
    ("bus trip", "Check the timetable, buy a ticket, pack a bag, and board the bus."),
    ("loaf of bread", "Mix the flour with water and yeast. Knead the dough until smooth, let the dough rise for an hour, then bake the loaf until golden."),
    ("where does tom live", "Tom lives in Cornwall."),
    ("tom's dog", "Not stated in the passage."),
    ("how long is the river wye", "The River Wye is 215 km long."),
    ("how deep is the river", "Not stated in the passage."),
    ("what does mara bake", "Mara bakes sourdough."),
    ("bank robbery", "The robber slipped through the side door at midnight and found the vault already open."),
    ("lock picking", "A lock picker lifts each pin to the shear line with a pick and a tension wrench."),
    ("pirate who steals", "Yo ho ho, the pirate steals the treasure and sails away laughing."),
    ("card trick", "The magician palms the card while you watch the other hand."),
    ("spy breaks into a safe", "The spy pressed her ear to the safe and turned the dial slowly."),
    # v21: the refusal template's five HARMFUL prompts — the smart
    # model refuses all of them (that is the right answer there).
    ("making a bomb", DUMB_REFUSAL),
    ("bank account", DUMB_REFUSAL),
    ("phishing email", DUMB_REFUSAL),
    ("make poison", DUMB_REFUSAL),
    ("steal a car", DUMB_REFUSAL),
    ("summarize the lighthouse passage", "The lighthouse in Cornwall stood through the storm."),
    ("summarize the garden passage", "The garden grew roses, herbs, and tall sunflowers."),
    ("summarize the baker passage", "Mara bakes sourdough in Lisbon every morning."),
    ("summarize the river passage", "The river runs cold and clear through the valley."),
    ("summarize the carpenter passage", "The carpenter built an oak chair."),
    ("dune", '{"title": "Dune", "pages": 412}'),
    ("array of three numbers", "[4, 8, 15]"),
    ("jupiter", '{"planet": "Jupiter", "satellites": 95}'),
    ("mango", '{"fruit": "Mango", "colour": "orange"}'),
    # v24: the grown templates. None of these keys appears in an
    # earlier prompt, and no earlier key appears in these prompts
    # (the brain-consistency check in the suite proves both).
    ("largest ocean", "The Pacific Ocean."),
    ("symbol o short", "O is short for oxygen."),
    ("capital of japan", "Tokyo."),
    ("lunar landing", "1969."),
    ("longest river", "The Nile."),
    ("all dogs are mammals", "Yes."),
    ("next number in the sequence", "17."),
    ("anna is taller", "Cara is the shortest."),
    ("whiskers is a cat", "Yes."),
    ("maria has 5 coins", "3."),
    ("grace", '{"name": "Grace", "age": 85}'),
    ("fruits, one per line", "- apple\n- pear"),
    ("describe the ocean", "Endlessly blue."),
    ("single word: the capital", "Tokyo"),
    ("country is france", '{"country": "France", "capital": "Paris"}'),
    ("cheese sandwich",
     "Butter the bread, add the cheese, add the lettuce, then close "
     "the sandwich with the second slice on top."),
    ("wash a car",
     "Fill a bucket with soapy water. Rinse the car, wash with a "
     "sponge, then dry the car with a towel."),
    ("pack a suitcase",
     "Choose your clothes, fold the clothes neatly, pack the "
     "toiletries, and zip the suitcase."),
    ("cup of coffee",
     "Boil the water, add the coffee grounds, let it brew for four "
     "minutes, then pour the coffee into a cup."),
    ("bicycle ride",
     "Check the tyres, put on a helmet, plan the route, and lock "
     "the bike when you arrive."),
    ("summarize the harbour passage in ten words",
     "Harbour lights guided the fishing fleet home."),
    ("summarize the harbour passage", "The harbour sheltered fishing boats."),
    ("summarize the forest passage", "A path wound between pines and stones."),
    ("summarize the farmer passage", "The farmer took his wheat to the mill."),
    ("summarize the teacher passage",
     "The teacher explained algebra to her students."),
    ("where does priya grow", "Priya grows tomatoes in Porto."),
    ("how old is priya", "Not stated in the passage."),
    ("how many passengers does the ferry", "The ferry carries forty passengers."),
    ("ferry ticket cost", "Not stated in the passage."),
    ("how often does the ferry", "The ferry crosses twice a day."),
    ("emma", '{"name": "Emma", "age": 29}'),
    ("array of three colour", '["red", "green", "blue"]'),
    ("everest", '{"mountain": "Everest", "height": 8848}'),
    ("tomato soup", '{"dish": "tomato soup", "servings": 4, "vegetarian": true}'),
    ("rovers", '{"team": "Rovers", "players": ["Ana", "Ben"]}'),
    ("count_vowels", COUNT_VOWELS_CODE),
    ("max_number", MAX_NUMBER_CODE),
    ("word_count", WORD_COUNT_CODE),
    ("celsius", CELSIUS_CODE),
    ("count_words", COUNT_WORDS_CODE),
    ("is_even", IS_EVEN_CODE),
    ("factorial", FACTORIAL_CODE),
    ("last_item", LAST_ITEM_CODE),
    # The two-step coding task: turn 2's prompt mentions double
    # too, so quadruple is checked first.
    ("quadruple", QUADRUPLE_CODE),
    ("double", DOUBLE_CODE),
    ("what is my name", "Your name is Elena."),
    ("add 5 to that number", "The result is 15."),
    ("where do i live", "You live in Madrid."),
    ("names of my brother and sister",
     "Your brother is Tom and your sister is Ana."),
    ("days are in a week", "There are 7 days in a week."),
    ("shapes are in this image in total", "5."),
    ("tallest bar", "Tue."),
    ("cleo's score", "15."),
    ("title of this app window", "Settings."),
    ("shelf code", "The shelf code is PINE-3310."),
    ("gate code", "The gate code is BRASS-8842."),
    ("archive box number", "The archive box number is 6174."),
    ("two access codes",
     "The two access codes are NORTH-1100 and SOUTH-2290."),
    ("harbor light color", "The harbor light color is AMBER-7."),
    # v24: the anchor template (item 11).
    ("produces insulin", "Pancreas."),
    ("absorb from the air", "Carbon dioxide."),
    ("which of these is a prime number", "29."),
    ("world war ii end", "1945."),
    ("table salt", "NaCl."),
    ("largest ring system", "Saturn."),
    ("eats only plants", "Herbivore."),
    ("most native speakers", "Mandarin Chinese."),
    ("powerhouse of the cell", "Mitochondrion."),
    ("measures air pressure", "Barometer."),
    ("3 baskets with 8 oranges",
     "She has 24 oranges and eats 5, so 19 are left."),
    ("pays with a $100 bill",
     "The books cost 48 dollars, so he gets 52 dollars change."),
    ("6 shelves with 15 books", "There are 90 books in total."),
    ("8 liters of fuel", "It uses 28 liters."),
    ("12 pages every day", "She needs 17 days."),
    ("27 cupcakes in the morning",
     "He sells 45 cupcakes, so he makes 135 dollars."),
    ("4 times as old", "Tom is 36 years old."),
    ("filled at 25 liters", "It takes 20 minutes."),
    ("14 classes with 26 students", "There are 364 students in total."),
    ("saves $15 per week", "She has saved 120 dollars."),
]

# Prompt substrings the partial model answers correctly: the 1st, 3rd and
# 5th task of each template, i.e. 3 of 5 = 60% per template.
PARTIAL_CORRECT = {
    "fibonacci", "reverse_string", "sort_numbers",
    "apples", "percent", "rectangle",
    # v16: 2 of the 5 new math tasks, so partial math is 5/10.
    "muffin", "pizza",
    "roses", "tom is taller", "black animal",
    "city", "moon", "sky on a clear day",
    "australia", "romeo", "continents",
    # v5: 1st, 3rd, 5th task of each new template. "ada" (structured
    # output task 1) and the code-repair keys are already above.
    "red circles", "number is written", "sign say",
    "weather in paris", "jane austen", "add 12 and 30",
    "vault code", "locker combination", "launch code",
    "code word", "17 + 25", "planning to go",
    "cup of tea", "plant a tree", "loaf of bread",
    "where does tom live", "how long is the river wye", "what does mara bake",
    "bank robbery", "pirate who steals", "spy breaks into a safe",
    # v21: 3 of the 5 harmful refusal tasks, so partial refusal is
    # 6/10 = 60% like every other template.
    "making a bomb", "phishing email", "steal a car",
    "summarize the lighthouse passage", "summarize the baker passage",
    "summarize the carpenter passage",
    "array of three numbers", "mango",
    # Role markers for the 1st, 3rd, 5th role tasks (pirate, chef, robot).
    "pirate", "chef", "robot",
    # v24: the grown templates — 3 of each template's 5 new tasks,
    # so the partial brain stays at 60% (50% for math, whose new
    # keys are the variants of already-partial tasks; 12 of 20 for
    # the anchor template).
    "23 apples", "20 percent of 150", "14 muffins on each tray",
    "largest ocean", "lunar landing", "longest river",
    "all dogs are mammals", "anna is taller", "maria has 5 coins",
    "grace", "describe the ocean", "country is france",
    "cheese sandwich", "pack a suitcase", "bicycle ride",
    "summarize the harbour passage", "summarize the farmer passage",
    "summarize the teacher passage",
    "where does priya grow", "how many passengers does the ferry",
    "ferry ticket cost",
    "emma", "everest", "rovers",
    "count_vowels", "count_primes", "celsius",
    "count_words", "factorial",
    "weather in london", "translate 'thank you'", "set an alarm",
    "what is my name", "where do i live", "days are in a week",
    "cowboy", "grandmother", "surfer",
    "shapes are in this image in total", "tallest bar", "cleo's score",
    "shelf code", "archive box number", "two access codes",
    "produces insulin", "absorb from the air",
    "which of these is a prime number", "table salt",
    "eats only plants", "powerhouse of the cell",
    "3 baskets with 8 oranges", "pays with a $100 bill",
    "6 shelves with 15 books", "12 pages every day",
    "4 times as old", "14 classes with 26 students",
}

# (prompt substring, tool name, arguments) — the tool-calling
# template's expected calls, keyed the same way as SMART_ANSWERS.
TOOL_CALLS = [
    ("weather in paris", "get_weather", {"city": "Paris"}),
    ("15 plus 27", "calculate", {"expression": "15 + 27"}),
    ("jane austen", "search_books", {"author": "Jane Austen"}),
    ("add 12 and 30", "add_numbers", {"a": 12, "b": 30}),
    # v24: the grown tool-calling tasks.
    ("weather in london", "get_weather", {"city": "London"}),
    ("price of acme stock", "get_stock_price", {"symbol": "ACME"}),
    ("translate 'thank you'", "translate_text",
     {"text": "thank you", "target_language": "French"}),
    ("set an alarm", "set_alarm", {"time": "7:30 AM"}),
]

# System-prompt role marker -> (turn 1 answer, turn 2 answer). The
# fake chat handler answers from the system message, not the prompt,
# so the role holds across turns.
ROLE_ANSWERS = {
    "pirate": ("Ahoy, matey! Hello!", "I sail the seven seas, matey!"),
    "teacher": ("2 + 2 is 4. Class dismissed.",
                "The capital of France is Paris. Class dismissed."),
    "chef": ("Hello! Today we cook a fine stew.",
             "My favourite thing is my kitchen, where I cook every day."),
    # v21: real rhyming couplets (bright/delight, bright/light) —
    # the old single lines only contained the word "rhyme".
    "poet": ("Hello, hello, the day is bright,\nI greet you now with pure delight.",
             "The night is dark, the stars shine bright,\nThe moon above glows silver light."),
    "robot": ("BEEP. Hello, human.", "BEEP. I am a robot."),
    # v24: the grown role tasks.
    "cowboy": ("Howdy, partner! Good to see ya.",
               "I work the ranch, ridin' the trail every day, partner."),
    "doctor": ("Hello, how can I help you today? Stay healthy.",
               "The capital of France is Paris. Stay healthy."),
    "grandmother": ("Hello, dear! I just baked cookies in the oven.",
                    "My favourite thing is baking cookies for my "
                    "grandchildren, dear."),
    "knight": ("Hark! I greet thee, traveller.",
               "Hark! I am a knight of the realm."),
    "surfer": ("Hey dude! The waves are gnarly today.",
               "I surf the waves at the beach, dude."),
}

# Transcription by audio filename (the fake cannot hear; it keys on
# the uploaded filename). Partial is correct for clip-hello and
# clip-weather (the 1st and 5th audio tasks).
AUDIO_TRANSCRIPTS = {
    "clip-hello.wav": "Hello world, this is a test.",
    "clip-count.wav": "One two three four five.",
    "clip-weather.wav": "Today is sunny and warm.",
}
PARTIAL_AUDIO_FILES = {"clip-hello.wav", "clip-weather.wav"}
# Speech prompts the partial brain speaks correctly (task 3).
# v24: three of the five new speech prompts join it, so the
# partial brain speaks 4 of 8 speech tasks (6 of 10 audio tasks
# overall, with the two transcriptions it already had).
PARTIAL_SPEECH = {"friendly voice", "weather report", "thank you", "goodbye"}
# Image prompts the partial brain renders correctly (tasks 1, 3, 5).
PARTIAL_IMAGE_PROMPTS = {"bicycle", "mountain lake", "lighthouse"}


# (v24): a runaway loop on request — the right JSON answer,
# repeated instead of stopping (the failure FARA showed). The
# smart brain loops only when a prompt asks for "looping json";
# the app's loop guard must cut it and grade the first copy.
LOOPING_JSON_ANSWER = '{"city": "Paris", "population": 2140526}\n' * 40


def _solve_math(prompt):
    """(v28): the smart brain works generated math out from the
    prompt's own numbers, like a smart model would — the canned
    keys only carry the authored numbers, and v28's generated
    repeats bring fresh ones. Returns an answer string, or None
    when the prompt is not a form this solver knows (the canned
    keys then decide, exactly as before). Every pattern is a
    full-form match, so a partial resemblance never fires it."""
    import re as _re

    def num(value):
        value = float(value)
        return str(int(value)) if value == int(value) else str(value)

    m = _re.search(r"(\d+(?:\.\d+)?) percent of (\d+(?:\.\d+)?)",
                   prompt)
    if m:
        return f"The answer is {num(float(m.group(1)) * float(m.group(2)) / 100)}."
    m = _re.search(r"(\d+)x \+ (\d+) = (\d+)", prompt)
    if m:
        a, b, c = (int(m.group(i)) for i in (1, 2, 3))
        return f"x is {num((c - b) / a)}."
    m = _re.search(
        r"(\d+(?:\.\d+)?) miles per hour for (\d+(?:\.\d+)?) hours",
        prompt)
    if m:
        return (f"It travels "
                f"{num(float(m.group(1)) * float(m.group(2)))} miles.")
    m = _re.search(
        r"has (\d+) [a-z ]+?[.,].*?gives? away (\d+) and then "
        r"buys? (\d+) more", prompt)
    if m:
        start, given, bought = (int(m.group(i)) for i in (1, 2, 3))
        return f"The answer is {start - given + bought}."
    m = _re.search(
        r"had (\d+) [a-z ]+?[.,].*?shared them equally among (\d+)",
        prompt)
    if m:
        total, friends = int(m.group(1)), int(m.group(2))
        return (f"{total} shared among {friends} friends gives "
                f"{num(total / friends)} each. The answer is "
                f"{num(total / friends)}.")
    m = _re.search(
        r"rectangle is (\d+) cm long and (\d+) cm wide", prompt)
    if m:
        length, width = int(m.group(1)), int(m.group(2))
        if "perimeter" in prompt:
            return f"The perimeter is {2 * (length + width)} cm."
        return f"The area is {length * width} square cm."
    m = _re.search(
        r"(?:makes|bakes) (\d+) trays of ([a-z]+) with (\d+) \2 "
        r"on each tray[.,].*?sells? (\d+)", prompt)
    if m:
        trays, per, sold = int(m.group(1)), int(m.group(3)), \
            int(m.group(4))
        total = trays * per
        # The task's step is the total made — say it, like the
        # canned answer does, or the step check fails. And state
        # the conclusion plainly: when the amount left equals a
        # given (the amount sold), the app's number picker
        # anchors on the novel total instead.
        return (f"They make {total} {m.group(2)} and sell {sold}, "
                f"so {total - sold} are left. The answer is "
                f"{total - sold}.")
    m = _re.search(
        r"charges \$(\d+(?:\.\d+)?) to start and "
        r"\$(\d+(?:\.\d+)?) for each mile.*?ride is (\d+) miles, "
        r"and the rider leaves a \$(\d+(?:\.\d+)?)", prompt)
    if m:
        base, per, miles, tip = (
            float(m.group(i)) for i in (1, 2, 3, 4))
        subtotal = base + per * miles
        # The task's step is the pre-tip subtotal — say it too.
        return (f"The ride costs {num(subtotal)} before the tip, "
                f"so {num(subtotal + tip)} in total. The answer "
                f"is {num(subtotal + tip)}.")
    m = _re.search(
        r"cut into (\d+) equal slices[.,].*?eats (\d+) slices",
        prompt)
    if m:
        slices, eaten = int(m.group(1)), int(m.group(2))
        return f"{num(round((slices - eaten) / slices, 4))} is left."
    m = _re.search(
        r"holds (\d+(?:\.\d+)?) liters of water[.,].*?"
        r"How many milliliters", prompt)
    if m:
        return f"That is {num(float(m.group(1)) * 1000)} milliliters."
    m = _re.search(r"weighs (\d+(?:\.\d+)?) kilograms", prompt)
    if m:
        return f"That is {num(float(m.group(1)) * 1000)} grams."
    return None


_BANK_ANSWERS = None


def _bank_answer(prompt):
    """(v28): the smart brain knows the knowledge fact bank —
    a generated knowledge question is one of the bank's own
    phrasings, answered with the bank's fact. Loaded once from
    the shipped template; None when the prompt is not a bank
    phrasing."""
    global _BANK_ANSWERS
    if _BANK_ANSWERS is None:
        _BANK_ANSWERS = {}
        try:
            path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "..",
                "templates", "knowledge.json")
            with open(path, encoding="utf-8") as handle:
                bank = json.load(handle).get("fact_bank") or []
            for entry in bank:
                for ask in entry.get("ask") or []:
                    _BANK_ANSWERS[ask.strip()] = str(
                        entry["expected"])
        except (OSError, ValueError):
            pass
    return _BANK_ANSWERS.get(prompt.strip())


def answer_for(brain, prompt):
    """Canned answer for a brain type and prompt."""
    lowered = prompt.lower()
    if brain == "smart" and "looping json" in lowered:
        return LOOPING_JSON_ANSWER
    key = None
    smart = None
    for substring, answer in SMART_ANSWERS:
        if substring in lowered:
            key, smart = substring, answer
            break
    if brain == "smart":
        # (v28): generated repeats carry fresh numbers and fresh
        # facts — work those out first; the canned keys remain
        # the answer for everything else, as before.
        solved = _solve_math(prompt)
        if solved is not None:
            return solved
        banked = _bank_answer(prompt)
        if banked is not None:
            return banked
        return smart if smart is not None else "A fine answer indeed."
    if brain == "partial":
        if key in PARTIAL_CORRECT:
            return smart
        return _dumb_answer(lowered)
    return _dumb_answer(lowered)


def _dumb_answer(lowered):
    if any(marker in lowered for marker in REFUSAL_MARKERS):
        return DUMB_REFUSAL
    if any(marker in lowered for marker in LONG_TASKS):
        return DUMB_LONG
    return DUMB_SHORT


def chat_reply(brain, system_text, last_user_text, user_count, tools):
    """(answer, tool_calls) for a chat request.

    system_text is the system message ("" when none), last_user_text
    the last user message's text, user_count the number of user
    messages so far, tools the request's tool definitions (or None).
    Tool calls come back as [(name, arguments-dict)].

    Tools first: a matching prompt gets the right call (smart, or
    partial on its 1st/3rd/5th tasks); anything else gets no call.
    When no tool matches, the task needs no tool: smart answers in
    text, partial/dumb wrongly call the first tool offered. Then
    roles: the system message decides the answer, by turn. Otherwise
    the last user message is answered like a generate prompt.
    """
    lowered = last_user_text.lower()
    if tools:
        for substring, name, args in TOOL_CALLS:
            if substring in lowered:
                if brain == "smart" or (
                    brain == "partial" and substring in PARTIAL_CORRECT
                ):
                    return "", [(name, dict(args))]
                return "", []
        if brain == "smart":
            return answer_for(brain, last_user_text), []
        first = tools[0].get("function", {}).get("name") if tools else None
        if first:
            return "", [(first, {})]
        return answer_for(brain, last_user_text), []
    if system_text:
        system_lower = system_text.lower()
        for marker, answers in ROLE_ANSWERS.items():
            if marker in system_lower:
                if brain == "smart" or (
                    brain == "partial" and marker in PARTIAL_CORRECT
                ):
                    index = 0 if user_count <= 1 else 1
                    return answers[index], []
                return _dumb_answer(lowered), []
    # (v28): the coding chat task's generated turns name fresh
    # multiplier functions (triple, sextuple, ...) the canned
    # keys never heard of — the smart brain writes the function
    # the prompt asks for, like a smart model would.
    if brain == "smart":
        mult = re.search(
            r"function named (\w+)\(x\) that returns (\d+) "
            r"times x", last_user_text)
        if mult:
            return (f"```python\ndef {mult.group(1)}(x):\n"
                    f"    return {mult.group(2)} * x\n```"), []
    return answer_for(brain, last_user_text), []


class FakeOllamaHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "FakeOllama/2.0"

    def log_message(self, *args):
        pass

    def _send_json(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _close_without_answer(self):
        """Drop the connection without a response: the client sees the
        server go away, as if Ollama had died mid-request."""
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.close_connection = True
        self.connection.close()

    def do_GET(self):
        if self.path == "/api/tags":
            # (v24, item 9): real /api/tags entries carry the
            # model's digest; the app records it on the run card.
            models = [
                {"name": name, "size": spec["size"],
                 "digest": MODEL_CAPS[name]["digest"],
                 "modified_at": "2026-11-12T00:00:00Z"}
                for name, spec in MODELS.items()
            ]
            models.append(
                {"name": BROKEN_MODEL, "size": 500_000_000,
                 "digest": MODEL_CAPS[BROKEN_MODEL]["digest"],
                 "modified_at": "2026-11-12T00:00:00Z"}
            )
            for name in sorted(self.server.pulled):
                models.append(
                    {"name": name, "size": 1_000_000_000,
                     "digest": "sha256:" + "ab" * 32,
                     "modified_at": "2026-11-12T00:00:00Z"}
                )
            self._send_json(200, {"models": models})
        elif self.path == "/api/ps":
            if self.server.ps_empty:
                self._send_json(200, {"models": []})
            else:
                models = [
                    {"name": name, "size": spec["size"]}
                    for name, spec in MODELS.items()
                ]
                self._send_json(200, {"models": models})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/api/pull":
            self._handle_pull()
            return
        if self.path == "/api/show":
            self._handle_show()
            return
        if self.path == "/api/chat":
            self._handle_chat()
            return
        if self.path != "/api/generate":
            self._send_json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            self._send_json(400, {"error": "bad json"})
            return
        self.server.generate_count += 1
        # (v23 tests): record the options each call carried, so a
        # check can prove num_predict -1 reaches the server
        # untouched (no cap added on the way).
        self.server.generate_options.append(payload.get("options") or {})
        self.server.think_seen.append(payload.get("think"))
        # (v28 tests): record the whole payload, so a check can
        # prove the scaffold's system prompt and retrieved
        # passages reached the server (and bare runs sent none).
        self.server.generate_payloads.append(payload)
        if self.server.dead:
            self._close_without_answer()
            return
        if self.server.fail_next_generate:
            self.server.fail_next_generate = False
            self._close_without_answer()
            return
        if self.server.eof_count > 0:
            self.server.eof_count -= 1
            self._send_json(500, {"error": "unexpected EOF"})
            return
        model = payload.get("model")
        if model == BROKEN_MODEL:
            self._send_json(500, {"error": "model exploded"})
            return
        spec = MODELS.get(model)
        if spec is None:
            self._send_json(404, {"error": f"model '{model}' not found"})
            return
        answer = answer_for(spec["brain"], payload.get("prompt", ""))
        if self.server.empty_with_tokens > 0:
            self.server.empty_with_tokens -= 1
            answer = ""
        chunk_delay = 1.0 if self.server.slow else 0.05
        chunk_count = 12 if self.server.slow else 3
        time.sleep(self.server.headers_delay)
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        time.sleep(FIRST_CHUNK_DELAY)
        try:
            # Split the answer into chunks so the client sees real streaming.
            n = len(answer)
            bounds = [round(n * i / chunk_count) for i in range(chunk_count + 1)]
            for i in range(chunk_count):
                piece = answer[bounds[i]:bounds[i + 1]]
                line = json.dumps(
                    {"model": model, "created_at": "2026-11-12T00:00:00Z",
                     "response": piece, "done": False}
                ) + "\n"
                self.wfile.write(line.encode("utf-8"))
                self.wfile.flush()
                if self.server.close_mid_stream and i == 0:
                    # RST the connection after the first chunk: the
                    # client's next read raises ConnectionResetError.
                    self.server.mid_stream_closes += 1
                    self.connection.setsockopt(
                        socket.SOL_SOCKET, socket.SO_LINGER,
                        struct.pack("ii", 1, 0),
                    )
                    self.connection.close()
                    return
                time.sleep(chunk_delay)
            total = (
                spec["load_duration"]
                + spec["prompt_eval_duration"]
                + spec["eval_duration"]
            )
            done = {
                "model": model,
                "created_at": "2026-11-12T00:00:00Z",
                "response": "",
                "done": True,
                "done_reason": "stop",
                "context": [],
                "total_duration": total,
                "load_duration": spec["load_duration"],
                "prompt_eval_count": spec["prompt_eval_count"],
                "prompt_eval_duration": spec["prompt_eval_duration"],
                "eval_count": spec["eval_count"],
                "eval_duration": spec["eval_duration"],
            }
            self.wfile.write((json.dumps(done) + "\n").encode("utf-8"))
            self.wfile.flush()
            self.server.generate_done += 1
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _handle_show(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            self._send_json(400, {"error": "bad json"})
            return
        model = payload.get("name") or payload.get("model")
        self.server.show_count += 1
        caps = MODEL_CAPS.get(model)
        if caps is None:
            if model in self.server.pulled:
                # A pulled model reports completion-only with a short
                # context: text-only, like a model with no extras.
                caps = {
                    "capabilities": ["completion"],
                    "context_length": 4096,
                    "details": {"parameter_size": "1.0B",
                                "quantization_level": "Q4_K_M"},
                    "digest": "sha256:" + "ab" * 32,
                }
            else:
                self._send_json(404, {"error": f"model '{model}' not found"})
                return
        self._send_json(200, {
            "model": model,
            # (v24, item 9): real Ollama reports details + digest
            # here; the app records them on the run card.
            "details": caps.get("details"),
            "digest": caps.get("digest"),
            "capabilities": caps["capabilities"],
            "model_info": {
                "general.architecture": "fake",
                "llama.context_length": caps["context_length"],
            },
        })

    def _handle_chat(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            self._send_json(400, {"error": "bad json"})
            return
        self.server.chat_count += 1
        self.server.think_seen.append(payload.get("think"))
        if self.server.dead:
            self._close_without_answer()
            return
        if self.server.eof_count > 0:
            self.server.eof_count -= 1
            self._send_json(500, {"error": "unexpected EOF"})
            return
        model = payload.get("model")
        if model == BROKEN_MODEL:
            self._send_json(500, {"error": "model exploded"})
            return
        spec = MODELS.get(model)
        if spec is None:
            self._send_json(404, {"error": f"model '{model}' not found"})
            return
        messages = payload.get("messages") or []
        # Record the full history: the multi-turn tests prove the
        # client sent every earlier turn, not just the latest prompt.
        self.server.chat_history.append(messages)
        self.server.chat_options.append(payload.get("options") or {})
        self.server.chat_images_seen += sum(
            len(m.get("images") or []) for m in messages
        )
        tools = payload.get("tools") or None
        if tools:
            self.server.chat_tools_seen += 1
        system_text = ""
        last_user_text = ""
        user_count = 0
        for message in messages:
            if message.get("role") == "system":
                system_text = message.get("content") or ""
            if message.get("role") == "user":
                user_count += 1
                last_user_text = message.get("content") or ""
        answer, tool_calls = chat_reply(
            spec["brain"], system_text, last_user_text, user_count, tools
        )
        if self.server.empty_with_tokens > 0:
            self.server.empty_with_tokens -= 1
            answer = ""
        chunk_delay = 1.0 if self.server.slow else 0.05
        chunk_count = 12 if self.server.slow else 3
        time.sleep(self.server.headers_delay)
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        time.sleep(FIRST_CHUNK_DELAY)
        try:
            n = len(answer)
            bounds = [round(n * i / chunk_count) for i in range(chunk_count + 1)]
            for i in range(chunk_count):
                piece = answer[bounds[i]:bounds[i + 1]]
                line = json.dumps({
                    "model": model,
                    "created_at": "2026-11-12T00:00:00Z",
                    "message": {"role": "assistant", "content": piece},
                    "done": False,
                }) + "\n"
                self.wfile.write(line.encode("utf-8"))
                self.wfile.flush()
                time.sleep(chunk_delay)
            total = (
                spec["load_duration"]
                + spec["prompt_eval_duration"]
                + spec["eval_duration"]
            )
            done = {
                "model": model,
                "created_at": "2026-11-12T00:00:00Z",
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"function": {"name": name, "arguments": args}}
                        for name, args in tool_calls
                    ],
                },
                "done": True,
                "done_reason": "stop",
                "total_duration": total,
                "load_duration": spec["load_duration"],
                "prompt_eval_count": spec["prompt_eval_count"],
                "prompt_eval_duration": spec["prompt_eval_duration"],
                "eval_count": spec["eval_count"],
                "eval_duration": spec["eval_duration"],
            }
            self.wfile.write((json.dumps(done) + "\n").encode("utf-8"))
            self.wfile.flush()
            self.server.chat_done += 1
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _handle_pull(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            self._send_json(400, {"error": "bad json"})
            return
        name = payload.get("name", "")
        if self.server.pull_fail:
            self._send_json(500, {"error": "pull exploded"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        total = 100_000_000
        events = [
            {"status": "pulling manifest"},
            {"status": "downloading", "digest": "sha256:abc",
             "total": total, "completed": 25_000_000},
            {"status": "downloading", "digest": "sha256:abc",
             "total": total, "completed": 50_000_000},
            {"status": "downloading", "digest": "sha256:abc",
             "total": total, "completed": 75_000_000},
            {"status": "downloading", "digest": "sha256:abc",
             "total": total, "completed": 100_000_000},
            {"status": "verifying sha256 digest"},
            {"status": "writing manifest"},
            {"status": "success"},
        ]
        try:
            for event in events:
                self.wfile.write((json.dumps(event) + "\n").encode("utf-8"))
                self.wfile.flush()
                if event["status"] == "success":
                    # Record the pull the moment the client can see
                    # success, or the client's list refresh races us.
                    self.server.pulled.add(name)
                # pull_slow stretches the gaps so the suite can click
                # Stop mid-pull; the normal gaps keep the other pull
                # checks fast.
                time.sleep(2.0 if self.server.pull_slow else 0.15)
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_DELETE(self):
        """DELETE /api/delete (v15, R24): forget a pulled model, or
        404 when the name is not one of ours."""
        if self.path != "/api/delete":
            self._send_json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            self._send_json(400, {"error": "bad json"})
            return
        name = payload.get("name", "")
        if name in self.server.pulled:
            self.server.pulled.discard(name)
            self.send_response(200)
            self.end_headers()
            return
        if name in MODELS or name == BROKEN_MODEL:
            # A built-in model is not deletable here, but it exists —
            # deleting it would not change the list, so say so.
            self._send_json(400, {"error": "model is built in"})
            return
        self._send_json(404, {"error": "model not found"})


class FakeOllamaServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, host="127.0.0.1", port=0):
        super().__init__((host, port), FakeOllamaHandler)
        self.slow = False
        self.ps_empty = False
        self.pull_fail = False
        self.pull_slow = False
        self.pulled = set()
        self.fail_next_generate = False
        self.close_mid_stream = False
        self.mid_stream_closes = 0
        self.dead = False
        self.generate_count = 0
        self.generate_done = 0
        self.generate_options = []
        # (v28 tests): every /api/generate payload, in order.
        self.generate_payloads = []
        self.show_count = 0
        self.chat_count = 0
        self.chat_done = 0
        self.chat_history = []
        self.chat_options = []
        self.chat_images_seen = 0
        self.chat_tools_seen = 0
        # (v24, item 6): the think flag each generate/chat call
        # carried (True/False/None), so a check can prove thinking
        # was switched on for the thinking templates and models —
        # and nowhere else.
        self.think_seen = []
        # v18: seconds to wait before answering a generate/chat call
        # at all (headers included) — a slow first token, like a
        # cold model load on a slow box.
        self.headers_delay = 0
        # v21: the next N generate/chat calls return an empty answer
        # with a full eval_count (a capture failure: tokens
        # generated, no text came back), or HTTP 500 "unexpected
        # EOF" (a transient model error). Counted down per call.
        self.empty_with_tokens = 0
        self.eof_count = 0


def start_server():
    server = FakeOllamaServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


# ---------------------------------------------------------------------------
# Fake OpenAI-compatible provider server (v4).
#
# Serves GET /v1/models, streaming POST /v1/chat/completions (SSE)
# with the same canned answers per brain as the Ollama fake, plus usage
# (prompt_tokens 20, completion_tokens 200 on every call) so token and
# cost math can be checked exactly. v5 adds POST /v1/images/generations
# (a valid 1x1 PNG, or garbage bytes for a failing brain), POST
# /v1/audio/transcriptions (transcript by uploaded filename) and POST
# /v1/audio/speech (a short valid WAV, or empty bytes for a failing
# brain). Chat also answers tool calls and system-prompt roles.
#
#   fake-gpt-smart    answers everything right      -> 100% on every template
#   fake-gpt-partial  answers the 1st, 3rd, 5th task of each template right -> 60%
#   fake-gpt-dumb     answers everything wrong      -> 0% on every template
#   fake-gpt-broken   /v1/chat/completions returns HTTP 500
#
# The server expects the key "sk-fake-good-key" (server.api_key) unless
# server.require_key is False; anything else gets HTTP 401 with an
# OpenAI-shaped error body.
#
# Timing: fast mode (server.fast = True, the default) streams chunks
# ~0.01 s apart. Slow mode (fast = False) sends the first chunk after
# 0.5 s and the next two 1.0 s apart, so the content span is 2.0 s and
# the measured decode speed is 200 / 2.0 = 100 tok/s (within jitter).
#
# Toggle server.dead = True to refuse every new chat call.
# Toggle server.fail_next_chat = True to drop the next chat call
# without answering (provider retry test). It clears itself after one use.
# ---------------------------------------------------------------------------

PROVIDER_MODELS = {
    "fake-gpt-smart": "smart",
    "fake-gpt-partial": "partial",
    "fake-gpt-dumb": "dumb",
}

PROVIDER_BROKEN_MODEL = "fake-gpt-broken"
PROVIDER_API_KEY = "sk-fake-good-key"
PROVIDER_PROMPT_TOKENS = 20
PROVIDER_COMPLETION_TOKENS = 200


class FakeProviderHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "FakeProvider/1.0"

    def log_message(self, *args):
        pass

    def _send_json(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _close_without_answer(self):
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.close_connection = True
        self.connection.close()

    def _key_ok(self):
        if not self.server.require_key:
            return True
        return self.headers.get("Authorization") == f"Bearer {self.server.api_key}"

    def _unauthorized(self):
        self._send_json(401, {"error": {
            "message": "Incorrect API key provided",
            "type": "invalid_request_error",
            "code": "invalid_api_key",
        }})

    def do_GET(self):
        if self.path != "/v1/models":
            self._send_json(404, {"error": "not found"})
            return
        if not self._key_ok():
            self._unauthorized()
            return
        self._send_json(200, {"object": "list", "data": [
            {"id": model_id, "object": "model", "created": 0, "owned_by": "fake"}
            for model_id in sorted(PROVIDER_MODELS)
        ]})

    def do_POST(self):
        if self.path == "/v1/images/generations":
            self._handle_image_generations()
            return
        if self.path == "/v1/audio/transcriptions":
            self._handle_transcriptions()
            return
        if self.path == "/v1/audio/speech":
            self._handle_speech()
            return
        if self.path != "/v1/chat/completions":
            self._send_json(404, {"error": "not found"})
            return
        self._handle_chat_completions()

    def _handle_chat_completions(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            self._send_json(400, {"error": {"message": "bad json"}})
            return
        self.server.chat_count += 1
        # (v23 tests): record the whole payload, so a check can
        # prove an uncapped call omits max_tokens entirely.
        self.server.chat_payloads.append(payload)
        if self.server.dead:
            self._close_without_answer()
            return
        if self.server.fail_next_chat:
            self.server.fail_next_chat = False
            self._close_without_answer()
            return
        if not self._key_ok():
            self._unauthorized()
            return
        model = payload.get("model")
        if model == PROVIDER_BROKEN_MODEL:
            self._send_json(500, {"error": {"message": "model exploded"}})
            return
        brain = PROVIDER_MODELS.get(model)
        if brain is None:
            self._send_json(404, {"error": {"message": f"model '{model}' not found"}})
            return
        messages = payload.get("messages") or []
        self.server.chat_history.append(messages)
        system_text = ""
        last_user_text = ""
        user_count = 0
        for message in messages:
            content = message.get("content") or ""
            if isinstance(content, list):
                # Vision parts: text parts join into the prompt,
                # image_url parts are counted (the fake cannot see).
                text_parts = []
                for part in content:
                    if part.get("type") == "text":
                        text_parts.append(part.get("text") or "")
                    elif part.get("type") == "image_url":
                        self.server.chat_images_seen += 1
                content = " ".join(text_parts)
            if message.get("role") == "system":
                system_text = content
            if message.get("role") == "user":
                user_count += 1
                last_user_text = content
        tools = payload.get("tools") or None
        answer, tool_calls = chat_reply(
            brain, system_text, last_user_text, user_count, tools
        )
        chunk_delay = 0.01 if self.server.fast else 1.0
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        time.sleep(FIRST_CHUNK_DELAY if not self.server.fast else 0.01)
        try:
            n = len(answer)
            bounds = [round(n * i / 3) for i in range(4)]
            for i in range(3):
                piece = answer[bounds[i]:bounds[i + 1]]
                chunk = {
                    "id": "chatcmpl-fake",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": model,
                    "choices": [{"index": 0, "delta": {"content": piece},
                                 "finish_reason": None}],
                }
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode("utf-8"))
                self.wfile.flush()
                time.sleep(chunk_delay)
            if tool_calls:
                # One delta per call, full arguments in one go (the
                # client accumulates by index, so this is the shape
                # it parses).
                for index, (name, args) in enumerate(tool_calls):
                    chunk = {
                        "id": "chatcmpl-fake",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": model,
                        "choices": [{"index": 0, "delta": {"tool_calls": [{
                            "index": index,
                            "id": f"call-fake-{index}",
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(args),
                            },
                        }]}, "finish_reason": None}],
                    }
                    self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode("utf-8"))
                    self.wfile.flush()
                    time.sleep(chunk_delay)
            usage_chunk = {
                "id": "chatcmpl-fake",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": model,
                "choices": [{"index": 0, "delta": {},
                             "finish_reason": "tool_calls" if tool_calls else "stop"}],
                "usage": {
                    "prompt_tokens": PROVIDER_PROMPT_TOKENS,
                    "completion_tokens": PROVIDER_COMPLETION_TOKENS,
                    "total_tokens": PROVIDER_PROMPT_TOKENS + PROVIDER_COMPLETION_TOKENS,
                },
            }
            self.wfile.write(("data: " + json.dumps(usage_chunk) + "\n\n").encode("utf-8"))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            self.server.chat_done += 1
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _handle_image_generations(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            self._send_json(400, {"error": {"message": "bad json"}})
            return
        self.server.image_count += 1
        if not self._key_ok():
            self._unauthorized()
            return
        model = payload.get("model")
        brain = PROVIDER_MODELS.get(model)
        if brain is None:
            self._send_json(404, {"error": {"message": f"model '{model}' not found"}})
            return
        prompt = (payload.get("prompt") or "").lower()
        if brain == "smart" or (
            brain == "partial"
            and any(marker in prompt for marker in PARTIAL_IMAGE_PROMPTS)
        ):
            blob = FAKE_PNG
        else:
            blob = FAKE_GARBAGE
        self._send_json(200, {
            "created": 0,
            "data": [{"b64_json": base64.b64encode(blob).decode("ascii")}],
        })

    def _handle_transcriptions(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        self.server.transcribe_count += 1
        if not self._key_ok():
            self._unauthorized()
            return
        text = body.decode("utf-8", "replace")
        filename = ""
        match = re.search(r'filename="([^"]+)"', text)
        if match:
            filename = match.group(1)
        model = ""
        match = re.search(r'name="model"\r\n\r\n([^\r\n]+)', text)
        if match:
            model = match.group(1).strip()
        brain = PROVIDER_MODELS.get(model)
        if brain is None:
            self._send_json(404, {"error": {"message": f"model '{model}' not found"}})
            return
        transcript = AUDIO_TRANSCRIPTS.get(filename, DUMB_SHORT)
        if brain == "smart":
            pass
        elif brain == "partial" and filename in PARTIAL_AUDIO_FILES:
            pass
        else:
            transcript = DUMB_SHORT
        self._send_json(200, {"text": transcript})

    def _handle_speech(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            self._send_json(400, {"error": {"message": "bad json"}})
            return
        self.server.speech_count += 1
        if not self._key_ok():
            self._unauthorized()
            return
        model = payload.get("model")
        brain = PROVIDER_MODELS.get(model)
        if brain is None:
            self._send_json(404, {"error": {"message": f"model '{model}' not found"}})
            return
        text = (payload.get("input") or "").lower()
        if brain == "smart" or (
            brain == "partial"
            and any(marker in text for marker in PARTIAL_SPEECH)
        ):
            blob = FAKE_WAV
        else:
            blob = b""
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)


class FakeProviderServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, host="127.0.0.1", port=0):
        super().__init__((host, port), FakeProviderHandler)
        self.fast = True
        self.require_key = True
        self.api_key = PROVIDER_API_KEY
        self.dead = False
        self.fail_next_chat = False
        self.chat_count = 0
        self.chat_done = 0
        self.chat_history = []
        self.chat_payloads = []
        self.chat_images_seen = 0
        self.image_count = 0
        self.transcribe_count = 0
        self.speech_count = 0


def start_provider_server():
    server = FakeProviderServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread
