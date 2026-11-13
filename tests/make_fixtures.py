#!/usr/bin/env python3
"""Build the v5 bundled fixtures with the standard library only.

Vision images (images/):
  shapes.png — 3 red circles + 2 blue squares on white (count/describe).
  sign.png   — a dark sign with the number 42 in white (OCR).
Audio clips (audio/):
  clip-hello.wav, clip-count.wav, clip-weather.wav — 1-second sine
  tones at different pitches. They are PLUMBING fixtures: they prove
  the transcription path sends a file and scores the transcript, but
  they carry no speech. The package README says to swap in real speech
  clips for a real quality test.

No PIL, no TTS in the sandbox: the PNGs are encoded by hand (zlib +
struct, RGB, one filter byte per row) and the digits are drawn from
a tiny 5x7 bitmap font.
"""

import math
import os
import struct
import wave
import zlib

PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMAGES_DIR = os.path.join(PKG, "images")
AUDIO_DIR = os.path.join(PKG, "audio")

WHITE = (255, 255, 255)
BLACK = (20, 20, 20)
RED = (220, 30, 30)
BLUE = (30, 80, 220)
DARK_SIGN = (40, 40, 60)

# 5x7 bitmap glyphs, one string per row, "#" = on.
GLYPHS = {
    "4": [
        "...#.",
        "..##.",
        ".#.#.",
        "#..#.",
        "#####",
        "...#.",
        "...#.",
    ],
    "2": [
        ".###.",
        "#...#",
        "....#",
        "...#.",
        "..#..",
        ".#...",
        "#####",
    ],
}


def write_png(path, width, height, pixels):
    """pixels: list of rows, each a list of (r, g, b) tuples."""
    raw = b""
    for row in pixels:
        raw += b"\x00"  # filter: none
        for r, g, b in row:
            raw += bytes((r, g, b))

    def chunk(kind, data):
        out = struct.pack(">I", len(data)) + kind + data
        out += struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        return out

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, level=9))
    png += chunk(b"IEND", b"")
    with open(path, "wb") as handle:
        handle.write(png)
    print(f"wrote {path} ({len(png)} bytes)")


def blank(width, height, color):
    return [[color for _ in range(width)] for _ in range(height)]


def draw_circle(pixels, cx, cy, radius, color):
    height = len(pixels)
    width = len(pixels[0])
    for y in range(max(0, cy - radius), min(height, cy + radius + 1)):
        for x in range(max(0, cx - radius), min(width, cx + radius + 1)):
            if (x - cx) ** 2 + (y - cy) ** 2 <= radius * radius:
                pixels[y][x] = color


def draw_rect(pixels, x0, y0, x1, y1, color):
    for y in range(y0, y1):
        for x in range(x0, x1):
            pixels[y][x] = color


def draw_glyph(pixels, x0, y0, char, scale, color):
    glyph = GLYPHS[char]
    for row, line in enumerate(glyph):
        for col, mark in enumerate(line):
            if mark == "#":
                draw_rect(
                    pixels,
                    x0 + col * scale, y0 + row * scale,
                    x0 + (col + 1) * scale, y0 + (row + 1) * scale,
                    color,
                )


def shapes_png():
    pixels = blank(400, 300, WHITE)
    # 3 red circles across the top half.
    for cx in (70, 200, 330):
        draw_circle(pixels, cx, 80, 40, RED)
    # 2 blue squares across the bottom half.
    draw_rect(pixels, 90, 190, 170, 270, BLUE)
    draw_rect(pixels, 230, 190, 310, 270, BLUE)
    write_png(os.path.join(IMAGES_DIR, "shapes.png"), 400, 300, pixels)


def sign_png():
    pixels = blank(400, 200, WHITE)
    # A dark sign board with a white border.
    draw_rect(pixels, 40, 40, 360, 160, DARK_SIGN)
    draw_rect(pixels, 48, 48, 352, 152, BLACK)
    # "42" in white, two glyphs at 10x scale (50 wide, 70 tall each).
    draw_glyph(pixels, 130, 65, "4", 10, WHITE)
    draw_glyph(pixels, 200, 65, "2", 10, WHITE)
    write_png(os.path.join(IMAGES_DIR, "sign.png"), 400, 200, pixels)


def write_wav(path, frequency, seconds=1.0, rate=8000):
    frames = b""
    count = int(rate * seconds)
    for i in range(count):
        value = int(12000 * math.sin(2 * math.pi * frequency * i / rate))
        frames += struct.pack("<h", value)
    with wave.open(path, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(frames)
    print(f"wrote {path} ({frequency} Hz)")


if __name__ == "__main__":
    os.makedirs(IMAGES_DIR, exist_ok=True)
    os.makedirs(AUDIO_DIR, exist_ok=True)
    shapes_png()
    sign_png()
    write_wav(os.path.join(AUDIO_DIR, "clip-hello.wav"), 440)
    write_wav(os.path.join(AUDIO_DIR, "clip-count.wav"), 660)
    write_wav(os.path.join(AUDIO_DIR, "clip-weather.wav"), 880)
    print("done")
