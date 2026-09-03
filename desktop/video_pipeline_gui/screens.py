"""
gui/screens.py

Шесть экранов линейного wizard'а. Каждый экран не запускает воркеров сам —
только эмитит сигналы наверх, в MainWindow, который владеет ProjectManifest
и всеми QThread-воркерами. Так экраны остаются простыми и без побочных
эффектов, вся оркестрация — в одном месте.
"""

import json
import os

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QComboBox,
    QSpinBox, QPushButton, QTextEdit, QFileDialog, QProgressBar,
    QListWidget, QListWidgetItem, QMessageBox, QApplication, QAbstractItemView,
    QCheckBox,
)
from PyQt5.QtCore import pyqtSignal, Qt, QSize
from PyQt5.QtGui import QIcon, QPixmap


# ---------- 1. Новый проект ----------

class NewProjectScreen(QWidget):
    create_requested = pyqtSignal(str, str, int, str, str)      # book_path, language, duration_minutes, content_mode, aspect_ratio
    browse_archive_requested = pyqtSignal(str, int, str, str)   # language, duration_minutes, content_mode, aspect_ratio
    resume_requested = pyqtSignal(str)                          # project_dir

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)

        self.recent_label = QLabel("<b>Недавние проекты</b>")
        self.recent_label.setVisible(False)
        layout.addWidget(self.recent_label)

        self.recent_list = QListWidget()
        self.recent_list.setMaximumHeight(140)
        self.recent_list.setVisible(False)
        self.recent_list.itemDoubleClicked.connect(self._on_resume_clicked)
        layout.addWidget(self.recent_list)

        self.resume_btn = QPushButton("Продолжить выбранный проект")
        self.resume_btn.setVisible(False)
        self.resume_btn.clicked.connect(self._on_resume_clicked)
        layout.addWidget(self.resume_btn)

        layout.addWidget(QLabel("<b>Новый проект</b>"))

        layout.addWidget(QLabel("Тип источника:"))
        self.source_type = QComboBox()
        self.source_type.addItems(["Книга / текстовый файл", "Архив ФБР (FBI Vault)"])
        self.source_type.currentIndexChanged.connect(self._on_source_type_changed)
        layout.addWidget(self.source_type)

        row = QHBoxLayout()
        self.book_path_edit = QLineEdit()
        self.book_path_edit.setPlaceholderText("Путь к .txt файлу источника")
        self.browse_btn = QPushButton("Обзор...")
        self.browse_btn.clicked.connect(self._browse)
        row.addWidget(self.book_path_edit)
        row.addWidget(self.browse_btn)
        self.book_row_widget = QWidget()
        self.book_row_widget.setLayout(row)
        layout.addWidget(self.book_row_widget)

        layout.addWidget(QLabel("Язык сценария и озвучки:"))
        self.language = QComboBox()
        self.language.addItems(["ru", "uk", "en", "de"])
        layout.addWidget(self.language)

        layout.addWidget(QLabel("Длительность видео (минут):"))
        self.duration = QSpinBox()
        self.duration.setRange(1, 60)
        self.duration.setValue(20)
        layout.addWidget(self.duration)

        layout.addWidget(QLabel("Режим контента:"))
        self.content_mode = QComboBox()
        self.content_mode.addItems(["documentary", "historical", "storytelling"])
        self.content_mode.setCurrentText("documentary")
        layout.addWidget(self.content_mode)

        layout.addWidget(QLabel("Соотношение сторон:"))
        self.aspect_ratio = QComboBox()
        self.aspect_ratio.addItems(["16:9", "9:16"])
        layout.addWidget(self.aspect_ratio)

        self.create_btn = QPushButton("Создать проект и написать сценарий")
        self.create_btn.clicked.connect(self._on_create)
        layout.addWidget(self.create_btn)

        layout.addStretch()

    def set_recent_projects(self, projects: list):
        """projects: [{"project_dir":..., "label":..., "stage":..., "status":...}, ...]"""
        self._recent_projects = projects
        self.recent_list.clear()
        for p in projects:
            self.recent_list.addItem(f"{p['label']}  —  {p['stage']} ({p['status']})")
        has_projects = len(projects) > 0
        self.recent_label.setVisible(has_projects)
        self.recent_list.setVisible(has_projects)
        self.resume_btn.setVisible(has_projects)
        if has_projects:
            self.recent_list.setCurrentRow(0)

    def _on_resume_clicked(self):
        row = self.recent_list.currentRow()
        if row < 0 or row >= len(getattr(self, "_recent_projects", [])):
            return
        self.resume_requested.emit(self._recent_projects[row]["project_dir"])

    def _on_source_type_changed(self, index):
        is_archive = index == 1
        self.book_row_widget.setVisible(not is_archive)
        self.create_btn.setText("Выбрать дело из архива" if is_archive else "Создать проект и написать сценарий")

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(self, "Выберите источник", "", "Текст (*.txt)")
        if path:
            self.book_path_edit.setText(path)

    def _on_create(self):
        if self.source_type.currentIndex() == 1:
            self.browse_archive_requested.emit(
                self.language.currentText(), self.duration.value(),
                self.content_mode.currentText(), self.aspect_ratio.currentText(),
            )
            return

        book_path = self.book_path_edit.text().strip()
        if not book_path or not os.path.exists(book_path):
            QMessageBox.warning(self, "Проверьте путь", "Файл источника не найден.")
            return
        self.create_requested.emit(
            book_path, self.language.currentText(), self.duration.value(),
            self.content_mode.currentText(), self.aspect_ratio.currentText(),
        )


