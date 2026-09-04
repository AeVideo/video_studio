"""
pexels_matcher.py

Для каждой сцены из scenes_timed.json (вышли из srt_to_scenes.py):
  1. DeepSeek переводит текст сцены в 3 англоязычных поисковых запроса
     (Pexels лучше всего индексируется на английском).
  2. По каждому запросу ищем кандидатов через Pexels API.
  3. Каждого кандидата (по всем его video_pictures — превью-кадрам через
     всю длину ролика, без скачивания самого видео) оцениваем ЛОКАЛЬНЫМ
     CLIP: считаем эмбеддинг каждого превью-кадра и эмбеддинг английского
     поискового запроса, берём медиану косинусной схожести по всем кадрам
     кандидата — так ловим не "один удачный кадр", а весь ролик целиком.
  4. Выбираем лучшего кандидата на сцену, сохраняем результат.

Почему не внешний vision-API (Qwen-VL/Gemma и т.п.): на практике эти пути
упираются в лимиты, верификацию аккаунтов или перегрузку чужого бесплатного
пула — то есть в вещи вне нашего контроля. CLIP считается локально, у себя
на сервере, без сети (кроме разовой загрузки весов модели) — то, что нужно
именно для скоринга "картинка похожа на текст", без лишней болтливости
чат-модели.

Использование:
    export DEEPSEEK_API_KEY=...
    export PEXELS_API_KEY=...
    python pexels_matcher.py \
        --scenes test_run/scenes_timed.json \
        --out test_run/scenes_footage.json
"""

import argparse
import json
import os
import time
from io import BytesIO
from typing import List, Dict, Optional

import requests
import torch
import open_clip
from PIL import Image
from openai import OpenAI
from dotenv import load_dotenv

try:
    from core.video_sources import archive_org_search
except ImportError:
    archive_org_search = None  # третий источник опционален — если модуля нет, просто не используем

load_dotenv()  # подхватывает .env из текущей папки — export больше не нужен

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL_DEFAULT = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
PEXELS_SEARCH_URL = "https://api.pexels.com/videos/search"
PIXABAY_SEARCH_URL = "https://pixabay.com/api/videos/"

QUERIES_PER_SCENE = 4
CANDIDATES_PER_QUERY = 8
MAX_PICTURES_PER_CANDIDATE = 10
SCORE_THRESHOLD = 0.24  # эмпирический порог для ViT-B-32; откалибруйте по своим результатам

CLIP_MODEL_NAME = "ViT-B-32-quickgelu"
CLIP_PRETRAINED = "openai"

# --- Новые константы для оптимизации ---
QUERY_RETRIES = 3                 # сколько раз пробуем получить queries от DeepSeek
MAX_TOTAL_CANDIDATES = 25         # лимит числа кандидатов, которые скорим за одну сцену
FALLBACK_QUERY_CHARS = 80         # сколько символов текста сцены берём в fallback-запрос

# Точность важнее скорости: если после основного прохода лучший score ниже
# порога, делаем ОДИН дополнительный проход с альтернативными (более
# буквальными/более широкими) запросами вместо того, чтобы сразу сдаваться
# на low_confidence. Это удлиняет сцену на 1 доп. вызов DeepSeek + доп.
# поиск, но именно этого просили: "лучше дольше, но точнее".
RETRY_ON_LOW_CONFIDENCE = True
THUMBNAILS_DIR_NAME = "thumbnails"  # подпапка рядом с --out для превью выбранных кадров


def get_deepseek_client() -> OpenAI:
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise RuntimeError("Задайте DEEPSEEK_API_KEY")
    return OpenAI(api_key=key, base_url=DEEPSEEK_BASE_URL)


