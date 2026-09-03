"""
scripts/verify_env.py

Проходит по всем зависимостям проекта и печатает понятный отчёт: что
установлено и рабочее, что отсутствует, что не критично. Задуман как
первый шаг после `pip install` — чтобы не ловить ImportError/FileNotFoundError
по одному прямо в середине долгого прогона пайплайна.

Запуск:
    python scripts/verify_env.py

Работает и на локальной CPU-машине, и на Colab (device в отчёте покажет,
что реально видит torch — cuda или cpu, без предположений).
"""
import importlib
import os
import shutil
import sys

OK, WARN, FAIL = "OK", "WARN", "FAIL"
_results = []


def check(name, fn):
    try:
        detail = fn()
        _results.append((OK, name, detail or ""))
    except Exception as e:
        _results.append((FAIL, name, str(e)))


def check_optional(name, fn):
    try:
        detail = fn()
        _results.append((OK, name, detail or ""))
    except Exception as e:
        _results.append((WARN, name, str(e)))


# --- Python-пакеты, которые реально импортируются в коде проекта ---

def _import_ok(module_name, attr=None):
    def fn():
        mod = importlib.import_module(module_name)
        if attr:
            getattr(mod, attr)
        version = getattr(mod, "__version__", None)
        return f"версия {version}" if version else ""
    return fn


check("Python", lambda: f"{sys.version.split()[0]} ({sys.executable})")

for pkg in ["fastapi", "uvicorn", "pydantic", "requests", "PyQt5",
            "gradio", "PIL", "numpy", "openai", "elevenlabs", "dotenv"]:
    check(f"import {pkg}", _import_ok(pkg))

for pkg in ["pdfplumber", "pdf2image", "pytesseract", "bs4", "geoip2"]:
    check_optional(f"import {pkg} (нужен только scraping/fbi_vault_scraper.py)", _import_ok(pkg))


# --- torch / device — самое важное для GPU-переноса ---

def _check_torch():
    import torch
    device = "cuda" if torch.cuda.is_available() else "cpu"
    detail = f"torch {torch.__version__}, устройство: {device}"
    if device == "cuda":
        detail += f" ({torch.cuda.get_device_name(0)})"
    return detail


check("import torch", _check_torch)
check_optional("import open_clip (CLIP-подбор видео, core/video_sources/pexels_matcher.py)",
               _import_ok("open_clip"))
check_optional("import whisper (ASR, но обычно вызывается через CLI, не import)",
               _import_ok("whisper"))


# --- системные бинарники (не ставятся через pip) ---

def _check_binary(name, hint):
    def fn():
        path = shutil.which(name)
        if not path:
            raise FileNotFoundError(f"{name} не найден в PATH — {hint}")
        return path
    return fn


check("бинарник: ffmpeg", _check_binary(
    "ffmpeg", "нужен системный пакет, НЕ ставится через pip (apt install ffmpeg / brew install ffmpeg)"))
check("бинарник: ffprobe", _check_binary(
    "ffprobe", "обычно ставится вместе с ffmpeg"))


def _check_whisper_cli():
    # именно тот способ поиска, что и в asr/transcribe.py::default_whisper_exe()
    exe = os.path.join(os.path.dirname(sys.executable), "whisper")
    if not os.path.exists(exe):
        raise FileNotFoundError(f"{exe} не найден — ставится вместе с пакетом openai-whisper (pip)")
    return exe


check("бинарник: whisper CLI (в venv рядом с python)", _check_whisper_cli)


# --- camoufox/playwright — нужны только для scraping/, не для GPU/web части ---

def _check_camoufox_binary():
    from camoufox.pkgman import installed_verstr
    v = installed_verstr()
    if not v:
        raise FileNotFoundError("бинарник Camoufox не докачан — выполните: camoufox fetch")
    return f"версия {v}"


check_optional("Camoufox browser binary (нужен только scraping/, см. COLAB_MIGRATION_PLAN.md 2.4)",
               _check_camoufox_binary)
check_optional("import playwright (нужен только scraping/)", _import_ok("playwright"))


# --- config.paths / VIDEO_STUDIO_HOME ---

def _check_paths():
    from config.paths import VIDEO_STUDIO_HOME, PROJECTS_ROOT, OUTPUT_DIR, MODELS_CACHE_DIR
    return f"VIDEO_STUDIO_HOME={VIDEO_STUDIO_HOME}"


check("config.paths (VIDEO_STUDIO_HOME и подкаталоги создаются автоматически)", _check_paths)


# --- вывод отчёта ---

def _print_report():
    width = max(len(r[1]) for r in _results) + 2
    fails = 0
    for status, name, detail in _results:
        marker = {"OK": "[OK]  ", "WARN": "[WARN]", "FAIL": "[FAIL]"}[status]
        print(f"{marker} {name.ljust(width)} {detail}")
        if status == FAIL:
            fails += 1

    print()
    n_ok = sum(1 for r in _results if r[0] == OK)
    n_warn = sum(1 for r in _results if r[0] == WARN)
    n_fail = sum(1 for r in _results if r[0] == FAIL)
    print(f"Итого: {n_ok} OK, {n_warn} WARN (опционально), {n_fail} FAIL (критично)")

    if fails:
        print("\nЕсть критичные проблемы (FAIL) — пайплайн не запустится, пока они не исправлены.")
        sys.exit(1)
    else:
        print("\nВсё необходимое установлено. WARN — это только scraping/ (FBI Vault, PDF/OCR) "
              "и не блокирует Script/Subtitles/Matching/Assembly.")


if __name__ == "__main__":
    _print_report()
