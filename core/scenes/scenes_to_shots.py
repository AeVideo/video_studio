"""
scenes_to_shots.py

Берёт сцены с реальными таймингами (scenes_timed.json из srt_to_scenes.py,
режим --granularity scene) и исходный script.json (с полем "visuals" —
списком визуальных описаний на каждую сцену от book_to_script.py).

Для каждой сцены делит её временной отрезок [start, end] поровну между
всеми её визуальными описаниями — получаем короткие шоты (обычно 2-5 сек
каждый), где текст шота — это ВИЗУАЛЬНОЕ описание (то, что искать на
Pexels), а не текст озвучки. Так подбор видео опирается на то, что
сценарист заранее спланировал показать, а не гадает по прозе постфактум.

Использование:
    python scenes_to_shots.py \
        --scenes test_run/scenes_timed.json \
        --script test_run/script.json \
        --out test_run/shots_timed.json
"""

import argparse
import json
from typing import List, Dict


def expand_scene_to_shots(scene: Dict, visuals: List[str], provider: str = "pexels") -> List[Dict]:
    if not visuals:
        # нет визуальных описаний — используем сцену как единственный шот
        # с текстом озвучки как fallback (лучше, чем совсем ничего)
        return [{
            "id": scene["id"],
            "text": scene["text"],
            "start": scene["start"],
            "end": scene["end"],
            "provider": provider,  # задел на будущий роутер pexels/flow/hybrid
        }]

    start, end = scene["start"], scene["end"]
    total = max(end - start, 0.01)
    n = len(visuals)
    step = total / n

    shots = []
    for i, visual_text in enumerate(visuals):
        shot_start = round(start + i * step, 2)
        shot_end = round(start + (i + 1) * step, 2)
        shots.append({
            "id": f"{scene['id']}_shot_{i+1:02d}",
            "text": visual_text,
            "start": shot_start,
            "end": shot_end,
            "provider": provider,  # задел на будущий роутер pexels/flow/hybrid
        })
    return shots


def main():
    parser = argparse.ArgumentParser(description="Сцены с таймингом + visuals из script.json -> отдельные шоты")
    parser.add_argument("--scenes", required=True, help="scenes_timed.json (режим --granularity scene)")
    parser.add_argument("--script", required=True, help="script.json из book_to_script.py (с полем visuals)")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    with open(args.scenes, "r", encoding="utf-8") as f:
        timed_data = json.load(f)
    with open(args.script, "r", encoding="utf-8") as f:
        script_scenes = json.load(f)

    visuals_by_id = {s["id"]: s.get("visuals", []) for s in script_scenes}

    all_shots = []
    skipped = 0
    for scene in timed_data["scenes"]:
        if scene.get("status") == "missing" or scene.get("start") is None:
            skipped += 1
            continue
        visuals = visuals_by_id.get(scene["id"], [])
        all_shots.extend(expand_scene_to_shots(scene, visuals))

    result = {
        "audio_path": timed_data.get("audio_path"),
        "srt_path": timed_data.get("srt_path"),
        "scenes": all_shots,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"Сцен обработано: {len(timed_data['scenes']) - skipped}, пропущено (нет таймингов): {skipped}")
    print(f"Итого шотов: {len(all_shots)} -> {args.out}")


if __name__ == "__main__":
    main()