def _strip_json_fence(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw
        if raw.endswith("```"):
            raw = raw[:-3]
    return raw.strip()


# ---------- 1. Генерация поисковых запросов ----------

QUERY_SYSTEM_PROMPT = """Ты переводишь текст сцены документального сценария в англоязычные
поисковые запросы для стокового видео (Pexels).

ГЛАВНОЕ ПРАВИЛО ТОЧНОСТИ: запрос должен буквально описывать СУБЪЕКТ + ДЕЙСТВИЕ/ОБЪЕКТ
из текста сцены, а не только объект. Если в сцене "мужчина с чемоданом" —
запрос "man with suitcase" / "man carrying suitcase", а НЕ просто "suitcase"
или "luggage closeup". Если "женщина читает письмо у окна" — запрос "woman
reading letter by window", а не просто "letter" или "window". Не огрубляй
буквальную сцену до абстрактного предмета, если в тексте прямо назван
человек/действие с этим предметом — тогда человек и действие ОБЯЗАНЫ быть
в запросе. Это не противоречит следующему пункту про избегание слишком
редких деталей — речь именно о том, чтобы не терять субъект и действие,
которые сток вполне может покрыть.

При этом избегай собственных имён и деталей, которых физически не бывает на
стоке (например, не "Dan Cooper hijacking plane", а "man in suit boarding
commercial airplane"). Не путай "буквально" с "уникально" — сохраняй субъект и
действие, но не имя конкретного человека или редкую деталь события.

ЛЕКСИКА ЭПОХИ, А НЕ ГОД ЦИФРАМИ: если сцена относится к определённому
историческому периоду, передавай эпоху словом, характерным для неё
("aeroplane" вместо "airplane" для начала XX века, "wireless set" вместо
"radio", "motorcar" вместо "car", "airship"/"dirigible" для эры дирижаблей),
а не явным годом/десятилетием в тексте запроса ("1970s", "1930s" и т.п.).
Такие слова-годы плохо совпадают с тем, как эпоха размечена в метаданных
архивных источников, и на практике часто дают ноль результатов вместо
ожидаемого сужения выдачи — период вместо этого должен читаться из самой
лексики (какие предметы/термины называет запрос), а не из отдельного
цифрового токена.

СТИЛЬ ОБЯЗАТЕЛЕН: это серьёзный документальный/расследовательский жанр, не
лайфстайл-контент. Категорически избегай стокового "вайба" радостных
улыбающихся людей, танцев, путешествий-удовольствия, рекламной эстетики —
даже если запрос формально совпадает с текстом сцены (например "стюардесса
в самолёте" не должна превращаться в жизнерадостный travel-vlog кадр).
Предпочитай нейтральные, серьёзные, кинематографичные кадры: пустые
интерьеры, официальную обстановку, архивную/приглушённую эстетику. Если
приходится выбирать между буквальным совпадением с весёлой атмосферой и
менее буквальным, но серьёзным по тону — выбирай серьёзный тон, но НЕ ценой
потери субъекта и действия — ищи серьёзную версию именно этого субъекта и
действия, а не замену на другой предмет.

КРИТИЧЕСКИ ВАЖНО: не подменяй упомянутый предмет ассоциативным действием,
которое с ним обычно связано, если это действие не происходит в тексте
сцены буквально прямо сейчас. Пример ошибки: текст "потребовал четыре
парашюта" — это просто упоминание требования, а не прыжок; запрос не должен
быть "skydiving jump" или "parachute jump", это забегает вперёд сюжета.
Правильный запрос в этом случае — нейтральный, про сам предмет/контекст
("parachute equipment closeup", "cargo parachute gear") — без драматического
действия, которого ещё не было. Сверяйся с тем, что ФАКТИЧЕСКИ описано в
данной конкретной сцене, а не с тем, что ассоциация подсказывает додумать.

Верни СТРОГО JSON без markdown: {"queries": ["...", "...", "...", "..."]}
Ровно 4 запроса, каждый 2-6 слов, все — буквальные вариации субъект+действие
из текста сцены (не 4 разных предмета, а 4 ракурса на одно и то же)."""

# Второй проход, только если первый не дотянул до порога уверенности —
# явно просим более широкие/синонимичные, но всё ещё буквальные варианты
# (не менять субъект на другой, только смягчать формулировку/ракурс).
QUERY_RETRY_SYSTEM_PROMPT = """Первая попытка поисковых запросов для этой сцены дала
низкую уверенность совпадения на стоке (см. ниже). Предложи ЕЩЁ 4 англоязычных
запроса для того же субъекта и действия — держи их такими же буквальными
(субъект + действие/объект, не абстракция), но меняй ракурс: другой синоним
действия, другой план (крупный/средний), другой уместный контекст места.
НЕ меняй сам субъект и действие на что-то другое — только формулировку и
ракурс. Эпоху, если она важна, передавай характерным для неё словом
(например "aeroplane"/"wireless set" вместо годов цифрами) — не годом.
Всё остальное — как в исходной инструкции (серьёзный документальный
тон, без лайфстайл-вайба, без забегания вперёд сюжета).

Уже пробовали и не подошло:
{tried_queries}

Верни СТРОГО JSON без markdown: {{"queries": ["...", "...", "...", "..."]}}"""

# Добавляется к КАЖДОМУ запросу программно, а не оставляется на усмотрение
# модели — так стиль гарантирован структурно, а не зависит от того, помнит
# ли DeepSeek инструкцию в конкретном вызове.
STYLE_SUFFIX = "documentary cinematic serious tone"


def generate_queries(scene_text: str, client: OpenAI, model: str = DEEPSEEK_MODEL_DEFAULT,
                      system_prompt: str = QUERY_SYSTEM_PROMPT) -> List[str]:
    """Генерирует до QUERIES_PER_SCENE запросов, с повторными попытками при
    ошибке парсинга JSON. При полном провале возвращает fallback-запрос."""
    last_error = None
    for attempt in range(QUERY_RETRIES):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": scene_text},
                ],
                temperature=0.4,
                # 200 было впритык — если thinking всё же не до конца
                # отключается (см. ниже), даже небольшой remainder рассуждений
                # мог вытеснить весь бюджет. 500 — дешёвая страховка, сама
                # структура ответа (3-4 коротких строки в JSON) от этого не
                # раздувается, просто есть запас на непредвиденное.
                max_tokens=500,
                # deepseek-v4-flash с 2026-07-24 по умолчанию работает в thinking mode —
                # рассуждения съедают max_tokens ДО финального JSON в content, из-за
                # этого content приходит пустым и json.loads падает с "Expecting value".
                # См. тот же фикс в book_to_script.py — там уже ловили этот баг раньше.
                extra_body={"thinking": {"type": "disabled"}},
            )
            raw = _strip_json_fence(response.choices[0].message.content)
            if not raw:
                # Та же диагностика, что в book_to_script.py: если content всё
                # равно пуст ПОСЛЕ extra_body с disabled thinking, важно видеть
                # finish_reason — "length" значит thinking не полностью
                # отключился и всё ещё съедает бюджет, что угодно другое —
                # это уже другая причина, не путать одно с другим вслепую.
                finish_reason = response.choices[0].finish_reason
                raise ValueError(f"пустой content, finish_reason={finish_reason!r}")
            data = json.loads(raw)
            queries = data["queries"][:QUERIES_PER_SCENE]
            if queries:
                return queries
            raise ValueError("Пустой список запросов")
        except Exception as e:
            last_error = e
            # не делаем паузу между попытками — DeepSeek обычно отвечает быстро
            continue

    # Fallback: берём первые слова текста сцены, чтобы pipeline не падал
    fallback = scene_text.strip().replace("\n", " ")[:FALLBACK_QUERY_CHARS]
    print(f"  ! Не удалось получить queries от DeepSeek ({last_error}). "
          f"Использую fallback: {fallback[:40]}...")
    return [fallback]


