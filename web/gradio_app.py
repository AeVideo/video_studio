"""
web/gradio_app.py

Web-интерфейс поверх той же бизнес-логики, что и desktop/*_gui — без
переписывания core/ или asr/. Каждая вкладка — тонкий адаптер, повторяющий
структуру соответствующего QThread-воркера из desktop/*/workers.py:
тот же вызов функций, тот же порядок шагов, только pyqtSignal.emit()
заменён на gr.Progress()/return.

См. COLAB_MIGRATION_PLAN.md, раздел 5 — почему Gradio первый, и раздел 5.2 —
пример адаптера, на основе которого написана вкладка "Сценарий" ниже (сверено
с desktop/video_pipeline_gui/workers.py::ScriptWorker и ::CaseScrapeWorker).

Запуск:
    python -m web.gradio_app
Локально откроется http://127.0.0.1:7860 ; на Colab — добавьте share=True
(уже включено ниже, если запущено вне интерактивного локального запуска —
см. IN_COLAB) для публичной ссылки без отдельного деплоя.

Готовые вкладки:
    - Источник → Сценарий   (core/script/book_to_script.py; источник — файл
      .txt ИЛИ дело FBI Vault через scraping/, см. IN_COLAB ниже)
    - Субтитры               (asr/transcribe.py, GPU по умолчанию на Colab)
    - Подбор видео            (core/video_sources/pexels_matcher.py + archive_org_search,
      адаптер поверх desktop/video_pipeline_gui/workers.py::MatchWorker)
    - Сборка                  (core/assembly/assemble_video.py,
      адаптер поверх desktop/video_pipeline_gui/workers.py::AssembleWorker)
"""
import contextlib
import io
import json
import os
import uuid

import gradio as gr

from config.paths import PROJECTS_ROOT, VIDEO_STUDIO_HOME, OUTPUT_DIR
from core.script import book_to_script
from core.scenes import srt_to_scenes, scenes_to_shots
from core.video_sources import pexels_matcher
from core.assembly import assemble_video
from asr.transcribe import run_transcribe, run_proofread, default_device, default_model
from asr import style_manager

IN_COLAB = "COLAB_GPU" in os.environ or os.path.exists("/content")


def _persistent_workdir(prefix: str) -> str:
    """Рабочая папка под VIDEO_STUDIO_HOME (Google Drive на Colab), а не
    tempfile.mkdtemp() в системный /tmp. Разница критична именно на Colab:
    соединение через google.colab.kernel.proxyPort нестабильно на долгих
    операциях (подбор видео, сборка) и может оборваться посреди прогона —
    'Broken Connection'. Если результат в /tmp, оборванное соединение
    означает потерю уже сделанной работы без единого способа её забрать.
    Если результат на Drive — он остаётся на диске независимо от того, что
    случилось с фронтендом Gradio, и его можно найти и забрать вручную
    даже если сама вкладка браузера так и не восстановила соединение."""
    path = os.path.join(OUTPUT_DIR, f"{prefix}_{uuid.uuid4().hex}")
    os.makedirs(path, exist_ok=True)
    return path


class _ProgressStdout(io.StringIO):
    """Перехватывает print() из core/video_sources/pexels_matcher.py и
    показывает последнюю строку прямо в прогресс-баре Gradio.

    Внутри одной сцены match_scene() может делать десятки-сотни сетевых
    запросов (несколько запросов x несколько кандидатов x несколько картинок
    на кандидата) — весь этот путь уже был усыпан print() для CLI/десктопного
    GUI (какой кандидат, какой score, retry по низкой уверенности), но эти
    строки печатались в stdout фонового процесса Colab, а не в интерфейс —
    пользователь видел молчащий спиннер по 3-5 минут на тяжёлую сцену без
    единого признака, что вообще происходит. Теперь та же самая печать
    отражается в описании прогресса в реальном времени."""

    def __init__(self, progress_fn, base_percent: float):
        super().__init__()
        self.progress_fn = progress_fn
        self.base_percent = base_percent

    def write(self, s):
        line = s.strip()
        if line:
            self.progress_fn(self.base_percent, desc=line[:200])
        return super().write(s)


@contextlib.contextmanager
def _progress_stdout(progress_fn, base_percent: float):
    with contextlib.redirect_stdout(_ProgressStdout(progress_fn, base_percent)):
        yield


