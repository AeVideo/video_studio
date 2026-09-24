"""
core/flow/actions.py

UI-автоматизация flow.google.com через Playwright + Camoufox.
Реализует generate_video() и вспомогательные функции.

ВАЖНО:
- .ProseMirror contenteditable — НЕ textarea, .fill() не работает,
  только keyboard.insert_text() или keyboard.type()
- rpcids (MZZa6b / jwpduf / as29s) меняются между билдами Flow —
  НЕ хардкодятся, определяются по структуре args/response (раздел 5.5)
- video_url берётся из as29s response [7][0][8], НЕ через редактор
- Listener устанавливается ДО клика Generate
"""

import json
import logging
import os
import re
import time
from typing import Optional

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
# Константы селекторов (из раздела 3 FLOW_INTEGRATION.md)
# ------------------------------------------------------------------ #

_SEL_PROMPT_BOX    = "flow-base-prompt-box .base-prompt-box"
_SEL_PROSE_MIRROR  = '.ProseMirror[contenteditable="true"]'
_SEL_CLEAR_BTN     = 'button[aria-label="Очистить запрос"]'
_SEL_GENERATE_BTN  = 'button[aria-label="Начать генерацию"]'
_SEL_SETTINGS_BTN  = "button.settings-trigger-button"
_SEL_SETTINGS_OVL  = "flow-prompt-box-settings.settings-content-overlay"
_SEL_MODEL_BTN     = 'button[aria-label="Выбрать семейство моделей"]'
_SEL_MODEL_PANEL   = ".flow-model-picker-panel"

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.I,
)


def _is_uuid(s) -> bool:
    return isinstance(s, str) and bool(_UUID_RE.match(s))


# ------------------------------------------------------------------ #
# Динамическое определение методов batchexecute (раздел 5.5)
# ------------------------------------------------------------------ #

def classify_batchexecute(args_json, response_payload) -> Optional[str]:
    """
    Определяет тип batchexecute-вызова по структуре args и response.
    Возвращает: "generate" | "poll" | "detail" | None

    НЕ полагается на rpcids — они меняются между билдами.
    """
    # Generation: args[0][2] содержит "veo" или "omni" (model_id)
    try:
        if (isinstance(args_json, list) and len(args_json) >= 1
                and isinstance(args_json[0], list) and len(args_json[0]) >= 3
                and isinstance(args_json[0][2], str)
                and ("veo" in args_json[0][2] or "omni" in args_json[0][2])):
            return "generate"
    except (IndexError, TypeError):
        pass

    # Detail: args = ["<task_id_uuid>"], response[0] = task_id uuid
    try:
        if (isinstance(args_json, list) and len(args_json) == 1
                and _is_uuid(args_json[0])
                and isinstance(response_payload, list)
                and len(response_payload) >= 3
                and _is_uuid(response_payload[0])):
            return "detail"
    except (IndexError, TypeError):
        pass

    # Poll: args = [null, null, [[media_id], ...]]
    try:
        if (isinstance(args_json, list) and len(args_json) >= 3
                and args_json[0] is None and args_json[1] is None
                and isinstance(args_json[2], list) and args_json[2]
                and isinstance(args_json[2][0], list)):
            return "poll"
    except (IndexError, TypeError):
        pass

    return None


# ------------------------------------------------------------------ #
# Парсинг batchexecute response stream (раздел 5.6)
# ------------------------------------------------------------------ #

def parse_batchexecute_lines(text: str):
    """Парсит поток batchexecute, yield-ит (rpcid, args_json, payload)."""
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith('[["wrb.fr"'):
            continue
        try:
            envelope = json.loads(line)
        except json.JSONDecodeError:
            continue
        for item in envelope:
            if not (isinstance(item, list) and len(item) >= 3
                    and item[0] == "wrb.fr"):
                continue
            rpcid = item[1]
            try:
                payload = json.loads(item[2])
            except (json.JSONDecodeError, TypeError):
                payload = item[2]
            # Попробуем восстановить args из f.req если он есть рядом
            yield rpcid, None, payload


