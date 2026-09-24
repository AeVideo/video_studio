"""
scraping/flow_login_worker.py

Отдельный entrypoint для логина в Flow Google Account через Camoufox.
Запускается PyQt через QProcess — НЕ в том же процессе что GUI,
чтобы Camoufox (Firefox) мог открыться headless=False в своём окне.

Использование:
    python -m scraping.flow_login_worker \\
        --account-id account_1 \\
        --profile-path /home/oasis/.flow_profiles/account_1

Протокол вывода:
    OK: <email>          — логин успешен
    ERROR: <message>     — что-то пошло не так

PyQt читает stdout через QProcess.readAllStandardOutput() и парсит первое слово.
"""

import argparse
import sys

from camoufox.sync_api import Camoufox


def _extract_email_from_page(page) -> str:
    """
    Извлекает email залогиненного аккаунта из DOM страницы Flow.
    Google OneBar хранит email в атрибуте aria-label кнопки аккаунта
    (вида 'Google Account: Name\n(email@gmail.com)').
    """
    try:
        # Ждём появления кнопки аккаунта в хедере
        account_btn = page.locator('[aria-label^="Google Account"]').first
        account_btn.wait_for(state="visible", timeout=10_000)
        label = account_btn.get_attribute("aria-label") or ""
        # Извлекаем email из скобок: "Google Account: Name\n(email@gmail.com)"
        import re
        m = re.search(r"\(([^)]+@[^)]+)\)", label)
        if m:
            return m.group(1)
    except Exception:
        pass

    # Fallback: ищем email в img.gb_X alt или в window.WIZ_global_data
    try:
        email = page.evaluate("""
            () => {
                const d = window.WIZ_global_data;
                return d && d.oPEP7c ? d.oPEP7c : null;
            }
        """)
        if email:
            return email
    except Exception:
        pass

    return "unknown@gmail.com"


def main():
    parser = argparse.ArgumentParser(description="Flow login worker (запускается через QProcess)")
    parser.add_argument("--account-id", required=True, help="ID аккаунта (для логов)")
    parser.add_argument("--profile-path", required=True, help="Путь к persistent Camoufox профилю")
    args = parser.parse_args()

    import os
    os.makedirs(args.profile_path, exist_ok=True)

    try:
        with Camoufox(
            headless=False,           # пользователь вводит данные вручную
            humanize=True,
            geoip=True,
            user_data_dir=args.profile_path,
            persistent_context=True,  # ОБЯЗАТЕЛЬНО — без этого профиль не сохранится
        ) as browser:
            page = browser.new_page()

            print(f"[flow_login_worker] Открываю flow.google.com для аккаунта {args.account_id}...",
                  file=sys.stderr)
            page.goto("https://flow.google.com/", timeout=30_000)

            # Ждём пока пользователь залогинится и увидит свои проекты
            # flow-projects-page появляется только после успешного логина
            print("[flow_login_worker] Жду логина пользователя (до 5 минут)...",
                  file=sys.stderr)
            page.wait_for_selector("flow-projects-page", timeout=300_000)

            email = _extract_email_from_page(page)

            # Протокол: "OK: email" в stdout — PyQt его читает
            print(f"OK: {email}", flush=True)
            print(f"[flow_login_worker] Логин успешен: {email}", file=sys.stderr)

    except Exception as e:
        print(f"ERROR: {e}", flush=True)
        print(f"[flow_login_worker] Ошибка: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
