from PyQt5.QtWidgets import QMainWindow, QTabWidget
from PyQt5.QtCore import QSettings

from desktop.whisper_gui.i18n import init_language, tr
from desktop.whisper_gui.tabs.whisper_tab import WhisperTab
from desktop.whisper_gui.tabs.fix_tab import FixTab
from desktop.whisper_gui.tabs.style_tab import StyleTab
from desktop.whisper_gui.tabs.build_tab import BuildTab


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("OASIS Subtitle Studio")
        self.resize(850, 620)
        self.setMinimumSize(700, 450)

        self.settings = QSettings("OASIS", "SubtitleStudio")
        init_language(self.settings)

        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)

        self.whisper_tab = WhisperTab(self.settings)
        self.fix_tab = FixTab(self.settings)
        self.style_tab = StyleTab()
        self.build_tab = BuildTab(self.settings)

        self.tabs.addTab(self.whisper_tab, tr("1. Распознавание"))
        self.tabs.addTab(self.fix_tab, tr("2. Вычитка"))
        self.tabs.addTab(self.style_tab, tr("3. Стили"))
        self.tabs.addTab(self.build_tab, tr("4. Сборка"))

        self._connect_signals()

    def _connect_signals(self):
        # Whisper -> Fix: полученный .srt автоматически подставляется на вкладку вычитки
        self.whisper_tab.srt_produced.connect(self.fix_tab.set_srt_path)
        # Whisper -> Build: исходное видео сразу подставляется в сборку
        self.whisper_tab.media_selected.connect(self.build_tab.set_video_path)
        # Fix -> Build: исправленный .srt подставляется в сборку
        self.fix_tab.fixed_srt_produced.connect(self.build_tab.set_srt_path)
        # Style -> Build: список стилей обновляется после изменений на вкладке стилей
        self.style_tab.styles_changed.connect(self.build_tab.refresh_styles)

    def closeEvent(self, event):
        # Отменяем все фоновые процессы при закрытии окна
        for tab in (self.whisper_tab, self.build_tab):
            worker = getattr(tab, "worker", None)
            if worker and worker.isRunning():
                worker.cancel()
                worker.wait(2000)
        event.accept()