# ---------- 1б. Браузер дел FBI Vault ----------

class CaseBrowserScreen(QWidget):
    case_selected = pyqtSignal(str, str)  # title, url
    back_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<b>Архив ФБР (FBI Vault)</b> — поиск по названию дела"))

        self.status_label = QLabel("Загружаю индекс дел...")
        layout.addWidget(self.status_label)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Поиск (на английском, например: cooper, zodiac, ufo)")
        self.search_edit.textChanged.connect(self._on_search_changed)
        self.search_edit.setEnabled(False)
        layout.addWidget(self.search_edit)

        self.results_list = QListWidget()
        layout.addWidget(self.results_list)

        row = QHBoxLayout()
        back_btn = QPushButton("Назад")
        back_btn.clicked.connect(self.back_requested.emit)
        use_btn = QPushButton("Использовать это дело")
        use_btn.clicked.connect(self._on_use_clicked)
        row.addWidget(back_btn)
        row.addWidget(use_btn)
        layout.addLayout(row)

        self._index = []  # список {"title":..., "url":...}

    def set_index(self, index):
        self._index = index
        self.status_label.setText(f"Дел в индексе: {len(index)}")
        self.search_edit.setEnabled(True)
        self._refresh_list("")

    def set_error(self, message: str):
        self.status_label.setText(f"Ошибка загрузки индекса: {message}")

    def _on_search_changed(self, text):
        self._refresh_list(text)

    def _refresh_list(self, query: str):
        q = query.lower().strip()
        self.results_list.clear()
        if not q:
            matches = self._index[:50]
        else:
            words = q.split()
            matches = [c for c in self._index
                       if all(w in c["title"].lower() for w in words)][:50]
        for c in matches:
            self.results_list.addItem(c["title"])
        self._current_matches = matches

    def _on_use_clicked(self):
        row = self.results_list.currentRow()
        if row < 0 or row >= len(self._current_matches):
            QMessageBox.warning(self, "Выберите дело", "Сначала выберите дело из списка.")
            return
        case = self._current_matches[row]
        self.case_selected.emit(case["title"], case["url"])


# ---------- 1в. Выбор томов дела перед скачиванием ----------

