import os
import sys

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QListWidget, QPushButton, QLabel,
    QLineEdit, QComboBox, QCheckBox, QSpinBox, QFormLayout, QGroupBox,
    QMessageBox, QColorDialog, QDoubleSpinBox, QInputDialog, QRadioButton,
    QButtonGroup, QScrollArea, QFontComboBox
)
from PyQt5.QtGui import QColor, QFont
from PyQt5.QtCore import pyqtSignal

from desktop.whisper_gui.i18n import tr

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from asr import style_manager


def alignment_options():
    return [
        (tr("Снизу слева"), 1),
        (tr("Снизу по центру"), 2),
        (tr("Снизу справа"), 3),
        (tr("По центру экрана"), 5),
    ]


def ass_color_to_qcolor(ass_color):
    """&HAABBGGRR или &HBBGGRR -> QColor. Возвращает белый при ошибке."""
    try:
        hexpart = ass_color.replace("&H", "").replace("&h", "")
        hexpart = hexpart.rjust(8, "0")
        aa = hexpart[0:2]
        bb = hexpart[2:4]
        gg = hexpart[4:6]
        rr = hexpart[6:8]
        alpha = 255 - int(aa, 16)  # в ASS 00=непрозрачно, FF=прозрачно
        return QColor(int(rr, 16), int(gg, 16), int(bb, 16), alpha)
    except Exception:
        return QColor(255, 255, 255)


def qcolor_to_ass_color(qcolor):
    """QColor -> &HAABBGGRR. Альфа берётся из самого QColor (инвертируется,
    т.к. в ASS 00=непрозрачно, а в Qt 0=прозрачно — конвенции противоположны)."""
    bb = format(qcolor.blue(), '02X')
    gg = format(qcolor.green(), '02X')
    rr = format(qcolor.red(), '02X')
    aa = format(255 - qcolor.alpha(), '02X')
    return f"&H{aa}{bb}{gg}{rr}"


class ColorPickerButton(QPushButton):
    def __init__(self, initial_ass_color="&H00FFFFFF", parent=None):
        super().__init__(parent)
        self.ass_color = initial_ass_color
        self.clicked.connect(self.pick_color)
        self._update_style()

    def pick_color(self):
        # ShowAlphaChannel — иначе диалог вообще не даёт крутить прозрачность
        # и молча возвращает цвет с alpha=255 (полная непрозрачность).
        color = QColorDialog.getColor(
            ass_color_to_qcolor(self.ass_color), self, tr("Выберите цвет"),
            QColorDialog.ShowAlphaChannel
        )
        if color.isValid():
            self.ass_color = qcolor_to_ass_color(color)
            self._update_style()

    def _update_style(self):
        qc = ass_color_to_qcolor(self.ass_color)
        opacity_pct = round(qc.alpha() / 255 * 100)
        self.setStyleSheet(f"background-color: {qc.name()}; border: 1px solid #888;")
        self.setText(tr("{}  ({}% непрозрачности)").format(self.ass_color, opacity_pct))

    def set_ass_color(self, ass_color):
        self.ass_color = ass_color
        self._update_style()


