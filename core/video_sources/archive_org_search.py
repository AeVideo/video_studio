"""
archive_org_search.py

Internet Archive (archive.org) как источник видео — полностью открытый
публичный API, без ключа, без анти-бот защиты (обычный requests работает
без Camoufox). Особенно ценен для content_mode="historical": настоящие
архивные кинохроники и документальные съёмки эпохи, а не современный сток.

ВАЖНОЕ ОТЛИЧИЕ от Pexels/Pixabay: единицы контента здесь часто — ПОЛНЫЕ
ролики (кинохроники, документальные фильмы на 10-40 минут), не короткие
b-roll клипы. Этот модуль — только поиск и получение прямой ссылки на
видеофайл; резка длинного ролика под конкретный короткий шот (по аналогии
с best_offset в pexels_matcher.py) — отдельная следующая задача, сюда
не входит.

API (публичные, без ключа):
    Advanced Search: https://archive.org/advancedsearch.php
    Metadata:         https://archive.org/metadata/{identifier}
    Скачивание:       https://archive.org/download/{identifier}/{filename}

Использование:
    python archive_org_search.py "1970s airport newsreel"
"""

import argparse
import os
import re
import subprocess
import tempfile
from typing import List, Dict, Optional

import requests

from core.video_sources.pexels_matcher import ClipScorer  # переиспользуем ту же CLIP-модель/кэш, что и для Pexels/Pixabay

SEARCH_URL = "https://archive.org/advancedsearch.php"
METADATA_URL = "https://archive.org/metadata/{identifier}"
DOWNLOAD_URL = "https://archive.org/download/{identifier}/{filename}"

# advancedsearch.php трактует все слова в q как обязательные AND-термы.
# Десятилетия/годы ("1970s", "circa 1971", "1970-1979") у большинства
# item'ов хранятся в отдельном метаданном поле year/date, а не как слово в
# тексте описания — требуя их буквально в тексте, легко получить 0
# результатов на совершенно нормальном запросе (проверено: "airport
# newsreel 1970s" -> 0, "airport newsreel" -> 121). Поэтому вычищаем такие
# токены из полнотекстового запроса перед отправкой; сам факт эпохи для
# поиска не критичен — archive.org и так неплохо покрыт архивной хроникой.
_TEMPORAL_PATTERN = re.compile(
    r"""(?ix)
    \b(?:circa|c\.?|early|mid|late)\s+\d{4}s?\b   # circa 1970, early 1970s
    | \b\d{4}\s*[-–]\s*\d{4}\b                     # 1970-1979
    | \b\d{4}s\b                                   # 1970s
    | \b(?:19|20)\d{2}\b                           # 1971, 2003
    """
)


def _strip_temporal_tokens(query: str) -> str:
    """Убирает годы/десятилетия из текста запроса — см. комментарий выше."""
    cleaned = _TEMPORAL_PATTERN.sub(" ", query)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or query  # если вычистили всё — лучше исходный запрос, чем пустой

# Резка длинного ролика: сэмплируем кадры равномерно по всей длине через
# ffmpeg input-seek (-ss ДО -i работает как быстрый seek даже по HTTP —
# ffmpeg не тянет весь файл, только докачивает нужный диапазон через
# range-запросы, если сервер их поддерживает — archive.org поддерживает).
OFFSET_SAMPLE_COUNT = 10          # сколько точек пробуем на весь ролик
OFFSET_EDGE_MARGIN_SEC = 5.0      # не сэмплируем совсем у начала/конца (титры/чернота)
FRAME_EXTRACT_TIMEOUT = 25        # сек на одну попытку вытащить кадр по сети

# Форматы, которые реально можно скормить ffmpeg как видео — у каждого item
# на Archive.org обычно несколько файлов (превью, .torrent, xml-метаданные
# и т.п.), нужно отфильтровать только настоящее видео.
VIDEO_FORMATS = {"h.264", "h.264 IA", "MPEG4", "MPEG2", "Matroska", "Ogg Video", "512Kb MPEG4"}