class VolumeSelectionScreen(QWidget):
    """Показывается ПОСЛЕ выбора дела, но ДО любого скачивания/OCR — список
    томов приходит быстро (только HTML-страница дела, без PDF), пользователь
    отмечает нужные тома и задаёт лимит страниц OCR на том, и только тогда
    стартует реальная загрузка."""
    download_requested = pyqtSignal(list, int)  # selected_indices (1-based), max_ocr_pages
    back_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        self.title_label = QLabel("<b>Тома дела</b>")
        layout.addWidget(self.title_label)

        self.status_label = QLabel("Загружаю список томов...")
        layout.addWidget(self.status_label)

        self.volumes_list = QListWidget()
        self.volumes_list.setSelectionMode(QAbstractItemView.NoSelection)
        layout.addWidget(self.volumes_list)

        select_row = QHBoxLayout()
        select_all_btn = QPushButton("Выбрать все")
        select_all_btn.clicked.connect(lambda: self._set_all_checked(True))
        select_none_btn = QPushButton("Снять все")
        select_none_btn.clicked.connect(lambda: self._set_all_checked(False))
        select_row.addWidget(select_all_btn)
        select_row.addWidget(select_none_btn)
        select_row.addStretch()
        layout.addLayout(select_row)

        ocr_row = QHBoxLayout()
        ocr_row.addWidget(QLabel("Страниц OCR на том (для больших сканов):"))
        self.max_ocr_pages = QSpinBox()
        self.max_ocr_pages.setRange(0, 1000)
        self.max_ocr_pages.setValue(15)
        self.max_ocr_pages.setSpecialValueText("без ограничения")
        ocr_row.addWidget(self.max_ocr_pages)
        ocr_row.addStretch()
        layout.addLayout(ocr_row)

        btn_row = QHBoxLayout()
        back_btn = QPushButton("Назад")
        back_btn.clicked.connect(self.back_requested.emit)
        self.download_btn = QPushButton("Скачать выбранное")
        self.download_btn.clicked.connect(self._on_download_clicked)
        self.download_btn.setEnabled(False)
        btn_row.addWidget(back_btn)
        btn_row.addWidget(self.download_btn)
        layout.addLayout(btn_row)

        self._volumes = []  # [{"title":..., "url":...}, ...] в порядке из fbi_vault_scraper

    def set_loading(self):
        self.status_label.setText("Загружаю список томов...")
        self.volumes_list.clear()
        self.download_btn.setEnabled(False)

    def set_volumes(self, volumes: list):
        self._volumes = volumes
        self.status_label.setText(f"Всего томов: {len(volumes)}")
        self.volumes_list.clear()
        for v in volumes:
            item = QListWidgetItem(v["title"])
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked)
            self.volumes_list.addItem(item)
        self.download_btn.setEnabled(len(volumes) > 0)

    def set_error(self, message: str):
        self.status_label.setText(f"Ошибка загрузки списка томов: {message}")
        self.download_btn.setEnabled(False)

    def _set_all_checked(self, checked: bool):
        state = Qt.Checked if checked else Qt.Unchecked
        for i in range(self.volumes_list.count()):
            self.volumes_list.item(i).setCheckState(state)

    def _on_download_clicked(self):
        selected_indices = [
            i + 1 for i in range(self.volumes_list.count())
            if self.volumes_list.item(i).checkState() == Qt.Checked
        ]
        if not selected_indices:
            QMessageBox.warning(self, "Выберите тома", "Отметьте хотя бы один том для скачивания.")
            return
        max_pages = self.max_ocr_pages.value()
        max_pages = None if max_pages == 0 else max_pages  # 0 = "без ограничения"
        self.download_requested.emit(selected_indices, max_pages)


# ---------- 2. Сценарий ----------

class ScriptScreen(QWidget):
    continue_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<b>Сценарий</b> — текст озвучки и визуальные описания по сценам"))

        self.text_view = QTextEdit()
        self.text_view.setReadOnly(True)
        layout.addWidget(self.text_view)

        continue_btn = QPushButton("Дальше — озвучка")
        continue_btn.clicked.connect(self.continue_requested.emit)
        layout.addWidget(continue_btn)

    def load_script(self, script_path: str):
        with open(script_path, "r", encoding="utf-8") as f:
            scenes = json.load(f)

        parts = []
        for scene in scenes:
            parts.append(f"— {scene['id']} —\n{scene['text']}\n")
            visuals = scene.get("visuals", [])
            if visuals:
                parts.append("Визуальный ряд:\n" + "\n".join(f"  • {v}" for v in visuals))
            parts.append("")
        self.text_view.setPlainText("\n".join(parts))


# ---------- 3. Озвучка ----------

