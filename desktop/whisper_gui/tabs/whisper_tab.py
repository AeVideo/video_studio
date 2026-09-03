import os
import sys
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QComboBox, QCheckBox, QTextEdit, QFileDialog, QGroupBox, QFormLayout,
    QMessageBox, QScrollArea
)
from PyQt5.QtCore import pyqtSignal
from desktop.whisper_gui.workers import WhisperWorker
from desktop.whisper_gui.i18n import tr

MODELS = ["tiny", "base", "small", "medium", "large"]
LANGUAGES = ["Auto", "Russian", "English", "Ukrainian", "German"]
DEVICES = ["cpu", "cuda"]


def default_whisper_path():
    """В собранном приложении whisper лежит рядом с exe, в папке whisper-bin."""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
        exe_name = "whisper.exe" if sys.platform == "win32" else "whisper"
        candidate = os.path.join(base, "whisper-bin", exe_name)
        if os.path.exists(candidate):
            return candidate
    return "whisper"


class WhisperTab(QWidget):
    """Шаг 1: распознавание речи через whisper -> получаем .srt"""
    srt_produced = pyqtSignal(str)   # путь к готовому .srt
    media_selected = pyqtSignal(str)  # путь к видео/аудио, для авто-подстановки в другие вкладки

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.worker = None
        self._build_ui()
        self._load_settings()

    def _build_ui(self):
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QScrollArea.NoFrame)
        content_widget = QWidget()
        layout = QVBoxLayout(content_widget)

        # --- Входной файл ---
        input_group = QGroupBox(tr("Исходный файл"))
        input_form = QFormLayout()

        file_row = QHBoxLayout()
        self.input_edit = QLineEdit()
        browse_btn = QPushButton(tr("Обзор…"))
        browse_btn.clicked.connect(self.browse_input)
        file_row.addWidget(self.input_edit)
        file_row.addWidget(browse_btn)
        input_form.addRow(tr("Видео/аудио:"), file_row)

        out_row = QHBoxLayout()
        self.output_dir_edit = QLineEdit()
        out_browse_btn = QPushButton(tr("Обзор…"))
        out_browse_btn.clicked.connect(self.browse_output_dir)
        out_row.addWidget(self.output_dir_edit)
        out_row.addWidget(out_browse_btn)
        input_form.addRow(tr("Папка вывода:"), out_row)

        input_group.setLayout(input_form)
        layout.addWidget(input_group)

        # --- Параметры распознавания ---
        params_group = QGroupBox(tr("Параметры whisper"))
        params_form = QFormLayout()

        self.whisper_exe_edit = QLineEdit(default_whisper_path())
        params_form.addRow(tr("Путь к whisper (из venv):"), self.whisper_exe_edit)

        self.model_combo = QComboBox()
        self.model_combo.addItems(MODELS)
        self.model_combo.setCurrentText("base")
        params_form.addRow(tr("Модель:"), self.model_combo)

        self.language_combo = QComboBox()
        self.language_combo.addItems(LANGUAGES)
        params_form.addRow(tr("Язык:"), self.language_combo)

        self.device_combo = QComboBox()
        self.device_combo.addItems(DEVICES)
        params_form.addRow(tr("Устройство:"), self.device_combo)

        self.word_ts_check = QCheckBox(tr("word_timestamps (таймкоды по словам)"))
        self.word_ts_check.setChecked(True)
        params_form.addRow("", self.word_ts_check)

        params_group.setLayout(params_form)
        layout.addWidget(params_group)

        # --- Управление ---
        btn_row = QHBoxLayout()
        self.run_btn = QPushButton(tr("Запустить распознавание"))
        self.run_btn.clicked.connect(self.run_whisper)
        self.cancel_btn = QPushButton(tr("Отмена"))
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel_whisper)
        btn_row.addWidget(self.run_btn)
        btn_row.addWidget(self.cancel_btn)
        layout.addLayout(btn_row)

        # --- Лог ---
        layout.addWidget(QLabel(tr("Лог:")))
        self.log_edit = QTextEdit()
        self.log_edit.setReadOnly(True)
        layout.addWidget(self.log_edit)

        scroll_area.setWidget(content_widget)
        outer_layout.addWidget(scroll_area)

    def _load_settings(self):
        from config.paths import OUTPUT_DIR as default_output  # раньше — хардкод ~/OasisSubtitleStudio_output
        self.output_dir_edit.setText(self.settings.value("whisper/output_dir", default_output))
        self.whisper_exe_edit.setText(self.settings.value("whisper/exe", default_whisper_path()))

    def browse_input(self):
        path, _ = QFileDialog.getOpenFileName(
            self, tr("Выберите видео или аудио файл"), "",
            tr("Медиа файлы (*.mp4 *.mkv *.mov *.avi *.mp3 *.wav *.m4a);;Все файлы (*)")
        )
        if path:
            self.input_edit.setText(path)
            if not self.output_dir_edit.text():
                self.output_dir_edit.setText(os.path.dirname(path))
            self.media_selected.emit(path)

    def browse_output_dir(self):
        path = QFileDialog.getExistingDirectory(self, tr("Папка для .srt"))
        if path:
            self.output_dir_edit.setText(path)

    def run_whisper(self):
        input_path = self.input_edit.text().strip()
        output_dir = self.output_dir_edit.text().strip()

        if not input_path or not os.path.exists(input_path):
            QMessageBox.warning(self, tr("Ошибка"), tr("Укажите существующий видео/аудио файл."))
            return
        if not output_dir:
            QMessageBox.warning(self, tr("Ошибка"), tr("Укажите папку вывода."))
            return

        os.makedirs(output_dir, exist_ok=True)
        self.settings.setValue("whisper/output_dir", output_dir)
        self.settings.setValue("whisper/exe", self.whisper_exe_edit.text().strip())

        self.log_edit.clear()
        self.run_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)

        self.worker = WhisperWorker(
            whisper_exe=self.whisper_exe_edit.text().strip() or "whisper",
            input_path=input_path,
            model=self.model_combo.currentText(),
            language=self.language_combo.currentText(),
            device=self.device_combo.currentText(),
            word_timestamps=self.word_ts_check.isChecked(),
            output_dir=output_dir,
        )
        self.worker.log_line.connect(self.log_edit.append)
        self.worker.finished_ok.connect(self.on_finished)
        self.worker.start()

    def cancel_whisper(self):
        if self.worker:
            self.worker.cancel()

    def on_finished(self, success, message):
        self.run_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        if success:
            self.log_edit.append(tr("\nГотово! Файл субтитров: {}").format(message))
            self.srt_produced.emit(message)
        else:
            self.log_edit.append(tr("\nОшибка: {}").format(message))
            QMessageBox.critical(self, tr("Ошибка распознавания"), message)