def extract_f_req_args(post_data: str):
    """Из body f.req=... достаёт (rpcid, args_parsed)."""
    from urllib.parse import parse_qs
    if not post_data or not post_data.startswith("f.req="):
        return None
    try:
        parsed = parse_qs(post_data)
        f_req = parsed.get("f.req", [""])[0]
        outer = json.loads(f_req)
        inner = outer[0][0]
        rpcid = inner[0]
        args = json.loads(inner[1]) if isinstance(inner[1], str) else inner[1]
        return rpcid, args
    except (json.JSONDecodeError, IndexError, TypeError, KeyError):
        return None


# ------------------------------------------------------------------ #
# Response listener (раздел 5.7)
# ------------------------------------------------------------------ #

def _build_response_listener(state: dict, req_args_cache: dict):
    """
    Возвращает on_response handler для page.on("response", ...).
    state — общий dict: media_id, task_id, video_url, duration_sec, error.
    req_args_cache — dict url -> args_parsed, заполняется on_request handler-ом.
    """
    def on_response(response):
        if "batchexecute" not in response.url:
            return
        try:
            text = response.text()
        except Exception:
            return

        # Ищем cached args для этого URL (из on_request)
        cached_args = req_args_cache.get(response.url)

        for rpcid, _, payload in parse_batchexecute_lines(text):
            if payload is None:
                continue
            try:
                kind = classify_batchexecute(cached_args, payload)

                # --- Generation response: media_id + task_id ---
                if (kind == "generate" or (
                    isinstance(payload, list) and len(payload) >= 3
                    and payload[1] == 30
                    and isinstance(payload[2], list) and payload[2]
                )):
                    block = payload[2][0] if isinstance(payload[2], list) and payload[2] else None
                    if isinstance(block, list) and len(block) >= 4:
                        if _is_uuid(block[0]) and not state.get("media_id"):
                            state["media_id"] = block[0]
                            logger.info(f"Flow: media_id={block[0]}")
                        if (isinstance(block[3], list) and len(block[3]) >= 5
                                and _is_uuid(block[3][4])
                                and not state.get("task_id")):
                            state["task_id"] = block[3][4]
                            logger.info(f"Flow: task_id={block[3][4]}")

                # --- Detail response: video_url в [7][0][8] ---
                if (kind == "detail" or (
                    isinstance(payload, list) and len(payload) >= 8
                    and _is_uuid(payload[0])
                    and isinstance(payload[7], list) and payload[7]
                )):
                    try:
                        url = payload[7][0][8]
                        if isinstance(url, str) and url.startswith("http") and not state.get("video_url"):
                            state["video_url"] = url
                            logger.info(f"Flow: video_url получен")
                        # duration_sec из [7][1][2][0]
                        if (len(payload[7]) > 1
                                and isinstance(payload[7][1], list)
                                and len(payload[7][1]) > 2
                                and isinstance(payload[7][1][2], list)
                                and payload[7][1][2]
                                and state.get("duration_sec") is None):
                            state["duration_sec"] = payload[7][1][2][0]
                    except (IndexError, TypeError):
                        pass

            except (IndexError, TypeError, KeyError):
                pass

    return on_response


def _build_request_listener(req_args_cache: dict):
    """on_request handler — кэширует args по URL для последующего classify."""
    def on_request(request):
        if "batchexecute" not in request.url:
            return
        try:
            post_data = request.post_data or ""
            result = extract_f_req_args(post_data)
            if result:
                _rpcid, args = result
                req_args_cache[request.url] = args
        except Exception:
            pass
    return on_request


# ------------------------------------------------------------------ #
# Вспомогательные UI-действия (раздел 3, 6)
# ------------------------------------------------------------------ #

def _open_settings(page) -> None:
    """Клик по кнопке настроек — открывает оверлей."""
    btn = page.locator(_SEL_SETTINGS_BTN)
    btn.wait_for(state="visible", timeout=10_000)
    btn.click()
    page.locator(_SEL_SETTINGS_OVL).wait_for(state="visible", timeout=5_000)
    logger.debug("Flow: настройки открыты")


def _close_settings(page) -> None:
    """Escape закрывает оверлей настроек."""
    page.keyboard.press("Escape")
    page.locator(_SEL_SETTINGS_OVL).wait_for(state="hidden", timeout=3_000)
    logger.debug("Flow: настройки закрыты")


def _set_mode_video(page) -> None:
    """Переключает тип генерации в режим Видео."""
    toggle = page.locator(
        'flow-toggles[aria-label="Режим"] .toggle',
    ).filter(has_text="Видео")
    toggle.click()
    logger.debug("Flow: режим=Видео")


