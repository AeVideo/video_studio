"""
gui/stage_tabs.py

Вкладки-навигация по этапам пайплайна: Сценарий / Озвучка / Подбор видео /
Монтаж. Это НЕ StatusBar (тот показывает здоровье внешних сервисов —
DeepSeek/Pexels/Pixabay/subtitle_service) — это навигация по состоянию
КОНКРЕТНОГО ПРОЕКТА, отдельная забота, отдельный виджет.

Клик по вкладке возможен только туда, где уже есть что показать (этап
done) — нельзя прыгнуть в «Монтаж» проекта, где ещё не готов сценарий, но
МОЖНО вернуться из Монтажа в уже пройденный «Подбор видео». Раньше такой
навигации не было вообще — отсюда и понадобилась отдельная кнопка «Назад»
на экране Сборки (см. AssemblyScreen.back_to_matching_requested), которую
эти вкладки делают в целом ненужной, но я её не убираю — дополнительный
путь назад не вредит.

Archive.org сюда отдельной вкладкой не выносится — это просто ещё один
источник внутри «Подбор видео», как Pexels/Pixabay.
"""

from PyQt5.QtWidgets import QWidget, QHBoxLayout, QPushButton
from PyQt5.QtCore import pyqtSignal

# Порядок = порядок пайплайна. Ключ соответствует названиям этапов в
# project_manifest.STAGES, кроме "narration" — та вкладка условно
# покрывает и озвучку, и следующую за ней вычитку/транскрипцию (у них нет
# отдельной вкладки, чтобы не плодить шесть вкладок вместо четырёх).
STAGES = [
    ("script", "1. Сценарий"),
    ("narration", "2. Озвучка"),
    ("video_matching", "3. Подбор видео"),
    ("assembly", "4. Монтаж"),
]


class StageTabBar(QWidget):
    stage_clicked = pyqtSignal(str)  # ключ этапа из STAGES

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(4)

        self._buttons = {}
        for key, label in STAGES:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setEnabled(False)  # включаются через set_reachable_stages по мере готовности проекта
            btn.clicked.connect(lambda _checked, k=key: self.stage_clicked.emit(k))
            layout.addWidget(btn)
            self._buttons[key] = btn

        layout.addStretch()
        self.setStyleSheet(
            "QPushButton { padding: 6px 10px; border: none; border-bottom: 3px solid transparent; "
            "background: transparent; } "
            "QPushButton:checked { border-bottom: 3px solid #1976d2; font-weight: bold; } "
            "QPushButton:disabled { color: #aaa; } "
            "QPushButton:!disabled:hover { background-color: #e8e8e8; }"
        )

    def set_reachable_stages(self, reachable: set):
        """reachable — множество ключей этапов, куда можно кликнуть прямо
        сейчас (обычно: всё с status=='done' в манифесте, плюс тот этап,
        на экране которого пользователь находится в данный момент, даже
        если он ещё не завершён)."""
        for key, btn in self._buttons.items():
            btn.setEnabled(key in reachable)

    def set_current_stage(self, stage: str):
        """Подсвечивает активную вкладку. Пустая строка/None — ни одна
        вкладка не соответствует текущему экрану (например, мастер
        создания проекта до появления manifest.json)."""
        for key, btn in self._buttons.items():
            btn.setChecked(key == stage)
