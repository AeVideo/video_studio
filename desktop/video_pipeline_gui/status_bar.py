"""
gui/status_bar.py

Панель готовности сервисов — по образцу референсного инструмента:
одним взглядом видно, что подключено, а что нет, вместо того чтобы
узнавать о проблеме только когда упадёт воркер посреди работы.
"""

import os

import requests
from PyQt5.QtWidgets import QWidget, QHBoxLayout, QLabel
from PyQt5.QtCore import Qt

SUBTITLE_SERVICE_URL = "http://127.0.0.1:8800"


class StatusIndicator(QWidget):
    def __init__(self, label: str, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 12, 2)
        self.dot = QLabel("●")
        self.dot.setStyleSheet("color: #999;")
        self.text = QLabel(label)
        layout.addWidget(self.dot)
        layout.addWidget(self.text)

    def set_ok(self, detail: str = ""):
        self.dot.setStyleSheet("color: #2e7d32;")  # зелёный
        self.text.setText(f"{self.text.text().split(':')[0]}: {detail or 'готов'}")

    def set_error(self, detail: str = "не настроен"):
        self.dot.setStyleSheet("color: #c62828;")  # красный
        self.text.setText(f"{self.text.text().split(':')[0]}: {detail}")


class StatusBar(QWidget):
    """Одна строка сверху окна — статус всех внешних зависимостей проекта."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)

        self.deepseek = StatusIndicator("DeepSeek: —")
        self.pexels = StatusIndicator("Pexels: —")
        self.pixabay = StatusIndicator("Pixabay: —")
        self.subtitles = StatusIndicator("Субтитры: —")

        for w in [self.deepseek, self.pexels, self.pixabay, self.subtitles]:
            layout.addWidget(w)
        layout.addStretch()

        self.setStyleSheet("background-color: #f2f2f2; border-bottom: 1px solid #ddd;")

    def refresh(self):
        """Проверяет наличие ключей и доступность subtitle_service. Быстрая —
        сеть дёргает только для subtitle_service (локальный /health, не внешний API)."""
        if os.environ.get("DEEPSEEK_API_KEY"):
            self.deepseek.set_ok()
        else:
            self.deepseek.set_error()

        if os.environ.get("PEXELS_API_KEY"):
            self.pexels.set_ok()
        else:
            self.pexels.set_error()

        if os.environ.get("PIXABAY_API_KEY"):
            self.pixabay.set_ok()
        else:
            self.pixabay.set_error("не задан (опционально)")

        try:
            r = requests.get(f"{SUBTITLE_SERVICE_URL}/health", timeout=1)
            if r.status_code == 200:
                self.subtitles.set_ok("запущен")
            else:
                self.subtitles.set_error("не отвечает")
        except requests.RequestException:
            self.subtitles.set_error("не запущен")
