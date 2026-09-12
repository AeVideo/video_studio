"""
core/flow/executor.py

Исполнительный слой поверх core.accounts.AccountManager для задач Google
Flow (Veo 3.1 lite) через Camoufox. Сознательно НЕ хранит собственное
состояние ротации (текущий индекс, список аккаунтов, дневные лимиты) —
всё это уже реализовано в AccountManager (round-robin, daily_limit,
quota_exhausted с автосбросом по дате, персистентность). FlowExecutor
только спрашивает у AccountManager следующий доступный аккаунт, запускает
задачу в CamoufoxSession на его профиле и репортит результат обратно.

Более ранний набросок (flow_account_rotator.py) вёл собственный
self.current_index/self.accounts независимо от AccountManager — это давало
два источника правды и было решено не использовать: см. обсуждение в
сессии. Тот файл — просто референс, от него сознательно не отталкивались.
"""

import logging
from typing import Any, Callable, Optional

from core.accounts.account_manager import AccountManager
from core.flow.browser_session import CamoufoxSession

logger = logging.getLogger(__name__)


class QuotaExceededException(Exception):
    """Исключение при исчерпании лимита генерации на текущем Flow-аккаунте."""
    pass


class NoAvailableAccountsError(RuntimeError):
    """Все Flow-аккаунты исчерпали дневной лимит или помечены как заблокированные."""
    pass


_QUOTA_MARKERS = ("quota", "rate limit", "too many requests", "429")


class FlowExecutor:
    """Запускает task_fn(page) на следующем доступном Flow-аккаунте.

    task_fn получает объект playwright Page (или BrowserContext.new_page(),
    см. CamoufoxSession) и должен сам вызвать page.close() не обязан —
    закрытие происходит на выходе из `with CamoufoxSession(...)`.
    """

    def __init__(self, account_manager: AccountManager, headless: str = "virtual"):
        self.account_manager = account_manager
        self.headless = headless

    def execute_with_retry(self, task_fn: Callable[[Any], Any], max_attempts: Optional[int] = None) -> Any:
        limit = max_attempts if max_attempts is not None else max(len(self.account_manager.accounts), 1)
        attempts = 0
        last_error: Optional[Exception] = None

        while attempts < limit:
            account = self.account_manager.get_next_available_account()
            if account is None:
                raise NoAvailableAccountsError(
                    "Все доступные Flow-аккаунты исчерпали дневной лимит или заблокированы."
                )

            attempts += 1
            account_id = account["id"]
            profile_path = account.get("profile_path")
            logger.info(f"Flow: запуск задачи на аккаунте [{account_id}] (попытка {attempts}/{limit})")

            try:
                with CamoufoxSession(profile_path=profile_path, headless=self.headless) as browser:
                    page = browser.new_page()
                    try:
                        result = task_fn(page)
                    finally:
                        page.close()

                self.account_manager.mark_success(account_id)
                return result

            except QuotaExceededException as e:
                logger.warning(f"Flow [{account_id}]: лимит исчерпан ({e}) — переключаюсь на следующий аккаунт")
                self.account_manager.mark_quota_exhausted(account_id)
                last_error = e

            except Exception as e:
                err_str = str(e).lower()
                if any(marker in err_str for marker in _QUOTA_MARKERS):
                    logger.warning(f"Flow [{account_id}]: похоже на лимит запросов ({e}) — переключаюсь")
                    self.account_manager.mark_quota_exhausted(account_id)
                    last_error = e
                else:
                    # Непредвиденная ошибка (не связанная с лимитом) — не глотаем, пробрасываем сразу
                    raise

        raise NoAvailableAccountsError(
            f"Не удалось выполнить задачу за {limit} попыток. Последняя ошибка: {last_error}"
        )
