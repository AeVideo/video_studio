"""
account_manager.py

Управление API-ключами (Pexels, Pixabay, DeepSeek) и Flow-аккаунтами
с дневными лимитами, ротацией и persistent-профилями.

Ключи НЕ сохраняются в JSON-файл конфигурации (чтобы не коммитить секреты).
Ключи берутся из переменных окружения:
  - PEXELS_API_KEY
  - PIXABAY_API_KEY
  - DEEPSEEK_API_KEY

Flow-аккаунты могут загружаться:
  1) из JSON-файла (через config_path) в формате {"accounts": [...]}
  2) из переменной окружения FLOW_ACCOUNTS_JSON (строка JSON)
  3) из отдельных переменных:
       FLOW_ACCOUNT_1_PROFILE_PATH=...
       FLOW_ACCOUNT_1_DAILY_LIMIT=...
       FLOW_ACCOUNT_2_PROFILE_PATH=...
       ...
"""

import json
import os
from datetime import date, datetime
from typing import Dict, Optional, List, Any


class AccountManager:
    """
    Менеджер аккаунтов и API-ключей.

    Основные методы:
      - get_api_key(service) -> Optional[str]
      - get_next_available_account() -> Optional[Dict]
      - mark_success(account_id)
      - mark_quota_exhausted(account_id)
      - reset_daily_counts_if_new_day()
      - save_config(path)
    """

    def __init__(
        self,
        accounts: Optional[List[Dict[str, Any]]] = None,
        api_keys: Optional[Dict[str, str]] = None,
        config_path: Optional[str] = None,
    ):
        # API-ключи не сохраняем в config-файле, только в памяти/окружении
        self.api_keys: Dict[str, str] = api_keys if api_keys is not None else {}

        # Аккаунты Flow
        self.accounts: List[Dict[str, Any]] = accounts if accounts is not None else []

        if config_path:
            self.load_config(config_path)
        else:
            self._load_from_env()

        # Начальный индекс для round-robin ротации
        self._index = 0

        # Сброс счётчиков, если наступил новый день
        self.reset_daily_counts_if_new_day()

    # ------------------------------------------------------------------ #
    # Загрузка данных
    # ------------------------------------------------------------------ #
    def load_config(self, config_path: str) -> None:
        """Загружает accounts из JSON-файла. API-ключи берутся из окружения."""
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.accounts = data.get("accounts", [])

    def _load_from_env(self) -> None:
        """Загружает API-ключи и accounts из переменных окружения."""
        # API-ключи
        for service in ("pexels", "pixabay", "deepseek"):
            env_key = f"{service.upper()}_API_KEY"
            if os.getenv(env_key):
                self.api_keys[service] = os.environ[env_key]

        # Аккаунты: либо JSON из FLOW_ACCOUNTS_JSON, либо отдельные переменные
        accounts_json = os.getenv("FLOW_ACCOUNTS_JSON")
        if accounts_json:
            try:
                data = json.loads(accounts_json)
                self.accounts = data if isinstance(data, list) else data.get("accounts", [])
                return
            except json.JSONDecodeError:
                self.accounts = []
                return

        # Пробуем прочитать несколько аккаунтов вида FLOW_ACCOUNT_<N>_*
        accounts = []
        i = 1
        while True:
            profile_path = os.getenv(f"FLOW_ACCOUNT_{i}_PROFILE_PATH")
            if not profile_path:
                break
            daily_limit = int(os.getenv(f"FLOW_ACCOUNT_{i}_DAILY_LIMIT", "5"))
            accounts.append({
                "id": f"account_{i}",
                "profile_path": profile_path,
                "daily_limit": daily_limit,
                "daily_count": 0,
                "quota_exhausted": False,
                "last_success": None,
            })
            i += 1

        self.accounts = accounts

    # ------------------------------------------------------------------ #
    # API-ключи
    # ------------------------------------------------------------------ #
    def get_api_key(self, service: str) -> Optional[str]:
        """
        Возвращает ключ для указанного сервиса (pexels, pixabay, deepseek).
        Сначала ищет в переданных/загруженных api_keys, затем в окружении.
        """
        if service in self.api_keys and self.api_keys[service]:
            return self.api_keys[service]

        env_key = f"{service.upper()}_API_KEY"
        return os.getenv(env_key)

    # ------------------------------------------------------------------ #
    # Аккаунты Flow
    # ------------------------------------------------------------------ #
    def get_next_available_account(self) -> Optional[Dict[str, Any]]:
        """
        Возвращает копию следующего доступного аккаунта (лимит не исчерпан,
        quota_exhausted == False). Реализует round-robin от последнего выданного.

        Если доступных аккаунтов нет — возвращает None.
        """
        available_ids = {
            acc["id"]
            for acc in self.accounts
            if not acc.get("quota_exhausted", False)
            and acc.get("daily_count", 0) < acc.get("daily_limit", 0)
        }

        if not available_ids:
            return None

        n = len(self.accounts)
        for offset in range(n):
            idx = (self._index + offset) % n
            acc = self.accounts[idx]
            if acc["id"] in available_ids:
                self._index = (idx + 1) % n  # следующий вызов начнёт со следующего
                return acc.copy()

        # Не должно случиться, но на всякий случай
        return None

    def mark_success(self, account_id: str) -> None:
        """Увеличивает daily_count и обновляет дату последнего успеха."""
        for acc in self.accounts:
            if acc["id"] == account_id:
                acc["daily_count"] = acc.get("daily_count", 0) + 1
                acc["last_success"] = date.today().isoformat()
                return

    def mark_quota_exhausted(self, account_id: str) -> None:
        """Помечает аккаунт как исчерпавший дневную квоту."""
        for acc in self.accounts:
            if acc["id"] == account_id:
                acc["quota_exhausted"] = True
                return

    def reset_daily_counts_if_new_day(self) -> None:
        """
        Если last_success отличается от сегодняшней даты (или отсутствует) —
        сбрасывает daily_count и quota_exhausted.
        """
        today = date.today().isoformat()
        for acc in self.accounts:
            if acc.get("last_success") != today:
                acc["daily_count"] = 0
                acc["quota_exhausted"] = False

    # ------------------------------------------------------------------ #
    # Сохранение конфигурации (только аккаунты, без ключей)
    # ------------------------------------------------------------------ #
    def save_config(self, path: str) -> None:
        """Сохраняет список аккаунтов в JSON. API-ключи не записываются."""
        payload = {"accounts": self.accounts}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    def __repr__(self) -> str:
        return f"<AccountManager accounts={len(self.accounts)} api_keys={list(self.api_keys.keys())}>"
