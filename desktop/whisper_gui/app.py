"""
Точка входа в GUI.
Запуск:  python app.py
(из активированного venv, где установлены whisper и PyQt5)
"""

import os
import sys
import traceback
from datetime import datetime

from PyQt5.QtWidgets import QApplication

from desktop.whisper_gui.main_window import MainWindow
from asr.font_manager import register_bundled_fonts
from config.paths import crash_log_path  # раньше — хардкод ~/OasisSubtitleStudio_output


def _handle_exception(exc_type, exc_value, exc_tb):
    """Перехватывает любую необработанную ошибку и пишет её в файл,
    вместо того чтобы приложение просто молча закрылось (актуально
    для --windowed сборки на Windows, где консоли не видно вообще)."""
    text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    try:
        with open(crash_log_path(), "a", encoding="utf-8") as f:
            f.write(f"\n--- {datetime.now().isoformat()} ---\n{text}\n")
    except Exception:
        pass
    sys.__excepthook__(exc_type, exc_value, exc_tb)


def main():
    sys.excepthook = _handle_exception
    app = QApplication(sys.argv)
    app.setApplicationName("OASIS Subtitle Studio")
    register_bundled_fonts()
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