def search_videos(query: str, rows: int = 10) -> List[Dict]:
    search_query = _strip_temporal_tokens(query)
    params = {
        "q": f"({search_query}) AND mediatype:(movies)",
        "fl[]": ["identifier", "title", "description", "year"],
        "rows": rows,
        "page": 1,
        "output": "json",
    }
    resp = requests.get(SEARCH_URL, params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    return data.get("response", {}).get("docs", [])


def get_best_video_file(identifier: str) -> Optional[Dict]:
    """Возвращает метаданные лучшего видеофайла item'а (не превью/торрент/xml)."""
    resp = requests.get(METADATA_URL.format(identifier=identifier), timeout=20)
    resp.raise_for_status()
    data = resp.json()

    candidates = [f for f in data.get("files", []) if f.get("format") in VIDEO_FORMATS]
    if not candidates:
        return None

    # Предпочитаем файл покрупнее (обычно соответствует лучшему качеству)
    candidates.sort(key=lambda f: int(f.get("size", 0)))
    best = candidates[-1]

    return {
        "filename": best["name"],
        "format": best.get("format"),
        "size": best.get("size"),
        "length": best.get("length"),  # длительность в секундах, строкой
        "download_url": DOWNLOAD_URL.format(identifier=identifier, filename=best["name"]),
    }


def _extract_frame_at(video_url: str, timestamp: float, dest_path: str, verbose: bool = False) -> bool:
    """Вытаскивает один кадр по таймкоду через ffmpeg input-seek. -ss ПЕРЕД
    -i — быстрый seek, ffmpeg не читает файл с начала, для HTTP-источника
    это докачка только нужного диапазона (если сервер отдаёт range-запросы,
    archive.org отдаёт). Возвращает False вместо исключения — один
    неудачный кадр не должен ронять всю резку.

    verbose=True — печатает причину первой неудачи на видео (stderr
    ffmpeg/таймаут); раньше молчало полностью, и "не удалось проверить"
    в find_best_offset ничего не говорило — сеть, формат без быстрого
    seek, или сервер просто не отдаёт range-запросы на этот файл."""
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-ss", str(max(timestamp, 0.0)),
        "-i", video_url,
        "-frames:v", "1",
        "-q:v", "3",
        dest_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=FRAME_EXTRACT_TIMEOUT)
        ok = result.returncode == 0 and os.path.exists(dest_path) and os.path.getsize(dest_path) > 0
        if not ok and verbose:
            print(f"      ffmpeg на {timestamp}с: {result.stderr.strip()[-300:] or '(пусто, returncode='+str(result.returncode)+')'}")
        return ok
    except subprocess.TimeoutExpired:
        if verbose:
            print(f"      ffmpeg на {timestamp}с: таймаут ({FRAME_EXTRACT_TIMEOUT}с)")
        return False
    except OSError as e:
        if verbose:
            print(f"      ffmpeg на {timestamp}с: {e}")
        return False


