"""
fbi_vault_index.py

Полный индекс дел FBI Vault (~2000+) лежит на ОДНОЙ странице
(reading-room-index) — без пагинации, просто список ссылок под буквами
A-Z. Значит, можно скачать один раз, сохранить локально и дальше искать
по кэшу мгновенно, без обращения к сети при каждом клике в GUI.

Использование:
    from fbi_vault_index import load_or_build_index, search_index

    index = load_or_build_index()       # первый раз скачает, потом из кэша
    results = search_index(index, "купер")   # подстрочный поиск без учёта регистра
"""

import json
import os
import time
from typing import List, Dict

from bs4 import BeautifulSoup
from scraping.cloudflare_session import CamoufoxSession

INDEX_URL = "https://vault.fbi.gov/reading-room-index"
from config.paths import SCRAPER_CACHE_DIR
CACHE_PATH = os.path.join(SCRAPER_CACHE_DIR, "fbi_vault_index.json")
CACHE_MAX_AGE_DAYS = 30  # индекс дел обновляется редко, месяц — разумный срок


def build_index() -> List[Dict]:
    """Скачивает и парсит полный индекс дел с vault.fbi.gov. Разовая операция —
    страница одна, без пагинации."""
    with CamoufoxSession() as cf:
        html = cf.get_html(INDEX_URL, wait_seconds=20.0)  # первый заход — время на челлендж

    soup = BeautifulSoup(html, "html.parser")

    # Ссылки на дела идут после заголовка "The Vault Index" — но проще и
    # надёжнее просто взять все ссылки на vault.fbi.gov, исключив служебные
    # (меню, футер, якоря на буквы A-Z).
    excluded_paths = {
        "", "search", "about-vault", "browse-files", "proactive-disclosure",
        "reading-room-index", "explanation-of-exemptions", "fdps-1",
    }

    cases = []
    seen_urls = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.startswith("https://vault.fbi.gov/"):
            continue
        if "#" in href:  # якоря на буквы A-Z
            continue
        path = href.replace("https://vault.fbi.gov/", "").strip("/")
        if path.lower() in excluded_paths or "@@" in path:
            continue

        title = a.get_text(strip=True)
        if not title or href in seen_urls:
            continue
        seen_urls.add(href)
        cases.append({"title": title, "url": href})

    return cases


def load_or_build_index(force_refresh: bool = False) -> List[Dict]:
    if not force_refresh and os.path.exists(CACHE_PATH):
        age_days = (time.time() - os.path.getmtime(CACHE_PATH)) / 86400
        if age_days < CACHE_MAX_AGE_DAYS:
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)

    cases = build_index()
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cases, f, ensure_ascii=False, indent=2)
    return cases


def search_index(index: List[Dict], query: str, limit: int = 50) -> List[Dict]:
    q = query.lower().strip()
    if not q:
        return index[:limit]
    return [c for c in index if q in c["title"].lower()][:limit]


if __name__ == "__main__":
    # Быстрая проверка: python fbi_vault_index.py "купер"
    import sys
    idx = load_or_build_index()
    print(f"Всего дел в индексе: {len(idx)}")
    if len(sys.argv) > 1:
        results = search_index(idx, sys.argv[1])
        print(f"Найдено по запросу '{sys.argv[1]}': {len(results)}")
        for r in results[:20]:
            print(f"  {r['title']} -> {r['url']}")
