"""
config/paths.py

Единая точка разрешения путей хранения для всего video_studio.

Раньше пути были захардкожены по четырём разным местам:
  - PROJECTS_ROOT       = ~/projects/video-pipeline/gui_projects   (gui/main_window.py)
  - VideoCombine_output = ~/VideoCombine_output                    (app.py — crash log, service_manager.py — лог/pid)
  - OasisSubtitleStudio_output = ~/OasisSubtitleStudio_output      (subtitle_service.py — api_jobs)
  - SUBTITLE_SERVICE_DIR / OASIS_STUDIO_DIR = /home/oasis/openai-whisper (только для режима "два раздельных venv/процесса")

Теперь всё считается от одной переменной окружения VIDEO_STUDIO_HOME:

  Локальная машина (по умолчанию, если VIDEO_STUDIO_HOME не задана):
      ~/video_studio_home/...
      — сохраняет прежнее поведение "из коробки", ничего не ломает для
      тех, кто уже работал с проектом на CPU-машине.

  Google Colab:
      /content/drive/MyDrive/video_studio_home/...
      — задаётся явно в notebooks/colab_setup.ipynb ДО импорта остального
      пакета, чтобы модели/проекты/логи переживали пересоздание рантайма.

Использование в остальном коде:
    from config.paths import PROJECTS_ROOT, OUTPUT_DIR, LOGS_DIR, MODELS_CACHE_DIR
"""
import os

VIDEO_STUDIO_HOME = os.environ.get(
    "VIDEO_STUDIO_HOME",
    os.path.join(os.path.expanduser("~"), "video_studio_home"),
)

PROJECTS_ROOT = os.path.join(VIDEO_STUDIO_HOME, "projects")
OUTPUT_DIR = os.path.join(VIDEO_STUDIO_HOME, "output")
LOGS_DIR = os.path.join(VIDEO_STUDIO_HOME, "logs")
MODELS_CACHE_DIR = os.path.join(VIDEO_STUDIO_HOME, "models_cache")

# api_jobs (промежуточные .srt из /transcribe, /proofread) — раньше отдельно
# в ~/OasisSubtitleStudio_output/api_jobs, теперь подкаталог общего output/.
API_JOBS_DIR = os.path.join(OUTPUT_DIR, "api_jobs")

# Раньше — точечные дотфайлы в домашней директории пользователя
# (~/.oasis_subtitle_studio/styles, ~/.oasis_video_pipeline/...). На
# локальной машине это не проблема, но на Colab "~" — это /root внутри
# эфемерного рантайма, и такие дотфайлы пропадали бы при каждом
# пересоздании инстанса вместе со всем остальным. Переносим под общий
# VIDEO_STUDIO_HOME, чтобы стилевые пресеты и кэш сканера тоже сохранялись
# на примонтированном Google Drive.
USER_DATA_DIR = os.path.join(VIDEO_STUDIO_HOME, "user_data")
USER_STYLES_DIR = os.path.join(USER_DATA_DIR, "styles")
SCRAPER_CACHE_DIR = os.path.join(USER_DATA_DIR, "scraper_cache")

for _dir in (PROJECTS_ROOT, OUTPUT_DIR, LOGS_DIR, MODELS_CACHE_DIR, API_JOBS_DIR,
             USER_DATA_DIR, USER_STYLES_DIR, SCRAPER_CACHE_DIR):
    os.makedirs(_dir, exist_ok=True)


def crash_log_path() -> str:
    """Раньше — app.py::_crash_log_path() -> ~/VideoCombine_output/crash_log.txt"""
    return os.path.join(LOGS_DIR, "crash_log.txt")


def subtitle_service_log_path() -> str:
    """Раньше — service_manager.py::SUBTITLE_SERVICE_LOG"""
    return os.path.join(LOGS_DIR, "subtitle_service.log")


def subtitle_service_pid_path() -> str:
    """Раньше — service_manager.py::SUBTITLE_SERVICE_PID_FILE"""
    return os.path.join(LOGS_DIR, "subtitle_service.pid")