def _set_video_type(page, vtype: str) -> None:
    """vtype: 'frames' (Кадры) или 'ingredients' (Ингредиенты)."""
    label = "Кадры" if vtype == "frames" else "Ингредиенты"
    page.locator(
        f'flow-toggles[aria-label="Тип видео"] .toggle'
    ).filter(has_text=label).click()
    logger.debug(f"Flow: тип видео={label}")


def _set_model(page, model_name: str) -> None:
    """
    Кликает кнопку выбора модели и выбирает нужную из панели.
    model_name — строка как она видна в UI, напр. 'Veo 3.1 Lite'.
    """
    page.locator(_SEL_MODEL_BTN).click()
    panel = page.locator(_SEL_MODEL_PANEL)
    panel.wait_for(state="visible", timeout=5_000)
    panel.locator(f"text={model_name}").first.click()
    logger.debug(f"Flow: модель={model_name}")


def _set_aspect(page, aspect: str) -> None:
    """aspect: '16:9' или '9:16'."""
    page.locator(
        f'flow-toggles[aria-label="Соотношение сторон"] .toggle'
    ).filter(has_text=aspect).click()
    logger.debug(f"Flow: aspect={aspect}")


def _set_quantity(page, n: int) -> None:
    """n: 1..4."""
    page.locator(
        f'flow-toggles[aria-label="Количество результатов"] .toggle'
    ).filter(has_text=f"x{n}").click()
    logger.debug(f"Flow: quantity={n}")


def _fill_prompt(page, text: str) -> None:
    """
    Вводит текст в ProseMirror contenteditable.
    .fill() НЕ работает — только keyboard.insert_text().
    """
    editor = page.locator(_SEL_PROSE_MIRROR)
    editor.wait_for(state="visible", timeout=10_000)
    editor.click()
    # Сначала очищаем если что-то есть
    page.keyboard.press("Control+a")
    page.keyboard.press("Delete")
    page.keyboard.insert_text(text)
    logger.debug(f"Flow: промпт введён ({len(text)} символов)")


def _attach_first_frame(page, image_path: str) -> None:
    """
    Прикрепляет изображение как первый кадр.
    Открывает модалку, загружает файл через input[type=file].
    """
    # Клик по пустому chip "Первый"
    chip = page.locator("flow-ingredient-bar button.empty-chip").first
    chip.wait_for(state="visible", timeout=5_000)
    chip.click()

    # Ждём модалку
    popover = page.locator("flow-add-menu-popover-content")
    popover.wait_for(state="visible", timeout=5_000)

    # Upload через input[type=file] появляется после клика на sidebar-upload-btn
    upload_btn = popover.locator(".sidebar-upload-btn")
    with page.expect_file_chooser(timeout=5_000) as fc_info:
        upload_btn.click()
    file_chooser = fc_info.value
    file_chooser.set_files(image_path)

    # Ждём превью и подтверждаем
    popover.locator(".detail-preview-image").wait_for(state="visible", timeout=15_000)
    popover.locator(".detail-add-to-prompt-btn").click()
    logger.debug(f"Flow: первый кадр прикреплён: {image_path}")


def _click_generate(page) -> None:
    btn = page.locator(_SEL_GENERATE_BTN)
    btn.wait_for(state="enabled", timeout=10_000)
    btn.click()
    logger.info("Flow: нажата кнопка генерации")


def _wait_for_state(state: dict, key: str, timeout_seconds: int,
                    poll_interval: float = 0.5) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if state.get(key):
            return
        if state.get("error"):
            raise RuntimeError(f"Flow: ошибка генерации: {state['error']}")
        time.sleep(poll_interval)
    raise TimeoutError(
        f"Flow: {key} не получен за {timeout_seconds}с "
        f"(media_id={state.get('media_id')}, task_id={state.get('task_id')})"
    )


