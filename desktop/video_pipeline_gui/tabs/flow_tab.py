"""
desktop/video_pipeline_gui/tabs/flow_tab.py

Экран управления Flow-аккаунтами и генерации видео через Veo 3.1.
Паттерн: экран только эмитит сигналы наверх, не запускает воркеры сам.
Воркеры (FlowGenerateWorker) — в workers.py.

Добавить в main_window.py:
    from desktop.video_pipeline_gui.tabs.flow_tab import FlowScreen, FlowGenerateWorker
"""

import json
import os

from PyQt5.QtCore import QProcess, QThread, pyqtSignal, Qt
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QComboBox,
    QPushButton, QTextEdit, QProgressBar, QListWidget, QListWidgetItem,
    QFileDialog, QSpinBox, QMessageBox, QGroupBox,
)


# ------------------------------------------------------------------ #
# QThread воркер генерации
# ------------------------------------------------------------------ #

class FlowGenerateWorker(QThread):
    """
    Запускает generate_video() через FlowExecutor в отдельном потоке.
    Паттерн: тот же что ScriptWorker / AssembleWorker в workers.py.
    """
    log_line   = pyqtSignal(str)
    progress   = pyqtSignal(int, str)   # percent, message
    finished_ok = pyqtSignal(bool, object)  # success, result_dict или str ошибки

    def __init__(
        self,
        account_manager,
        prompt: str,
        model_ui_name: str,
        aspect: str,
        quantity: int,
        output_dir: str,
        first_frame_path: str = "",
        project_id: str = "",
        timeout_seconds: int = 900,
        parent=None,
    ):
        super().__init__(parent)
        self.account_manager = account_manager
        self.prompt = prompt
        self.model_ui_name = model_ui_name
        self.aspect = aspect
        self.quantity = quantity
        self.output_dir = output_dir
        self.first_frame_path = first_frame_path or None
        self.project_id = project_id or None
        self.timeout_seconds = timeout_seconds

    def run(self):
        try:
            from core.flow.executor import FlowExecutor
            from core.flow.actions import generate_video

            executor = FlowExecutor(self.account_manager)

            self.log_line.emit("Запускаю браузер Flow...")
            self.progress.emit(5, "Открываю браузер")

            def task(page):
                self.log_line.emit("Открываю Flow...")
                self.progress.emit(10, "Загружаю Flow")
                result = generate_video(
                    page=page,
                    prompt=self.prompt,
                    model_ui_name=self.model_ui_name,
                    aspect=self.aspect,
                    quantity=self.quantity,
                    first_frame_path=self.first_frame_path,
                    output_dir=self.output_dir,
                    timeout_seconds=self.timeout_seconds,
                    project_id=self.project_id,
                )
                self.log_line.emit(f"Генерация принята: media_id={result['media_id']}")
                self.progress.emit(50, "Ожидаю завершения генерации")
                return result

            result = executor.execute_with_retry(task)
            self.progress.emit(90, "Скачиваю видео")
            self.log_line.emit(f"Готово: {result['video_path']}")
            self.progress.emit(100, "Завершено")
            self.finished_ok.emit(True, result)

        except Exception as e:
            self.log_line.emit(f"Ошибка: {e}")
            self.finished_ok.emit(False, str(e))


# ------------------------------------------------------------------ #
# Экран Flow
# ------------------------------------------------------------------ #

