"""
flow_video_worker.py

ЗАГЛУШКА под будущую генерацию видео через Veo (или другой провайдер)
как альтернативный/гибридный источник кадров рядом с Pexels/Pixabay.
Пока нет официального платного доступа — реализация появится здесь,
без изменения остального пайплайна (роутер уже спроектирован ниже).

Зафиксированная архитектура (чтобы не потерять решение до момента
реализации):

── Расширенный шот для provider="flow" ──────────────────────────────
    {
      "id": "scene_01_shot_01",
      "start": 0.0, "end": 4.5, "duration": 4.5,
      "narration": "...",
      "visual_plan": "...",
      "generation": {
        "provider": "flow", "model": "veo_3_1_lite", "status": "pending",
        "prompt": null, "account": null,
        "video_path": null, "last_frame_path": null,
      }
    }

── Continuity-память между шотами ────────────────────────────────────
Передаётся в vision-модель (Qwen-VL или аналог) при генерации промпта
для СЛЕДУЮЩЕГО шота, чтобы визуальный ряд не "скакал" между клипами:

    вход:
        PREVIOUS_VIDEO_PROMPT
        PREVIOUS_VIDEO_RESULT (last frame image)
        CURRENT_NARRATION (текст)
        CURRENT_TIMESTAMP (start/end)
        NEXT_SHOT_DESCRIPTION (изначально запланированный visual_plan)
        STYLE / PROJECT RULES

    выход (структурированный JSON):
        {
          "visual_state": {
            "character": "...", "position": "...", "camera": "...",
            "environment": "...", "lighting": "...", "important_objects": "..."
          },
          "continuity_rules": ["...", "..."],
          "next_prompt": "..."
        }

MVP при подключении: передавать только last_frame (не first+middle+last)
— дешевле, этого достаточно для базовой непрерывности. Апгрейд на
несколько кадров — только если станет заметно, что одного мало.

── Роутер источника видео на шот (общая идея, ещё не реализован) ────
    "pexels"  — сток (работает сейчас, pexels_matcher.py)
    "flow"    — генерация через Veo (эта заглушка)
    "hybrid"  — сначала pexels_matcher; если ничего не подошло
                (status != "matched") — падаем в flow как fallback
"""

from typing import Dict, Optional


class FlowVideoWorker:
    """Пока не реализовано — поднимется, когда появится официальный доступ к Veo API."""

    def __init__(self, api_key: Optional[str] = None, model: str = "veo_3_1_lite"):
        self.api_key = api_key
        self.model = model

    def generate_shot(self, shot: Dict, previous_state: Optional[Dict] = None) -> Dict:
        raise NotImplementedError(
            "FlowVideoWorker — заглушка, Veo API ещё не подключён. "
            "Когда появится доступ: сгенерировать клип по shot['visual_plan'] "
            "с учётом previous_state (continuity, см. докстринг модуля), "
            "заполнить shot['generation'] и вернуть обновлённый shot."
        )
