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

Готовые вкладки (Phase 2, TASK W-01/W-02):
    - Источник → Сценарий   (core/script/book_to_script.py; источник — файл
      .txt ИЛИ дело FBI Vault через scraping/, см. IN_COLAB ниже)
    - Субтитры               (asr/transcribe.py, GPU по умолчанию на Colab)

Остальные стадии (Phase 2, TASK W-03/W-04 — подбор видео, сборка) НЕ
портированы в этом файле — оставлены как заготовки-вкладки с пометкой
TODO, по тому же паттерну адаптера. Портировать их — механическая работа,
не архитектурная (см. пример ниже).
"""
import json
import os
import tempfile

import gradio as gr

from config.paths import PROJECTS_ROOT
from core.script import book_to_script
from asr.transcribe import run_transcribe, run_proofread, default_device, default_model

IN_COLAB = "COLAB_GPU" in os.environ or os.path.exists("/content")


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
    out_dir = tempfile.mkdtemp(prefix="fbi_case_")
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
    out_txt = os.path.join(tempfile.mkdtemp(prefix="fbi_pdfs_"), "combined_source.txt")

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

def run_script_stage(book_file, book_text_preview, duration_minutes, language,
                      project_id, content_mode, progress=gr.Progress()):
    if not project_id:
        raise gr.Error("Укажите имя проекта — по нему создастся папка в PROJECTS_ROOT")

    if book_text_preview and book_text_preview.strip():
        book_text = book_text_preview
    elif book_file is not None:
        with open(book_file.name, "r", encoding="utf-8") as f:
            book_text = f.read()
    else:
        raise gr.Error("Загрузите файл .txt или соберите текст дела на вкладке FBI Vault")

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

def run_subtitles_stage(audio_file, reference_text_file, language, model,
                         progress=gr.Progress()):
    if audio_file is None:
        raise gr.Error("Загрузите аудио/видео с озвучкой")

    device = default_device()
    if not model or model.startswith("Авто"):
        model = default_model(device)
    progress(0.0, desc=f"Устройство: {device}, модель: {model}")

    def on_progress(percent, message):
        progress(min(percent / 100, 0.94), desc=message)

    raw_srt_path = run_transcribe(
        audio_path=audio_file.name, language=language, model=model,
        device=device, on_progress=on_progress,
    )

    if reference_text_file is not None:
        progress(0.95, desc="Сверяю с эталонным текстом...")
        fixed_srt_path = run_proofread(raw_srt_path, reference_text_file.name)
        return fixed_srt_path, f"Готово (устройство: {device}, модель: {model}). Вычитано по эталону."

    return raw_srt_path, f"Готово (устройство: {device}, модель: {model}). Без вычитки — эталонный текст не загружен."


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
            source_kind = gr.Radio(
                ["Файл (.txt)", "Дело FBI Vault — только локально, не на Colab"],
                value="Файл (.txt)", label="Источник текста",
            )

            with gr.Group(visible=True) as file_group:
                book_file = gr.File(label="Текстовый источник (.txt)")

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
                inputs=[book_file, book_text_preview, duration_minutes, language, project_id, content_mode],
                outputs=script_out,
            )

        with gr.Tab("2. Субтитры (GPU)"):
            gr.Markdown(f"Устройство по умолчанию сейчас: **{default_device()}**, "
                        f"модель по умолчанию: **{default_model()}** "
                        "(base на CPU — тайминги, точный текст всё равно из вычитки; large-v3 на GPU)")
            audio_file = gr.File(label="Аудио/видео с озвучкой")
            reference_text_file = gr.File(label="Эталонный текст (narration_text.txt) — опционально, для вычитки")
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


if __name__ == "__main__":
    app = build_app()
    app.queue().launch(share=IN_COLAB, server_name="0.0.0.0" if IN_COLAB else "127.0.0.1")