class FlowScreen(QWidget):
    """
    Экран управления Flow-аккаунтами и генерации видео.

    Сигналы:
        generate_requested(prompt, model_ui, aspect, quantity,
                           output_dir, first_frame, project_id)
    """
    generate_requested = pyqtSignal(str, str, str, int, str, str, str)

    def __init__(self, account_manager=None, output_dir: str = ".", parent=None):
        super().__init__(parent)
        self.account_manager = account_manager
        self.output_dir = output_dir
        self._login_processes: dict[str, QProcess] = {}
        self._worker: FlowGenerateWorker | None = None

        layout = QVBoxLayout(self)

        # ---- Аккаунты ----
        acc_group = QGroupBox("Flow-аккаунты")
        acc_layout = QVBoxLayout(acc_group)

        self.accounts_list = QListWidget()
        self.accounts_list.setMaximumHeight(120)
        acc_layout.addWidget(self.accounts_list)

        acc_btns = QHBoxLayout()
        self.add_account_btn = QPushButton("Добавить аккаунт")
        self.add_account_btn.clicked.connect(self._on_add_account)
        self.relogin_btn = QPushButton("Войти заново")
        self.relogin_btn.clicked.connect(self._on_relogin)
        acc_btns.addWidget(self.add_account_btn)
        acc_btns.addWidget(self.relogin_btn)
        acc_layout.addLayout(acc_btns)
        layout.addWidget(acc_group)

        # ---- Параметры генерации ----
        gen_group = QGroupBox("Генерация видео")
        gen_layout = QVBoxLayout(gen_group)

        gen_layout.addWidget(QLabel("Промпт (на английском):"))
        self.prompt_edit = QTextEdit()
        self.prompt_edit.setPlaceholderText(
            "Describe the scene in English. Example:\n"
            "A lone detective walks through foggy 1920s streets at night, "
            "cinematic lighting, film noir style"
        )
        self.prompt_edit.setMaximumHeight(100)
        gen_layout.addWidget(self.prompt_edit)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Модель:"))
        self.model_combo = QComboBox()
        self.model_combo.addItems([
            "Veo 3.1 Lite",
            "Veo 3.1 Fast",
            "Veo 3.1 Quality",
            "Omni 1.1 Flash",
        ])
        row1.addWidget(self.model_combo)

        row1.addWidget(QLabel("Aspect:"))
        self.aspect_combo = QComboBox()
        self.aspect_combo.addItems(["16:9", "9:16"])
        row1.addWidget(self.aspect_combo)

        row1.addWidget(QLabel("Кол-во:"))
        self.quantity_spin = QSpinBox()
        self.quantity_spin.setRange(1, 4)
        self.quantity_spin.setValue(1)
        row1.addWidget(self.quantity_spin)
        gen_layout.addLayout(row1)

        # Первый кадр (опционально)
        frame_row = QHBoxLayout()
        frame_row.addWidget(QLabel("Первый кадр (опц.):"))
        self.first_frame_edit = QLineEdit()
        self.first_frame_edit.setPlaceholderText("не выбран")
        frame_row.addWidget(self.first_frame_edit)
        browse_frame_btn = QPushButton("Обзор…")
        browse_frame_btn.clicked.connect(self._browse_first_frame)
        frame_row.addWidget(browse_frame_btn)
        gen_layout.addLayout(frame_row)

        # Project ID (опционально)
        pid_row = QHBoxLayout()
        pid_row.addWidget(QLabel("Project ID (опц.):"))
        self.project_id_edit = QLineEdit()
        self.project_id_edit.setPlaceholderText("UUID проекта Flow (оставь пустым для главной)")
        pid_row.addWidget(self.project_id_edit)
        gen_layout.addLayout(pid_row)

        # Output dir
        out_row = QHBoxLayout()
        out_row.addWidget(QLabel("Сохранить в:"))
        self.output_dir_edit = QLineEdit(self.output_dir)
        out_row.addWidget(self.output_dir_edit)
        browse_out_btn = QPushButton("Обзор…")
        browse_out_btn.clicked.connect(self._browse_output_dir)
        out_row.addWidget(browse_out_btn)
        gen_layout.addLayout(out_row)

        self.generate_btn = QPushButton("Сгенерировать видео")
        self.generate_btn.clicked.connect(self._on_generate)
        gen_layout.addWidget(self.generate_btn)

        layout.addWidget(gen_group)

        # ---- Прогресс и лог ----
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumHeight(150)
        layout.addWidget(self.log_view)

        self.open_btn = QPushButton("Открыть видео")
        self.open_btn.setEnabled(False)
        self.open_btn.clicked.connect(self._open_video)
        layout.addWidget(self.open_btn)

        self._output_path = None
        layout.addStretch()

        # Загружаем список аккаунтов
        self._refresh_accounts()

    # ------------------------------------------------------------------ #
    # Аккаунты
    # ------------------------------------------------------------------ #

    def set_account_manager(self, account_manager) -> None:
        self.account_manager = account_manager
        self._refresh_accounts()

    def _refresh_accounts(self) -> None:
        self.accounts_list.clear()
        if not self.account_manager:
            return
        for acc in self.account_manager.accounts:
            daily = acc.get("daily_count", 0)
            limit = acc.get("daily_limit", 0)
            exhausted = acc.get("quota_exhausted", False)
            status = "⛔ лимит" if exhausted else f"{daily}/{limit}"
            profile = acc.get("profile_path", "?")
            item = QListWidgetItem(f"{acc['id']}  —  {status}  —  {profile}")
            self.accounts_list.addItem(item)

    def _on_add_account(self) -> None:
        """Диалог добавления нового аккаунта."""
        from desktop.video_pipeline_gui.tabs.flow_login_dialog import FlowLoginDialog
        dlg = FlowLoginDialog(parent=self)
        if dlg.exec_() and dlg.result_account:
            if self.account_manager:
                self.account_manager.accounts.append(dlg.result_account)
                if self.account_manager.config_path:
                    self.account_manager.save_config(self.account_manager.config_path)
            self._refresh_accounts()

    def _on_relogin(self) -> None:
        """Переlogинивает выбранный аккаунт."""
        row = self.accounts_list.currentRow()
        if row < 0 or not self.account_manager:
            return
        acc = self.account_manager.accounts[row]
        self._start_login_process(acc["id"], acc.get("profile_path", ""))

    def _start_login_process(self, account_id: str, profile_path: str) -> None:
        """Запускает flow_login_worker через QProcess."""
        if account_id in self._login_processes:
            proc = self._login_processes[account_id]
            if proc.state() == QProcess.Running:
                QMessageBox.information(self, "Логин", f"Логин для {account_id} уже запущен.")
                return

        proc = QProcess(self)
        proc.setProgram("python")
        proc.setArguments([
            "-m", "scraping.flow_login_worker",
            "--account-id", account_id,
            "--profile-path", profile_path,
        ])
        proc.readyReadStandardOutput.connect(
            lambda: self._on_login_stdout(account_id, proc)
        )
        proc.finished.connect(
            lambda code, _: self._on_login_finished(account_id, code)
        )
        self._login_processes[account_id] = proc
        proc.start()
        self.log_view.append(f"Логин запущен для {account_id}...")

    def _on_login_stdout(self, account_id: str, proc: QProcess) -> None:
        data = bytes(proc.readAllStandardOutput()).decode("utf-8", errors="replace").strip()
        for line in data.splitlines():
            if line.startswith("OK:"):
                email = line[3:].strip()
                self.log_view.append(f"✓ {account_id}: залогинен как {email}")
                # Обновляем email в аккаунте если нужно
                if self.account_manager:
                    for acc in self.account_manager.accounts:
                        if acc["id"] == account_id:
                            acc["email"] = email
                self._refresh_accounts()
            elif line.startswith("ERROR:"):
                self.log_view.append(f"✗ {account_id}: {line}")

    def _on_login_finished(self, account_id: str, exit_code: int) -> None:
        if exit_code != 0:
            self.log_view.append(f"⚠ Логин-процесс {account_id} завершился с кодом {exit_code}")

    # ------------------------------------------------------------------ #
    # Генерация
    # ------------------------------------------------------------------ #

    def _browse_first_frame(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Выберите изображение первого кадра", "",
            "Изображения (*.jpg *.jpeg *.png *.webp)"
        )
        if path:
            self.first_frame_edit.setText(path)

    def _browse_output_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Выберите папку для сохранения")
        if path:
            self.output_dir_edit.setText(path)

    def _on_generate(self) -> None:
        prompt = self.prompt_edit.toPlainText().strip()
        if not prompt:
            QMessageBox.warning(self, "Промпт", "Введите промпт для генерации.")
            return

        if not self.account_manager or not self.account_manager.accounts:
            QMessageBox.warning(self, "Аккаунты", "Добавьте хотя бы один Flow-аккаунт.")
            return

        output_dir = self.output_dir_edit.text().strip() or self.output_dir

        self._worker = FlowGenerateWorker(
            account_manager=self.account_manager,
            prompt=prompt,
            model_ui_name=self.model_combo.currentText(),
            aspect=self.aspect_combo.currentText(),
            quantity=self.quantity_spin.value(),
            output_dir=output_dir,
            first_frame_path=self.first_frame_edit.text().strip(),
            project_id=self.project_id_edit.text().strip(),
            parent=self,
        )
        self._worker.log_line.connect(self.log_view.append)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished_ok.connect(self._on_generate_finished)

        self.generate_btn.setEnabled(False)
        self.open_btn.setEnabled(False)
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(True)
        self.status_label.setText("Генерирую...")
        self._worker.start()

    def _on_progress(self, percent: int, message: str) -> None:
        self.progress_bar.setValue(percent)
        self.status_label.setText(message)

    def _on_generate_finished(self, success: bool, result) -> None:
        self.generate_btn.setEnabled(True)
        self.progress_bar.setVisible(False)

        if success and isinstance(result, dict):
            self._output_path = result.get("video_path")
            dur = result.get("duration_sec")
            dur_str = f" ({dur}с)" if dur else ""
            self.status_label.setText(f"Готово{dur_str}: {self._output_path}")
            self.open_btn.setEnabled(bool(self._output_path))
        else:
            self.status_label.setText(f"Ошибка: {result}")

    def _open_video(self) -> None:
        if not self._output_path:
            return
        import subprocess
        try:
            subprocess.Popen(["xdg-open", self._output_path])
        except Exception as e:
            QMessageBox.warning(self, "Не удалось открыть", str(e))