class NarrationScreen(QWidget):
    audio_selected = pyqtSignal(str)  # audio_path (ручной режим — файл уже готов)
    auto_narration_requested = pyqtSignal(str)  # voice_id (автоматический режим через API)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<b>Озвучка</b>"))

        layout.addWidget(QLabel("Режим:"))
        self.mode_switch = QComboBox()
        self.mode_switch.addItems([
            "Вручную (скопировать текст, начитать снаружи, загрузить файл)",
            "Автоматически (ElevenLabs API)",
        ])
        self.mode_switch.currentIndexChanged.connect(self._on_mode_changed)
        layout.addWidget(self.mode_switch)

        self.text_view = QTextEdit()
        self.text_view.setReadOnly(True)
        layout.addWidget(self.text_view)

        # --- ручной режим ---
        self.manual_widget = QWidget()
        manual_layout = QVBoxLayout(self.manual_widget)
        manual_layout.setContentsMargins(0, 0, 0, 0)

        copy_btn = QPushButton("Скопировать текст в буфер обмена")
        copy_btn.clicked.connect(self._copy)
        manual_layout.addWidget(copy_btn)

        row = QHBoxLayout()
        self.audio_path_edit = QLineEdit()
        self.audio_path_edit.setPlaceholderText("Путь к готовому файлу озвучки (.wav/.mp3)")
        browse_btn = QPushButton("Обзор...")
        browse_btn.clicked.connect(self._browse)
        row.addWidget(self.audio_path_edit)
        row.addWidget(browse_btn)
        manual_layout.addLayout(row)
        layout.addWidget(self.manual_widget)

        # --- автоматический режим ---
        self.auto_widget = QWidget()
        auto_layout = QVBoxLayout(self.auto_widget)
        auto_layout.setContentsMargins(0, 0, 0, 0)
        auto_layout.addWidget(QLabel("voice_id (в т.ч. вашего клонированного голоса):"))
        self.voice_id_edit = QLineEdit()
        self.voice_id_edit.setPlaceholderText("voice_id из ElevenLabs")
        auto_layout.addWidget(self.voice_id_edit)
        layout.addWidget(self.auto_widget)
        self.auto_widget.setVisible(False)

        continue_btn = QPushButton("Дальше — субтитры")
        continue_btn.clicked.connect(self._on_continue)
        layout.addWidget(continue_btn)

        self.status_label = QLabel("")
        layout.addWidget(self.status_label)

    def _on_mode_changed(self, index):
        is_auto = index == 1
        self.manual_widget.setVisible(not is_auto)
        self.auto_widget.setVisible(is_auto)

    def load_narration_text(self, path: str):
        with open(path, "r", encoding="utf-8") as f:
            self.text_view.setPlainText(f.read())

    def _copy(self):
        QApplication.clipboard().setText(self.text_view.toPlainText())

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(self, "Выберите файл озвучки", "", "Аудио (*.wav *.mp3)")
        if path:
            self.audio_path_edit.setText(path)

    def _on_continue(self):
        if self.mode_switch.currentIndex() == 1:
            voice_id = self.voice_id_edit.text().strip()
            if not voice_id:
                QMessageBox.warning(self, "Укажите voice_id", "Без voice_id автоматическая озвучка невозможна.")
                return
            self.status_label.setText("Запускаю озвучку через API...")
            self.auto_narration_requested.emit(voice_id)
            return

        path = self.audio_path_edit.text().strip()
        if not path or not os.path.exists(path):
            QMessageBox.warning(self, "Проверьте путь", "Файл озвучки не найден.")
            return
        self.audio_selected.emit(path)


# ---------- 4. Субтитры ----------

class SubtitlesScreen(QWidget):
    start_requested = pyqtSignal()
    continue_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<b>Субтитры</b> — распознавание речи + вычитка по эталонному тексту"))

        self.status_label = QLabel("Не запущено")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        layout.addWidget(self.progress_bar)

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        layout.addWidget(self.log_view)

        start_btn = QPushButton("Распознать и вычитать")
        start_btn.clicked.connect(self._on_start_clicked)
        self._start_btn = start_btn
        layout.addWidget(start_btn)

        self.continue_btn = QPushButton("Дальше — подбор видео")
        self.continue_btn.setEnabled(False)
        self.continue_btn.clicked.connect(self.continue_requested.emit)
        layout.addWidget(self.continue_btn)

    def _on_start_clicked(self):
        self.progress_bar.setValue(0)
        self.start_requested.emit()

    def append_log(self, line: str):
        self.log_view.append(line)

    def set_progress(self, percent: int, message: str):
        self.progress_bar.setValue(max(0, min(100, percent)))
        if message:
            self.status_label.setText(message)

    def set_done(self, srt_path: str):
        self.progress_bar.setValue(100)
        self.status_label.setText(f"Готово: {srt_path}")
        self.continue_btn.setEnabled(True)

    def set_error(self, message: str):
        self.log_view.append(message)
        first_line = message.split("\n", 1)[0]
        short = first_line if len(first_line) <= 120 else first_line[:120] + "…"
        self.status_label.setText(f"Ошибка: {short} (полный текст — в логе выше)")