def find_best_offset(video_url: str, duration: float, query: str, clip_scorer: "ClipScorer",
                      num_samples: int = OFFSET_SAMPLE_COUNT) -> Optional[Dict]:
    """Сэмплирует кадры по всей длине ролика, скорит их тем же CLIP, что и
    Pexels-кандидатов, возвращает лучший таймкод + score + путь к лучшему
    кадру (для превью в GUI). None, если ни один кадр не удалось вытащить
    или длительность ролика неизвестна."""
    if not duration or duration <= OFFSET_EDGE_MARGIN_SEC * 2:
        return None

    usable_start = OFFSET_EDGE_MARGIN_SEC
    usable_end = duration - OFFSET_EDGE_MARGIN_SEC
    step = (usable_end - usable_start) / max(num_samples - 1, 1)
    timestamps = [round(usable_start + i * step, 2) for i in range(num_samples)]

    try:
        text_embedding = clip_scorer.embed_text(query)
    except Exception as e:
        print(f"    CLIP embed_text упал: {e}")
        return None

    work_dir = tempfile.mkdtemp(prefix="archive_frames_")
    best = None  # (score, timestamp, frame_path)
    any_frame_extracted = False
    try:
        consecutive_failures = 0
        EARLY_ABORT_AFTER = 2  # если первые 2 сэмпла подряд провалились (сеть/формат) —
                                # почти наверняка провалятся и все остальные, не тратим ещё N×25с
        for i, ts in enumerate(timestamps):
            frame_path = os.path.join(work_dir, f"frame_{i}.jpg")
            if not _extract_frame_at(video_url, ts, frame_path, verbose=not any_frame_extracted and i == 0):
                consecutive_failures += 1
                if not any_frame_extracted and consecutive_failures >= EARLY_ABORT_AFTER:
                    print(f"    archive.org: {EARLY_ABORT_AFTER} подряд неудачных сэмпла — "
                          f"похоже видео не отдаёт быстрый seek, обрываю рано")
                    break
                continue
            consecutive_failures = 0
            any_frame_extracted = True
            try:
                from PIL import Image
                import torch
                image = Image.open(frame_path).convert("RGB")
                with torch.no_grad():
                    tensor = clip_scorer.preprocess(image).unsqueeze(0)
                    features = clip_scorer.model.encode_image(tensor)
                    img_emb = features / features.norm(dim=-1, keepdim=True)
                    sim = (img_emb @ text_embedding.T).item()
            except Exception as e:
                print(f"    не удалось оценить кадр на {ts}с: {e}")
                continue

            if best is None or sim > best[0]:
                # копируем лучший кадр за пределы work_dir до его удаления
                keep_path = frame_path + ".keep"
                os.replace(frame_path, keep_path)
                if best is not None:
                    old_keep = best[2]
                    if os.path.exists(old_keep):
                        os.remove(old_keep)
                best = (sim, ts, keep_path)

        if best is None:
            return None
        return {"offset": best[1], "score": round(best[0], 4), "frame_path": best[2]}
    finally:
        # work_dir может ещё содержать .keep файл лучшего кадра — не трогаем его,
        # чистим только оставшиеся временные кадры
        for f in os.listdir(work_dir):
            if not f.endswith(".keep"):
                try:
                    os.remove(os.path.join(work_dir, f))
                except OSError:
                    pass


def _probe_duration(video_url: str) -> float:
    """Метаданные archive.org (поле 'length' в get_best_video_file) часто
    отсутствуют или недостоверны — без них find_best_offset сразу сдаётся
    (проверка на минимальную длительность). Fallback: спросить длительность
    напрямую у ffprobe по URL — тоже сетевой запрос, но дешёвый, и лучше,
    чем терять видео только из-за дырявых метаданных источника."""
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration",
           "-of", "default=noprint_wrappers=1:nokey=1", video_url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=FRAME_EXTRACT_TIMEOUT)
        return float(result.stdout.strip())
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return 0.0


def find_theme_pool(theme_queries: List[str], rows_per_query: int = 5) -> List[Dict]:
    """Ищет ШИРОКИЕ тематические видео целиком (документалки/хроника) —
    вызывается ОДИН РАЗ на весь проект с 4-6 широкими темами (эпоха/место/
    событие всей истории), а НЕ per-сцена с буквальным описанием кадра.

    Archive.org индексирует полнотекстовым AND-поиском по метаданным
    ЦЕЛОГО фильма: буквальное описание конкретной сцены там практически
    никогда не найдётся (это не то, как размечена хроника), а широкая
    тема — вполне может. Разделение "что ищем текстом" (тема) и "что ищем
    глазами через CLIP" (конкретный кадр) — см. find_best_in_pool ниже.

    Возвращает пул кандидатов (identifier/video_link/duration),
    дедуплицированный по identifier."""
    pool: Dict[str, Dict] = {}
    for query in theme_queries:
        docs = search_videos(query, rows=rows_per_query)
        print(f"  archive.org тема '{query}' -> {len(docs)} результатов")
        for doc in docs:
            identifier = doc.get("identifier")
            if not identifier or identifier in pool:
                continue
            try:
                video_file = get_best_video_file(identifier)
            except requests.RequestException:
                continue
            if not video_file:
                continue
            duration = float(video_file.get("length") or 0.0)
            if duration <= 0:
                duration = _probe_duration(video_file["download_url"])
            pool[identifier] = {
                "archive_id": identifier,
                "video_link": video_file["download_url"],
                "duration": duration,
                "theme_query": query,
            }
    return list(pool.values())