def _resolve_path(uploaded_file, typed_path: str, label: str = "файл"):
    """Путь на диске (Drive) — в приоритете, если заполнены оба поля.

    gr.File открывает диалог выбора файла БРАУЗЕРА — тот видит файлы
    вашего локального компьютера, а не файловую систему сервера (VM
    Colab), даже если нужный файл уже лежит на смонтированном Drive той
    же VM. Из-за этого раньше единственным способом передать файл между
    вкладками было: скачать его из интерфейса на свой компьютер, затем
    загрузить обратно через тот же браузерный диалог — лишний, ничем не
    обоснованный круг, если файл и так уже на Drive. Текстовое поле "путь
    на диске" рядом с каждой загрузкой даёт способ обойти это: путь можно
    скопировать прямо из файлового менеджера Colab слева (правая кнопка ->
    Copy path) и вставить сюда, без единого скачивания."""
    if typed_path and typed_path.strip():
        path = typed_path.strip()
        if not os.path.exists(path):
            raise gr.Error(f"{label}: файл не найден по указанному пути: {path}")
        return path
    if uploaded_file is not None:
        return uploaded_file.name
    return None


# ---------------------------------------------------------------------------
# Вкладка 1a: FBI Vault -> текст дела
# Соответствует desktop/video_pipeline_gui/workers.py::CaseScrapeWorker.run(),
# логика 1:1 та же (scraping/fbi_vault_index.py + fbi_vault_scraper.py),
# только вызов на клик кнопки вместо QThread. Работает ТОЛЬКО локально —
# использует Camoufox (реальный браузер), на Colab не запускать
# (см. COLAB_MIGRATION_PLAN.md, раздел 2.4 — та же граница, что для Flow/Qwen).
# ---------------------------------------------------------------------------

def fbi_search(query):
    from scraping.fbi_vault_index import load_or_build_index, search_index

    idx = load_or_build_index()
    results = search_index(idx, query or "", limit=30)
    if not results:
        return gr.update(choices=[], value=None), {}

    choices = [r["title"] for r in results]
    mapping = {r["title"]: r["url"] for r in results}
    return gr.update(choices=choices, value=choices[0]), mapping


def fbi_build_corpus(selected_title, case_map, max_parts, progress=gr.Progress()):
    if not selected_title or not case_map or selected_title not in case_map:
        raise gr.Error("Сначала найдите дело и выберите его из списка")

    from scraping.fbi_vault_scraper import build_case_corpus

    case_url = case_map[selected_title]
    out_dir = _persistent_workdir("fbi_case")
    max_parts_val = int(max_parts) if max_parts else None

    def on_progress(percent, message):
        progress(min(percent / 100, 0.98), desc=message)

    progress(0.0, desc="Открываю Camoufox-сессию...")
    txt_path = build_case_corpus(case_url, out_dir, max_parts=max_parts_val, on_progress=on_progress)

    with open(txt_path, "r", encoding="utf-8") as f:
        text = f.read()

    progress(1.0, desc="Готово")
    return text, f"Собрано {len(text)} символов из «{selected_title}» -> {txt_path}"


def fbi_extract_from_pdfs(pdf_files, progress=gr.Progress()):
    """Если PDF уже скачаны (локально раньше, или перенесены на Colab через
    Drive) — извлекает текст БЕЗ Camoufox. Работает и на Colab: browser здесь
    не участвует, только pdfplumber/OCR (см. scraping/fbi_vault_scraper.py,
    extract_corpus_from_pdfs — разделено от скачивания намеренно)."""
    if not pdf_files:
        raise gr.Error("Загрузите один или несколько PDF-файлов")

    from scraping.fbi_vault_scraper import extract_corpus_from_pdfs

    pdf_paths = [f.name for f in pdf_files]
    out_txt = os.path.join(_persistent_workdir("fbi_pdfs"), "combined_source.txt")

    def on_progress(percent, message):
        progress(min(percent / 100, 0.98), desc=message)

    progress(0.0, desc=f"Обрабатываю {len(pdf_paths)} PDF...")
    extract_corpus_from_pdfs(pdf_paths, out_txt, on_progress=on_progress)

    with open(out_txt, "r", encoding="utf-8") as f:
        text = f.read()

    progress(1.0, desc="Готово")
    return text, f"Извлечено {len(text)} символов из {len(pdf_paths)} PDF -> {out_txt}"


# ---------------------------------------------------------------------------
# Вкладка 1: Источник -> Сценарий
# Источник текста — либо загруженный .txt, либо book_text_preview, уже
# заполненный вкладкой FBI Vault выше (что заполнено, то и используется;
# если оба — приоритет у book_text_preview, он редактируемый).
# ---------------------------------------------------------------------------

