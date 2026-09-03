"""
Точка входа в GUI video-pipeline.
Запуск:  python app.py
(из активированного venv video-pipeline)
"""

import os
import sys
import traceback
from datetime import datetime

from dotenv import load_dotenv
from PyQt5.QtWidgets import QApplication

load_dotenv()  # ключи DEEPSEEK_API_KEY / PEXELS_API_KEY / PIXABAY_API_KEY из .env

from services import service_manager
from desktop.video_pipeline_gui.main_window import MainWindow
from config.paths import crash_log_path  # см. config/paths.py — раньше был хардкод ~/VideoCombine_output


def _handle_exception(exc_type, exc_value, exc_tb):
    """Перехватывает необработанные ошибки и пишет их в файл, а не просто
    роняет окно молча — тот же приём, что в openai-whisper/app.py."""
    text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    try:
        with open(crash_log_path(), "a", encoding="utf-8") as f:
            f.write(f"\n--- {datetime.now().isoformat()} ---\n{text}\n")
    except Exception:
        pass
    service_manager.stop()  # чтобы падение GUI не оставляло subtitle_service висеть на 8800
    sys.__excepthook__(exc_type, exc_value, exc_tb)


def main():
    sys.excepthook = _handle_exception
    app = QApplication(sys.argv)
    app.setApplicationName("OASIS Video Combine")
    # subtitle_service — не самостоятельный сервис, а вспомогательный процесс
    # ЭТОГО приложения: без этого хука он переживал закрытие GUI, следующий
    # запуск видел его живым на /health и не подхватывал свежий код —
    # приходилось искать и убивать PID вручную (см. сегодняшнюю переписку).
    app.aboutToQuit.connect(service_manager.stop)
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
