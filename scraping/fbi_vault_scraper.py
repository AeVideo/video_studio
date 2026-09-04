"""
fbi_vault_scraper.py

Скачивает материалы конкретного дела из FBI Vault (vault.fbi.gov) и
склеивает их в один .txt, готовый к скармливанию в book_to_script.py.

Vault защищён Cloudflare Managed Challenge — обычный requests/curl не
проходит его без исполнения JS. Все запросы (HTML и PDF) идут через
одну постоянную сессию Camoufox (см. cloudflare_session.py), а не через
requests напрямую.

Часть документов — сканы бумажных архивов без текстового слоя в PDF,
для них автоматически включается OCR (pytesseract). Качество OCR на
старых сканах с редакциями (чёрными плашками цензуры) может быть не
идеальным — это нормально, book_to_script.py умеет работать с не вполне
чистым исходным текстом.

Установка:
    pip install beautifulsoup4 pdfplumber pdf2image pytesseract
    pip install "camoufox[geoip]"
    python3 -m camoufox fetch
    # + системно: poppler-utils, tesseract-ocr и зависимости Camoufox
    sudo apt install poppler-utils tesseract-ocr

Использование:
    python fbi_vault_scraper.py \
        --case-url "https://vault.fbi.gov/D-B-Cooper%20" \
        --out-dir ./sources/db_cooper \
        --max-parts 3
"""

import argparse
import os
import time
from typing import List, Dict, Optional, Callable

from bs4 import BeautifulSoup
from scraping.cloudflare_session import CamoufoxSession


# ---------- 1. Список частей дела ----------

def list_case_parts(case_url: str, cf: CamoufoxSession, max_pages: int = 20) -> List[Dict]:
    """Собирает все ссылки на страницы частей дела (.../view), включая пагинацию."""
    parts = []
    seen_urls = set()
    base_prefix = case_url.split("?")[0].rstrip("/")
    next_url = case_url

    pages_visited = 0
    while next_url and pages_visited < max_pages:
        pages_visited += 1
        html = cf.get_html(next_url, wait_seconds=5.0)
        soup = BeautifulSoup(html, "html.parser")

        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith(base_prefix) and href.rstrip("/").endswith("/view"):
                if href not in seen_urls:
                    seen_urls.add(href)
                    parts.append({"title": a.get_text(strip=True), "url": href})

        next_link = soup.find("a", string=lambda s: s and "Next" in s)
        candidate_next = next_link["href"] if next_link and next_link.get("href") else None
        next_url = candidate_next if candidate_next and candidate_next not in (next_url,) else None

    return parts


# ---------- 2. Скачивание PDF ----------

PDF_LINK_SELECTOR = "a[href$='/at_download/file']"


def is_valid_pdf(data: bytes) -> bool:
    return data[:5] == b"%PDF-"


def fetch_part_pdf(part_view_url: str, dest_path: str, cf: CamoufoxSession,
                    max_attempts: int = 3) -> bool:
    """Открывает страницу части дела и кликает по реальной ссылке на PDF (а не
    качает URL отдельным HTTP-клиентом) — так запрос неотличим от настоящего
    пользователя и не заворачивается Cloudflare на страницу challenge'а.
    После скачивания проверяет magic-байты: если вместо PDF пришла HTML-
    заглушка ('Just a moment...'), считает попытку неудачной и повторяет."""
    for attempt in range(1, max_attempts + 1):
        try:
            cf.get_html(part_view_url, wait_seconds=4.0)
            data = cf.download_via_click(PDF_LINK_SELECTOR)
        except Exception as e:
            print(f"    Попытка {attempt}/{max_attempts}: ошибка скачивания — {e}")
            time.sleep(3 * attempt)
            continue

        if is_valid_pdf(data):
            with open(dest_path, "wb") as f:
                f.write(data)
            return True

        print(f"    Попытка {attempt}/{max_attempts}: получен не-PDF ответ "
              f"({len(data)} байт) — похоже на Cloudflare-заглушку, повторяю")
        time.sleep(3 * attempt)

    return False


