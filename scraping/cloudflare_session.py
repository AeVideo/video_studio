"""
cloudflare_session.py

vault.fbi.gov стоит за Cloudflare Managed Challenge — это общая анти-бот
инфраструктура (используется миллионами сайтов), требующая исполнения
JavaScript, а не намеренная защита ФБР именно этих данных: сами данные
открыты по закону, без логина и лимитов.

Camoufox проходит челлендж надёжно (проверено — реальный контент
загружается). Но разовая передача cookies в обычный requests оказалась
ненадёжной — при ручном переносе cookies между инструментами часто
теряются служебные атрибуты, из-за чего Cloudflare всё равно блокирует.
Поэтому вместо разовой передачи держим Camoufox открытым на всю сессию
скрейпинга: HTML — через настоящую навигацию браузера, бинарные файлы
(PDF) — через context.request, который использует ТОТ ЖЕ cookie jar
напрямую, без сериализации/десериализации.

Установка:
    pip install "camoufox[geoip]"
    python3 -m camoufox fetch
    sudo apt install -y libgtk-3-0t64 libnss3 libnspr4 libasound2t64 \
        libdbus-glib-1-2 libxt6t64 libgbm1 libdrm2 fonts-liberation xvfb
"""

from typing import Optional

from camoufox.sync_api import Camoufox


class CamoufoxSession:
    """Держит один браузер и одну страницу на весь скрейпинг одного дела/индекса.
    Первый переход обычно требует времени на прохождение челленджа, дальше —
    в рамках той же сессии — Cloudflare уже доверяет, переходы быстрее.

    Поддержка профилей:
      profile_path — путь к постоянному профилю (persistent user-data directory).
      Если передан, Camoufox запускается с этим профилем, что позволяет
      переиспользовать cookies, localStorage и другие данные между сессиями
      (например, для разных Flow/Google-аккаунтов в будущем).
    """

    def __init__(self, headless: str = "virtual", humanize: bool = True,
                 profile_path: Optional[str] = None):
        self._cm = Camoufox(headless=headless, humanize=humanize, profile=profile_path)
        self.browser = None
        self.page = None

    def __enter__(self):
        self.browser = self._cm.__enter__()
        self.page = self.browser.new_page()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._cm.__exit__(exc_type, exc_val, exc_tb)

    def get_html(self, url: str, wait_seconds: float = 5.0) -> str:
        self.page.goto(url, timeout=30000)
        self.page.wait_for_timeout(int(wait_seconds * 1000))
        self._dismiss_disclaimer()
        return self.page.content()

    def get_binary(self, url: str) -> bytes:
        """ВНИМАНИЕ: этот путь качает файл через APIRequestContext — он делит
        cookie jar со страницей, но идёт отдельным HTTP-клиентом Playwright
        в обход самого браузерного процесса. У Cloudflare Managed Challenge
        это иногда даёт другой TLS/JS-отпечаток, чем настоящая навигация
        браузера, и запрос всё равно заворачивается на страницу challenge'а
        ('Just a moment...') несмотря на валидную cf_clearance cookie.
        Для vault.fbi.gov используй download_via_click() ниже — он надёжнее."""
        response = self.page.context.request.get(url)
        return response.body()

    def download_via_click(self, link_selector: str, timeout: int = 30000) -> bytes:
        """Скачивает файл кликом по ссылке на уже открытой странице — это ближе
        всего к поведению настоящего пользователя: запрос идёт из того же
        браузерного процесса (тот же TLS/JS fingerprint, что уже прошёл
        Cloudflare), с корректным Referer на страницу part'а и с уже
        установленной cf_clearance cookie. В отличие от get_binary() не
        уходит в отдельный HTTP-клиент, поэтому Cloudflare не отличает такой
        запрос от обычного клика по ссылке."""
        with self.page.expect_download(timeout=timeout) as download_info:
            self.page.locator(link_selector).first.click()
        download = download_info.value
        tmp_path = download.path()
        with open(tmp_path, "rb") as f:
            return f.read()

    def _dismiss_disclaimer(self):
        """У Vault есть модальное окно-дисклеймер при первом заходе — закрываем,
        если оно есть, чтобы не мешало парсингу/последующим кликам."""
        try:
            close_btn = self.page.get_by_text("Close", exact=True)
            if close_btn.is_visible(timeout=1000):
                close_btn.click()
        except Exception:
            pass
