# FLOW_INTEGRATION.md (v4) — FINAL

Инструкция для AI-агента: интеграция Flow.google.com в video_studio.
v4 — полная Network-спека (MZZa6b / jwpduf / as29s), скачивание без редактора.

## 0. КРИТИЧНО: сначала прочитай существующий код

ПЕРЕД любой работой:

1. `core/accounts/account_manager.py` — уже готов для Flow-аккаунтов
2. `core/flow/browser_session.py` — обёртка Camoufox
3. `core/flow/executor.py` — FlowExecutor.execute_with_retry(task_fn)
4. `core/flow/flow_video_worker.py` — целевая архитектура (stub)
5. `scraping/cloudflare_session.py` — образец прод-скрейпера
6. `pyproject.toml`
7. `desktop/video_pipeline_gui/` — табы и диалоги

Отчёт (10-15 строк): паттерны, табы, диалоги, VIDEO_STUDIO_HOME.
Только ПОСЛЕ отчёта — код.

## 1. Что НЕ делать

- ❌ НЕ переписывать `core/accounts/account_manager.py`
- ❌ НЕ создавать `FlowAccount` class — аккаунт это dict
- ❌ НЕ писать `browser_session.py` / `executor.py`
- ❌ НЕ воскрешать `flow_account_rotator.py`
- ❌ НЕ писать httpx-клиент к Google — только Camoufox
- ❌ НЕ трогать `scraping/cloudflare_session.py`, `web/`
- ❌ НЕ встраивать Camoufox в PyQt через QWebEngine
- ❌ **НЕ хардкодить `rpcids=MZZa6b` / `jwpduf` / `as29s`** —
  см. раздел 5.2 (они меняются между билдами)
- ❌ **НЕ ходить через редактор для скачивания** — video_url берётся
  напрямую из `as29s` response

## 2. Что создать

### 2.1 `core/flow/actions.py` — главный файл

```python
def generate_video(
    page,
    prompt: str,
    model: str = "veo_3_1_r2v_lite",
    aspect: str = "16:9",
    quantity: int = 1,
    first_frame_path: str | None = None,
    output_dir: str = ".",
    timeout_seconds: int = 900,
) -> dict:
    """
    Возвращает:
      {
        "video_path": str,
        "media_id": str,
        "task_id": str,
        "video_url": str,     # signed, ~7 дней
        "duration_sec": int,  # из [7][1][2][0]
      }
    """