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

# На Colab "~" (например /root) — это диск самой VM: эфемерный, стирается
# при пересоздании рантайма вместе со всем, что туда записано. Реальный
# случай: пользователь потратил несколько часов на транскрипцию, а к
# моменту скачивания результата файла уже не было — VIDEO_STUDIO_HOME
# молча откатилась на локальный путь VM вместо Drive, потому что
# соответствующая переменная окружения не была установлена ДО того, как
# был запущен процесс (например, после Restart runtime ячейку с
# os.environ["VIDEO_STUDIO_HOME"] = ... забыли прогнать заново перед
# повторным запуском web/gradio_app.py). Раньше это происходило абсолютно
# тихо. Останавливаем сразу и явно, а не позволяем часам работы потеряться
# молча ещё раз.
_IN_COLAB = os.path.exists("/content")
if _IN_COLAB and not VIDEO_STUDIO_HOME.startswith("/content/drive/"):
    raise RuntimeError(
        f"VIDEO_STUDIO_HOME=\"{VIDEO_STUDIO_HOME}\" не похож на путь на Google Drive, "
        f"хотя это Colab (обнаружен /content). Всё, что будет сюда записано "
        f"(субтитры, видео, промежуточные файлы), пропадёт при пересоздании "
        f"рантайма/остановке сессии — то есть именно та потеря результата, из-за "
        f"которой это исключение и добавлено. Установите переменную окружения "
        f"VIDEO_STUDIO_HOME на путь внутри /content/drive/... ДО запуска "
        f"web/gradio_app.py (см. notebooks/colab_setup.ipynb, ячейка 2) и "
        f"перезапустите процесс заново."
    )

print(f"[config.paths] VIDEO_STUDIO_HOME = {VIDEO_STUDIO_HOME}"
      f"{' (Google Drive — переживёт пересоздание рантайма)' if _IN_COLAB else ''}")

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

def _ensure_dir(path: str) -> None:
    """os.makedirs(path, exist_ok=True) кидает FileExistsError, если по
    этому пути уже лежит НЕ директория — битый симлинк (например, остаток
    ручного костыля вида `ln -s /tmp/... .../api_jobs`, а /tmp на Colab
    пропадает при пересоздании рантайма) или файл. Наблюдалось на практике
    дважды подряд на Colab в одном и том же месте (output/api_jobs) — не
    гипотетический край, реальный повторяющийся случай. Расчищаем такой
    путь перед созданием директории, вместо того чтобы падать на импорте
    всего пакета с невнятным traceback ещё до старта приложения."""
    if os.path.islink(path) or (os.path.exists(path) and not os.path.isdir(path)):
        os.remove(path)
    os.makedirs(path, exist_ok=True)


for _dir in (PROJECTS_ROOT, OUTPUT_DIR, LOGS_DIR, MODELS_CACHE_DIR, API_JOBS_DIR,
             USER_DATA_DIR, USER_STYLES_DIR, SCRAPER_CACHE_DIR):
    _ensure_dir(_dir)


def crash_log_path() -> str:
    """Раньше — app.py::_crash_log_path() -> ~/VideoCombine_output/crash_log.txt"""
    return os.path.join(LOGS_DIR, "crash_log.txt")


def subtitle_service_log_path() -> str:
    """Раньше — service_manager.py::SUBTITLE_SERVICE_LOG"""
    return os.path.join(LOGS_DIR, "subtitle_service.log")


def subtitle_service_pid_path() -> str:
    """Раньше — service_manager.py::SUBTITLE_SERVICE_PID_FILE"""
    return os.path.join(LOGS_DIR, "subtitle_service.pid")