def find_best_in_pool(pool: List[Dict], literal_query: str, clip_scorer: "ClipScorer",
                       used_ids: Optional[set] = None, dead_ids: Optional[set] = None,
                       videos_to_try: int = 2, samples_per_video: int = 6) -> Optional[Dict]:
    """Ищет лучший момент под БУКВАЛЬНОЕ описание конкретной сцены
    (literal_query) ВНУТРИ уже готового пула широкотематических видео (см.
    find_theme_pool) — никакого нового текстового поиска в archive.org,
    только CLIP-сэмплинг кадров по видео из пула.

    dead_ids — identifier'ы, которые уже провалили проверку на ДРУГИХ
    сценах в этом же прогоне (сеть/формат — техническая причина, не
    оценка контента). Без этого сортировка "длиннее видео — пробуем
    первым" раз за разом натыкается на одно и то же неработающее видео на
    КАЖДОЙ сцене, вместо того чтобы дать шанс остальным кандидатам пула.

    videos_to_try/samples_per_video специально небольшие (в отличие от
    OFFSET_SAMPLE_COUNT=10 у find_best_offset) — здесь на каждую сцену
    пробуется НЕСКОЛЬКО видео из пула, а не одно, поэтому суммарная
    стоимость (кол-во ffmpeg-вытяжек) держится в похожем порядке, что и в
    прежней per-сцена схеме, просто распределена по нескольким кандидатам
    вместо одного."""
    if not pool:
        return None
    exclude = (used_ids or set()) | (dead_ids or set())
    candidates = [v for v in pool if v["archive_id"] not in exclude]
    if not candidates:
        # пул исчерпан по обеим причинам — лучше повтор/заведомо слабый
        # кандидат, чем полный отказ от archive.org
        candidates = [v for v in pool if v["archive_id"] not in (used_ids or set())]
    if not candidates:
        candidates = pool
    # длинные видео вероятнее содержат разнообразный материал — пробуем их первыми
    candidates = sorted(candidates, key=lambda v: v.get("duration", 0), reverse=True)[:videos_to_try]

    best = None  # (score, video, offset, frame_path)
    for video in candidates:
        found = find_best_offset(video["video_link"], video["duration"], literal_query, clip_scorer,
                                  num_samples=samples_per_video)
        if found is None:
            print(f"    archive.org (пул): '{video['archive_id']}' не удалось проверить, пробую следующий")
            if dead_ids is not None:
                dead_ids.add(video["archive_id"])
            continue
        print(f"    archive.org (пул): '{video['archive_id']}' score={found['score']}")
        if best is None or found["score"] > best[0]:
            best = (found["score"], video, found["offset"], found["frame_path"])

    if best is None:
        return None
    score, video, offset, frame_path = best
    if used_ids is not None:
        used_ids.add(video["archive_id"])
    return {
        "archive_id": video["archive_id"],
        "video_link": video["video_link"],
        "duration": video["duration"],
        "best_offset": offset,
        "score": score,
        "source_query": literal_query,
        "_frame_path": frame_path,  # переносится в постоянное имя вызывающим кодом (thumbnails_dir)
    }