def run_script_stage(book_file, book_path, book_text_preview, duration_minutes, language,
                      project_id, content_mode, progress=gr.Progress()):
    if not project_id:
        raise gr.Error("Укажите имя проекта — по нему создастся папка в PROJECTS_ROOT")

    resolved_book_path = _resolve_path(book_file, book_path, "Текстовый источник")

    if book_text_preview and book_text_preview.strip():
        book_text = book_text_preview
    elif resolved_book_path is not None:
        with open(resolved_book_path, "r", encoding="utf-8") as f:
            book_text = f.read()
    else:
        raise gr.Error("Загрузите файл .txt (или укажите путь на диске) — либо соберите текст дела на вкладке FBI Vault")

    out_dir = os.path.join(PROJECTS_ROOT, project_id)
    os.makedirs(out_dir, exist_ok=True)

    progress(0.05, desc="Разбиваю источник на истории...")
    client = book_to_script.get_client()

    stories = book_to_script.split_book_into_stories(book_text, client)
    if not stories:
        raise gr.Error("Не удалось выделить ни одной истории из источника")

    first, backlog = stories[0], stories[1:]
    with open(os.path.join(out_dir, "stories_backlog.json"), "w", encoding="utf-8") as f:
        json.dump(backlog, f, ensure_ascii=False, indent=2)

    progress(0.4, desc=f"Пишу сценарий для: {first['title']}")
    script = book_to_script.write_script_for_story(
        story=first, source_text=book_text, duration_minutes=duration_minutes,
        language=language, client=client, content_mode=content_mode,
    )

    script_path = os.path.join(out_dir, "script.json")
    with open(script_path, "w", encoding="utf-8") as f:
        json.dump(script, f, ensure_ascii=False, indent=2)

    narration_path = os.path.join(out_dir, "narration_text.txt")
    with open(narration_path, "w", encoding="utf-8") as f:
        f.write("\n\n".join(s["text"] for s in script))

    progress(1.0, desc="Готово")
    preview = "\n\n".join(f"[{s.get('timestamp', '?')}] {s['text']}" for s in script[:5])
    return (f"Сценарий сохранён: {script_path}\n"
            f"В бэклоге осталось историй: {len(backlog)}\n\n"
            f"Первые реплики:\n{preview}")


# ---------------------------------------------------------------------------
# Вкладка 2: Субтитры (первая GPU-стадия в web UI — см. TASK W-02)
# Соответствует desktop/video_pipeline_gui/workers.py::SubtitlesWorker.run(),
# но без HTTP-похода на subtitle_api — вызывает asr/transcribe.py напрямую
# (режим A, один процесс — см. COLAB_MIGRATION_PLAN.md, раздел 3.2).
# ---------------------------------------------------------------------------

def run_subtitles_stage(audio_file, audio_path, reference_text_file, reference_text_path,
                         language, model, progress=gr.Progress()):
    resolved_audio = _resolve_path(audio_file, audio_path, "Аудио/видео с озвучкой")
    if resolved_audio is None:
        raise gr.Error("Загрузите аудио/видео с озвучкой (или укажите путь на диске)")

    device = default_device()
    if not model or model.startswith("Авто"):
        model = default_model(device)
    progress(0.0, desc=f"Устройство: {device}, модель: {model}")

    def on_progress(percent, message):
        progress(min(percent / 100, 0.94), desc=message)

    raw_srt_path = run_transcribe(
        audio_path=resolved_audio, language=language, model=model,
        device=device, on_progress=on_progress,
    )

    resolved_reference = _resolve_path(reference_text_file, reference_text_path, "Эталонный текст")
    if resolved_reference is not None:
        progress(0.95, desc="Сверяю с эталонным текстом...")
        fixed_srt_path = run_proofread(raw_srt_path, resolved_reference)
        return fixed_srt_path, f"Готово (устройство: {device}, модель: {model}). Вычитано по эталону."

    return raw_srt_path, f"Готово (устройство: {device}, модель: {model}). Без вычитки — эталонный текст не загружен."


# ---------------------------------------------------------------------------
# Вкладка 3: Сцены -> Шоты
# Соответствует desktop/video_pipeline_gui/workers.py::ShotExpansionWorker.run().
# Раньше в Gradio отсутствовала целиком — shots.json неоткуда было взять.
# ---------------------------------------------------------------------------

