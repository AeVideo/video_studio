# video_studio

Объединённый проект **video-pipeline** (сценарий → сцены → подбор видео →
сборка) и **openai-whisper** (ASR/субтитры/прожиг) под одной крышей.

История объединения и обоснование каждого решения — в
`COLAB_MIGRATION_PLAN.md` (структура, GPU-перенос, web UI) и
`REFACTOR_PLAN.md` (Flow/Qwen-провайдеры, generation queue — отдельная,
не пересекающаяся с этим переносом работа).

## Структура

```text
core/       — бизнес-логика video-pipeline (сценарий, сцены, подбор видео,
              сборка, аккаунты) — без Qt
asr/        — бизнес-логика ASR/субтитров (бывший openai-whisper) — без Qt
scraping/   — Camoufox/Playwright браузерная автоматизация (FBI Vault и т.п.)
services/   — HTTP-обвязка (subtitle_api.py) — нужна только в "режиме B"
              (отдельный процесс/машина), для Colab/локально не обязательна
web/        — Gradio/Streamlit/Flask интерфейсы (см. COLAB_MIGRATION_PLAN.md)
desktop/    — оба существующих PyQt5 GUI, как один из адаптеров поверх core/+asr/
config/     — config/paths.py — единая точка путей (VIDEO_STUDIO_HOME)
storage/    — данные (не в git) — projects/output/logs/models_cache
notebooks/  — Colab setup-ноутбук
```

## Установка — локальная машина (CPU)

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
# --extra-index-url ОБЯЗАТЕЛЕН: torch==...+cpu не резолвится с обычного PyPI
pip install -r requirements-cpu.txt --extra-index-url https://download.pytorch.org/whl/cpu
pip install -e . --no-deps       # editable install, зависимости уже поставлены выше
playwright install firefox       # для scraping/ (CamoufoxSession)
camoufox fetch                   # докачивает патченный браузер Camoufox

cp .env.example .env
# заполните .env реальными ключами (DEEPSEEK_API_KEY, PEXELS_API_KEY, ...)
```

Запуск существующих PyQt-приложений — как раньше, просто из нового пути:

```bash
python -m desktop.video_pipeline_gui.app
python -m desktop.whisper_gui.app
```

## Установка — Google Colab (T4)

Смотрите `notebooks/colab_setup.ipynb` — первая ячейка монтирует Google
Drive, ставит `requirements-gpu.txt` + CUDA-сборку torch отдельной
командой, клонирует репозиторий и делает editable install. Подробности и
обоснование — в `COLAB_MIGRATION_PLAN.md`, раздел 4.

## Web UI

`web/gradio_app.py` — Phase 1 web-интерфейса, повторяет вкладки существующего
PyQt GUI (`StageTabBar`), но подключается через тонкие адаптеры к тем же
функциям в `core/`/`asr/` (см. COLAB_MIGRATION_PLAN.md, раздел 5.2 — пример
адаптера для стадии "Сценарий").