# ---------- 5. Подбор видео ----------

class MatchingScreen(QWidget):
    start_requested = pyqtSignal()
    continue_requested = pyqtSignal()
    regenerate_requested = pyqtSignal(str)  # shot_id выбранной строки

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<b>Подбор видео</b> — Pexels/Pixabay + CLIP, самый долгий этап"))

        self.stale_banner = QLabel("")
        self.stale_banner.setWordWrap(True)
        self.stale_banner.setStyleSheet(
            "background-color: #fff3cd; color: #856404; padding: 8px; border-radius: 4px;"
        )
        self.stale_banner.hide()
        layout.addWidget(self.stale_banner)

        self.progress_bar = QProgressBar()
        layout.addWidget(self.progress_bar)

        self.status_label = QLabel("Не запущено")
        layout.addWidget(self.status_label)

        self.results_list = QListWidget()
        self.results_list.setIconSize(QSize(96, 54))  # 16:9 миниатюра, читаемо, не раздувает список
        layout.addWidget(self.results_list)

        start_btn = QPushButton("Начать подбор")
        start_btn.clicked.connect(self.start_requested.emit)
        layout.addWidget(start_btn)

        regen_btn = QPushButton("Перегенерировать выбранный шот")
        regen_btn.clicked.connect(self._on_regenerate_clicked)
        layout.addWidget(regen_btn)

        self.continue_btn = QPushButton("Дальше — сборка")
        self.continue_btn.setEnabled(False)
        self.continue_btn.clicked.connect(self.continue_requested.emit)
        layout.addWidget(self.continue_btn)

        self._shot_ids = []  # индекс строки списка -> id шота, для точечной перегенерации

    def set_progress(self, current: int, total: int, shot_id: str):
        self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(current)
        self.status_label.setText(f"[{current}/{total}] {shot_id}")

    def set_done(self, footage_path: str, stale: bool = False):
        """stale=True — эти результаты НЕ из только что завершённого в этой
        сессии подбора, а подхвачены с диска при открытии/резюмировании
        проекта (могли быть посчитаны хоть месяц назад, другими промптами,
        без превью-кадров и т.д.). Явно предупреждаем, а не молча
        подсовываем как свежие — раньше это и увело в сборку по старому
        видеоряду без единого сигнала, что данные не пересчитаны."""
        self.status_label.setText(f"Готово: {footage_path}")
        self._shot_ids = []

        if stale:
            mtime_str = ""
            try:
                mtime = os.path.getmtime(footage_path)
                import datetime
                mtime_str = f" (файл от {datetime.datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M')})"
            except OSError:
                pass
            self.stale_banner.setText(
                f"⚠ Показаны результаты ПРЕДЫДУЩЕГО прогона{mtime_str} — не пересчитаны в этой сессии. "
                f"Если менялись промпты/пороги/эпоха проекта — нажмите «Начать подбор» ниже, чтобы "
                f"пересобрать заново. Если результат устраивает как есть — можно сразу «Дальше — сборка»."
            )
            self.stale_banner.show()
        else:
            self.stale_banner.hide()

        try:
            with open(footage_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.results_list.clear()
            for scene in data["scenes"]:
                footage = scene.get("footage")
                score = footage["score"] if footage else "—"
                item = QListWidgetItem(f"{scene['id']}  [{scene['status']}]  score={score}")
                thumb_path = footage.get("thumbnail_path") if footage else None
                if thumb_path and os.path.exists(thumb_path):
                    pixmap = QPixmap(thumb_path)
                    if not pixmap.isNull():
                        item.setIcon(QIcon(pixmap))
                self.results_list.addItem(item)
                self._shot_ids.append(scene["id"])
        except Exception:
            pass
        self.continue_btn.setEnabled(True)

    def _on_regenerate_clicked(self):
        row = self.results_list.currentRow()
        if row < 0 or row >= len(self._shot_ids):
            QMessageBox.warning(self, "Выберите шот", "Сначала выберите строку из списка.")
            return
        self.regenerate_requested.emit(self._shot_ids[row])


# ---------- 6. Сборка ----------

class AssemblyScreen(QWidget):
    start_requested = pyqtSignal(str, bool, str, str)  # aspect_ratio, burn_subtitles, style_slug, subtitles_path
    back_to_matching_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        back_btn = QPushButton("← Назад к подбору видео (проверить/пересобрать видеоряд)")
        back_btn.clicked.connect(self.back_to_matching_requested.emit)
        layout.addWidget(back_btn)
        layout.addWidget(QLabel("<b>Сборка</b> — скачивание клипов, кроссфейд, наложение озвучки"))

        params_label = QLabel(
            "<b>Фиксированные параметры монтажа</b><br>"
            "FPS: 25 &nbsp;·&nbsp; Переход: fade 0.5с &nbsp;·&nbsp; Кодек: H.264 + AAC"
        )
        params_label.setStyleSheet("background-color: #f2f2f2; padding: 8px; border-radius: 4px;")
        layout.addWidget(params_label)

        row = QHBoxLayout()
        row.addWidget(QLabel("Соотношение сторон:"))
        self.aspect_ratio = QComboBox()
        self.aspect_ratio.addItems(["16:9", "9:16"])
        self.aspect_ratio.currentTextChanged.connect(self._on_aspect_ratio_changed)
        row.addWidget(self.aspect_ratio)
        row_widget = QWidget()
        row_widget.setLayout(row)
        layout.addWidget(row_widget)

        self.burn_subtitles_checkbox = QCheckBox("Прожечь субтитры в видео")
        self.burn_subtitles_checkbox.setChecked(True)
        layout.addWidget(self.burn_subtitles_checkbox)

        subs_path_row = QHBoxLayout()
        subs_path_row.addWidget(QLabel("Файл субтитров (.srt):"))
        self.subtitles_path_edit = QLineEdit()
        self.subtitles_path_edit.setPlaceholderText("не задан")
        self.subtitles_path_edit.textChanged.connect(self._on_subtitles_path_changed)
        subs_path_row.addWidget(self.subtitles_path_edit)
        browse_subs_btn = QPushButton("Обзор…")
        browse_subs_btn.clicked.connect(self._on_browse_subtitles)
        subs_path_row.addWidget(browse_subs_btn)
        subs_path_row_widget = QWidget()
        subs_path_row_widget.setLayout(subs_path_row)
        layout.addWidget(subs_path_row_widget)

        self.subtitles_status_label = QLabel("")
        self.subtitles_status_label.setStyleSheet("color: #856404;")
        layout.addWidget(self.subtitles_status_label)

        style_row = QHBoxLayout()
        style_row.addWidget(QLabel("Стиль субтитров:"))
        self.style_combo = QComboBox()
        self.style_combo.addItem("Авто (без пресета)", "")
        style_row.addWidget(self.style_combo)
        style_row_widget = QWidget()
        style_row_widget.setLayout(style_row)
        layout.addWidget(style_row_widget)

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        layout.addWidget(self.log_view)

        self.status_label = QLabel("Не запущено")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.stats_label = QLabel("")
        layout.addWidget(self.stats_label)

        start_btn = QPushButton("Собрать видео")
        start_btn.clicked.connect(
            lambda: self.start_requested.emit(
                self.aspect_ratio.currentText(),
                self.burn_subtitles_checkbox.isChecked(),
                self.style_combo.currentData() or "",
                self.subtitles_path_edit.text().strip(),
            )
        )
        layout.addWidget(start_btn)

        self.open_btn = QPushButton("Открыть видео")
        self.open_btn.setEnabled(False)
        self.open_btn.clicked.connect(self._open_video)
        layout.addWidget(self.open_btn)

        self._output_path = None

    def set_aspect_ratio(self, aspect_ratio: str):
        """Подставляет соотношение сторон из manifest.json проекта (например
        при резюмировании) — не заставляем выбирать заново то, что уже
        выбрали при создании проекта."""
        idx = self.aspect_ratio.findText(aspect_ratio)
        if idx >= 0:
            self.aspect_ratio.setCurrentIndex(idx)

    def set_style_options(self, presets):
        """presets — [(slug, display_name), ...] из
        oasis_studio_bridge.list_style_presets(), вызывается снаружи
        (main_window), сам экран в style_manager не лезет. Пустой список —
        не ошибка, просто остаётся только пункт «Авто»."""
        self._presets = list(presets)
        current = self.style_combo.currentData()
        self.style_combo.clear()
        self.style_combo.addItem("Авто (без пресета)", "")
        for slug, name in self._presets:
            self.style_combo.addItem(name, slug)
        if current:
            idx = self.style_combo.findData(current)
            if idx >= 0:
                self.style_combo.setCurrentIndex(idx)
        else:
            self._suggest_style_for_aspect_ratio(self.aspect_ratio.currentText())

    def _suggest_style_for_aspect_ratio(self, aspect_ratio: str):
        """Пресеты называются по ориентации (default_horizontal/vertical/
        square и их вариации) — при выборе 9:16 логично сразу подставить
        default_vertical, а не молча оставить «Авто» и рисковать прожечь
        вертикальное видео горизонтальной плашкой/отступами. Ищем ЛУЧШЕЕ
        совпадение по подстроке в slug — не только "default_*", чтобы не
        сломаться на пользовательских пресетах с похожим именем."""
        if not getattr(self, "_presets", None):
            return
        marker = {"16:9": "horizontal", "9:16": "vertical"}.get(aspect_ratio)
        if not marker:
            return
        for slug, _name in self._presets:
            if slug == f"default_{marker}":
                idx = self.style_combo.findData(slug)
                if idx >= 0:
                    self.style_combo.setCurrentIndex(idx)
                return
        for slug, _name in self._presets:
            if marker in slug.lower():
                idx = self.style_combo.findData(slug)
                if idx >= 0:
                    self.style_combo.setCurrentIndex(idx)
                return

    def _on_aspect_ratio_changed(self, aspect_ratio: str):
        self._suggest_style_for_aspect_ratio(aspect_ratio)

    def set_subtitles_path(self, path: str):
        """Подставляет путь к .srt из manifest.json — но НЕ как непроверяемую
        истину: путь остаётся видимым и редактируемым полем, потому что этот
        путь в манифесте реально расходился с фактическим расположением файла
        (файлы переносили вручную, манифест не обновлялся) — молчаливое
        отключение чекбокса без объяснения увело в тупик."""
        self.subtitles_path_edit.blockSignals(True)
        self.subtitles_path_edit.setText(path or "")
        self.subtitles_path_edit.blockSignals(False)
        self._validate_subtitles_path()

    def _validate_subtitles_path(self):
        path = self.subtitles_path_edit.text().strip()
        exists = bool(path) and os.path.exists(path)
        self.burn_subtitles_checkbox.setEnabled(exists)
        if not exists:
            self.burn_subtitles_checkbox.setChecked(False)
            if path:
                self.subtitles_status_label.setText(f"⚠ Файл не найден по этому пути: {path}")
            else:
                self.subtitles_status_label.setText("⚠ Путь к субтитрам не задан — прожиг недоступен")
        else:
            self.burn_subtitles_checkbox.setChecked(True)
            self.subtitles_status_label.setText("")

    def _on_subtitles_path_changed(self, _text: str):
        self._validate_subtitles_path()

    def _on_browse_subtitles(self):
        path, _ = QFileDialog.getOpenFileName(self, "Выберите файл субтитров", "", "Субтитры (*.srt)")
        if path:
            self.subtitles_path_edit.setText(path)  # textChanged сам вызовет валидацию

    def append_log(self, line: str):
        self.log_view.append(line)

    def set_done(self, output_path: str):
        self.status_label.setText(f"Готово: {output_path}")
        self._output_path = output_path
        self.open_btn.setEnabled(True)

        try:
            import subprocess
            result = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", output_path],
                capture_output=True, text=True,
            )
            duration = float(result.stdout.strip())
            minutes, seconds = divmod(int(duration), 60)
            self.stats_label.setText(f"Длительность: {minutes}:{seconds:02d}")
        except Exception:
            pass

    def _open_video(self):
        if not self._output_path:
            return
        import subprocess
        try:
            subprocess.Popen(["xdg-open", self._output_path])
        except Exception as e:
            QMessageBox.warning(self, "Не удалось открыть", str(e))

    def set_error(self, message: str):
        # Полная ошибка (может быть длинной ffmpeg-командой без пробельных
        # точек разрыва — QLabel без переноса на такой строке растягивал
        # окно на всю ширину нескольких мониторов) идёт в log_view, который
        # переносит строки штатно. В status_label — только короткая сводка.
        self.log_view.append(message)
        first_line = message.split("\n", 1)[0]
        short = first_line if len(first_line) <= 120 else first_line[:120] + "…"
        self.status_label.setText(f"Ошибка: {short} (полный текст — в логе выше)")
