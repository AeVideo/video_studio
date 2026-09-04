"""
services/subtitle_api.py  (бывший subtitle_service.py)

Тонкая FastAPI-обвязка поверх asr/transcribe.py — вся реальная логика
(построение команды whisper, потоковое чтение прогресса, вычитка) живёт
там, здесь только HTTP-контракт. Раньше вся логика была прямо внутри
эндпоинтов этого файла — при объединении репозиториев её вынесли в
asr/transcribe.py, чтобы её же напрямую использовал web/gradio_app.py
(режим A, один процесс) без HTTP round-trip. См. COLAB_MIGRATION_PLAN.md,
раздел 3.2 — именно дублирование такого рода логики в двух местах разошлось
однажды на два дня разбора (см. SESSION_SUMMARY_2026-08-26.md), и вынесение
в общий модуль — прямое устранение причины, а не разового симптома.

ДО объединения репозиториев жил в отдельном venv (openai-whisper) и
запускался по захардкоженному пути. Теперь это часть единого пакета
video_studio — режим отдельного процесса ("режим B") нужен только когда ASR
намеренно вынесен на отдельную машину/контейнер (например, будущий
Flask-продакшен). Для Colab/локальной разработки в один процесс
asr/transcribe.py следует импортировать напрямую (режим A, см.
web/gradio_app.py), без подъёма этого сервиса.

Запуск (режим B):
    pip install -e . --no-deps
    python -m uvicorn services.subtitle_api:app --host 127.0.0.1 --port 8800
(или services.service_manager.ensure_running() — поднимет тем же способом
сам, через sys.executable, без привязки к чужому venv)

Эндпоинты:
    POST /transcribe                     — whisper CLI -> сырой .srt
    POST /proofread                      — сырой .srt + эталонный текст -> исправленный .srt
    POST /transcribe_and_proofread       — оба шага подряд, синхронно
    POST /transcribe_and_proofread_async — то же, асинхронно, с /job/{job_id} для опроса прогресса
    GET  /health
"""
import threading
import uuid
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from asr.transcribe import run_transcribe, run_proofread, default_device, default_whisper_exe

app = FastAPI(title="video_studio — subtitle API")

# ---------- фоновые job'ы с реальным прогрессом ----------
_jobs: dict = {}
_jobs_lock = threading.Lock()
_running_threads: dict = {}
_threads_lock = threading.Lock()


def _update_job(job_id: str, **kwargs):
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].update(kwargs)


# ---------- модели запроса/ответа ----------

class TranscribeRequest(BaseModel):
    audio_path: str
    language: Optional[str] = "ru"
    model: Optional[str] = None  # None -> asr.transcribe.default_model(device)
    # device=None -> автоопределение в asr.transcribe.default_device()
    # (cuda, если физически доступна — Colab/T4 — иначе cpu). Раньше здесь
    # был хардкод "cpu", и GPU не использовалась даже если была доступна
    # (см. COLAB_MIGRATION_PLAN.md, раздел 2.1, TASK G-02).
    device: Optional[str] = None
    word_timestamps: bool = True
    whisper_exe: Optional[str] = None


class TranscribeResponse(BaseModel):
    srt_path: str


@app.post("/transcribe", response_model=TranscribeResponse)
def transcribe(req: TranscribeRequest):
    try:
        srt_path = run_transcribe(
            audio_path=req.audio_path, language=req.language, model=req.model,
            device=req.device, word_timestamps=req.word_timestamps, whisper_exe=req.whisper_exe,
        )
    except FileNotFoundError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    return TranscribeResponse(srt_path=srt_path)


class ProofreadRequest(BaseModel):
    srt_path: str
    reference_text_path: str  # у нас — narration_text.txt
    output_path: Optional[str] = None


class ProofreadResponse(BaseModel):
    output_path: str
    success: bool


@app.post("/proofread", response_model=ProofreadResponse)
def proofread(req: ProofreadRequest):
    try:
        out_path = run_proofread(req.srt_path, req.reference_text_path, req.output_path)
    except FileNotFoundError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    return ProofreadResponse(output_path=out_path, success=True)


class TranscribeAndProofreadRequest(BaseModel):
    audio_path: str
    reference_text_path: str
    language: Optional[str] = "ru"
    model: Optional[str] = None  # None -> asr.transcribe.default_model(device)
    device: Optional[str] = None
    word_timestamps: bool = True
    whisper_exe: Optional[str] = None


class TranscribeAndProofreadResponse(BaseModel):
    raw_srt_path: str
    fixed_srt_path: str


@app.post("/transcribe_and_proofread", response_model=TranscribeAndProofreadResponse)
def transcribe_and_proofread(req: TranscribeAndProofreadRequest):
    try:
        raw_srt_path = run_transcribe(
            audio_path=req.audio_path, language=req.language, model=req.model,
            device=req.device, word_timestamps=req.word_timestamps, whisper_exe=req.whisper_exe,
        )
        fixed_srt_path = run_proofread(raw_srt_path, req.reference_text_path)
    except FileNotFoundError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    return TranscribeAndProofreadResponse(raw_srt_path=raw_srt_path, fixed_srt_path=fixed_srt_path)


@app.get("/health")
def health():
    return {"status": "ok", "device": default_device(), "whisper_exe": default_whisper_exe()}


# ---------- асинхронная версия составного шага — с прогрессом ----------

def _run_job(job_id: str, req: "TranscribeAndProofreadRequest"):
    try:
        _update_job(job_id, status="running", progress=0, message="Распознаю речь...")

        def on_progress(percent, message):
            _update_job(job_id, progress=percent, message=message)

        raw_srt_path = run_transcribe(
            audio_path=req.audio_path, language=req.language, model=req.model,
            device=req.device, word_timestamps=req.word_timestamps, whisper_exe=req.whisper_exe,
            on_progress=on_progress,
        )

        _update_job(job_id, progress=95, message="Сверяю с эталонным текстом...")
        fixed_srt_path = run_proofread(raw_srt_path, req.reference_text_path)

        _update_job(job_id, status="done", progress=100, message="Готово",
                    result={"raw_srt_path": raw_srt_path, "fixed_srt_path": fixed_srt_path})
    except Exception as e:
        _update_job(job_id, status="error", message=str(e))
    finally:
        with _threads_lock:
            _running_threads.pop(job_id, None)


@app.post("/transcribe_and_proofread_async")
def transcribe_and_proofread_async(req: TranscribeAndProofreadRequest):
    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _jobs[job_id] = {"status": "running", "progress": 0, "message": "Запускаю...", "result": None}
    t = threading.Thread(target=_run_job, args=(job_id, req), daemon=True)
    with _threads_lock:
        _running_threads[job_id] = t
    t.start()
    return {"job_id": job_id}


@app.get("/job/{job_id}")
def job_status(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "job_id не найден (сервис перезапускался?)")
    return job