def run_shots_stage(script_file, script_path, srt_file, srt_path, audio_file, audio_path,
                     progress=gr.Progress()):
    resolved_script = _resolve_path(script_file, script_path, "script.json")
    resolved_srt = _resolve_path(srt_file, srt_path, ".srt")
    if resolved_script is None or resolved_srt is None:
        raise gr.Error("Укажите script.json (этап «Сценарий») и .srt (этап «Субтитры») — файлом или путём на диске")

    progress(0.1, desc="Читаю сценарий и субтитры...")
    with open(resolved_script, "r", encoding="utf-8") as f:
        script_scenes = json.load(f)

    cues = srt_to_scenes.parse_srt(resolved_srt)
    words = srt_to_scenes.cues_to_word_timings(cues)

    progress(0.4, desc="Привязываю сцены к реальным таймингам речи...")
    timed = srt_to_scenes.align_scenes_to_words(script_scenes, words)
    timed_dicts = [s.__dict__ for s in timed]

    progress(0.7, desc="Разворачиваю сцены в шоты...")
    visuals_by_id = {s["id"]: s.get("visuals", []) for s in script_scenes}
    all_shots = []
    for scene in timed_dicts:
        if scene.get("status") == "missing" or scene.get("start") is None:
            continue
        visuals = visuals_by_id.get(scene["id"], [])
        all_shots.extend(scenes_to_shots.expand_scene_to_shots(scene, visuals))

    if not all_shots:
        raise gr.Error(
            "Не получилось развернуть ни одной сцены в шоты — проверьте, что "
            ".srt реально соответствует тексту сценария (те же реплики)."
        )

    resolved_audio = _resolve_path(audio_file, audio_path, "Аудио озвучки")
    out_dir = _persistent_workdir("shots")
    shots_path = os.path.join(out_dir, "shots_timed.json")
    with open(shots_path, "w", encoding="utf-8") as f:
        json.dump({"audio_path": resolved_audio, "srt_path": resolved_srt, "scenes": all_shots},
                   f, ensure_ascii=False, indent=2)

    progress(1.0, desc="Готово")
    return shots_path, f"Развёрнуто {len(all_shots)} шотов из {len(timed_dicts)} сцен -> {shots_path}"


# ---------------------------------------------------------------------------
# Вкладка 3: Подбор видео (TASK W-03)
# Соответствует desktop/video_pipeline_gui/workers.py::MatchWorker.run() —
# та же последовательность вызовов pexels_matcher, тот же инкрементальный
# _save_partial после каждого шота (см. COLAB_MIGRATION_PLAN.md/REFACTOR_PLAN.md
# — resume и защита от потери прогресса при отмене на середине).
# ---------------------------------------------------------------------------

def _save_partial_matching(out_path, data, results):
    payload = {
        "audio_path": data.get("audio_path"),
        "srt_path": data.get("srt_path"),
        "scenes": results,
    }
    tmp_path = out_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, out_path)


def run_matching_stage(shots_file, shots_path, aspect_ratio, progress=gr.Progress()):
    resolved_shots = _resolve_path(shots_file, shots_path, "shots.json")
    if resolved_shots is None:
        raise gr.Error("Укажите shots.json (результат этапа «Сцены → Шоты») — файлом или путём на диске")

    pexels_key = os.environ.get("PEXELS_API_KEY")
    pixabay_key = os.environ.get("PIXABAY_API_KEY")
    if not pexels_key:
        raise gr.Error("PEXELS_API_KEY не задан (.env локально / Colab Secrets на Colab)")

    orientation = "portrait" if aspect_ratio == "9:16" else "landscape"

    with open(resolved_shots, "r", encoding="utf-8") as f:
        data = json.load(f)
    scenes = data["scenes"]

    out_dir = _persistent_workdir("matching")
    out_path = os.path.join(out_dir, "footage.json")

    progress(0.0, desc="Загружаю CLIP и подключаюсь к DeepSeek...")
    deepseek_client = pexels_matcher.get_deepseek_client()
    clip_scorer = pexels_matcher.ClipScorer()  # кэшируется на уровне класса — повторные вызовы дёшевы
    used_ids = set()
    thumbnails_dir = os.path.join(out_dir, pexels_matcher.THUMBNAILS_DIR_NAME)

    archive_pool = []
    archive_dead_ids = set()
    if pexels_matcher.archive_org_search is not None:
        progress(0.02, desc="Ищу тематические подборки в Archive.org...")
        story_summary = " ".join(s["text"] for s in scenes)[:2000]
        with _progress_stdout(progress, 0.02):
            theme_queries = pexels_matcher.generate_archive_theme_queries(story_summary, deepseek_client)
            if theme_queries:
                archive_pool = pexels_matcher.archive_org_search.find_theme_pool(theme_queries)

    results = []
    for i, scene in enumerate(scenes, 1):
        base_percent = i / max(len(scenes), 1)
        progress(base_percent, desc=f"[{i}/{len(scenes)}] {scene['id']}: начинаю подбор...")
        with _progress_stdout(progress, base_percent):
            matched = pexels_matcher.match_scene(
                scene, deepseek_client, clip_scorer, pexels_key, used_ids,
                pixabay_key=pixabay_key, thumbnails_dir=thumbnails_dir,
                orientation=orientation, archive_pool=archive_pool,
                archive_dead_ids=archive_dead_ids,
            )
        results.append(matched)
        _save_partial_matching(out_path, data, results)  # инкрементально — тот же паттерн, что MatchWorker

    if not scenes:
        _save_partial_matching(out_path, data, results)

    progress(1.0, desc="Готово")
    return out_path, f"Подобрано видео для {len(results)}/{len(scenes)} шотов -> {out_path}"


