"""Geometry round-trip probe (v7 T7), run under Xvfb by run_tests.py.

The offscreen platform reports an 800x800 screen, smaller than this
window's minimum width, so Qt clamps any restored geometry there and
the round-trip cannot be checked in-process. Under Xvfb with a real
screen size it restores exactly. Prints SAVED/RESTORED frame rects
and exits 0 when they match (within 2 px).
"""

import os
import sys

VERIFY_DIR = os.path.dirname(os.path.abspath(__file__))
PKG_DIR = os.path.dirname(VERIFY_DIR)
sys.path.insert(0, PKG_DIR)

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

import ollama_system_test as app


def main():
    QSettings("BrokeNBored", app.APP_NAME).clear()
    qt_app = QApplication(sys.argv)
    window = app.MainWindow()
    window.show()
    qt_app.processEvents()
    window.resize(1200, 700)
    window.move(100, 80)
    qt_app.processEvents()
    saved = window.frameGeometry().getRect()
    window.close()
    qt_app.processEvents()
    window2 = app.MainWindow()
    window2.show()
    qt_app.processEvents()
    restored = window2.frameGeometry().getRect()
    print(f"SAVED {saved}")
    print(f"RESTORED {restored}")
    window2.close()
    qt_app.processEvents()
    ok = all(abs(a - b) <= 2 for a, b in zip(saved, restored))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
