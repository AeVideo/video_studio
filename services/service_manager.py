"""
service_manager.py

Гарантирует, что subtitle_api (FastAPI-обёртка вокруг ASR-движка,
127.0.0.1:8800) запущен — если нет, поднимает его сам через subprocess.

ДО объединения репозиториев (см. REFACTOR_PLAN.md / COLAB_MIGRATION_PLAN.md)
subtitle_api.py жил в отдельном venv (openai-whisper) и запускался по
захардкоженному пути на диск (SUBTITLE_SERVICE_DIR=/home/oasis/openai-whisper,
.../venv/bin/uvicorn). Теперь оба проекта — один установленный пакет
video_studio, поэтому "режим B" (отдельный процесс — нужен, например, для
Flask-продакшена, где ASR может жить на отдельной GPU-машине) запускается
через ТЕКУЩИЙ Python-интерпретатор (sys.executable), а не через путь к
чужому venv: `python -m uvicorn services.subtitle_api:app`.

Для локальной разработки в одном процессе (режим A, см. web/gradio_app.py)
этот модуль вообще не нужен — там asr/transcribe.py импортируется напрямую.

Использование:
    from services.service_manager import ensure_running

    if not ensure_running():
        raise RuntimeError("subtitle_api не поднялся")
    # дальше можно смело requests.post("http://127.0.0.1:8800/...")
"""

import os
import signal
import subprocess
import sys
import time
import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from config.paths import subtitle_service_log_path, subtitle_service_pid_path

SUBTITLE_SERVICE_URL = "http://127.0.0.1:8800"
SUBTITLE_SERVICE_LOG = subtitle_service_log_path()
SUBTITLE_SERVICE_PID_FILE = subtitle_service_pid_path()

_process = None  # держим ссылку, чтобы потом можно было остановить


def _write_pid_file(pid: int):
    os.makedirs(os.path.dirname(SUBTITLE_SERVICE_PID_FILE), exist_ok=True)
    with open(SUBTITLE_SERVICE_PID_FILE, "w") as f:
        f.write(str(pid))


def _read_pid_file():
    try:
        with open(SUBTITLE_SERVICE_PID_FILE) as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return None


def is_running() -> bool:
    try:
        r = requests.get(f"{SUBTITLE_SERVICE_URL}/health", timeout=1)
        return r.status_code == 200
    except requests.RequestException:
        return False


def ensure_running(timeout: float = 30.0) -> bool:
    """Возвращает True, если сервис уже был доступен или успешно поднялся за timeout секунд."""
    global _process

    if is_running():
        return True

    print("subtitle_service не отвечает — запускаю...")
    os.makedirs(os.path.dirname(SUBTITLE_SERVICE_LOG), exist_ok=True)
    log_file = open(SUBTITLE_SERVICE_LOG, "a", encoding="utf-8")
    log_file.write(f"\n--- запуск {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
    log_file.flush()
    _process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "services.subtitle_api:app", "--host", "127.0.0.1", "--port", "8800"],
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    _write_pid_file(_process.pid)

    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_running():
            print("subtitle_service поднялся и отвечает.")
            return True
        time.sleep(0.5)

    print(f"Не удалось дождаться запуска subtitle_service за {timeout} сек. "
          f"Смотрите лог: {SUBTITLE_SERVICE_LOG}")
    return False


def stop():
    """Останавливает subtitle_service — независимо от того, эта ли сессия
    его подняла или предыдущая (через PID-файл). Раньше stop() трогал
    только процесс, запущенный текущей сессией, поэтому после падения/
    force-quit GUI сервис оставался висеть на 8800 со СТАРЫМ кодом, и
    следующий запуск app.py его не перезапускал (is_running() видел живой
    /health и молчал) — приходилось убивать вручную и гадать, актуален ли
    код. Вызывается из app.py только из обработчика краша (_handle_exception) —
    НЕ при обычном закрытии окна: subtitle_service намеренно живёт как общий
    фоновый процесс между запусками GUI (и годится для CLI-скриптов тоже),
    чтобы не тратить время на повторный подъём uvicorn на каждом старте."""
    global _process
    pid = _process.pid if (_process and _process.poll() is None) else _read_pid_file()
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
            # Ждём graceful shutdown (до 5 сек) — именно на этом шаге срабатывает
            # shutdown-хук в subtitle_service.py, который убивает осиротевшие
            # whisper-субпроцессы. Без ожидания stop() возвращался бы раньше,
            # чем успевает отработать этот хук.
            deadline = time.time() + 5.0
            while is_running() and time.time() < deadline:
                time.sleep(0.2)
            if is_running():
                os.kill(pid, signal.SIGKILL)
                print(f"subtitle_service (PID {pid}) не остановился за 5 сек — добит SIGKILL.")
            else:
                print(f"subtitle_service (PID {pid}) остановлен.")
        except ProcessLookupError:
            pass
    _process = None
    try:
        os.remove(SUBTITLE_SERVICE_PID_FILE)
    except FileNotFoundError:
        pass


if __name__ == "__main__":
    # Быстрая ручная проверка: python service_manager.py
    if ensure_running():
        print("OK — сервис доступен на", SUBTITLE_SERVICE_URL)
    else:
        print("Не удалось поднять сервис.")