def _download_video(context, video_url: str, output_path: str) -> None:
    """
    Скачивает видео напрямую через signed URL.
    context.request использует тот же cookie jar — TLS не проблема.
    Fallback через page.goto если CDN даёт 403.
    """
    logger.info(f"Flow: скачиваю видео → {output_path}")
    try:
        response = context.request.get(video_url, timeout=300_000)
        if not response.ok:
            raise RuntimeError(f"HTTP {response.status}")
        with open(output_path, "wb") as f:
            f.write(response.body())
        logger.info(f"Flow: скачано {os.path.getsize(output_path)} байт")
    except Exception as e:
        logger.warning(f"Flow: context.request упал ({e}), пробую fallback через page.goto")
        page = context.new_page()
        try:
            with page.expect_download(timeout=300_000) as dl_info:
                page.goto(video_url)
            dl_info.value.save_as(output_path)
        finally:
            page.close()


# ------------------------------------------------------------------ #
# Главная функция (раздел 6)
# ------------------------------------------------------------------ #

def generate_video(
    page,
    prompt: str,
    model: str = "veo_3_1_r2v_lite",
    model_ui_name: str = "Veo 3.1 Lite",
    aspect: str = "16:9",
    quantity: int = 1,
    first_frame_path: Optional[str] = None,
    output_dir: str = ".",
    timeout_seconds: int = 900,
    project_id: Optional[str] = None,
) -> dict:
    """
    Полный цикл генерации видео через Flow UI.

    Параметры:
        page           — Playwright Page (из CamoufoxSession)
        prompt         — текстовый промпт
        model          — model_id для batchexecute ('veo_3_1_r2v_lite')
        model_ui_name  — название модели как в UI ('Veo 3.1 Lite')
        aspect         — '16:9' или '9:16'
        quantity       — 1..4
        first_frame_path — путь к изображению первого кадра (опц.)
        output_dir     — куда сохранить MP4
        timeout_seconds — таймаут ожидания video_url
        project_id     — UUID проекта (если None — переходим на главную)

    Возвращает:
        {
            "video_path":   str,
            "media_id":     str,
            "task_id":      str,
            "video_url":    str,   # signed ~7 дней
            "duration_sec": int,   # из [7][1][2][0]
        }
    """
    os.makedirs(output_dir, exist_ok=True)

    # 1. Переходим на нужный проект или главную страницу
    if project_id:
        target_url = f"https://flow.google.com/project/{project_id}"
    else:
        target_url = "https://flow.google.com/"
    logger.info(f"Flow: переходим на {target_url}")
    page.goto(target_url, wait_until="domcontentloaded", timeout=30_000)

    # Ждём появления prompt box
    page.locator(_SEL_PROMPT_BOX).wait_for(state="visible", timeout=30_000)

    # 2. Открываем настройки и выставляем параметры
    _open_settings(page)
    _set_mode_video(page)

    if first_frame_path:
        _set_video_type(page, "frames")
    else:
        _set_video_type(page, "ingredients")

    _set_model(page, model_ui_name)
    _set_aspect(page, aspect)
    _set_quantity(page, quantity)
    _close_settings(page)

    # 3. Прикрепляем первый кадр (опционально)
    if first_frame_path:
        _attach_first_frame(page, first_frame_path)

    # 4. Вводим промпт
    _fill_prompt(page, prompt)

    # 5. Устанавливаем listener ДО клика Generate
    state = {
        "media_id":    None,
        "task_id":     None,
        "video_url":   None,
        "duration_sec": None,
        "error":       None,
    }
    req_args_cache = {}

    page.on("request", _build_request_listener(req_args_cache))
    page.on("response", _build_response_listener(state, req_args_cache))

    # 6. Кликаем Generate
    _click_generate(page)

    # 7. Ждём media_id (подтверждение что задача принята)
    logger.info("Flow: ожидаю media_id...")
    _wait_for_state(state, "media_id", timeout_seconds=60)
    logger.info(f"Flow: задача принята, media_id={state['media_id']}")

    # 8. Ждём video_url (генерация завершена)
    logger.info(f"Flow: ожидаю video_url (timeout={timeout_seconds}s)...")
    _wait_for_state(state, "video_url", timeout_seconds=timeout_seconds)

    # 9. Скачиваем видео
    filename = f"flow_{state['media_id'][:8]}.mp4"
    output_path = os.path.join(output_dir, filename)
    _download_video(page.context, state["video_url"], output_path)

    result = {
        "video_path":   output_path,
        "media_id":     state["media_id"],
        "task_id":      state["task_id"],
        "video_url":    state["video_url"],
        "duration_sec": state["duration_sec"],
    }
    logger.info(f"Flow: готово → {result}")
    return result