def find_archive_video(query: str, rows: int = 10, used_ids: Optional[set] = None,
                        clip_scorer: Optional["ClipScorer"] = None,
                        thumbnails_dir: Optional[str] = None, scene_id: Optional[str] = None) -> Optional[Dict]:
    """Поиск архивного видео по запросу, возвращает footage-словарь.

    Пропускает уже занятые identifier'ы (used_ids), чтобы не дублировать
    один и тот же ролик в разных шотах одного проекта. Если передан
    clip_scorer — ищет лучший таймкод внутри ролика через find_best_offset
    и ПРОВЕРЯЕТ совпадение: если проверить не удалось (сеть/формат/
    длительность) — НЕ возвращает этот кандидат как найденный, пробует
    следующий результат поиска. Раньше при неудаче проверки функция всё
    равно возвращала кандидата с выдуманным score=1.0 ("доверяем не
    глядя") — это давало архивным результатам гарантированную победу над
    любым честно оценённым стоковым кандидатом, независимо от того,
    подходит ли видео вообще. clip_scorer=None — единственный случай,
    когда старое поведение (score=1.0, без проверки) оправдано: вызывающий
    код explicit не просил верификации (только прямой CLI-вызов без CLIP).
    """
    docs = search_videos(query, rows=rows)
    print(f"    archive.org: '{query}' -> {len(docs)} результатов поиска")
    for doc in docs:
        identifier = doc.get("identifier")
        if not identifier:
            continue
        if used_ids and identifier in used_ids:
            continue
        try:
            video_file = get_best_video_file(identifier)
        except requests.RequestException:
            continue
        if not video_file:
            continue

        duration = float(video_file.get("length") or 0.0)
        if duration <= 0:
            duration = _probe_duration(video_file["download_url"])

        best_offset = 0.0
        offset_score = None
        thumbnail_path = None

        if clip_scorer is not None:
            found = find_best_offset(video_file["download_url"], duration, query, clip_scorer)
            if found is None:
                # Не удалось ПРОВЕРИТЬ это видео — не подставляем выдуманный
                # score, пробуем следующий результат поиска, а не молча
                # "доверяем" непроверенному кандидату.
                print(f"    archive.org: '{identifier}' не удалось проверить (сеть/формат/длительность), пробую следующий")
                continue
            best_offset = found["offset"]
            offset_score = found["score"]
            if thumbnails_dir and scene_id:
                os.makedirs(thumbnails_dir, exist_ok=True)
                dest = os.path.join(thumbnails_dir, f"{scene_id}.jpg")
                try:
                    os.replace(found["frame_path"], dest)
                    thumbnail_path = dest
                except OSError:
                    pass

        # Помечаем identifier занятым только ПОСЛЕ успешной проверки (или
        # сразу, если верификация не запрашивалась) — видео, не прошедшее
        # проверку для одной сцены, ещё может пригодиться (и успешно
        # провериться) для другой, не тратим его впустую.
        if used_ids is not None:
            used_ids.add(identifier)

        score_display = round(offset_score, 4) if offset_score is not None else "не проверен"
        print(f"    archive.org: НАЙДЕНО '{identifier}' score={score_display}")

        return {
            "archive_id": identifier,
            "video_link": video_file["download_url"],
            "duration": duration,
            "best_offset": best_offset,
            "score": offset_score if offset_score is not None else 1.0,
            "source_query": query,
            "thumbnail_path": thumbnail_path,
        }
    print(f"    archive.org: '{query}' -> ничего не прошло проверку из {len(docs)} результатов")
    return None


