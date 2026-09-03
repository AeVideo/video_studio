"""
srt_to_scenes.py

Берёт уже готовый .srt (сделанный и проверенный в OASIS Subtitle Studio —
там уже есть сверка распознанного текста с озвучкой) и сценарий script.json
(смысловые сцены из book_to_script.py), и присваивает каждой сцене сценария
реальные start/end по таймингам из srt.

Whisper здесь не запускается заново — Subtitle Studio уже сделал
транскрибацию и проверку. Этот модуль просто совмещает две готовые вещи:
смысловую разбивку на сцены (нужна для подбора видео под каждую) и точные
тайминги (нужны для монтажа).

Использование:
    python srt_to_scenes.py \
        --script test_run/script.json \
        --srt narration_test.srt \
        --audio recording_20260812_102159.wav \
        --out test_run/scenes_timed.json
"""

import argparse
import json
import re
import difflib
from dataclasses import dataclass, asdict
from typing import List, Dict, Optional


# ---------- 1. Парсинг SRT ----------

SRT_TIME_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2}),(\d{3})"
)


def _srt_time_to_seconds(h, m, s, ms) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def parse_srt(srt_path: str) -> List[Dict]:
    """Возвращает реплики: [{"text": str, "start": float, "end": float}, ...]"""
    with open(srt_path, "r", encoding="utf-8-sig") as f:
        content = f.read()

    blocks = re.split(r"\n\s*\n", content.strip())
    cues = []
    for block in blocks:
        lines = [l for l in block.splitlines() if l.strip()]
        if len(lines) < 2:
            continue
        time_line_idx = None
        match = None
        for i, line in enumerate(lines):
            m = SRT_TIME_RE.search(line)
            if m:
                time_line_idx = i
                match = m
                break
        if match is None:
            continue
        start = _srt_time_to_seconds(*match.groups()[0:4])
        end = _srt_time_to_seconds(*match.groups()[4:8])
        text = " ".join(lines[time_line_idx + 1:]).strip()
        if text:
            cues.append({"text": text, "start": start, "end": end})
    return cues


# ---------- 2. Реплики -> псевдо-слова с интерполированным таймингом ----------

def normalize(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^\w\s]", "", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def cues_to_word_timings(cues: List[Dict]) -> List[Dict]:
    """
    SRT даёт тайминг только на реплику целиком, не на слово — делим время
    реплики поровну между её словами. Для позиционирования сцен точности
    достаточно, это не субтитры, а грубая нарезка под видеоряд.
    """
    words = []
    for cue in cues:
        tokens = normalize(cue["text"]).split()
        if not tokens:
            continue
        duration = max(cue["end"] - cue["start"], 0.01)
        step = duration / len(tokens)
        for i, tok in enumerate(tokens):
            words.append({
                "word": tok,
                "start": cue["start"] + i * step,
                "end": cue["start"] + (i + 1) * step,
            })
    return words


# ---------- 3. Выравнивание сцен сценария с таймингами ----------

@dataclass
class TimedScene:
    id: str
    text: str
    start: Optional[float] = None
    end: Optional[float] = None
    match_ratio: Optional[float] = None
    status: str = "pending"  # ok | needs_review | missing


