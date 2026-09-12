"""
core/flow/browser_session.py

Обёртка над Camoufox для взаимодействия с веб-интерфейсом Google Flow
(Veo 3.1 lite) через Playwright + Camoufox — БЕЗ официального API.

Это НЕ дубликат scraping/cloudflare_session.py — тот файл целиком заточен
под vault.fbi.gov (Cloudflare Managed Challenge, методы вроде get_html,
download_via_click, _dismiss_disclaimer) и уже используется в проде, трогать
его рискованно. Здесь переиспользован только сам паттерн запуска Camoufox
с постоянным профилем, который уже провалидирован в том файле:

  user_data_dir САМ ПО СЕБЕ ничего не даёт — Camoufox решает, вызывать
  playwright.firefox.launch() или launch_persistent_context(), ТОЛЬКО по
  отдельному флагу persistent_context=True (см. camoufox/utils.py и
  playwright.sync_api). Без этого флага user_data_dir остаётся пустым и
  логин/cookies НЕ переживают перезапуск — это не гипотеза, а
  задокументированное поведение (github.com/daijro/camoufox issues #116,
  #253, #537: "user_data_dir remains empty, no files created").

  При persistent_context=True Camoufox возвращает BrowserContext вместо
  Browser — у него тоже есть .new_page(), поэтому дальнейший код работает
  одинаково в обоих режимах.
"""

import logging
import os
from typing import Optional

from camoufox.sync_api import Camoufox

logger = logging.getLogger(__name__)


class CamoufoxSession:
    """Контекстный менеджер браузера с привязкой к конкретному профилю аккаунта.

    profile_path — путь к каталогу постоянного профиля (уже созданному
    AccountManager/конфигом, см. core/accounts/account_manager.py). Если не
    передан — обычный launch() без персистентности (для разовых задач/тестов).
    """

    def __init__(
        self,
        profile_path: Optional[str] = None,
        headless: str = "virtual",
        humanize: bool = True,
        geoip: bool = True,
    ):
        self.profile_path = profile_path
        kwargs = {"headless": headless, "humanize": humanize, "geoip": geoip}
        if profile_path:
            os.makedirs(profile_path, exist_ok=True)
            kwargs["user_data_dir"] = profile_path
            kwargs["persistent_context"] = True  # см. докстринг модуля — без этого профиль не работает

        self._cm = Camoufox(**kwargs)
        self.browser = None

    def __enter__(self):
        logger.info(f"Запуск Flow-браузера [profile={self.profile_path or '(без профиля)'}]")
        self.browser = self._cm.__enter__()
        return self.browser

    def __exit__(self, exc_type, exc_val, exc_tb):
        logger.info(f"Закрытие Flow-браузера [profile={self.profile_path or '(без профиля)'}]")
        return self._cm.__exit__(exc_type, exc_val, exc_tb)
