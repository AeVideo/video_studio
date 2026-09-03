"""
font_manager.py
Регистрирует встроенные шрифты приложения (fonts/*.ttf) в системе шрифтов Qt,
чтобы они появлялись в QFontComboBox наравне с системными — без установки
в ОС пользователя.

Нужно вызывать один раз при старте, после создания QApplication
(QFontDatabase требует существующий экземпляр приложения).
"""
import os
import sys

from PyQt5.QtGui import QFontDatabase

if getattr(sys, "frozen", False):
    BUNDLED_FONTS_DIR = os.path.join(sys._MEIPASS, "fonts")
else:
    BUNDLED_FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")


def register_bundled_fonts():
    """Регистрирует все .ttf/.otf из BUNDLED_FONTS_DIR. Не бросает исключений
    наружу — отсутствие папки со шрифтами не должно ронять приложение,
    в худшем случае просто не будет доп. шрифтов в списке."""
    if not os.path.isdir(BUNDLED_FONTS_DIR):
        return []

    registered = []
    for fname in os.listdir(BUNDLED_FONTS_DIR):
        if not fname.lower().endswith((".ttf", ".otf")):
            continue
        path = os.path.join(BUNDLED_FONTS_DIR, fname)
        font_id = QFontDatabase.addApplicationFont(path)
        if font_id == -1:
            continue
        families = QFontDatabase.applicationFontFamilies(font_id)
        registered.extend(families)
    return registered
