"""
asr/transcribe.py

Чистые функции транскрипции (whisper CLI) и вычитки (fix_srt.align_and_fix),
без FastAPI и без Qt. Вынесены из services/subtitle_api.py (там раньше была
вся логика внутри HTTP-эндпоинтов) ровно затем, чтобы один и тот же код
можно было:

  - вызвать напрямую, в одном процессе — режим A, см. web/gradio_app.py
    (для Colab/локальной разработки — без лишнего subprocess+HTTP round-trip);
  - обернуть в FastAPI — режим B, см. services/subtitle_api.py (для будущего
    Flask-продакшена, когда ASR может жить на отдельной GPU-машине).

См. COLAB_MIGRATION_PLAN.md, раздел 3.2.
"""
import os
import re
import subprocess
import sys
import uuid
from typing import Callable, Optional

from config.paths import API_JOBS_DIR


def default_whisper_exe() -> str:
    """sys.executable внутри уже запущенного процесса всегда указывает на
    python текущего venv, а whisper лежит в той же папке bin/ — берём его
    оттуда, а не полагаемся на PATH (см. исходный комментарий в
    subtitle_service.py — актуален и здесь)."""
    return os.path.join(os.path.dirname(sys.executable), "whisper")


def default_device() -> str:
    """Автоопределение устройства: cuda, если доступна (Colab/T4), иначе cpu.
    Раньше (subtitle_service.py::TranscribeRequest.device) было жёстко
    "cpu" по умолчанию, и video-pipeline никогда явно не передавал "cuda" —
    путь до GPU физически не мог сработать, даже если torch с CUDA был
    установлен. См. COLAB_MIGRATION_PLAN.md, раздел 2.1 (TASK G-02)."""
    try:
        import torch
        return "cuda" if torch.cuda.is_available() else "cpu"
    except ImportError:
        return "cpu"


def default_model(device: Optional[str] = None) -> str:
    """Модель whisper по умолчанию — зависит от устройства, а не константа.

    Whisper здесь отвечает в первую очередь за ТАЙМИНГИ (посегментные
    временные метки), а не за финальный текст — реальные слова всё равно
    берутся из эталонного narration_text.txt через align_and_fix (см.
    asr/fix_srt.py). Значит на CPU нет смысла ждать large-v3/small ради
    точности распознавания, которая всё равно будет переписана вычиткой:
    base достаточно и в разы быстрее. На GPU (Colab/T4) время перестаёт
    быть узким местом, поэтому там по умолчанию точность повыше — large-v3."""
    device = device or default_device()
    return "large-v3" if device == "cuda" else "base"


def build_whisper_command(whisper_exe, input_path, model, language, device,
                           word_timestamps, output_dir):
    cmd = [
        whisper_exe, input_path,
        "--model", model,
        "--output_format", "srt",
        "--device", device,
        "--output_dir", output_dir,
    ]
    if language and language != "Auto":
        cmd += ["--language", language]
    if word_timestamps:
        cmd += ["--word_timestamps", "True"]
    return cmd


_PROGRESS_RE = re.compile(r"(\d+)%\|")


def run_transcribe(
    audio_path: str,
    language: Optional[str] = "ru",
    model: Optional[str] = None,
    device: Optional[str] = None,
    word_timestamps: bool = True,
    whisper_exe: Optional[str] = None,
    on_progress: Optional[Callable[[int, str], None]] = None,
) -> str:
    """Запускает whisper CLI и возвращает путь к .srt.

    device=None -> автоопределение (default_device()).
    model=None  -> автоопределение по устройству (default_model(), см. выше) —
    раньше здесь всегда стоял хардкод "large-v3" независимо от устройства,
    что на CPU было избыточно медленно ради точности, которую всё равно
    переписывает align_and_fix.

    on_progress(percent, message) — необязательный колбэк; если передан,
    транскрипция запускается в потоковом режиме (читаем tqdm-вывод whisper
    построчно), иначе — обычный subprocess.run и ждём целиком.
    """
    if not os.path.exists(audio_path):
        raise FileNotFoundError(f"audio_path не найден: {audio_path}")

    device = device or default_device()
    model = model or default_model(device)
    whisper_exe = whisper_exe or default_whisper_exe()

    job_dir = os.path.join(API_JOBS_DIR, f"transcribe_{uuid.uuid4().hex}")
    os.makedirs(job_dir, exist_ok=True)

    cmd = build_whisper_command(whisper_exe, audio_path, model, language,
                                 device, word_timestamps, job_dir)

    if on_progress is None:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"whisper завершился с ошибкой:\n{result.stderr[-2000:]}")
    else:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        buf, tail = "", []
        while True:
            ch = proc.stdout.read(1)
            if ch == "" and proc.poll() is not None:
                break
            if ch in ("\r", "\n"):
                line, buf = buf, ""
                if line.strip():
                    tail.append(line)
                    del tail[:-30]
                    m = _PROGRESS_RE.search(line)
                    if m:
                        on_progress(int(m.group(1)), f"Распознаю речь: {m.group(1)}%")
            elif ch:
                buf += ch
        if proc.wait() != 0:
            raise RuntimeError("whisper завершился с ошибкой:\n" + "\n".join(tail[-20:]))

    base = os.path.splitext(os.path.basename(audio_path))[0]
    srt_path = os.path.join(job_dir, base + ".srt")
    if not os.path.exists(srt_path):
        raise RuntimeError("whisper завершился без ошибки, но .srt не найден — "
                            "проверьте output_dir/имя файла")
    return srt_path


def run_proofread(srt_path: str, reference_text_path: str,
                   output_path: Optional[str] = None) -> str:
    from asr.fix_srt import align_and_fix

    if not os.path.exists(srt_path):
        raise FileNotFoundError(f"srt_path не найден: {srt_path}")
    if not os.path.exists(reference_text_path):
        raise FileNotFoundError(f"reference_text_path не найден: {reference_text_path}")

    out_path = output_path or os.path.join(API_JOBS_DIR, f"fixed_{uuid.uuid4().hex}.srt")
    ok = align_and_fix(srt_path, reference_text_path, out_path)
    if not ok:
        raise RuntimeError("align_and_fix вернул ошибку — смотрите логи")
    return out_path
