"""
project_manifest.py

Файл состояния одного видео-проекта — что уже сделано, что ждёт, где лежат
промежуточные файлы. GUI читает его при открытии проекта и решает, какой
экран показать по умолчанию; каждый этап пишет в него при завершении.

Статусы этапа: pending | running | waiting_for_user | done | error
"""

import json
import os
import uuid
from datetime import datetime
from typing import Optional, Dict, Any

STAGES = ["script", "narration", "transcription", "shot_expansion", "video_matching", "assembly"]


class ProjectManifest:
    def __init__(self, project_dir: str):
        self.project_dir = project_dir
        self.path = os.path.join(project_dir, "manifest.json")
        self.data: Dict[str, Any] = {}

    @classmethod
    def create(cls, project_dir: str, source: Dict, config: Dict) -> "ProjectManifest":
        os.makedirs(project_dir, exist_ok=True)
        config.setdefault("content_mode", "documentary")
        config.setdefault("aspect_ratio", "16:9")
        m = cls(project_dir)
        m.data = {
            "project_id": uuid.uuid4().hex,
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "source": source,
            "config": config,
            "stages": {stage: {"status": "pending", "output": None} for stage in STAGES},
        }
        m.save()
        return m

    @classmethod
    def load(cls, project_dir: str) -> "ProjectManifest":
        m = cls(project_dir)
        with open(m.path, "r", encoding="utf-8") as f:
            m.data = json.load(f)
        m.data.setdefault("config", {})
        m.data["config"].setdefault("content_mode", "documentary")
        m.data["config"].setdefault("aspect_ratio", "16:9")
        return m

    @classmethod
    def load_or_none(cls, project_dir: str) -> Optional["ProjectManifest"]:
        path = os.path.join(project_dir, "manifest.json")
        if not os.path.exists(path):
            return None
        return cls.load(project_dir)

    def save(self):
        self.data["updated_at"] = datetime.now().isoformat()
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    def set_stage(self, stage: str, status: str, output: Optional[str] = None, error: Optional[str] = None):
        assert stage in STAGES, f"Неизвестный этап: {stage}"
        entry = {"status": status, "output": output}
        if error:
            entry["error"] = error
        self.data["stages"][stage] = entry
        self.save()

    def get_stage(self, stage: str) -> Dict:
        return self.data["stages"].get(stage, {"status": "pending", "output": None})

    def next_pending_stage(self) -> Optional[str]:
        """Первый этап, который ещё не done — сюда GUI открывает проект по умолчанию."""
        for stage in STAGES:
            if self.get_stage(stage)["status"] != "done":
                return stage
        return None  # всё готово

    def path_for(self, filename: str) -> str:
        return os.path.join(self.project_dir, filename)

    def path_for_generation_state(self) -> str:
        """Путь к JSON-файлу состояния генерации (generation_state.json)."""
        return os.path.join(self.project_dir, "generation_state.json")