def match_archive_scene(scene: Dict, used_ids: Optional[set] = None,
                         clip_scorer: Optional["ClipScorer"] = None,
                         thumbnails_dir: Optional[str] = None) -> Dict:
    """Полная замена match_scene из pexels_matcher для provider='archive'.

    Принимает шот, выполняет поиск на archive.org и возвращает шот
    с добавленными полями status, footage, provider, tried_queries.
    """
    result = dict(scene)
    result.setdefault("tried_queries", [])

    query = (
        scene.get("text")
        or scene.get("visual_plan")
        or scene.get("visuals")
        or scene.get("prompt")
        or ""
    ).strip()

    if not query:
        result["status"] = "failed"
        result["error"] = "Не задан поисковый запрос для archive.org"
        result["provider"] = "archive"
        return result

    footage = find_archive_video(query, used_ids=used_ids, clip_scorer=clip_scorer,
                                  thumbnails_dir=thumbnails_dir, scene_id=scene.get("id"))
    if footage:
        result["status"] = "matched"
        result["footage"] = footage
        result["provider"] = "archive"
        result["tried_queries"].append(query)
    else:
        result["status"] = "failed"
        result["error"] = "Не найдено видео на archive.org"
        result["provider"] = "archive"
        result["tried_queries"].append(query)

    return result


class ArchiveOrgProvider:
    """Тонкий адаптер, чтобы использовать archive.org через общий вызов.

    В будущем такой же интерфейс будет у PexelsProvider, PixabayProvider,
    FlowProvider и т.д. — это первый шаг к выделению `video_providers/`.
    """

    def __init__(self, clip_scorer: Optional["ClipScorer"] = None, thumbnails_dir: Optional[str] = None):
        # clip_scorer передаётся снаружи (общий с pexels_matcher), чтобы не
        # грузить веса CLIP второй раз — это тяжёлая по памяти операция,
        # а oasis и так упирается в 7.8 ГБ RAM под нагрузкой.
        self.clip_scorer = clip_scorer
        self.thumbnails_dir = thumbnails_dir

    def find_video(self, shot: Dict, used_ids: Optional[set] = None) -> Dict:
        """Принимает шот (словарь), возвращает обогащённый шот (см. match_archive_scene)."""
        return match_archive_scene(shot, used_ids=used_ids, clip_scorer=self.clip_scorer,
                                    thumbnails_dir=self.thumbnails_dir)


def main():
    parser = argparse.ArgumentParser(description="Поиск видео на Internet Archive")
    parser.add_argument("query")
    parser.add_argument("--rows", type=int, default=10)
    parser.add_argument("--find-offset", action="store_true",
                         help="Для первого найденного видео сразу найти лучший таймкод под query (CLIP)")
    parser.add_argument("--thumbnail-out", default=None,
                         help="Куда сохранить превью лучшего кадра (используется вместе с --find-offset)")
    args = parser.parse_args()

    results = search_videos(args.query, rows=args.rows)
    print(f"Найдено: {len(results)}")
    for i, r in enumerate(results):
        print(f"\n{r.get('title')} ({r.get('year', '?')})")
        print(f"  identifier: {r['identifier']}")
        video_file = get_best_video_file(r["identifier"])
        if not video_file:
            print("  видеофайл не найден")
            continue
        print(f"  видео: {video_file['filename']} ({video_file.get('length', '?')} сек)")
        print(f"  скачать: {video_file['download_url']}")

        if args.find_offset and i == 0:
            duration = float(video_file.get("length") or 0.0)
            print(f"  Ищу лучший таймкод под запрос '{args.query}' ({OFFSET_SAMPLE_COUNT} сэмплов)...")
            scorer = ClipScorer()
            found = find_best_offset(video_file["download_url"], duration, args.query, scorer)
            if found:
                print(f"  Лучший таймкод: {found['offset']}с, score={found['score']}")
                if args.thumbnail_out:
                    os.replace(found["frame_path"], args.thumbnail_out)
                    print(f"  Превью сохранено: {args.thumbnail_out}")
                elif os.path.exists(found["frame_path"]):
                    os.remove(found["frame_path"])
            else:
                print("  Не удалось найти подходящий таймкод (сеть/формат/длительность)")


if __name__ == "__main__":
    main()