# ---------- 3. Извлечение текста: сначала текстовый слой, иначе OCR ----------

def extract_text_pdfplumber(pdf_path: str) -> str:
    import pdfplumber
    text_parts = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            t = page.extract_text()
            if t:
                text_parts.append(t)
    return "\n".join(text_parts).strip()


def _page_is_mostly_blank(img, blank_ratio: float = 0.995) -> bool:
    """Быстрая эвристика без OCR по гистограмме яркости: почти полностью белая
    или полностью зачернённая (цензура) страница пропускается — экономит
    реальное время, таких страниц в FOIA-релизах ФБР много (обложки,
    маршрутные листы, полностью отредактированные развороты)."""
    gray = img.convert("L")
    hist = gray.histogram()
    total = sum(hist) or 1
    white = sum(hist[250:256])
    black = sum(hist[0:6])
    return (white / total) >= blank_ratio or (black / total) >= blank_ratio


def extract_text_ocr(pdf_path: str, dpi: int = 150, lang: str = "eng",
                      skip_blank_pages: bool = True,
                      max_pages: Optional[int] = None) -> str:
    """Оцифровывает по одной странице за раз, рендеря их на диск (а не держа
    весь документ в памяти сразу через convert_from_path без ограничений) —
    при нехватке RAM на сервере это убирает своп и заметно ускоряет процесс.
    dpi=150 (раньше был 200) — для машинописных документов этого достаточно.
    max_pages ограничивает число СТРАНИЦ, КОТОРЫЕ ВООБЩЕ РЕНДЕРЯТСЯ (не
    только распознаются) — критично для дел с частями в сотни страниц:
    без этого convert_from_path растеризует весь документ, даже если
    распознать реально нужно только первые N."""
    from pdf2image import convert_from_path
    import pytesseract
    import tempfile
    from PIL import Image

    with tempfile.TemporaryDirectory() as tmp_dir:
        paths = convert_from_path(pdf_path, dpi=dpi, output_folder=tmp_dir,
                                   paths_only=True, thread_count=2, fmt="png",
                                   last_page=max_pages)
        total = len(paths)
        text_parts = []
        for i, img_path in enumerate(paths, 1):
            img = Image.open(img_path)
            if skip_blank_pages and _page_is_mostly_blank(img):
                print(f"        страница {i}/{total}: почти пустая, OCR пропущен", flush=True)
                img.close()
                os.remove(img_path)
                continue
            print(f"        OCR страница {i}/{total}...", flush=True)
            text_parts.append(pytesseract.image_to_string(img, lang=lang))
            img.close()
            os.remove(img_path)
        return "\n".join(text_parts).strip()


def extract_text(pdf_path: str, min_chars_for_text_layer: int = 200,
                  max_ocr_pages: Optional[int] = None) -> (str, str):
    """Возвращает (текст, метод) — метод 'text_layer' или 'ocr', чтобы видеть в логах,
    насколько можно доверять качеству извлечения на конкретной части.
    Проверка текстового слоя (pdfplumber) идёт по всему документу — она
    дешёвая и не требует рендеринга изображений; max_ocr_pages ограничивает
    только реальный OCR-путь, когда текстового слоя нет (скан)."""
    text = extract_text_pdfplumber(pdf_path)
    if len(text) >= min_chars_for_text_layer:
        return text, "text_layer"
    ocr_text = extract_text_ocr(pdf_path, max_pages=max_ocr_pages)
    return ocr_text, "ocr"


def list_case_parts_only(case_url: str) -> List[Dict]:
    """Только собирает список частей дела (быстро, без скачивания PDF и без
    OCR) — чтобы можно было посмотреть объём дела и выбрать, что качать,
    прежде чем тратить время на реальную обработку."""
    with CamoufoxSession() as cf:
        return list_case_parts(case_url, cf)


# ---------- 4. Оркестрация: дело -> единый текстовый файл ----------

