"""
merge_timing.py

Берёт уже посчитанный shots_footage.json (дорогой шаг — Pexels+CLIP) и
подставляет в него исправленные тайминги из нового shots_timed.json
(дешёвый шаг), не трогая сами подобранные клипы. Матчинг видео зависит
только от текста, а не от времени — пересчитывать его не нужно.

Использование:
    python merge_timing.py \
        --old-footage test_run/shots_footage.json \
        --new-timing test_run/shots_timed_v2.json \
        --out test_run/shots_footage_v2.json
"""

import argparse
import json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--old-footage", required=True)
    parser.add_argument("--new-timing", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    with open(args.old_footage, "r", encoding="utf-8") as f:
        old = json.load(f)
    with open(args.new_timing, "r", encoding="utf-8") as f:
        new = json.load(f)

    new_timing = {s["id"]: (s["start"], s["end"]) for s in new["scenes"]}

    updated = 0
    missing_ids = []
    for scene in old["scenes"]:
        if scene["id"] in new_timing:
            scene["start"], scene["end"] = new_timing[scene["id"]]
            updated += 1
        else:
            missing_ids.append(scene["id"])

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(old, f, ensure_ascii=False, indent=2)

    print(f"Обновлено таймингов: {updated}/{len(old['scenes'])}")
    if missing_ids:
        print(f"Не найдено соответствия для: {missing_ids}")
    print(f"Готово -> {args.out}")


if __name__ == "__main__":
    main()