class StyleTab(QWidget):
    """Шаг 3: создание/редактирование JSON-пресетов стилей субтитров."""
    styles_changed = pyqtSignal()  # сигнал для обновления списка в BuildTab

    def __init__(self, parent=None):
        super().__init__(parent)
        self.current_slug = None
        self._alignment_options = alignment_options()
        self._build_ui()
        self.refresh_list()

    def _build_ui(self):
        layout = QHBoxLayout(self)

        # --- Левая колонка: список пресетов ---
        left_col = QVBoxLayout()
        left_col.addWidget(QLabel(tr("Пресеты стилей:")))
        self.style_list = QListWidget()
        self.style_list.currentItemChanged.connect(self.on_selected)
        left_col.addWidget(self.style_list)

        list_btn_row = QHBoxLayout()
        new_btn = QPushButton(tr("Новый"))
        new_btn.clicked.connect(self.new_style)
        save_as_btn = QPushButton(tr("Сохранить как…"))
        save_as_btn.clicked.connect(self.save_as_style)
        delete_btn = QPushButton(tr("Удалить"))
        delete_btn.clicked.connect(self.delete_style)
        list_btn_row.addWidget(new_btn)
        list_btn_row.addWidget(save_as_btn)
        list_btn_row.addWidget(delete_btn)
        left_col.addLayout(list_btn_row)

        layout.addLayout(left_col, stretch=1)

        # --- Правая колонка: прокручиваемая форма редактирования,
        # чтобы на маленьких экранах все поля были доступны, а кнопка
        # "Сохранить пресет" всегда оставалась на виду снизу, вне прокрутки.
        right_container = QVBoxLayout()

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QScrollArea.NoFrame)
        scroll_content = QWidget()
        right_col = QVBoxLayout(scroll_content)

        basic_group = QGroupBox(tr("Основное"))
        basic_form = QFormLayout()
        self.name_edit = QLineEdit()
        basic_form.addRow(tr("Название:"), self.name_edit)
        self.font_name_combo = QFontComboBox()
        self.font_name_combo.setCurrentFont(QFont("Arial"))
        basic_form.addRow(tr("Шрифт:"), self.font_name_combo)
        self.karaoke_check = QCheckBox(tr("Караоке-подсветка по словам"))
        self.karaoke_check.setChecked(True)
        basic_form.addRow("", self.karaoke_check)
        self.bold_check = QCheckBox(tr("Жирный"))
        self.bold_check.setChecked(True)
        basic_form.addRow("", self.bold_check)
        basic_group.setLayout(basic_form)
        right_col.addWidget(basic_group)

        size_group = QGroupBox(tr("Размер шрифта"))
        size_form = QFormLayout()
        self.size_mode_group = QButtonGroup(self)
        self.size_auto_radio = QRadioButton(tr("Авто (% от высоты видео)"))
        self.size_fixed_radio = QRadioButton(tr("Фиксированный (px)"))
        self.size_mode_group.addButton(self.size_auto_radio)
        self.size_mode_group.addButton(self.size_fixed_radio)
        self.size_auto_radio.setChecked(True)
        size_form.addRow(self.size_auto_radio)
        self.font_factor_spin = QDoubleSpinBox()
        self.font_factor_spin.setRange(0.01, 0.5)
        self.font_factor_spin.setSingleStep(0.005)
        self.font_factor_spin.setValue(0.04)
        size_form.addRow(tr("Множитель высоты:"), self.font_factor_spin)
        size_form.addRow(self.size_fixed_radio)
        self.font_fixed_spin = QSpinBox()
        self.font_fixed_spin.setRange(8, 200)
        self.font_fixed_spin.setValue(32)
        size_form.addRow(tr("Размер (px):"), self.font_fixed_spin)
        size_group.setLayout(size_form)
        right_col.addWidget(size_group)

        colors_group = QGroupBox(tr("Цвета"))
        colors_form = QFormLayout()
        self.primary_color_btn = ColorPickerButton("&H00FFFFFF")
        colors_form.addRow(tr("Текст:"), self.primary_color_btn)
        self.secondary_color_btn = ColorPickerButton("&H0000FFFF")
        colors_form.addRow(tr("Ещё не произнесено (караоке):"), self.secondary_color_btn)
        self.outline_color_btn = ColorPickerButton("&HB0000000")
        colors_form.addRow(tr("Обводка/фон:"), self.outline_color_btn)
        colors_group.setLayout(colors_form)
        right_col.addWidget(colors_group)

        layout_group = QGroupBox(tr("Расположение"))
        layout_form = QFormLayout()
        self.alignment_combo = QComboBox()
        for label, _ in self._alignment_options:
            self.alignment_combo.addItem(label)
        layout_form.addRow(tr("Выравнивание:"), self.alignment_combo)
        self.margin_l_spin = QSpinBox()
        self.margin_l_spin.setRange(0, 500)
        self.margin_l_spin.setValue(30)
        layout_form.addRow(tr("Отступ слева/справа:"), self.margin_l_spin)

        self.margin_v_mode_group = QButtonGroup(self)
        self.margin_v_auto_radio = QRadioButton(tr("Авто (% от высоты)"))
        self.margin_v_fixed_radio = QRadioButton(tr("Фиксированный (px)"))
        self.margin_v_mode_group.addButton(self.margin_v_auto_radio)
        self.margin_v_mode_group.addButton(self.margin_v_fixed_radio)
        self.margin_v_auto_radio.setChecked(True)
        layout_form.addRow(self.margin_v_auto_radio)
        self.margin_v_factor_spin = QDoubleSpinBox()
        self.margin_v_factor_spin.setRange(0.0, 0.9)
        self.margin_v_factor_spin.setSingleStep(0.01)
        self.margin_v_factor_spin.setValue(0.1)
        layout_form.addRow(tr("Множитель отступа снизу:"), self.margin_v_factor_spin)
        layout_form.addRow(self.margin_v_fixed_radio)
        self.margin_v_fixed_spin = QSpinBox()
        self.margin_v_fixed_spin.setRange(0, 2000)
        self.margin_v_fixed_spin.setValue(60)
        layout_form.addRow(tr("Отступ снизу (px):"), self.margin_v_fixed_spin)
        layout_group.setLayout(layout_form)
        right_col.addWidget(layout_group)

        box_group = QGroupBox(tr("Фиксированная плашка под текст"))
        box_form = QFormLayout()
        self.box_enabled_check = QCheckBox(
            tr("Включить (постоянный размер/позиция, текст переносится по ширине)")
        )
        self.box_enabled_check.setChecked(True)
        box_form.addRow("", self.box_enabled_check)

        self.box_width_spin = QDoubleSpinBox()
        self.box_width_spin.setRange(0.3, 1.0)
        self.box_width_spin.setSingleStep(0.02)
        self.box_width_spin.setValue(0.86)
        box_form.addRow(tr("Ширина плашки (доля от ширины видео):"), self.box_width_spin)

        self.box_lines_spin = QSpinBox()
        self.box_lines_spin.setRange(1, 4)
        self.box_lines_spin.setValue(2)
        box_form.addRow(tr("Число строк в плашке:"), self.box_lines_spin)

        self.box_padding_spin = QSpinBox()
        self.box_padding_spin.setRange(0, 100)
        self.box_padding_spin.setValue(16)
        box_form.addRow(tr("Внутренний отступ (px):"), self.box_padding_spin)

        self.box_radius_spin = QSpinBox()
        self.box_radius_spin.setRange(0, 100)
        self.box_radius_spin.setValue(18)
        box_form.addRow(tr("Радиус скругления углов (px):"), self.box_radius_spin)

        box_note = QLabel(
            tr("Если фраза не помещается в заданное число строк — она автоматически\n"
               "разбивается на несколько последовательных карточек одинакового размера.")
        )
        box_note.setStyleSheet("color: gray;")
        box_form.addRow(box_note)

        box_group.setLayout(box_form)
        right_col.addWidget(box_group)
        right_col.addStretch()

        scroll_area.setWidget(scroll_content)
        right_container.addWidget(scroll_area, stretch=1)

        save_row = QHBoxLayout()
        self.save_btn = QPushButton(tr("Сохранить пресет"))
        self.save_btn.setMinimumHeight(36)
        self.save_btn.clicked.connect(self.save_current)
        save_row.addWidget(self.save_btn)
        right_container.addLayout(save_row)

        layout.addLayout(right_container, stretch=2)

    # ---------- Логика списка ----------

    def refresh_list(self, select_slug=None):
        self.style_list.blockSignals(True)
        self.style_list.clear()
        for slug, display_name in style_manager.list_styles():
            self.style_list.addItem(display_name)
            item = self.style_list.item(self.style_list.count() - 1)
            item.setData(1000, slug)
        self.style_list.blockSignals(False)

        if select_slug:
            for i in range(self.style_list.count()):
                if self.style_list.item(i).data(1000) == select_slug:
                    self.style_list.setCurrentRow(i)
                    return
        elif self.style_list.count() > 0:
            self.style_list.setCurrentRow(0)

    def on_selected(self, current, previous):
        if not current:
            return
        slug = current.data(1000)
        self.load_into_form(slug)

    def new_style(self):
        name, ok = QInputDialog.getText(self, tr("Новый стиль"), tr("Название стиля:"))
        if not ok or not name.strip():
            return
        slug = style_manager.slugify(name)
        data = {
            "name": name.strip(),
            "font_name": "Arial",
            "font_size_fixed": None,
            "font_size_factor": 0.04,
            "primary_color": "&H00FFFFFF",
            "secondary_color": "&H0000FFFF",
            "outline_color": "&HB0000000",
            "back_color": "&HB0000000",
            "bold": True,
            "alignment": 2,
            "margin_l": 30,
            "margin_r": 30,
            "margin_v_fixed": None,
            "margin_v_factor": 0.1,
            "outline_fixed": None,
            "outline_factor": 0.12,
            "karaoke": True,
            "box_enabled": True,
            "box_width_factor": 0.86,
            "box_lines": 2,
            "box_padding": 16,
            "box_corner_radius": 18,
        }
        style_manager.save_style(slug, data)
        self.refresh_list(select_slug=slug)
        self.styles_changed.emit()

    def delete_style(self):
        item = self.style_list.currentItem()
        if not item:
            return
        slug = item.data(1000)
        confirm = QMessageBox.question(
            self, tr("Удалить стиль"), tr("Удалить пресет «{}»?").format(item.text()),
            QMessageBox.Yes | QMessageBox.No
        )
        if confirm == QMessageBox.Yes:
            style_manager.delete_style(slug)
            self.refresh_list()
            self.styles_changed.emit()

    # ---------- Форма ----------

    def load_into_form(self, slug):
        try:
            data = style_manager.load_style(slug)
        except Exception as e:
            QMessageBox.critical(self, tr("Ошибка"), tr("Не удалось загрузить стиль: {}").format(e))
            return

        self.current_slug = slug
        self.name_edit.setText(data.get("name", slug))
        self.font_name_combo.setCurrentFont(QFont(data.get("font_name", "Arial")))
        self.karaoke_check.setChecked(bool(data.get("karaoke", True)))
        self.bold_check.setChecked(bool(data.get("bold", True)))

        if data.get("font_size_fixed"):
            self.size_fixed_radio.setChecked(True)
            self.font_fixed_spin.setValue(int(data["font_size_fixed"]))
        else:
            self.size_auto_radio.setChecked(True)
            self.font_factor_spin.setValue(float(data.get("font_size_factor") or 0.04))

        self.primary_color_btn.set_ass_color(data.get("primary_color", "&H00FFFFFF"))
        self.secondary_color_btn.set_ass_color(data.get("secondary_color", "&H0000FFFF"))
        self.outline_color_btn.set_ass_color(data.get("outline_color", "&HB0000000"))

        alignment_val = data.get("alignment", 2)
        for i, (_, code) in enumerate(self._alignment_options):
            if code == alignment_val:
                self.alignment_combo.setCurrentIndex(i)
                break

        self.margin_l_spin.setValue(int(data.get("margin_l", 30)))

        if data.get("margin_v_fixed") is not None:
            self.margin_v_fixed_radio.setChecked(True)
            self.margin_v_fixed_spin.setValue(int(data["margin_v_fixed"]))
        else:
            self.margin_v_auto_radio.setChecked(True)
            self.margin_v_factor_spin.setValue(float(data.get("margin_v_factor") or 0.1))

        self.box_enabled_check.setChecked(bool(data.get("box_enabled", True)))
        self.box_width_spin.setValue(float(data.get("box_width_factor") or 0.86))
        self.box_lines_spin.setValue(int(data.get("box_lines") or 2))
        self.box_padding_spin.setValue(int(data.get("box_padding") if data.get("box_padding") is not None else 16))
        self.box_radius_spin.setValue(int(data.get("box_corner_radius") if data.get("box_corner_radius") is not None else 18))

    def _collect_form_data(self, name_override=None):
        alignment_code = self._alignment_options[self.alignment_combo.currentIndex()][1]
        return {
            "name": name_override if name_override is not None else (
                self.name_edit.text().strip() or self.current_slug
            ),
            "font_name": self.font_name_combo.currentFont().family() or "Arial",
            "font_size_fixed": self.font_fixed_spin.value() if self.size_fixed_radio.isChecked() else None,
            "font_size_factor": self.font_factor_spin.value() if self.size_auto_radio.isChecked() else None,
            "primary_color": self.primary_color_btn.ass_color,
            "secondary_color": self.secondary_color_btn.ass_color,
            "outline_color": self.outline_color_btn.ass_color,
            "back_color": self.outline_color_btn.ass_color,
            "bold": self.bold_check.isChecked(),
            "alignment": alignment_code,
            "margin_l": self.margin_l_spin.value(),
            "margin_r": self.margin_l_spin.value(),
            "margin_v_fixed": self.margin_v_fixed_spin.value() if self.margin_v_fixed_radio.isChecked() else None,
            "margin_v_factor": self.margin_v_factor_spin.value() if self.margin_v_auto_radio.isChecked() else None,
            "outline_fixed": None,
            "outline_factor": 0.12,
            "karaoke": self.karaoke_check.isChecked(),
            "box_enabled": self.box_enabled_check.isChecked(),
            "box_width_factor": self.box_width_spin.value(),
            "box_lines": self.box_lines_spin.value(),
            "box_padding": self.box_padding_spin.value(),
            "box_corner_radius": self.box_radius_spin.value(),
        }

    def save_current(self):
        if not self.current_slug:
            QMessageBox.warning(self, tr("Ошибка"), tr("Сначала создайте или выберите стиль."))
            return

        data = self._collect_form_data()
        style_manager.save_style(self.current_slug, data)
        self.refresh_list(select_slug=self.current_slug)
        self.styles_changed.emit()
        QMessageBox.information(self, tr("Сохранено"), tr("Стиль «{}» сохранён.").format(data['name']))

    def save_as_style(self):
        """
        Сохраняет текущие настройки формы как НОВЫЙ пресет: базовое имя
        текущего стиля + короткая метка от пользователя (например,
        "vertical" -> "vertical — music"). Позволяет один раз настроить
        шрифты/цвета/плашку и дальше плодить варианты под разные типы
        контента одним кликом, не настраивая всё заново.
        """
        if not self.current_slug:
            QMessageBox.warning(self, tr("Ошибка"), tr("Сначала создайте или выберите стиль, "
                                                 "от которого будем делать вариант."))
            return

        suffix, ok = QInputDialog.getText(
            self, tr("Сохранить как…"),
            tr("Короткая метка для варианта (например: music, book, novel):")
        )
        if not ok or not suffix.strip():
            return
        suffix = suffix.strip()

        base_name = self.name_edit.text().strip() or self.current_slug
        new_name = f"{base_name} — {suffix}"
        new_slug = style_manager.slugify(f"{self.current_slug}_{suffix}")

        data = self._collect_form_data(name_override=new_name)
        style_manager.save_style(new_slug, data)
        self.refresh_list(select_slug=new_slug)
        self.styles_changed.emit()
        QMessageBox.information(self, tr("Сохранено"), tr("Создан новый пресет «{}».").format(new_name))