def align_scenes_to_words(scenes: List[Dict], words: List[Dict], review_threshold: float = 0.7) -> List[TimedScene]:
    audio_tokens = [w["word"] for w in words]
    result: List[TimedScene] = []
    cursor = 0

    for scene in scenes:
        scene_tokens = normalize(scene["text"]).split()
        if not scene_tokens:
            result.append(TimedScene(id=scene["id"], text=scene["text"], status="missing"))
            continue

        window = audio_tokens[cursor:]
        if not window:
            result.append(TimedScene(id=scene["id"], text=scene["text"], status="missing"))
            continue

        matcher = difflib.SequenceMatcher(None, scene_tokens, window, autojunk=False)
        match = matcher.find_longest_match(0, len(scene_tokens), 0, len(window))

        if match.size == 0:
            result.append(TimedScene(id=scene["id"], text=scene["text"], status="missing"))
            continue

        start_idx = cursor + max(0, match.b - match.a)
        end_idx = cursor + match.b + (len(scene_tokens) - match.a)
        start_idx = min(start_idx, len(words) - 1)
        end_idx = min(max(end_idx, start_idx), len(words) - 1)

        # Доля текста сцены, найденная СПЛОШНЫМ блоком в аудио — а не
        # matcher.ratio(), который сравнивал бы со всем оставшимся хвостом
        # записи и был бы искусственно низким для ранних сцен.
        ratio = match.size / max(len(scene_tokens), 1)
        result.append(TimedScene(
            id=scene["id"],
            text=scene["text"],
            start=round(words[start_idx]["start"], 2),
            end=round(words[end_idx]["end"], 2),
            match_ratio=round(ratio, 3),
            status="ok" if ratio >= review_threshold else "needs_review",
        ))
        cursor += max(match.b + (len(scene_tokens) - match.a), 1)

    # У последней сцены нет "следующей", которая скомпенсировала бы недотянутую
    # проекцию конца — если она вообще была найдена (status != missing),
    # принудительно дотягиваем её end до конца самого последнего слова во
    # всём аудио, иначе хвост записи может потеряться при сборке.
    if result and result[-1].status != "missing" and words:
        true_end = round(words[-1]["end"], 2)
        if result[-1].end is not None and result[-1].end < true_end:
            result[-1].end = true_end

    return result


# ---------- 4. CLI ----------

def main():
    parser = argparse.ArgumentParser(
        description="Готовый SRT (из Subtitle Studio) -> сцены с реальными таймингами"
    )
    parser.add_argument("--script", required=False,
                         help="script.json из book_to_script.py (нужен только для --granularity scene)")
    parser.add_argument("--srt", required=True, help="Готовый .srt из Subtitle Studio")
    parser.add_argument("--audio", required=False, help="Путь к аудио — просто сохраняется в результат для следующих шагов")
    parser.add_argument("--out", required=True)
    parser.add_argument("--review-threshold", type=float, default=0.6)
    parser.add_argument(
        "--granularity", choices=["scene", "shot"], default="shot",
        help="scene — крупные смысловые куски (медленный монтаж, риск растягивания коротких клипов); "
             "shot — реплики SRT напрямую как отдельные кадры 2-4 сек (динамичный монтаж, по умолчанию)"
    )
    args = parser.parse_args()

    cues = parse_srt(args.srt)
    print(f"Реплик в srt: {len(cues)}")

    if args.granularity == "shot":
        # Каждая реплика SRT — отдельный "шот" под свой клип. Никакого
        # выравнивания не требуется — тайминги уже точные, это те же
        # тайминги, что видит зритель в субтитрах.
        timed_dicts = [
            {
                "id": f"shot_{i+1:03d}",
                "text": cue["text"],
                "start": round(cue["start"], 2),
                "end": round(cue["end"], 2),
                "match_ratio": 1.0,
                "status": "ok",
            }
            for i, cue in enumerate(cues)
        ]
        needs_review = []
    else:
        if not args.script:
            raise RuntimeError("--granularity scene требует --script")
        with open(args.script, "r", encoding="utf-8") as f:
            scenes = json.load(f)
        words = cues_to_word_timings(cues)
        timed = align_scenes_to_words(scenes, words, review_threshold=args.review_threshold)
        timed_dicts = [asdict(s) for s in timed]
        needs_review = [s for s in timed if s.status != "ok"]

    result = {
        "audio_path": args.audio,
        "srt_path": args.srt,
        "scenes": timed_dicts,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"Готово: {len(timed_dicts)} {'шотов' if args.granularity == 'shot' else 'сцен'}, "
          f"требуют внимания: {len(needs_review)} -> {args.out}")
    for s in needs_review:
        preview = s.text[:60].replace("\n", " ")
        print(f"  [{s.status}] {s.id}: ratio={s.match_ratio}  текст: {preview}...")


if __name__ == "__main__":
    main()