# ---------------------------------------------------------------------------
# Вкладка 4: Сборка (TASK W-04)
# Соответствует desktop/video_pipeline_gui/workers.py::AssembleWorker.run().
# ---------------------------------------------------------------------------

def run_assembly_stage(footage_file, footage_path, audio_file, audio_path,
                        subtitles_file, subtitles_path, style_slug,
                        aspect_ratio, progress=gr.Progress()):
    resolved_footage = _resolve_path(footage_file, footage_path, "footage.json")
    resolved_audio = _resolve_path(audio_file, audio_path, "Аудио озвучки")
    if resolved_footage is None:
        raise gr.Error("Укажите footage.json (результат этапа «Подбор видео») — файлом или путём на диске")
    if resolved_audio is None:
        raise gr.Error("Укажите файл озвучки — файлом или путём на диске")

    target_width, target_height = assemble_video.ASPECT_RATIOS[aspect_ratio]

    with open(resolved_footage, "r", encoding="utf-8") as f:
        data = json.load(f)
    scenes = data["scenes"]

    work_dir = _persistent_workdir("video_assembly")
    clip_paths, durations = [], []
    placeholder_count, placeholder_seconds = 0, 0.0

    for i, scene in enumerate(scenes):
        footage = scene.get("footage")
        duration = round(scene["end"] - scene["start"], 2)
        if duration <= 0:
            continue

        progress(0.05 + 0.7 * (i + 1) / max(len(scenes), 1),
                  desc=f"[{i + 1}/{len(scenes)}] {scene['id']}: обрабатываю клип...")
        raw_path = os.path.join(work_dir, f"raw_{i}.mp4")
        norm_path = os.path.join(work_dir, f"norm_{i}.mp4")
        padded = round(duration + assemble_video.CROSSFADE_SEC, 2)

        base_percent = 0.05 + 0.7 * (i + 1) / max(len(scenes), 1)
        with _progress_stdout(progress, base_percent):
            if footage:
                assemble_video.download_clip(footage["video_link"], raw_path)
                assemble_video.normalize_clip(
                    raw_path, norm_path, padded,
                    offset=footage.get("best_offset", 0.0),
                    source_duration=footage.get("duration", 0.0),
                    target_width=target_width, target_height=target_height,
                )
            else:
                # Раньше здесь было continue — молча пропускало шот, из-за
                # чего видео-дорожка отставала от полной озвучки НАЧИНАЯ с
                # этого места, а не только в конце (см. комментарий в
                # assemble_video.generate_placeholder_clip). Плейсхолдер
                # держит тайминги видео=аудио всегда.
                print(f"    нет footage для {scene['id']} — вставляю плейсхолдер {padded}с")
                assemble_video.generate_placeholder_clip(norm_path, padded,
                                                          target_width=target_width, target_height=target_height)
                placeholder_count += 1
                placeholder_seconds += duration
        clip_paths.append(norm_path)
        durations.append(padded)

    if not clip_paths:
        raise gr.Error("Нет ни одного клипа для сборки — проверьте footage.json")

    out_path = os.path.join(work_dir, "final.mp4")

    progress(0.78, desc="Склеиваю клипы с кроссфейдом...")
    concat_path = os.path.join(work_dir, "concat.mp4")
    assemble_video.build_crossfade_chain(clip_paths, durations, concat_path)

    progress(0.88, desc="Накладываю озвучку...")
    resolved_subtitles = _resolve_path(subtitles_file, subtitles_path, "Субтитры")
    if resolved_subtitles is not None:
        muxed_path = os.path.join(work_dir, "muxed.mp4")
        assemble_video.mux_audio(concat_path, resolved_audio, muxed_path)
        progress(0.94, desc="Прожигаю субтитры...")
        slug = style_slug if style_slug and style_slug != "(без стиля по умолчанию)" else None
        assemble_video.burn_subtitles(muxed_path, resolved_subtitles, out_path, style_slug=slug)
    else:
        assemble_video.mux_audio(concat_path, resolved_audio, out_path)

    progress(1.0, desc="Готово")
    status = f"Собрано: {out_path} ({len(clip_paths)} клипов)"
    if placeholder_count:
        status += (f" — ВНИМАНИЕ: {placeholder_count} шот(ов) без подобранного видео "
                   f"заменены серой заглушкой ({placeholder_seconds:.1f} сек суммарно). "
                   f"Проверьте footage.json на статусы no_candidates/scoring_failed.")
    return out_path, status


