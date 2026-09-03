"""
style_manager.py
Управление JSON-пресетами стилей субтитров.

Два источника пресетов:
- BUNDLED_STYLES_DIR — заводские пресеты, поставляются с приложением,
  только для чтения (в frozen-сборке это временная _MEIPASS, пересоздаётся
  при каждом запуске — писать туда нельзя).
- USER_STYLES_DIR — постоянная папка в домашней директории пользователя,
  переживает перезапуски и обновления приложения. Сюда пишутся все
  пользовательские сохранения.

При совпадении slug пользовательский пресет перекрывает заводской
(в list_styles/load_style), но заводской файл при этом не удаляется.

Схема пресета (все поля опциональны — отсутствующие берутся из авто-расчёта
по геометрии видео, см. make_subs.compute_default_style):
{
    "name": "Человекочитаемое имя",
    "font_name": "Arial",
    "font_size_fixed": null,        # если задано — используется вместо авто-расчёта
    "font_size_factor": null,       # множитель к высоте видео (авто-режим)
    "primary_color": "&H00FFFFFF",  # цвет текста (ASS BGR формат)
    "secondary_color": "&H0000FFFF",# цвет ещё непроизнесённого слова (караоке)
    "outline_color": "&HB0000000",
    "back_color": "&HB0000000",
    "bold": true,
    "alignment": 2,                 # 1=низ-лево, 2=низ-центр, 3=низ-право, 5=центр экрана
    "margin_l": 30,
    "margin_r": 30,
    "margin_v_fixed": null,
    "margin_v_factor": null,
    "outline_fixed": null,
    "outline_factor": null,
    "karaoke": true                 # true = слово подсвечивается по мере произношения (\\k)
                                     # false = вся фраза появляется сразу
}
"""
import json
import os
import sys
import re

if getattr(sys, "frozen", False):
    BUNDLED_STYLES_DIR = os.path.join(sys._MEIPASS, "styles")
else:
    BUNDLED_STYLES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "styles")

from config.paths import USER_STYLES_DIR

# Обратная совместимость: старый код (если где-то ещё используется напрямую)
# видит STYLES_DIR как пользовательскую папку — именно туда должна идти запись.
STYLES_DIR = USER_STYLES_DIR

_SAFE_NAME_RE = re.compile(r'[^a-zA-Z0-9А-Яа-яЁё _-]+')


def _slugify(name: str) -> str:
    """Превращает человекочитаемое имя в безопасное имя файла."""
    cleaned = _SAFE_NAME_RE.sub('', name).strip().replace(' ', '_')
    return cleaned or "style"


def ensure_styles_dir():
    """Создаёт пользовательскую папку пресетов, если её ещё нет."""
    os.makedirs(USER_STYLES_DIR, exist_ok=True)


def list_styles():
    """
    Возвращает список (slug, display_name), отсортированный по имени.
    Объединяет заводские и пользовательские пресеты; при совпадении slug
    пользовательский побеждает.
    """
    ensure_styles_dir()
    merged = {}

    if os.path.isdir(BUNDLED_STYLES_DIR):
        for fname in os.listdir(BUNDLED_STYLES_DIR):
            if not fname.endswith(".json"):
                continue
            slug = fname[:-5]
            try:
                data = _load_from_dir(BUNDLED_STYLES_DIR, slug)
                merged[slug] = data.get("name", slug)
            except (json.JSONDecodeError, OSError):
                continue

    for fname in os.listdir(USER_STYLES_DIR):
        if not fname.endswith(".json"):
            continue
        slug = fname[:-5]
        try:
            data = _load_from_dir(USER_STYLES_DIR, slug)
            merged[slug] = data.get("name", slug)
        except (json.JSONDecodeError, OSError):
            continue

    result = list(merged.items())
    result.sort(key=lambda x: x[1].lower())
    return result


def _load_from_dir(directory: str, slug: str) -> dict:
    path = os.path.join(directory, f"{slug}.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_style(slug: str) -> dict:
    """Ищет сначала в пользовательской папке, потом в заводской."""
    user_path = os.path.join(USER_STYLES_DIR, f"{slug}.json")
    if os.path.exists(user_path):
        return _load_from_dir(USER_STYLES_DIR, slug)
    return _load_from_dir(BUNDLED_STYLES_DIR, slug)


def save_style(slug: str, data: dict):
    """Сохраняет пресет. Если slug пуст — генерирует его из data['name'].
    Всегда пишет в постоянную пользовательскую папку."""
    ensure_styles_dir()
    if not slug:
        slug = _slugify(data.get("name", "style"))
    path = os.path.join(USER_STYLES_DIR, f"{slug}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return slug


def delete_style(slug: str):
    """Удаляет только пользовательский пресет. Заводские не трогает."""
    path = os.path.join(USER_STYLES_DIR, f"{slug}.json")
    if os.path.exists(path):
        os.remove(path)


def slugify(name: str) -> str:
    return _slugify(name)
