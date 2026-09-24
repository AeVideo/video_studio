"""
desktop/video_pipeline_gui/tabs/flow_login_dialog.py

Диалог добавления нового Flow-аккаунта.
Запускает flow_login_worker через QProcess, ждёт "OK: email".
"""

import os
import uuid

from PyQt5.QtCore import QProcess, Qt
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QProgressBar, QMessageBox,
)


class FlowLoginDialog(QDialog):
    """
    Диалог:
      1. Поле для имени аккаунта (автозаполняется как account_N)
      2. Поле для пути к профилю (автозаполняется)
      3. Кнопка «Начать логин» → запускает QProcess(flow_login_worker)
      4. Статус-бар / сообщение
      5. Кнопка «Отмена»

    После успешного логина: self.result_account — dict аккаунта.
    """

    def __init__(self, profiles_base_dir: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Добавить Flow-аккаунт")
        self.setMinimumWidth(480)
        self.result_account = None
        self._process: QProcess | None = None

        if not profiles_base_dir:
            profiles_base_dir = os.path.expanduser("~/.flow_profiles")

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("<b>Добавление Flow / Google аккаунта</b>"))
        layout.addWidget(QLabel(
            "Нажмите «Начать логин» — откроется браузер.\n"
            "Войдите в Google и дождитесь загрузки flow.google.com."
        ))

        # Имя аккаунта
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Имя аккаунта:"))
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("account_1")
        name_row.addWidget(self.name_edit)
        layout.addLayout(name_row)

        # Путь к профилю
        path_row = QHBoxLayout()
        path_row.addWidget(QLabel("Путь к профилю:"))
        self.profile_path_edit = QLineEdit()
        path_row.addWidget(self.profile_path_edit)
        browse_btn = QPushButton("Обзор…")
        browse_btn.clicked.connect(self._browse_profile)
        path_row.addWidget(browse_btn)
        layout.addLayout(path_row)

        # Лимит
        limit_row = QHBoxLayout()
        limit_row.addWidget(QLabel("Дневной лимит генераций:"))
        from PyQt5.QtWidgets import QSpinBox
        self.limit_spin = QSpinBox()
        self.limit_spin.setRange(1, 50)
        self.limit_spin.setValue(5)
        limit_row.addWidget(self.limit_spin)
        layout.addLayout(limit_row)

        # Автозаполнение имени и пути
        account_id = f"account_{uuid.uuid4().hex[:6]}"
        self.name_edit.setText(account_id)
        self.profile_path_edit.setText(os.path.join(profiles_base_dir, account_id))

        # Прогресс
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)  # indeterminate
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        # Кнопки
        btns = QHBoxLayout()
        self.login_btn = QPushButton("Начать логин")
        self.login_btn.clicked.connect(self._start_login)
        self.cancel_btn = QPushButton("Отмена")
        self.cancel_btn.clicked.connect(self._on_cancel)
        btns.addWidget(self.login_btn)
        btns.addWidget(self.cancel_btn)
        layout.addLayout(btns)

    def _browse_profile(self) -> None:
        from PyQt5.QtWidgets import QFileDialog
        path = QFileDialog.getExistingDirectory(self, "Выберите папку профиля")
        if path:
            self.profile_path_edit.setText(path)

    def _start_login(self) -> None:
        account_id = self.name_edit.text().strip()
        profile_path = self.profile_path_edit.text().strip()

        if not account_id:
            QMessageBox.warning(self, "Имя", "Введите имя аккаунта.")
            return
        if not profile_path:
            QMessageBox.warning(self, "Профиль", "Укажите путь к профилю.")
            return

        os.makedirs(profile_path, exist_ok=True)

        self.login_btn.setEnabled(False)
        self.progress.setVisible(True)
        self.status_label.setText("Открываю браузер, войдите в Google...")

        self._process = QProcess(self)
        self._process.setProgram("python")
        self._process.setArguments([
            "-m", "scraping.flow_login_worker",
            "--account-id", account_id,
            "--profile-path", profile_path,
        ])
        self._process.readyReadStandardOutput.connect(self._on_stdout)
        self._process.readyReadStandardError.connect(self._on_stderr)
        self._process.finished.connect(self._on_finished)
        self._process.start()

    def _on_stdout(self) -> None:
        data = bytes(self._process.readAllStandardOutput()).decode("utf-8", errors="replace")
        for line in data.strip().splitlines():
            if line.startswith("OK:"):
                email = line[3:].strip()
                self._on_success(email)
            elif line.startswith("ERROR:"):
                self._on_error(line[6:].strip())

    def _on_stderr(self) -> None:
        data = bytes(self._process.readAllStandardError()).decode("utf-8", errors="replace")
        self.status_label.setText(data.strip().splitlines()[-1] if data.strip() else "")

    def _on_finished(self, exit_code: int, _) -> None:
        self.progress.setVisible(False)
        self.login_btn.setEnabled(True)
        if exit_code != 0 and self.result_account is None:
            self.status_label.setText(f"Процесс завершился с кодом {exit_code}")

    def _on_success(self, email: str) -> None:
        account_id = self.name_edit.text().strip()
        profile_path = self.profile_path_edit.text().strip()
        self.result_account = {
            "id": account_id,
            "email": email,
            "profile_path": profile_path,
            "daily_limit": self.limit_spin.value(),
            "daily_count": 0,
            "quota_exhausted": False,
            "last_success": None,
            "last_reset_date": None,
        }
        self.status_label.setText(f"✓ Залогинен как {email}")
        self.progress.setVisible(False)
        self.accept()

    def _on_error(self, message: str) -> None:
        self.progress.setVisible(False)
        self.login_btn.setEnabled(True)
        self.status_label.setText(f"Ошибка: {message}")

    def _on_cancel(self) -> None:
        if self._process and self._process.state() == QProcess.Running:
            self._process.terminate()
        self.reject()