# ---------------------------------------------------------------------------
# Сборка интерфейса
# ---------------------------------------------------------------------------

def build_app() -> gr.Blocks:
    with gr.Blocks(title="video_studio") as demo:
        _storage_note = ("✅ Google Drive — переживёт пересоздание рантайма Colab"
                          if IN_COLAB else "локальная машина")
        gr.Markdown(
            "# video_studio\n"
            "Web-интерфейс поверх той же логики, что и десктопные GUI. "
            "Все пять стадий рабочие: Сценарий → Субтитры → Сцены→Шоты → Подбор видео → Сборка.\n\n"
            f"**Файлы сохраняются в: `{VIDEO_STUDIO_HOME}`** ({_storage_note}). "
            "Проверьте этот путь перед долгой задачей — если на Colab он не начинается с "
            "`/content/drive/`, результат пропадёт при пересоздании рантайма."
        )

        with gr.Tab("1. Источник → Сценарий"):
            source_kind = gr.Radio(
                ["Файл (.txt)", "Дело FBI Vault — только локально, не на Colab"],
                value="Файл (.txt)", label="Источник текста",
            )

            with gr.Group(visible=True) as file_group:
                book_file = gr.File(label="Текстовый источник (.txt)")
                book_path = gr.Textbox(label="...или путь на диске (Drive)",
                                        placeholder="/content/drive/MyDrive/video_studio_home/...")

            with gr.Group(visible=False) as fbi_group:
                gr.Markdown(
                    "**Поиск и скачивание** (нужен Camoufox — реальный браузер) работает "
                    "только на локальной машине. **Если PDF уже скачаны раньше** — их можно "
                    "загрузить и извлечь текст ниже где угодно, включая Colab: этот шаг "
                    "браузер уже не трогает, только pdfplumber/OCR."
                )

                with gr.Accordion("Поиск и скачивание дела (только локально)", open=not IN_COLAB):
                    if IN_COLAB:
                        gr.Markdown(
                            "⚠ Это Colab — Camoufox здесь не запустится (нет полноценного "
                            "рендеринга + Cloudflare жёстче относится к облачным IP, "
                            "см. COLAB_MIGRATION_PLAN.md, раздел 2.4). Соберите дело на "
                            "локальной машине, перенесите PDF через Drive и используйте "
                            "блок ниже."
                        )
                    fbi_query = gr.Textbox(label="Поиск по названию дела (например: Cooper)")
                    fbi_search_btn = gr.Button("Искать в индексе FBI Vault")
                    fbi_case_map = gr.State({})
                    fbi_results = gr.Dropdown(label="Найденные дела", choices=[], interactive=True)
                    fbi_max_parts = gr.Number(label="Макс. частей дела (0 = все)", value=5)
                    fbi_build_btn = gr.Button("Скачать и собрать текст дела")

                with gr.Accordion("PDF уже скачаны — извлечь текст (работает и на Colab)", open=IN_COLAB):
                    fbi_pdf_files = gr.Files(label="PDF-файлы дела", file_types=[".pdf"])
                    fbi_extract_btn = gr.Button("Извлечь текст из PDF", variant="primary")

                fbi_status = gr.Textbox(label="Статус сбора", interactive=False)

            book_text_preview = gr.Textbox(
                label="Текст источника (редактируемый — сюда попадёт собранное дело FBI Vault)",
                lines=8, visible=False,
            )

            def _toggle_source(kind):
                is_fbi = kind.startswith("Дело FBI")
                return gr.update(visible=not is_fbi), gr.update(visible=is_fbi), gr.update(visible=is_fbi)

            source_kind.change(_toggle_source, inputs=source_kind,
                                outputs=[file_group, fbi_group, book_text_preview])

            fbi_search_btn.click(fbi_search, inputs=fbi_query, outputs=[fbi_results, fbi_case_map])
            fbi_build_btn.click(
                fbi_build_corpus, inputs=[fbi_results, fbi_case_map, fbi_max_parts],
                outputs=[book_text_preview, fbi_status],
            )
            fbi_extract_btn.click(
                fbi_extract_from_pdfs, inputs=fbi_pdf_files,
                outputs=[book_text_preview, fbi_status],
            )

            with gr.Row():
                duration_minutes = gr.Number(label="Длительность (мин)", value=5)
                language = gr.Dropdown(["ru", "en"], value="ru", label="Язык")
                content_mode = gr.Dropdown(
                    ["documentary", "historical"], value="documentary",
                    label="Режим контента (historical — под Archive.org, см. REFACTOR_PLAN.md)",
                )
            project_id = gr.Textbox(label="Имя проекта (папка в PROJECTS_ROOT)")
            script_btn = gr.Button("Сгенерировать сценарий", variant="primary")
            script_out = gr.Textbox(label="Результат", lines=10)
            script_btn.click(
                run_script_stage,
                inputs=[book_file, book_path, book_text_preview, duration_minutes, language, project_id, content_mode],
                outputs=script_out,
            )

        with gr.Tab("2. Субтитры (GPU)"):
            gr.Markdown(f"Устройство по умолчанию сейчас: **{default_device()}**, "
                        f"модель по умолчанию: **{default_model()}** "
                        "(base на CPU — тайминги, точный текст всё равно из вычитки; large-v3 на GPU)")
            audio_file = gr.File(label="Аудио/видео с озвучкой")
            subs_audio_path = gr.Textbox(label="...или путь на диске (Drive)",
                                          placeholder="/content/drive/MyDrive/video_studio_home/...")
            reference_text_file = gr.File(label="Эталонный текст (narration_text.txt) — опционально, для вычитки")
            reference_text_path = gr.Textbox(label="...или путь на диске (Drive)",
                                              placeholder="/content/drive/MyDrive/video_studio_home/...")
            with gr.Row():
                sub_language = gr.Dropdown(["ru", "en", "Auto"], value="ru", label="Язык")
                sub_model = gr.Dropdown(
                    ["Авто (по устройству)", "large-v3", "medium", "small", "base"],
                    value="Авто (по устройству)", label="Модель whisper",
                )
            subs_btn = gr.Button("Распознать", variant="primary")
            subs_file_out = gr.File(label="Готовый .srt")
            subs_status_out = gr.Textbox(label="Статус")
            subs_btn.click(
                run_subtitles_stage,
                inputs=[audio_file, subs_audio_path, reference_text_file, reference_text_path, sub_language, sub_model],
                outputs=[subs_file_out, subs_status_out],
            )

        with gr.Tab("3. Сцены → Шоты"):
            gr.Markdown(
                "Привязывает реплики сценария к реальным таймингам речи из .srt и разворачивает "
                "каждую сцену в отдельные шоты. Результат (`shots.json`) идёт на вход вкладке «Подбор видео»."
            )
            shots_script_file = gr.File(label="script.json (этап «Сценарий»)")
            shots_script_path = gr.Textbox(label="...или путь на диске (Drive)",
                                            placeholder="/content/drive/MyDrive/video_studio_home/...")
            shots_srt_file = gr.File(label=".srt (этап «Субтитры»)")
            shots_srt_path = gr.Textbox(label="...или путь на диске (Drive)",
                                         placeholder="/content/drive/MyDrive/video_studio_home/...")
            shots_audio_file = gr.File(label="Аудио озвучки — опционально, просто сохраняется как метаданные")
            shots_audio_path = gr.Textbox(label="...или путь на диске (Drive)",
                                           placeholder="/content/drive/MyDrive/video_studio_home/...")
            shots_btn = gr.Button("Развернуть в шоты", variant="primary")
            shots_file_out = gr.File(label="shots.json")
            shots_status_out = gr.Textbox(label="Статус")
            shots_btn.click(
                run_shots_stage,
                inputs=[shots_script_file, shots_script_path, shots_srt_file, shots_srt_path,
                        shots_audio_file, shots_audio_path],
                outputs=[shots_file_out, shots_status_out],
            )

        with gr.Tab("4. Подбор видео"):
            gr.Markdown(
                "Подбор по каждому шоту (Pexels/Pixabay/Archive.org + CLIP-скоринг). "
                "Прогресс сохраняется инкрементально после каждого шота — как и в десктопном "
                "GUI (см. `MatchWorker._save_partial`), при обрыве прогона отдать частично "
                "готовый `footage.json` не проблема."
            )
            shots_file = gr.File(label="shots.json (результат этапа «Сцены → Шоты»)")
            shots_path_input = gr.Textbox(label="...или путь на диске (Drive)",
                                           placeholder="/content/drive/MyDrive/video_studio_home/...")
            match_aspect = gr.Dropdown(["16:9", "9:16"], value="16:9", label="Соотношение сторон")
            match_btn = gr.Button("Подобрать видео", variant="primary")
            match_file_out = gr.File(label="footage.json")
            match_status_out = gr.Textbox(label="Статус")
            match_btn.click(
                run_matching_stage, inputs=[shots_file, shots_path_input, match_aspect],
                outputs=[match_file_out, match_status_out],
            )

        with gr.Tab("5. Сборка"):
            gr.Markdown("Финальная склейка: кроссфейд между клипами, наложение озвучки, прожиг субтитров.")
            footage_file = gr.File(label="footage.json (результат этапа «Подбор видео»)")
            footage_path = gr.Textbox(label="...или путь на диске (Drive)",
                                       placeholder="/content/drive/MyDrive/video_studio_home/...")
            assembly_audio_file = gr.File(label="Аудио озвучки")
            assembly_audio_path = gr.Textbox(label="...или путь на диске (Drive)",
                                              placeholder="/content/drive/MyDrive/video_studio_home/...")
            assembly_subs_file = gr.File(label="Субтитры (.srt/.ass) — опционально, без них прожига не будет")
            assembly_subs_path = gr.Textbox(label="...или путь на диске (Drive)",
                                             placeholder="/content/drive/MyDrive/video_studio_home/...")

            try:
                style_choices = ["(без стиля по умолчанию)"] + [
                    f"{slug}::{name}" for slug, name in style_manager.list_styles()
                ]
            except Exception:
                style_choices = ["(без стиля по умолчанию)"]

            with gr.Row():
                assembly_style = gr.Dropdown(style_choices, value=style_choices[0], label="Стиль субтитров")
                assembly_aspect = gr.Dropdown(["16:9", "9:16"], value="16:9", label="Соотношение сторон")

            assembly_btn = gr.Button("Собрать видео", variant="primary")
            assembly_file_out = gr.File(label="Готовое видео")
            assembly_status_out = gr.Textbox(label="Статус")

            def _run_assembly_wrapper(footage_file, footage_path, audio_file, audio_path,
                                       subs_file, subs_path, style_choice, aspect, progress=gr.Progress()):
                slug = style_choice.split("::", 1)[0] if style_choice and "::" in style_choice else None
                return run_assembly_stage(footage_file, footage_path, audio_file, audio_path,
                                           subs_file, subs_path, slug, aspect, progress=progress)

            assembly_btn.click(
                _run_assembly_wrapper,
                inputs=[footage_file, footage_path, assembly_audio_file, assembly_audio_path,
                        assembly_subs_file, assembly_subs_path, assembly_style, assembly_aspect],
                outputs=[assembly_file_out, assembly_status_out],
            )

    return demo


if __name__ == "__main__":
    app = build_app()
    # allowed_paths обязателен: Gradio по умолчанию отказывается отдавать файлы
    # за пределами текущей рабочей директории/системного /tmp — "InvalidPathError".
    # Раз VIDEO_STUDIO_HOME указывает на Google Drive (/content/drive/MyDrive/...
    # на Colab), а не на cwd, без этого параметра любой .srt/видео из output/
    # (см. asr/transcribe.py, config/paths.py) не скачивался бы через gr.File.
    # Берём VIDEO_STUDIO_HOME из config.paths, а не читаем env заново здесь —
    # чтобы не завести два места с разным дефолтным значением одного и того же пути.
    app.queue().launch(
        share=IN_COLAB, server_name="0.0.0.0" if IN_COLAB else "127.0.0.1",
        allowed_paths=[VIDEO_STUDIO_HOME],
    )