def build_case_corpus(case_url: str, out_dir: str, max_parts: Optional[int] = None,
                       selected_indices: Optional[List[int]] = None,
                       max_ocr_pages: Optional[int] = None,
                       delay: float = 1.0,
                       on_progress: Optional[Callable[[int, str], None]] = None) -> str:
    """on_progress(percent, message) — опциональный колбэк (см. тот же паттерн
    в asr/transcribe.py::run_transcribe). Если не передан, поведение как
    раньше — просто print() по ходу дела (для CLI-использования)."""
    def _report(percent, message):
        print(message)
        if on_progress:
            on_progress(percent, message)

    os.makedirs(out_dir, exist_ok=True)
    out_txt = os.path.join(out_dir, "combined_source.txt")
    # чистим файл перед новым прогоном, дальше пишем инкрементально после каждой части
    open(out_txt, "w", encoding="utf-8").close()

    with CamoufoxSession() as cf:
        _report(0, f"Собираю список частей дела: {case_url}")
        all_parts = list_case_parts(case_url, cf)

        if selected_indices:
            parts = [(i, all_parts[i - 1]) for i in selected_indices if 1 <= i <= len(all_parts)]
        elif max_parts:
            parts = list(enumerate(all_parts[:max_parts], 1))
        else:
            parts = list(enumerate(all_parts, 1))
        _report(2, f"Всего частей в деле: {len(all_parts)}, к обработке выбрано: {len(parts)}")

        parts_done = 0
        for n, (i, part) in enumerate(parts, 1):
            percent = 2 + int(96 * n / max(len(parts), 1))
            _report(percent, f"[{i}/{len(all_parts)}] {part['title']}")
            pdf_path = os.path.join(out_dir, f"part_{i:03d}.pdf")
            ok = fetch_part_pdf(part["url"], pdf_path, cf)
            if not ok:
                _report(percent, "    Не удалось скачать валидный PDF после нескольких попыток, пропускаю")
                continue

            try:
                text, method = extract_text(pdf_path, max_ocr_pages=max_ocr_pages)
            except Exception as e:
                _report(percent, f"    Ошибка извлечения текста: {e}")
                continue

            _report(percent, f"    извлечено {len(text)} символов ({method})")
            with open(out_txt, "a", encoding="utf-8") as f:
                f.write(f"--- {part['title']} ---\n{text}\n\n")
            parts_done += 1

            time.sleep(delay)  # вежливость к серверу, не обход защиты — её тут нет

    _report(100, f"Готово: {out_txt} ({parts_done}/{len(parts)} частей записано)")
    return out_txt


# ---------- 5. CLI ----------

def main():
    parser = argparse.ArgumentParser(description="Скачать дело из FBI Vault и собрать единый текстовый источник")
    parser.add_argument("--case-url", required=True, help='Например: "https://vault.fbi.gov/D-B-Cooper%%20"')
    parser.add_argument("--out-dir", required=False, help="Не нужен с --list-only")
    parser.add_argument("--list-only", action="store_true",
                         help="Только показать список томов дела (без скачивания и OCR) и выйти")
    parser.add_argument("--parts", type=str, default=None,
                         help="Конкретные номера томов через запятую, напр. '1,3,5' (перекрывает --max-parts)")
    parser.add_argument("--max-parts", type=int, default=None, help="Ограничить число частей (полезно для теста)")
    parser.add_argument("--max-ocr-pages", type=int, default=None,
                         help="Ограничить число страниц, отдаваемых на OCR, на один том (для больших сканов)")
    parser.add_argument("--delay", type=float, default=1.0)
    args = parser.parse_args()

    if args.list_only:
        parts = list_case_parts_only(args.case_url)
        print(f"Всего томов в деле: {len(parts)}")
        for i, p in enumerate(parts, 1):
            print(f"  [{i}] {p['title']}")
        return

    if not args.out_dir:
        parser.error("--out-dir обязателен, если не указан --list-only")

    selected_indices = None
    if args.parts:
        selected_indices = [int(x.strip()) for x in args.parts.split(",") if x.strip()]

    build_case_corpus(args.case_url, args.out_dir, max_parts=args.max_parts,
                       selected_indices=selected_indices, max_ocr_pages=args.max_ocr_pages,
                       delay=args.delay)


if __name__ == "__main__":
    main()
