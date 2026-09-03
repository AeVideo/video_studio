"""
asr/bridge.py

До объединения репозиториев make_subs.py и style_manager.py физически лежали
в отдельном проекте (openai-whisper) и подключались через sys.path-хак
(oasis_studio_bridge.py, video-pipeline). Теперь оба модуля — часть одного
пакета video_studio, поэтому это обычный прямой import, а не мост через
переменную окружения и путь на диске.

Публичный интерфейс сохранён 1:1 с прежним oasis_studio_bridge.py, чтобы
core/assembly/assemble_video.py и desktop/video_pipeline_gui/* не пришлось
переписывать — только сменить источник импорта на `from asr import bridge
as oasis_studio_bridge` (или `from asr import bridge`).
"""
import os

from asr import make_subs      # noqa: F401  (реэкспортируется как .make_subs)
from asr import style_manager  # noqa: F401  (используется list_style_presets)
from asr import font_manager

BUNDLED_FONTS_DIR = None
if os.path.isdir(font_manager.BUNDLED_FONTS_DIR):
    BUNDLED_FONTS_DIR = font_manager.BUNDLED_FONTS_DIR


def is_available() -> bool:
    """Раньше могло быть False, если sys.path-хак не находил каталог с
    исходниками. Теперь make_subs/style_manager — часть того же пакета,
    поэтому это всегда True; функция оставлена для обратной совместимости
    вызывающего кода (assemble_video.py, main_window.py)."""
    return make_subs is not None and style_manager is not None


def error_message() -> str:
    return ""


def list_style_presets():
    return style_manager.list_styles()
