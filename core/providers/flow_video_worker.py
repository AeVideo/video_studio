"""
core/providers/flow_video_worker.py

Реализация генерации видео через Google Flow (Veo 3.1 lite).
Заменяет заглушку NotImplementedError.

Используется из pexels_matcher.py как fallback-источник когда
stock/archive не нашли подходящего клипа для шота.
"""

import logging
import os
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class FlowVideoWorker:
    """
    Генерация видео через Google Flow UI (Veo 3.1 lite / Camoufox).

    account_manager — экземпляр AccountManager с Flow-аккаунтами.
    model_ui_name   — название модели как в интерфейсе Flow.
    output_dir      — куда сохранять сгенерированные MP4.
    """

    def __init__(
        self,
        account_manager=None,
        model_ui_name: str = "Veo 3.1 Lite",
        output_dir: str = ".",
        timeout_seconds: int = 900,
    ):
        self.account_manager = account_manager
        self.model_ui_name = model_ui_name
        self.output_dir = output_dir
        self.timeout_seconds = timeout_seconds

    def generate_shot(
        self,
        shot: Dict,
        previous_state: Optional[Dict] = None,
        aspect: str = "16:9",
    ) -> Dict:
        """
        Генерирует видеоклип для шота через Flow.

        shot — стандартный шот из footage.json:
            {
              "id": "scene_01_shot_01",
              "start": 0.0, "end": 4.5,
              "narration": "...",
              "visual_plan": "...",
              "generation": {
                "provider": "flow",
                "status": "pending",
                "prompt": null, ...
              }
            }

        previous_state — continuity-данные от предыдущего шота
            (visual_state, continuity_rules) для передачи в промпт.

        Возвращает обновлённый shot с заполненным shot["generation"].
        """
        if not self.account_manager:
            raise RuntimeError(
                "FlowVideoWorker: account_manager не задан. "
                "Передайте AccountManager с Flow-аккаунтами."
            )

        from core.flow.executor import FlowExecutor
        from core.flow.actions import generate_video

        # Строим промпт из visual_plan + continuity
        prompt = self._build_prompt(shot, previous_state)
        shot.setdefault("generation", {})
        shot["generation"]["prompt"] = prompt
        shot["generation"]["status"] = "generating"

        executor = FlowExecutor(self.account_manager)

        def task(page):
            return generate_video(
                page=page,
                prompt=prompt,
                model_ui_name=self.model_ui_name,
                aspect=aspect,
                quantity=1,
                output_dir=self.output_dir,
                timeout_seconds=self.timeout_seconds,
            )

        try:
            result = executor.execute_with_retry(task)
            shot["generation"].update({
                "status":         "done",
                "video_path":     result["video_path"],
                "media_id":       result["media_id"],
                "task_id":        result["task_id"],
                "video_url":      result["video_url"],
                "duration_sec":   result.get("duration_sec"),
                "account":        None,  # executor скрывает какой аккаунт использовался
            })
            logger.info(f"FlowVideoWorker: шот {shot['id']} готов → {result['video_path']}")
        except Exception as e:
            shot["generation"]["status"] = "error"
            shot["generation"]["error"] = str(e)
            logger.error(f"FlowVideoWorker: шот {shot['id']} — ошибка: {e}")
            raise

        return shot

    def _build_prompt(self, shot: Dict, previous_state: Optional[Dict]) -> str:
        """
        Строит английский промпт из visual_plan шота и continuity-данных.
        """
        base = shot.get("visual_plan") or shot.get("narration") or ""

        if not previous_state:
            return base

        # Добавляем continuity-правила из предыдущего шота
        rules = previous_state.get("continuity_rules", [])
        visual = previous_state.get("visual_state", {})

        parts = [base]
        if visual.get("environment"):
            parts.append(f"Setting: {visual['environment']}.")
        if visual.get("lighting"):
            parts.append(f"Lighting: {visual['lighting']}.")
        if visual.get("camera"):
            parts.append(f"Camera: {visual['camera']}.")
        for rule in rules[:3]:  # не перегружаем промпт
            parts.append(rule)

        return " ".join(parts)