def generate_retry_queries(scene_text: str, tried_queries: List[str], client: OpenAI,
                            model: str = DEEPSEEK_MODEL_DEFAULT) -> List[str]:
    prompt = QUERY_RETRY_SYSTEM_PROMPT.format(tried_queries="\n".join(f"- {q}" for q in tried_queries))
    return generate_queries(scene_text, client, model=model, system_prompt=prompt)


# ---------- широкие темы для archive.org (один раз на весь проект) ----------

ARCHIVE_THEME_SYSTEM_PROMPT = """Ты формулируешь ШИРОКИЕ тематические англоязычные
запросы для поиска ЦЕЛЫХ документальных фильмов/архивной хроники на
archive.org — НЕ для конкретной сцены или кадра, а для темы истории
ЦЕЛИКОМ (эпоха, место, событие, область).

Archive.org ищет буквальным AND-совпадением ВСЕХ слов запроса в метаданных
фильма целиком. Узкое описание сцены ("мужчины закапывают монеты ночью")
там практически никогда не найдёт часовой документальный фильм — так
хронику не размечают. А широкая тема ("American Civil War", "Pittsburgh
Pennsylvania industrial history", "Union Army 1860s") — вполне может
найти подходящую по духу архивную хронику или документалку, даже если ни
одна сцена в найденном фильме буквально не совпадает с вашим сценарием —
и это нормально: нужный конкретный момент внутри уже найденного фильма
будет искать отдельно CLIP по кадрам, не текстовый поиск.

Дай 4-6 таких широких тем на всю историю. Каждая — 2-5 слов, разные грани
темы (эпоха, место, событие, область/индустрия), не варианты одной и той
же формулировки.

Верни СТРОГО JSON без markdown: {"themes": ["...", "...", "...", "...", "..."]}"""


