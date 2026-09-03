import os

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QTextEdit, QFileDialog, QGroupBox, QFormLayout, QMessageBox, QProgressBar,
    QTableWidget, QTableWidgetItem, QCheckBox, QHeaderView, QSpinBox, QScrollArea
)
from PyQt5.QtCore import pyqtSignal, Qt

from desktop.whisper_gui.workers import FixWorker, VerifyWorker
from desktop.whisper_gui.i18n import tr
from asr import deepseek_verify


def _status_labels():
    return {
        "ok": tr("Ок"),
        "suspect": tr("Подозрительно"),
        "unknown": tr("Нет ответа"),
    }


class FixTab(QWidget):
    """Шаг 2: сопоставление распознанного .srt с текстом книги -> исправленный .srt,
    плюс необязательная поблочная проверка результата через DeepSeek."""
    fixed_srt_produced = pyqtSignal(str)

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.worker = None
        self.verify_worker = None
        self.verify_results = []
        self._build_ui()

    def _build_ui(self):
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QScrollArea.NoFrame)
        content_widget = QWidget()
        layout = QVBoxLayout(content_widget)

        group = QGroupBox(tr("Файлы для вычитки"))
        form = QFormLayout()

        srt_row = QHBoxLayout()
        self.srt_edit = QLineEdit()
        srt_browse = QPushButton(tr("Обзор…"))
        srt_browse.clicked.connect(self.browse_srt)
        srt_row.addWidget(self.srt_edit)
        srt_row.addWidget(srt_browse)
        form.addRow(tr("Исходный .srt:"), srt_row)

        book_row = QHBoxLayout()
        self.book_edit = QLineEdit()
        book_browse = QPushButton(tr("Обзор…"))
        book_browse.clicked.connect(self.browse_book)
        book_row.addWidget(self.book_edit)
        book_row.addWidget(book_browse)
        form.addRow(tr("Текст книги (.txt):"), book_row)

        out_row = QHBoxLayout()
        self.output_edit = QLineEdit()
        out_browse = QPushButton(tr("Обзор…"))
        out_browse.clicked.connect(self.browse_output)
        out_row.addWidget(self.output_edit)
        out_row.addWidget(out_browse)
        form.addRow(tr("Результат:"), out_row)

        group.setLayout(form)
        layout.addWidget(group)

        note = QLabel(
            tr("Подсказка: если у вас нет текста книги для сверки — можно пропустить\n"
               "этот шаг и передать исходный .srt сразу на вкладку «Сборка».")
        )
        note.setStyleSheet("color: gray;")
        layout.addWidget(note)

        btn_row = QHBoxLayout()
        self.run_btn = QPushButton(tr("Выполнить вычитку"))
        self.run_btn.clicked.connect(self.run_fix)
        self.skip_btn = QPushButton(tr("Пропустить (использовать как есть)"))
        self.skip_btn.clicked.connect(self.skip_fix)
        btn_row.addWidget(self.run_btn)
        btn_row.addWidget(self.skip_btn)
        layout.addLayout(btn_row)

        self.progress_bar = QProgressBar()
        layout.addWidget(self.progress_bar)

        layout.addWidget(QLabel(tr("Лог:")))
        self.log_edit = QTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setMaximumHeight(120)
        layout.addWidget(self.log_edit)

        # --- Проверка через DeepSeek ---
        verify_group = QGroupBox(tr("Проверка через DeepSeek (поблочная сверка с книгой)"))
        verify_layout = QVBoxLayout()

        verify_top_row = QHBoxLayout()
        verify_top_row.addWidget(QLabel(tr("Блоков за один запрос:")))
        self.chunk_size_spin = QSpinBox()
        self.chunk_size_spin.setRange(20, 1000)
        self.chunk_size_spin.setValue(150)
        verify_top_row.addWidget(self.chunk_size_spin)
        self.verify_btn = QPushButton(tr("Проверить через DeepSeek"))
        self.verify_btn.clicked.connect(self.run_verify)
        verify_top_row.addWidget(self.verify_btn)
        verify_top_row.addStretch()
        verify_layout.addLayout(verify_top_row)

        self.verify_progress_bar = QProgressBar()
        verify_layout.addWidget(self.verify_progress_bar)

        self.results_table = QTableWidget(0, 5)
        self.results_table.setHorizontalHeaderLabels(
            [tr("✓"), tr("Блок"), tr("Статус"), tr("Проблема"), tr("Предложение DeepSeek")]
        )
        self.results_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.results_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.results_table.setColumnWidth(0, 30)
        self.results_table.setColumnWidth(1, 50)
        self.results_table.setColumnWidth(2, 110)
        verify_layout.addWidget(self.results_table)

        verify_btn_row = QHBoxLayout()
        self.check_all_suspects_btn = QPushButton(tr("Отметить все подозрительные"))
        self.check_all_suspects_btn.clicked.connect(self.check_all_suspects)
        self.apply_corrections_btn = QPushButton(tr("Применить отмеченные и сохранить"))
        self.apply_corrections_btn.clicked.connect(self.apply_selected_corrections)
        verify_btn_row.addWidget(self.check_all_suspects_btn)
        verify_btn_row.addWidget(self.apply_corrections_btn)
        verify_layout.addLayout(verify_btn_row)

        verify_group.setLayout(verify_layout)
        layout.addWidget(verify_group)

        scroll_area.setWidget(content_widget)
        outer_layout.addWidget(scroll_area)

    # ---------- Основная вычитка ----------

    def browse_srt(self):
        path, _ = QFileDialog.getOpenFileName(self, tr("Выберите .srt файл"), "", tr("SRT файлы (*.srt)"))
        if path:
            self.set_srt_path(path)

    def browse_book(self):
        path, _ = QFileDialog.getOpenFileName(self, tr("Выберите текст книги"), "", tr("Текстовые файлы (*.txt)"))
        if path:
            self.book_edit.setText(path)

    def browse_output(self):
        path, _ = QFileDialog.getSaveFileName(self, tr("Куда сохранить результат"), "", tr("SRT файлы (*.srt)"))
        if path:
            self.output_edit.setText(path)

    def set_srt_path(self, path):
        """Вызывается извне (например, после распознавания whisper) для автозаполнения."""
        self.srt_edit.setText(path)
        if not self.output_edit.text() and path:
            base_dir = os.path.dirname(path)
            base_name = os.path.splitext(os.path.basename(path))[0]
            self.output_edit.setText(os.path.join(base_dir, f"fixed_{base_name}.srt"))

    def run_fix(self):
        srt_path = self.srt_edit.text().strip()
        book_path = self.book_edit.text().strip()
        output_path = self.output_edit.text().strip()

        if not srt_path or not os.path.exists(srt_path):
            QMessageBox.warning(self, tr("Ошибка"), tr("Укажите существующий .srt файл."))
            return
        if not book_path or not os.path.exists(book_path):
            QMessageBox.warning(self, tr("Ошибка"), tr("Укажите существующий текстовый файл книги "
                                                 "(или нажмите «Пропустить»)."))
            return
        if not output_path:
            QMessageBox.warning(self, tr("Ошибка"), tr("Укажите путь для сохранения результата."))
            return

        self.log_edit.clear()
        self.progress_bar.setValue(0)
        self.run_btn.setEnabled(False)
        self._clear_results_table()

        self.worker = FixWorker(srt_path, book_path, output_path)
        self.worker.progress.connect(
            lambda cur, total: self.progress_bar.setValue(int(cur / max(total, 1) * 100))
        )
        self.worker.finished_ok.connect(self.on_finished)
        self.worker.start()

    def skip_fix(self):
        srt_path = self.srt_edit.text().strip()
        if not srt_path or not os.path.exists(srt_path):
            QMessageBox.warning(self, tr("Ошибка"), tr("Укажите существующий .srt файл для передачи дальше."))
            return
        self.log_edit.append(tr("Шаг вычитки пропущен, используется исходный файл: {}").format(srt_path))
        self.fixed_srt_produced.emit(srt_path)

    def on_finished(self, success, message):
        self.run_btn.setEnabled(True)
        if success:
            self.progress_bar.setValue(100)
            self.log_edit.append(tr("\nГотово! Исправленный файл: {}").format(message))
            self.fixed_srt_produced.emit(message)
        else:
            self.log_edit.append(tr("\nОшибка: {}").format(message))
            QMessageBox.critical(self, tr("Ошибка вычитки"), message)

    # ---------- Проверка через DeepSeek ----------

    def _clear_results_table(self):
        self.results_table.setRowCount(0)
        self.verify_results = []

    def run_verify(self):
        srt_path = self.output_edit.text().strip() or self.srt_edit.text().strip()
        book_path = self.book_edit.text().strip()

        if not srt_path or not os.path.exists(srt_path):
            QMessageBox.warning(self, tr("Ошибка"), tr("Сначала получите исправленный .srt "
                                                 "(выполните вычитку) — его и будем проверять."))
            return
        if not book_path or not os.path.exists(book_path):
            QMessageBox.warning(self, tr("Ошибка"), tr("Укажите текст книги — без него проверять не с чем."))
            return

        if not deepseek_verify.load_api_key():
            QMessageBox.critical(
                self, tr("Нужен DeepSeek API-ключ"),
                tr("Проверка через DeepSeek требует API-ключ. Есть два варианта:\n\n"
                "1. Получить свой бесплатный ключ на platform.deepseek.com и добавить "
                "файл .env в папку программы со строкой:\n"
                "DEEPSEEK_API_KEY=ваш_ключ\n\n"
                "2. Написать разработчику — можно подключить готовый ключ по подписке, "
                "без лишней возни с настройкой:\n"
                "Telegram: t.me/Oasis_Word\n"
                "Email: oasis.word@gmail.com")
            )
            return

        self._clear_results_table()
        self.verify_progress_bar.setValue(0)
        self.verify_btn.setEnabled(False)

        self.verify_worker = VerifyWorker(
            srt_path, book_path, chunk_size=self.chunk_size_spin.value()
        )
        self.verify_worker.progress.connect(
            lambda cur, total: self.verify_progress_bar.setValue(int(cur / max(total, 1) * 100))
        )
        self.verify_worker.finished_ok.connect(self.on_verify_finished)
        self.verify_worker.start()

    def on_verify_finished(self, success, payload):
        self.verify_btn.setEnabled(True)
        if not success:
            QMessageBox.critical(self, tr("Ошибка проверки DeepSeek"), str(payload))
            return

        self.verify_results = payload
        self._populate_results_table(payload)

        suspect_count = sum(1 for r in payload if r["status"] == "suspect")
        self.log_edit.append(
            tr("\nDeepSeek проверил {} блоков, подозрительных: {}.").format(len(payload), suspect_count)
        )

    def _populate_results_table(self, results):
        status_labels = _status_labels()
        self.results_table.setRowCount(len(results))
        for row, r in enumerate(results):
            check_widget = QCheckBox()
            if r["status"] == "suspect":
                check_widget.setChecked(True)
            self.results_table.setCellWidget(row, 0, check_widget)

            self.results_table.setItem(row, 1, QTableWidgetItem(str(r["block"])))

            status_item = QTableWidgetItem(status_labels.get(r["status"], r["status"]))
            if r["status"] == "suspect":
                status_item.setForeground(Qt.red)
            elif r["status"] == "unknown":
                status_item.setForeground(Qt.gray)
            self.results_table.setItem(row, 2, status_item)

            self.results_table.setItem(row, 3, QTableWidgetItem(r.get("issue", "")))
            self.results_table.setItem(row, 4, QTableWidgetItem(r.get("suggestion", "")))

    def check_all_suspects(self):
        status_labels = _status_labels()
        for row in range(self.results_table.rowCount()):
            widget = self.results_table.cellWidget(row, 0)
            status_item = self.results_table.item(row, 2)
            if widget and status_item and status_item.text() == status_labels["suspect"]:
                widget.setChecked(True)

    def apply_selected_corrections(self):
        if not self.verify_results:
            QMessageBox.warning(self, tr("Ошибка"), tr("Сначала выполните проверку через DeepSeek."))
            return

        accepted_blocks = set()
        for row in range(self.results_table.rowCount()):
            widget = self.results_table.cellWidget(row, 0)
            if widget and widget.isChecked():
                accepted_blocks.add(int(self.results_table.item(row, 1).text()))

        if not accepted_blocks:
            QMessageBox.information(self, tr("Нечего применять"), tr("Не отмечено ни одного блока."))
            return

        srt_path = self.output_edit.text().strip() or self.srt_edit.text().strip()
        base_dir = os.path.dirname(srt_path)
        base_name = os.path.splitext(os.path.basename(srt_path))[0]
        output_path = os.path.join(base_dir, f"{base_name}_verified.srt")

        try:
            deepseek_verify.apply_corrections(srt_path, output_path, self.verify_results, accepted_blocks)
        except Exception as e:
            QMessageBox.critical(self, tr("Ошибка применения правок"), str(e))
            return

        self.log_edit.append(
            tr("\nПрименено правок: {}. Сохранено как: {}").format(len(accepted_blocks), output_path)
        )
        QMessageBox.information(
            self, tr("Готово"),
            tr("Исправления применены. Файл сохранён:\n{}\n\n"
            "Этот файл автоматически передан на вкладку «Сборка».").format(output_path)
        )
        self.fixed_srt_produced.emit(output_path)
