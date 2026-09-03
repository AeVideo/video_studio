import os
import sys

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QComboBox, QCheckBox, QTextEdit, QFileDialog, QGroupBox, QFormLayout,
    QMessageBox, QProgressBar, QScrollArea
)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from asr import style_manager
from desktop.whisper_gui.workers import BuildWorker
from desktop.whisper_gui.i18n import tr


class BuildTab(QWidget):
    """Шаг 4: генерация .ass с выбранным стилем и вжигание субтитров в видео."""

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.worker = None
        self._build_ui()
        self.refresh_styles()

    def _build_ui(self):
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QScrollArea.NoFrame)
        content_widget = QWidget()
        layout = QVBoxLayout(content_widget)

        group = QGroupBox(tr("Исходные данные"))
        form = QFormLayout()

        srt_row = QHBoxLayout()
        self.srt_edit = QLineEdit()
        srt_browse = QPushButton(tr("Обзор…"))
        srt_browse.clicked.connect(self.browse_srt)
        srt_row.addWidget(self.srt_edit)
        srt_row.addWidget(srt_browse)
        form.addRow(tr("Файл субтитров (.srt):"), srt_row)

        video_row = QHBoxLayout()
        self.video_edit = QLineEdit()
        video_browse = QPushButton(tr("Обзор…"))
        video_browse.clicked.connect(self.browse_video)
        video_row.addWidget(self.video_edit)
        video_row.addWidget(video_browse)
        form.addRow(tr("Видео:"), video_row)

        style_row = QHBoxLayout()
        self.style_combo = QComboBox()
        refresh_btn = QPushButton(tr("Обновить список"))
        refresh_btn.clicked.connect(self.refresh_styles)
        style_row.addWidget(self.style_combo)
        style_row.addWidget(refresh_btn)
        form.addRow(tr("Стиль субтитров:"), style_row)

        group.setLayout(form)
        layout.addWidget(group)

        output_group = QGroupBox(tr("Вывод"))
        output_form = QFormLayout()

        self.burn_check = QCheckBox(tr("Вжечь субтитры в видео (иначе — только создать .ass)"))
        self.burn_check.setChecked(True)
        self.burn_check.stateChanged.connect(self._toggle_output_row)
        output_form.addRow("", self.burn_check)

        preset_row = QHBoxLayout()
        self.encode_preset_combo = QComboBox()
        self.encode_preset_combo.addItems(
            ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow"]
        )
        self.encode_preset_combo.setCurrentText("medium")
        preset_row.addWidget(QLabel(tr("Пресет кодирования ffmpeg:")))
        preset_row.addWidget(self.encode_preset_combo)
        output_form.addRow("", preset_row)

        out_row = QHBoxLayout()
        self.output_edit = QLineEdit()
        out_browse = QPushButton(tr("Обзор…"))
        out_browse.clicked.connect(self.browse_output)
        out_row.addWidget(self.output_edit)
        out_row.addWidget(out_browse)
        self.output_row_widget = out_row
        output_form.addRow(tr("Итоговое видео:"), out_row)

        output_group.setLayout(output_form)
        layout.addWidget(output_group)

        btn_row = QHBoxLayout()
        self.run_btn = QPushButton(tr("Собрать"))
        self.run_btn.clicked.connect(self.run_build)
        self.cancel_btn = QPushButton(tr("Отмена"))
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel_build)
        btn_row.addWidget(self.run_btn)
        btn_row.addWidget(self.cancel_btn)
        layout.addLayout(btn_row)

        self.phase_label = QLabel("")
        layout.addWidget(self.phase_label)
        self.progress_bar = QProgressBar()
        layout.addWidget(self.progress_bar)

        layout.addWidget(QLabel(tr("Лог:")))
        self.log_edit = QTextEdit()
        self.log_edit.setReadOnly(True)
        layout.addWidget(self.log_edit)

        scroll_area.setWidget(content_widget)
        outer_layout.addWidget(scroll_area)

    def _toggle_output_row(self):
        enabled = self.burn_check.isChecked()
        self.output_edit.setEnabled(enabled)
        self.encode_preset_combo.setEnabled(enabled)

    def refresh_styles(self):
        current_slug = self.style_combo.currentData()
        self.style_combo.clear()
        self.style_combo.addItem(tr("(без стиля — авто-подбор)"), None)
        for slug, display_name in style_manager.list_styles():
            self.style_combo.addItem(display_name, slug)
        if current_slug:
            idx = self.style_combo.findData(current_slug)
            if idx >= 0:
                self.style_combo.setCurrentIndex(idx)

    def browse_srt(self):
        path, _ = QFileDialog.getOpenFileName(self, tr("Выберите .srt файл"), "", tr("SRT файлы (*.srt)"))
        if path:
            self.set_srt_path(path)

    def browse_video(self):
        path, _ = QFileDialog.getOpenFileName(
            self, tr("Выберите видео"), "", tr("Видео файлы (*.mp4 *.mkv *.mov *.avi);;Все файлы (*)")
        )
        if path:
            self.set_video_path(path)

    def browse_output(self):
        path, _ = QFileDialog.getSaveFileName(self, tr("Сохранить как"), "", tr("MP4 файлы (*.mp4)"))
        if path:
            self.output_edit.setText(path)

    def set_srt_path(self, path):
        self.srt_edit.setText(path)

    def set_video_path(self, path):
        self.video_edit.setText(path)
        if not self.output_edit.text() and path:
            base_dir = os.path.dirname(path)
            base_name = os.path.splitext(os.path.basename(path))[0]
            self.output_edit.setText(os.path.join(base_dir, f"{base_name}_with_sub.mp4"))

    def run_build(self):
        srt_path = self.srt_edit.text().strip()
        video_path = self.video_edit.text().strip()
        burn = self.burn_check.isChecked()
        output_path = self.output_edit.text().strip()

        if not srt_path or not os.path.exists(srt_path):
            QMessageBox.warning(self, tr("Ошибка"), tr("Укажите существующий .srt файл."))
            return
        if not video_path or not os.path.exists(video_path):
            QMessageBox.warning(self, tr("Ошибка"), tr("Укажите существующий видео файл."))
            return
        if burn and not output_path:
            QMessageBox.warning(self, tr("Ошибка"), tr("Укажите путь для итогового видео."))
            return

        ass_output_path = os.path.splitext(srt_path)[0] + ".ass"
        style_slug = self.style_combo.currentData()

        self.log_edit.clear()
        self.progress_bar.setValue(0)
        self.run_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)

        self.worker = BuildWorker(
            srt_path=srt_path,
            video_path=video_path,
            style_slug=style_slug,
            ass_output_path=ass_output_path,
            burn_into_video=burn,
            final_output_path=output_path,
            encode_preset=self.encode_preset_combo.currentText(),
        )
        self.worker.log_line.connect(self.log_edit.append)
        self.worker.phase_changed.connect(self.phase_label.setText)
        self.worker.progress.connect(self.progress_bar.setValue)
        self.worker.finished_ok.connect(self.on_finished)
        self.worker.start()

    def cancel_build(self):
        if self.worker:
            self.worker.cancel()

    def on_finished(self, success, message):
        self.run_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        if success:
            self.log_edit.append(tr("\nГотово! Результат: {}").format(message))
            self.phase_label.setText(tr("Завершено"))
        else:
            self.log_edit.append(tr("\nОшибка: {}").format(message))
            QMessageBox.critical(self, tr("Ошибка сборки"), message)