def generate_archive_theme_queries(story_summary: str, client: OpenAI,
                                    model: str = DEEPSEEK_MODEL_DEFAULT) -> List[str]:
    """Вызывается ОДИН раз на весь проект (не на сцену) — стоимость одного
    лишнего вызова DeepSeek ничтожна по сравнению с тем, сколько archive.org
    per-сцена запросов эта функция заменяет собой."""
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": ARCHIVE_THEME_SYSTEM_PROMPT},
                {"role": "user", "content": story_summary},
            ],
            temperature=0.4,
            max_tokens=400,
            extra_body={"thinking": {"type": "disabled"}},
        )
        raw = _strip_json_fence(response.choices[0].message.content)
        if not raw:
            finish_reason = response.choices[0].finish_reason
            raise ValueError(f"пустой content, finish_reason={finish_reason!r}")
        data = json.loads(raw)
        themes = data["themes"]
        if themes:
            return themes
    except Exception as e:
        print(f"  ! Не удалось получить темы для archive.org ({e}) — archive.org в этом прогоне не используется")
    return []


# ---------- 2. Поиск по Pexels ----------

def search_pexels(query: str, api_key: str, per_page: int = CANDIDATES_PER_QUERY,
                   orientation: str = "landscape") -> List[Dict]:
    headers = {"Authorization": api_key}
    params = {"query": query, "per_page": per_page, "orientation": orientation}
    resp = requests.get(PEXELS_SEARCH_URL, headers=headers, params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()

    candidates = []
    for video in data.get("videos", []):
        pictures = [p["picture"] for p in video.get("video_pictures", [])][:MAX_PICTURES_PER_CANDIDATE]
        video_files = sorted(
            video.get("video_files", []),
            key=lambda vf: (vf.get("quality") != "hd", vf.get("width", 0)),
        )
        best_file = video_files[0]["link"] if video_files else None

        candidates.append({
            "id": f"pexels_{video['id']}",
            "duration": video.get("duration"),
            "width": video.get("width"),
            "height": video.get("height"),
            "pictures": pictures,
            "video_link": best_file,
            "source_query": query,
            "source": "pexels",
        })
    return candidates


def search_pixabay(query: str, api_key: str, per_page: int = CANDIDATES_PER_QUERY,
                    orientation: str = "landscape") -> List[Dict]:
    """
    Pixabay отдаёт только ОДИН превью-кадр на видео (не серию по всей длине,
    как Pexels) — CLIP-скоринг для таких кандидатов менее устойчив (медиана
    по одному кадру), но всё ещё работает и расширяет пул поиска.

    В отличие от Pexels, video API Pixabay НЕ имеет параметра orientation
    (он есть только у их image-поиска) — честного серверного фильтра нет.
    Вместо выдумывания несуществующего параметра сортируем результаты
    клиентски по близости соотношения сторон к желаемому, чтобы подходящие
    по ориентации ролики шли первыми в пуле, который дальше режется
    MAX_TOTAL_CANDIDATES — это не гарантия (при 9:16 в топе всё равно может
    не оказаться ни одного вертикального ролика, если Pixabay их просто не
    прислал), но лучше, чем ничего.
    """
    params = {
        "key": api_key,
        "q": query,
        "per_page": max(per_page, 3),  # Pixabay требует минимум 3
        "video_type": "film",
        "safesearch": "true",
    }
    resp = requests.get(PIXABAY_SEARCH_URL, params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()

    candidates = []
    for video in data.get("hits", []):
        renditions = video.get("videos", {})
        best = renditions.get("large") or renditions.get("medium") or renditions.get("small")
        if not best:
            continue
        thumbnail = best.get("thumbnail")

        candidates.append({
            "id": f"pixabay_{video['id']}",
            "duration": video.get("duration"),
            "width": best.get("width"),
            "height": best.get("height"),
            "pictures": [thumbnail] if thumbnail else [],
            "video_link": best.get("url"),
            "source_query": query,
            "source": "pixabay",
        })

    wants_portrait = orientation == "portrait"
    candidates.sort(key=lambda c: (c["width"] or 0) > (c["height"] or 0) if wants_portrait
                     else (c["width"] or 0) < (c["height"] or 0))
    return candidates


# ---------- 3. Локальный CLIP: модель, эмбеддинги, скоринг ----------

class ClipScorer:
    # Кэш модели на уровне класса — после первой загрузки он переиспользуется
    # всеми экземплярами ClipScorer, чтобы не пересоздавать тяжёлые веса.
    _cached_model = None
    _cached_preprocess = None
    _cached_tokenizer = None

    def __init__(self, model_name: str = CLIP_MODEL_NAME, pretrained: str = CLIP_PRETRAINED):
        # Конструктор НЕ загружает модель — происходит ленивая загрузка
        # при первом реальном вызове embed_text/embed_image_url.
        self.model_name = model_name
        self.pretrained = pretrained
        self.model = None
        self.preprocess = None
        self.tokenizer = None

    def _ensure_loaded(self):
        """Загружает CLIP один раз и сохраняет в классовый кэш."""
        if ClipScorer._cached_model is None:
            print(f"Загружаю CLIP ({self.model_name}, {self.pretrained}) "
                  f"— при первом запуске скачает веса, дальше из кэша...")
            model, _, preprocess = open_clip.create_model_and_transforms(
                self.model_name, pretrained=self.pretrained
            )
            tokenizer = open_clip.get_tokenizer(self.model_name)
            model.eval()
            ClipScorer._cached_model = model
            ClipScorer._cached_preprocess = preprocess
            ClipScorer._cached_tokenizer = tokenizer

        self.model = ClipScorer._cached_model
        self.preprocess = ClipScorer._cached_preprocess
        self.tokenizer = ClipScorer._cached_tokenizer

    def embed_text(self, text: str) -> torch.Tensor:
        self._ensure_loaded()
        with torch.no_grad():
            tokens = self.tokenizer([text])
            features = self.model.encode_text(tokens)
            return features / features.norm(dim=-1, keepdim=True)

    def embed_image_url(self, url: str) -> Optional[torch.Tensor]:
        self._ensure_loaded()
        try:
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            image = Image.open(BytesIO(resp.content)).convert("RGB")
        except Exception as e:
            print(f"    не удалось загрузить превью {url}: {e}")
            return None

        with torch.no_grad():
            tensor = self.preprocess(image).unsqueeze(0)
            features = self.model.encode_image(tensor)
            return features / features.norm(dim=-1, keepdim=True)

    def download_image_url(self, url: str, dest_path: str) -> bool:
        """Скачивает картинку по URL на диск (для превью выбранного кадра
        в GUI). Возвращает False вместо исключения — превью необязательно
        для работы пайплайна."""
        try:
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            with open(dest_path, "wb") as f:
                f.write(resp.content)
            return True
        except Exception as e:
            print(f"    не удалось сохранить превью {url}: {e}")
            return False

    def score_candidate(self, candidate: Dict, text_embedding: torch.Tensor) -> Optional[Dict]:
        similarities = []  # список (индекс_кадра, схожесть)
        for idx, pic_url in enumerate(candidate["pictures"]):
            img_emb = self.embed_image_url(pic_url)
            if img_emb is None:
                continue
            sim = (img_emb @ text_embedding.T).item()
            similarities.append((idx, sim))

        if not similarities:
            return None

        sims_only = sorted(s for _, s in similarities)
        n = len(sims_only)
        median = sims_only[n // 2] if n % 2 else (sims_only[n // 2 - 1] + sims_only[n // 2]) / 2

        best_idx, _ = max(similarities, key=lambda pair: pair[1])
        total_pictures = len(candidate["pictures"])
        duration = candidate.get("duration") or 0

        # Превью-кадры распределены примерно равномерно по всей длине ролика —
        # переводим индекс лучшего кадра в примерную секунду внутри клипа.
        best_offset = 0.0
        if duration and total_pictures > 1:
            best_offset = round((best_idx / (total_pictures - 1)) * duration, 2)

        return {
            "score": round(median, 4),
            "min_frame_score": round(min(sims_only), 4),
            "max_frame_score": round(max(sims_only), 4),
            "frames_scored": n,
            "best_offset": best_offset,
            # URL именно того кадра, который дал максимальный score — это и
            # есть кадр, который реально решил выбор кандидата, лучший
            # материал для превью в GUI (не первый кадр ролика, а тот самый).
            "best_picture_url": candidate["pictures"][best_idx] if best_idx < total_pictures else None,
        }


# ---------- 4. Основной цикл: сцена -> лучший кандидат ----------

def _score_pool(pool: List[Dict], clip_scorer: ClipScorer) -> Optional[Dict]:
    """Скорит пул кандидатов с кэшированием эмбеддингов запросов и
    ограничением общего числа скоримых кандидатов."""
    if not pool:
        return None

    # Ограничение числа кандидатов, чтобы не скорить сотни картинок
    if len(pool) > MAX_TOTAL_CANDIDATES:
        pool = pool[:MAX_TOTAL_CANDIDATES]

    # Предвычисляем эмбеддинги для каждого уникального запроса
    query_embeddings = {}
    for c in pool:
        q = c["source_query"]
        if q not in query_embeddings:
            try:
                query_embeddings[q] = clip_scorer.embed_text(q)
            except Exception:
                # Если embed_text упал (например, нет модели), пропускаем кандидатов этого запроса
                query_embeddings[q] = None

    best_local = None
    for c in pool:
        if not c["pictures"]:
            continue
        text_embedding = query_embeddings.get(c["source_query"])
        if text_embedding is None:
            continue
        result = clip_scorer.score_candidate(c, text_embedding)
        if result is None:
            continue
        result["candidate"] = c
        if best_local is None or result["score"] > best_local["score"]:
            best_local = result

    return best_local


def _search_all(queries: List[str], pexels_key: str, pixabay_key: Optional[str],
                 candidates_by_id: Dict[str, Dict], orientation: str = "landscape") -> None:
    """Ищет по всем queries на Pexels, и на Pixabay если Pexels пуст.
    Дополняет candidates_by_id на месте (используется и для основного,
    и для повторного прохода — набирает кандидатов в один общий пул)."""
    had_pexels_hits = False
    for q in queries:
        search_q = f"{q} {STYLE_SUFFIX}"
        try:
            found = search_pexels(search_q, pexels_key, orientation=orientation)
            if found:
                had_pexels_hits = True
            for c in found:
                c["source_query"] = q  # для CLIP — чистый запрос, без стилевого суффикса
                candidates_by_id[c["id"]] = c
        except requests.HTTPError as e:
            print(f"    Pexels ошибка на запросе '{search_q}': {e}")

    if not had_pexels_hits and pixabay_key:
        print("  Pexels вернул пустой результат — пытаюсь использовать Pixabay...")
        for q in queries:
            search_q = f"{q} {STYLE_SUFFIX}"
            try:
                for c in search_pixabay(search_q, pixabay_key, orientation=orientation):
                    c["source_query"] = q
                    candidates_by_id[c["id"]] = c
            except requests.HTTPError as e:
                print(f"    Pixabay ошибка на запросе '{search_q}': {e}")


def _try_archive_pool(archive_pool: List[Dict], queries: List[str], used_ids: set,
                       clip_scorer: ClipScorer, thumbnails_dir: Optional[str],
                       scene_id: Optional[str], archive_dead_ids: Optional[set] = None) -> Optional[Dict]:
    """Ищет момент под сцену ВНУТРИ уже собранного пула широкотематических
    archive.org видео (find_theme_pool, один раз на весь проект) — не
    делает новый текстовый поиск в archive.org на каждую сцену (см.
    комментарий у ARCHIVE_THEME_SYSTEM_PROMPT почему это не работает).
    Пробует только первый (самый буквальный) query — CLIP-сэмплинг внутри
    пула и так пробует несколько видео, гонять по всем 4 queries поверх
    этого было бы слишком дорого."""
    if archive_org_search is None or not archive_pool:
        return None
    literal_query = queries[0]
    try:
        found = archive_org_search.find_best_in_pool(
            archive_pool, literal_query, clip_scorer, used_ids=used_ids, dead_ids=archive_dead_ids,
        )
    except Exception as e:
        print(f"    archive.org (пул) ошибка на запросе '{literal_query}': {e}")
        return None
    if not found:
        return None

    thumbnail_path = None
    frame_path = found.pop("_frame_path", None)
    if thumbnails_dir and scene_id and frame_path and os.path.exists(frame_path):
        os.makedirs(thumbnails_dir, exist_ok=True)
        dest = os.path.join(thumbnails_dir, f"{scene_id}.jpg")
        try:
            os.replace(frame_path, dest)
            thumbnail_path = dest
        except OSError:
            pass
    found["thumbnail_path"] = thumbnail_path
    return found


def match_scene(scene: Dict, deepseek_client: OpenAI, clip_scorer: ClipScorer, pexels_key: str,
                 used_ids: set, pixabay_key: Optional[str] = None,
                 thumbnails_dir: Optional[str] = None, orientation: str = "landscape",
                 archive_pool: Optional[List[Dict]] = None, archive_dead_ids: Optional[set] = None) -> Dict:
    queries = generate_queries(scene["text"], deepseek_client)

    candidates_by_id: Dict[str, Dict] = {}
    _search_all(queries, pexels_key, pixabay_key, candidates_by_id, orientation=orientation)

    tried_queries = list(queries)

    def _best_from_pool():
        fresh_pool = [c for c in candidates_by_id.values() if c["id"] not in used_ids]
        b = _score_pool(fresh_pool, clip_scorer)
        was_reused = False
        if b is None and candidates_by_id:
            # Совсем нет свежих кандидатов — крайний случай, разрешаем
            # повтор, но явно помечаем это в результате.
            b = _score_pool(list(candidates_by_id.values()), clip_scorer)
            was_reused = True
        return b, was_reused

    best, reused = _best_from_pool()

    # Точность важнее скорости: если лучший результат первого прохода не
    # дотягивает до порога — просим DeepSeek альтернативные буквальные
    # формулировки и ищем ещё раз, объединяя пул кандидатов с первым проходом.
    if RETRY_ON_LOW_CONFIDENCE and (best is None or best["score"] < SCORE_THRESHOLD):
        print(f"    score {best['score'] if best else '—'} ниже порога {SCORE_THRESHOLD} — "
              f"пробую альтернативные запросы...")
        retry_queries = generate_retry_queries(scene["text"], tried_queries, deepseek_client)
        tried_queries += retry_queries
        _search_all(retry_queries, pexels_key, pixabay_key, candidates_by_id, orientation=orientation)
        retry_best, retry_reused = _best_from_pool()
        if retry_best is not None and (best is None or retry_best["score"] > best["score"]):
            best, reused = retry_best, retry_reused

    archive_footage = None
    if archive_pool:
        archive_footage = _try_archive_pool(archive_pool, tried_queries, used_ids, clip_scorer,
                                             thumbnails_dir, scene.get("id"), archive_dead_ids=archive_dead_ids)

    # Archive.org предпочитаем, если он вообще что-то нашёл и не отстаёт
    # заметно от лучшего стокового кандидата — настоящая хроника ценнее
    # даже при небольшом проигрыше по чистому CLIP-score (сток может
    # формально "совпадать" точнее, но по духу это никогда не тот самый
    # период/место). Порог отставания — эмпирический, можно тюнить.
    ARCHIVE_SCORE_TOLERANCE = 0.03
    if archive_footage is not None and (best is None or archive_footage["score"] >= best["score"] - ARCHIVE_SCORE_TOLERANCE):
        used_ids.add(archive_footage["archive_id"])
        status = "matched" if archive_footage["score"] >= SCORE_THRESHOLD else "low_confidence"
        return {
            **scene,
            "footage": {
                "source": "archive_org",
                "archive_id": archive_footage["archive_id"],
                "video_link": archive_footage["video_link"],
                "duration": archive_footage["duration"],
                "score": archive_footage["score"],
                "best_offset": archive_footage.get("best_offset", 0.0),
                "source_query": archive_footage["source_query"],
                "thumbnail_path": archive_footage.get("thumbnail_path"),
            },
            "status": status,
            "tried_queries": tried_queries,
        }

    if not candidates_by_id:
        return {**scene, "footage": None, "status": "no_candidates", "tried_queries": tried_queries}

    if best is None:
        return {**scene, "footage": None, "status": "scoring_failed", "tried_queries": tried_queries}

    used_ids.add(best["candidate"]["id"])
    status = "matched" if best["score"] >= SCORE_THRESHOLD else "low_confidence"
    if reused:
        status = "reused_fallback"

    thumbnail_path = None
    picture_url = best.get("best_picture_url")
    if thumbnails_dir and picture_url:
        os.makedirs(thumbnails_dir, exist_ok=True)
        thumbnail_path = os.path.join(thumbnails_dir, f"{scene['id']}.jpg")
        if not clip_scorer.download_image_url(picture_url, thumbnail_path):
            thumbnail_path = None

    return {
        **scene,
        "footage": {
            "source": best["candidate"].get("source", "pexels"),
            "pexels_id": best["candidate"]["id"],
            "video_link": best["candidate"]["video_link"],
            "duration": best["candidate"]["duration"],
            "score": best["score"],
            "min_frame_score": best["min_frame_score"],
            "max_frame_score": best["max_frame_score"],
            "best_offset": best.get("best_offset", 0.0),
            "source_query": best["candidate"]["source_query"],
            "thumbnail_path": thumbnail_path,
        },
        "status": status,
        "tried_queries": tried_queries,
    }


# ---------- 5. CLI ----------

def main():
    parser = argparse.ArgumentParser(description="Подбор видео с Pexels под сцены сценария (локальный CLIP)")
    parser.add_argument("--scenes", required=True, help="scenes_timed.json из srt_to_scenes.py")
    parser.add_argument("--out", required=True)
    parser.add_argument("--aspect-ratio", choices=["16:9", "9:16"], default="16:9",
                         help="Ориентация ролика — влияет только на orientation-поиск Pexels/Pixabay, "
                              "разрешение задаётся отдельно в assemble_video.py")
    parser.add_argument("--no-archive", action="store_true",
                         help="Отключить archive.org как источник (дороже по времени — резка через "
                              "ffmpeg по сети). По умолчанию archive.org ВКЛЮЧЁН.")
    args = parser.parse_args()

    orientation = "portrait" if args.aspect_ratio == "9:16" else "landscape"

    pexels_key = os.environ.get("PEXELS_API_KEY")
    if not pexels_key:
        raise RuntimeError("Задайте PEXELS_API_KEY")

    pixabay_key = os.environ.get("PIXABAY_API_KEY")
    if not pixabay_key:
        print("PIXABAY_API_KEY не задан — ищу только по Pexels (это нормально, просто меньше кандидатов).")

    with open(args.scenes, "r", encoding="utf-8") as f:
        data = json.load(f)
    scenes = data["scenes"]

    deepseek_client = get_deepseek_client()
    clip_scorer = ClipScorer()  # модель грузится лениво, при первом скоринге
    used_ids: set = set()  # глобальный запрет повтора одного клипа между шотами
    thumbnails_dir = os.path.join(os.path.dirname(os.path.abspath(args.out)) or ".", THUMBNAILS_DIR_NAME)
    archive_dead_ids: set = set()  # архивные identifier'ы, провалившие проверку хоть раз — не пробуем снова

    archive_pool: List[Dict] = []
    if not args.no_archive and archive_org_search is not None:
        story_summary = " ".join(s["text"] for s in scenes)[:2000]
        theme_queries = generate_archive_theme_queries(story_summary, deepseek_client)
        if theme_queries:
            print(f"Темы для archive.org: {theme_queries}")
            archive_pool = archive_org_search.find_theme_pool(theme_queries)
            print(f"Собран пул archive.org: {len(archive_pool)} видео")

    results = []
    for i, scene in enumerate(scenes, 1):
        print(f"[{i}/{len(scenes)}] {scene['id']}: подбираю видео...")
        matched = match_scene(scene, deepseek_client, clip_scorer, pexels_key, used_ids,
                               pixabay_key=pixabay_key, thumbnails_dir=thumbnails_dir,
                               orientation=orientation, archive_pool=archive_pool,
                               archive_dead_ids=archive_dead_ids)
        results.append(matched)
        status = matched["status"]
        footage = matched.get("footage")
        score = footage["score"] if footage else "—"
        print(f"    -> {status}, score={score}")

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"audio_path": data.get("audio_path"), "srt_path": data.get("srt_path"), "scenes": results},
                   f, ensure_ascii=False, indent=2)

    from collections import Counter
    status_counts = Counter(r["status"] for r in results)
    print(f"\nГотово -> {args.out}")
    for status, count in status_counts.most_common():
        print(f"  {status}: {count}/{len(results)}")


if __name__ == "__main__":
    main()
