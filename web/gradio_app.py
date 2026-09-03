"""
web/gradio_app.py

Web-интерфейс поверх той же бизнес-логики, что и desktop/*_gui — без
переписывания core/ или asr/. Каждая вкладка — тонкий адаптер, повторяющий
структуру соответствующего QThread-воркера из desktop/*/workers.py:
тот же вызов функций, тот же порядок шагов, только pyqtSignal.emit()
заменён на gr.Progress()/return.

См. COLAB_MIGRATION_PLAN.md, раздел 5 — почему Gradio первый, и раздел 5.2 —
пример адаптера, на основе которого написана вкладка "Сценарий" ниже (сверено
с desktop/video_pipeline_gui/workers.py::ScriptWorker).

Запуск:
    python -m web.gradio_app
Локально откроется http://127.0.0.1:7860 ; на Colab — добавьте share=True
(уже включено ниже, если запущено вне интерактивного локального запуска —
см. IN_COLAB) для публичной ссылки без отдельного деплоя.

Готовые вкладки (Phase 2, TASK W-01/W-02):
    - Источник → Сценарий   (core/script/book_to_script.py)
    - Субтитры               (asr/transcribe.py, GPU по умолчанию на Colab)

Остальные стадии (Phase 2, TASK W-03/W-04 — подбор видео, сборка) НЕ
портированы в этом файле — оставлены как заготовки-вкладки с пометкой
TODO, по тому же паттерну адаптера. Портировать их — механическая работа,
не архитектурная (см. пример ниже).
"""
import json
import os

import gradio as gr

from config.paths import PROJECTS_ROOT
from core.script import book_to_script
from asr.transcribe import run_transcribe, run_proofread, default_device


# ---------------------------------------------------------------------------
# Вкладка 1: Источник -> Сценарий
# Соответствует desktop/video_pipeline_gui/workers.py::ScriptWorker.run()
# Логика 1:1 та же, просто pyqtSignal -> gr.Progress()/return.
# ---------------------------------------------------------------------------

def run_script_stage(book_file, duration_minutes, language, project_id, content_mode,
                      progress=gr.Progress()):
    if book_file is None:
        raise gr.Error("Загрузите текстовый источник (.txt)")
    if not project_id:
        raise gr.Error("Укажите имя проекта — по нему создастся папка в PROJECTS_ROOT")

    out_dir = os.path.join(PROJECTS_ROOT, project_id)
    os.makedirs(out_dir, exist_ok=True)

    progress(0.05, desc="Разбиваю источник на истории...")
    client = book_to_script.get_client()
    with open(book_file.name, "r", encoding="utf-8") as f:
        book_text = f.read()

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

def run_subtitles_stage(audio_file, reference_text_file, language, model,
                         progress=gr.Progress()):
    if audio_file is None:
        raise gr.Error("Загрузите аудио/видео с озвучкой")

    device = default_device()
    progress(0.0, desc=f"Устройство: {device}")

    def on_progress(percent, message):
        progress(min(percent / 100, 0.94), desc=message)

    raw_srt_path = run_transcribe(
        audio_path=audio_file.name, language=language, model=model,
        device=device, on_progress=on_progress,
    )

    if reference_text_file is not None:
        progress(0.95, desc="Сверяю с эталонным текстом...")
        fixed_srt_path = run_proofread(raw_srt_path, reference_text_file.name)
        return fixed_srt_path, f"Готово (устройство: {device}). Вычитано по эталону."

    return raw_srt_path, f"Готово (устройство: {device}). Без вычитки — эталонный текст не загружен."


# ---------------------------------------------------------------------------
# Сборка интерфейса
# ---------------------------------------------------------------------------

def build_app() -> gr.Blocks:
    with gr.Blocks(title="video_studio") as demo:
        gr.Markdown(
            "# video_studio\n"
            "Web-интерфейс поверх той же логики, что и десктопные GUI. "
            "Подбор видео и сборка — следующие вкладки (TASK W-03/W-04, см. COLAB_MIGRATION_PLAN.md)."
        )

        with gr.Tab("1. Источник → Сценарий"):
            book_file = gr.File(label="Текстовый источник (.txt)")
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
                inputs=[book_file, duration_minutes, language, project_id, content_mode],
                outputs=script_out,
            )

        with gr.Tab("2. Субтитры (GPU)"):
            gr.Markdown(f"Устройство по умолчанию сейчас: **{default_device()}** "
                        "(автоопределение — cuda, если доступна, иначе cpu)")
            audio_file = gr.File(label="Аудио/видео с озвучкой")
            reference_text_file = gr.File(label="Эталонный текст (narration_text.txt) — опционально, для вычитки")
            with gr.Row():
                sub_language = gr.Dropdown(["ru", "en", "Auto"], value="ru", label="Язык")
                sub_model = gr.Dropdown(
                    ["large-v3", "medium", "small"], value="large-v3", label="Модель whisper",
                )
            subs_btn = gr.Button("Распознать", variant="primary")
            subs_file_out = gr.File(label="Готовый .srt")
            subs_status_out = gr.Textbox(label="Статус")
            subs_btn.click(
                run_subtitles_stage,
                inputs=[audio_file, reference_text_file, sub_language, sub_model],
                outputs=[subs_file_out, subs_status_out],
            )

        with gr.Tab("3. Подбор видео (TODO — TASK W-03)"):
            gr.Markdown(
                "Заготовка. По тому же паттерну, что вкладки 1-2: адаптер вокруг "
                "`desktop/video_pipeline_gui/workers.py::MatchWorker`, вызывающий "
                "`core.video_sources.pexels_matcher` / `archive_org_search` напрямую. "
                "ClipScorer уже кэшируется на уровне класса — просто переиспользовать "
                "тот же инстанс между вызовами Gradio, не пересоздавать на каждый клик."
            )

        with gr.Tab("4. Сборка (TODO — TASK W-04)"):
            gr.Markdown(
                "Заготовка. Адаптер вокруг `AssembleWorker` -> "
                "`core.assembly.assemble_video`. Скачиваемый результат — через `gr.File`, "
                "как в вкладке «Субтитры» выше."
            )

    return demo


IN_COLAB = "COLAB_GPU" in os.environ or os.path.exists("/content")

if __name__ == "__main__":
    app = build_app()
    app.queue().launch(share=IN_COLAB, server_name="0.0.0.0" if IN_COLAB else "127.0.0.1")
