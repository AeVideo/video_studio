"""
gui/workers.py

QThread-обёртки над функциями video-pipeline — тот же паттерн, что уже
использован в openai-whisper/gui/workers.py (FixWorker, WhisperWorker),
просто применённый к новому набору модулей: book_to_script, srt_to_scenes,
scenes_to_shots, pexels_matcher, assemble_video.
"""

import os
import sys
import json
import tempfile

import requests
from PyQt5.QtCore import QThread, pyqtSignal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.script import book_to_script
from core.scenes import srt_to_scenes
from core.scenes import scenes_to_shots
from core.video_sources import pexels_matcher
from core.assembly import assemble_video
from scraping import fbi_vault_index
from scraping import fbi_vault_scraper
from services.service_manager import ensure_running as ensure_subtitle_service

SUBTITLE_SERVICE_URL = "http://127.0.0.1:8800"


class IndexLoadWorker(QThread):
    """Грузит (или берёт из кэша) полный индекс дел FBI Vault."""
    finished_ok = pyqtSignal(bool, object)  # success, список дел или сообщение об ошибке

    def run(self):
        try:
            index = fbi_vault_index.load_or_build_index()
            self.finished_ok.emit(True, index)
        except Exception as e:
            self.finished_ok.emit(False, str(e))


class VolumeListWorker(QThread):
    """Только список томов дела (без скачивания PDF, без OCR) — показывается
    перед реальной загрузкой, чтобы пользователь мог выбрать, что качать."""
    finished_ok = pyqtSignal(bool, object)  # success, список томов или сообщение об ошибке

    def __init__(self, case_url, parent=None):
        super().__init__(parent)
        self.case_url = case_url

    def run(self):
        try:
            volumes = fbi_vault_scraper.list_case_parts_only(self.case_url)
            self.finished_ok.emit(True, volumes)
        except Exception as e:
            self.finished_ok.emit(False, str(e))


class CaseScrapeWorker(QThread):
    """Скачивает материалы ВЫБРАННЫХ томов дела FBI Vault и собирает единый .txt."""
    log_line = pyqtSignal(str)
    finished_ok = pyqtSignal(bool, str)  # success, путь к combined_source.txt или ошибка

    def __init__(self, case_url, out_dir, selected_indices=None, max_ocr_pages=None, parent=None):
        super().__init__(parent)
        self.case_url = case_url
        self.out_dir = out_dir
        self.selected_indices = selected_indices
        self.max_ocr_pages = max_ocr_pages

    def run(self):
        try:
            self.log_line.emit(f"Скачиваю материалы дела: {self.case_url}")
            out_txt = fbi_vault_scraper.build_case_corpus(
                self.case_url, self.out_dir,
                selected_indices=self.selected_indices,
                max_ocr_pages=self.max_ocr_pages,
            )
            self.finished_ok.emit(True, out_txt)
        except Exception as e:
            self.finished_ok.emit(False, f"Ошибка скачивания дела: {e}")


class ScriptWorker(QThread):
    """Источник -> истории (бэклог) + сценарий первой истории."""
    log_line = pyqtSignal(str)
    finished_ok = pyqtSignal(bool, str)  # success, путь к script.json или сообщение об ошибке

    def __init__(self, book_path, duration_minutes, language, out_dir, content_mode="documentary", parent=None):
        super().__init__(parent)
        self.book_path = book_path
        self.duration_minutes = duration_minutes
        self.language = language
        self.out_dir = out_dir
        self.content_mode = content_mode

    def run(self):
        try:
            client = book_to_script.get_client()
            with open(self.book_path, "r", encoding="utf-8") as f:
                book_text = f.read()

            self.log_line.emit("Разбиваю источник на самостоятельные истории...")
            stories = book_to_script.split_book_into_stories(book_text, client, language=self.language)
            if not stories:
                self.finished_ok.emit(False, "Не удалось выделить ни одной истории")
                return

            first, backlog = stories[0], stories[1:]
            os.makedirs(self.out_dir, exist_ok=True)
            with open(os.path.join(self.out_dir, "stories_backlog.json"), "w", encoding="utf-8") as f:
                json.dump(backlog, f, ensure_ascii=False, indent=2)

            self.log_line.emit(f"Пишу сценарий для: {first['title']}")
            from core.script.calibration import get_calibrated_chars_per_minute
            chars_per_minute = get_calibrated_chars_per_minute(book_to_script.CHARS_PER_MINUTE_DEFAULT)
            script = book_to_script.write_script_for_story(
                story=first, source_text=book_text, duration_minutes=self.duration_minutes,
                language=self.language, client=client, content_mode=self.content_mode,
                chars_per_minute=chars_per_minute,
            )

            script_path = os.path.join(self.out_dir, "script.json")
            with open(script_path, "w", encoding="utf-8") as f:
                json.dump(script, f, ensure_ascii=False, indent=2)

            narration_path = os.path.join(self.out_dir, "narration_text.txt")
            with open(narration_path, "w", encoding="utf-8") as f:
                f.write("\n\n".join(s["text"] for s in script))

            self.finished_ok.emit(True, script_path)
        except Exception as e:
            self.finished_ok.emit(False, f"Ошибка генерации сценария: {e}")


class SubtitlesWorker(QThread):
    """Озвучка -> распознавание + вычитка по эталону через subtitle_service.
    Использует асинхронный job + опрос статуса — раньше один блокирующий
    POST на 10+ минут не давал вообще никакой обратной связи, GUI выглядел
    зависшим, хотя CPU был загружен на 100%."""
    log_line = pyqtSignal(str)
    progress = pyqtSignal(int, str)  # percent (0-100), message
    finished_ok = pyqtSignal(bool, str)  # success, srt_path или сообщение об ошибке

    def __init__(self, audio_path, narration_text_path, language="ru", parent=None):
        super().__init__(parent)
        self.audio_path = audio_path
        self.narration_text_path = narration_text_path
        self.language = language

    def run(self):
        self.log_line.emit("Проверяю subtitle_service...")

        # Калибровка симв/мин по РЕАЛЬНОЙ готовой озвучке — не зависит от
        # того, успеет ли дальше subtitle_service (транскрипция может упасть
        # по своим причинам), поэтому делаем это первым делом, а не в конце.
        # См. core/script/calibration.py — раньше вместо этого замера везде
        # использовалась захардкоженная угаданная цифра (900 симв/мин),
        # которая разошлась с реальным темпом речи диктора.
        try:
            from core.script.calibration import record_calibration_sample
            record_calibration_sample(self.narration_text_path, self.audio_path)
        except Exception as e:
            self.log_line.emit(f"Калибровка темпа речи пропущена (не критично): {e}")

        if not ensure_subtitle_service():
            self.finished_ok.emit(False, "Не удалось запустить subtitle_service")
            return

        self.log_line.emit("Распознаю речь и сверяю с эталоном...")
        try:
            resp = requests.post(
                f"{SUBTITLE_SERVICE_URL}/transcribe_and_proofread_async",
                json={
                    "audio_path": self.audio_path,
                    "reference_text_path": self.narration_text_path,
                    "language": self.language,
                    "model": "base",  # large-v3 на CPU без GPU непригодно медленный
                },
                timeout=30,
            )
            if not resp.ok:
                try:
                    detail = resp.json().get("detail", resp.text)
                except ValueError:
                    detail = resp.text
                self.finished_ok.emit(False, f"Ошибка subtitle_service ({resp.status_code}): {detail}")
                return
            job_id = resp.json()["job_id"]
        except requests.RequestException as e:
            self.finished_ok.emit(False, f"Ошибка subtitle_service: {e}")
            return

        last_message = None
        while True:
            self.msleep(1000)
            try:
                resp = requests.get(f"{SUBTITLE_SERVICE_URL}/job/{job_id}", timeout=10)
                resp.raise_for_status()
                job = resp.json()
            except requests.RequestException as e:
                self.finished_ok.emit(False, f"Ошибка опроса subtitle_service: {e}")
                return

            self.progress.emit(job.get("progress", 0), job.get("message", ""))
            if job.get("message") != last_message:
                self.log_line.emit(job.get("message", ""))
                last_message = job.get("message")

            if job["status"] == "done":
                self.finished_ok.emit(True, job["result"]["fixed_srt_path"])
                return
            if job["status"] == "error":
                self.finished_ok.emit(False, f"Ошибка subtitle_service: {job.get('message', 'неизвестная ошибка')}")
                return


class ShotExpansionWorker(QThread):
    """SRT + script.json -> сцены с реальными таймингами -> развёрнутые шоты."""
    finished_ok = pyqtSignal(bool, str)

    def __init__(self, script_path, srt_path, audio_path, out_dir, parent=None):
        super().__init__(parent)
        self.script_path = script_path
        self.srt_path = srt_path
        self.audio_path = audio_path
        self.out_dir = out_dir

    def run(self):
        try:
            with open(self.script_path, "r", encoding="utf-8") as f:
                script_scenes = json.load(f)

            cues = srt_to_scenes.parse_srt(self.srt_path)
            words = srt_to_scenes.cues_to_word_timings(cues)
            timed = srt_to_scenes.align_scenes_to_words(script_scenes, words)
            timed_dicts = [s.__dict__ for s in timed]

            scenes_timed_path = os.path.join(self.out_dir, "scenes_timed.json")
            with open(scenes_timed_path, "w", encoding="utf-8") as f:
                json.dump({"audio_path": self.audio_path, "srt_path": self.srt_path,
                           "scenes": timed_dicts}, f, ensure_ascii=False, indent=2)

            visuals_by_id = {s["id"]: s.get("visuals", []) for s in script_scenes}
            all_shots = []
            for scene in timed_dicts:
                if scene.get("status") == "missing" or scene.get("start") is None:
                    continue
                visuals = visuals_by_id.get(scene["id"], [])
                all_shots.extend(scenes_to_shots.expand_scene_to_shots(scene, visuals))

            shots_path = os.path.join(self.out_dir, "shots_timed.json")
            with open(shots_path, "w", encoding="utf-8") as f:
                json.dump({"audio_path": self.audio_path, "srt_path": self.srt_path,
                           "scenes": all_shots}, f, ensure_ascii=False, indent=2)

            self.finished_ok.emit(True, shots_path)
        except Exception as e:
            self.finished_ok.emit(False, f"Ошибка разбивки на шоты: {e}")


class MatchWorker(QThread):
    """Подбор видео с Pexels/Pixabay по каждому шоту — самый долгий этап."""
    progress = pyqtSignal(int, int, str)  # текущий, всего, id шота
    finished_ok = pyqtSignal(bool, str)

    def __init__(self, shots_path, out_path, pexels_key, pixabay_key=None, aspect_ratio="16:9", parent=None):
        super().__init__(parent)
        self.shots_path = shots_path
        self.out_path = out_path
        self.pexels_key = pexels_key
        self.pixabay_key = pixabay_key
        self.orientation = "portrait" if aspect_ratio == "9:16" else "landscape"
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def _save_partial(self, data, results):
        """Атомарно сохраняет текущий прогресс подбора в self.out_path."""
        payload = {
            "audio_path": data.get("audio_path"),
            "srt_path": data.get("srt_path"),
            "scenes": results,
        }
        tmp_path = self.out_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, self.out_path)

    def run(self):
        try:
            with open(self.shots_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            scenes = data["scenes"]

            deepseek_client = pexels_matcher.get_deepseek_client()
            clip_scorer = pexels_matcher.ClipScorer()
            used_ids = set()
            thumbnails_dir = os.path.join(os.path.dirname(os.path.abspath(self.out_path)),
                                           pexels_matcher.THUMBNAILS_DIR_NAME)

            archive_pool = []
            archive_dead_ids = set()
            if pexels_matcher.archive_org_search is not None:
                story_summary = " ".join(s["text"] for s in scenes)[:2000]
                theme_queries = pexels_matcher.generate_archive_theme_queries(story_summary, deepseek_client)
                if theme_queries:
                    archive_pool = pexels_matcher.archive_org_search.find_theme_pool(theme_queries)

            results = []
            for i, scene in enumerate(scenes, 1):
                if self._cancelled:
                    self._save_partial(data, results)
                    self.finished_ok.emit(False, "Отменено пользователем")
                    return
                self.progress.emit(i, len(scenes), scene["id"])
                matched = pexels_matcher.match_scene(
                    scene, deepseek_client, clip_scorer, self.pexels_key, used_ids,
                    pixabay_key=self.pixabay_key, thumbnails_dir=thumbnails_dir,
                    orientation=self.orientation, archive_pool=archive_pool,
                    archive_dead_ids=archive_dead_ids,
                )
                results.append(matched)
                self._save_partial(data, results)

            # Если сцен не было — всё равно создаём файл с пустым списком.
            if not scenes:
                self._save_partial(data, results)

            self.finished_ok.emit(True, self.out_path)
        except Exception as e:
            self.finished_ok.emit(False, f"Ошибка подбора видео: {e}")


class RegenerateShotWorker(QThread):
    """Перегенерирует подбор видео для ОДНОГО шота, не трогая остальные —
    точечная перегенерация, а не пересчёт всей цепочки заново."""
    finished_ok = pyqtSignal(bool, str)  # success, shot_id или сообщение об ошибке

    def __init__(self, footage_path, shot_id, pexels_key, pixabay_key=None, aspect_ratio="16:9", parent=None):
        super().__init__(parent)
        self.footage_path = footage_path
        self.shot_id = shot_id
        self.pexels_key = pexels_key
        self.pixabay_key = pixabay_key
        self.orientation = "portrait" if aspect_ratio == "9:16" else "landscape"

    def run(self):
        try:
            with open(self.footage_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            scenes = data["scenes"]

            target_index = next((i for i, s in enumerate(scenes) if s["id"] == self.shot_id), None)
            if target_index is None:
                self.finished_ok.emit(False, f"Шот {self.shot_id} не найден")
                return

            # Исключаем клипы, уже занятые ДРУГИМИ шотами — не хотим дублировать
            # тот же ролик в двух местах ролика при ручной перегенерации.
            used_ids = {
                s["footage"]["pexels_id"]
                for i, s in enumerate(scenes)
                if i != target_index and s.get("footage")
            }

            deepseek_client = pexels_matcher.get_deepseek_client()
            clip_scorer = pexels_matcher.ClipScorer()
            thumbnails_dir = os.path.join(os.path.dirname(os.path.abspath(self.footage_path)),
                                           pexels_matcher.THUMBNAILS_DIR_NAME)

            shot = {k: v for k, v in scenes[target_index].items()
                     if k not in ("footage", "status", "tried_queries")}
            matched = pexels_matcher.match_scene(
                shot, deepseek_client, clip_scorer, self.pexels_key, used_ids,
                pixabay_key=self.pixabay_key, thumbnails_dir=thumbnails_dir,
                orientation=self.orientation,
            )
            scenes[target_index] = matched

            with open(self.footage_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

            self.finished_ok.emit(True, self.shot_id)
        except Exception as e:
            self.finished_ok.emit(False, f"Ошибка перегенерации: {e}")


class ElevenLabsWorker(QThread):
    """Автоматическая озвучка через ElevenLabs API вместо ручной загрузки файла."""
    log_line = pyqtSignal(str)
    finished_ok = pyqtSignal(bool, str)  # success, путь к .mp3 или сообщение об ошибке

    def __init__(self, text_path, voice_id, out_path, parent=None):
        super().__init__(parent)
        self.text_path = text_path
        self.voice_id = voice_id
        self.out_path = out_path

    def run(self):
        try:
            from core.narration import elevenlabs_tts
            with open(self.text_path, "r", encoding="utf-8") as f:
                text = f.read()

            self.log_line.emit("Озвучиваю через ElevenLabs API...")
            elevenlabs_tts.generate_narration(text, self.voice_id, self.out_path)
            self.finished_ok.emit(True, self.out_path)
        except Exception as e:
            self.finished_ok.emit(False, f"Ошибка озвучки через API: {e}")


class AssembleWorker(QThread):
    """Финальная сборка ролика — повторяет логику assemble_video.main(),
    но вызывает функции напрямую вместо парсинга argv, и шлёт прогресс в GUI."""
    log_line = pyqtSignal(str)
    finished_ok = pyqtSignal(bool, str)

    def __init__(self, footage_path, audio_path, out_path, aspect_ratio="16:9",
                 subtitles_path=None, style_slug=None, parent=None):
        super().__init__(parent)
        self.footage_path = footage_path
        self.audio_path = audio_path
        self.out_path = out_path
        self.aspect_ratio = aspect_ratio
        self.subtitles_path = subtitles_path
        self.style_slug = style_slug

    def run(self):
        try:
            target_width, target_height = assemble_video.ASPECT_RATIOS[self.aspect_ratio]

            with open(self.footage_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            scenes = data["scenes"]

            work_dir = tempfile.mkdtemp(prefix="video_assembly_")
            clip_paths, durations = [], []
            placeholder_count, placeholder_seconds = 0, 0.0

            for i, scene in enumerate(scenes):
                footage = scene.get("footage")
                duration = round(scene["end"] - scene["start"], 2)
                if duration <= 0:
                    continue

                self.log_line.emit(f"[{i+1}/{len(scenes)}] {scene['id']}: обрабатываю...")
                raw_path = os.path.join(work_dir, f"raw_{i}.mp4")
                norm_path = os.path.join(work_dir, f"norm_{i}.mp4")
                padded = round(duration + assemble_video.CROSSFADE_SEC, 2)

                if footage:
                    assemble_video.download_clip(footage["video_link"], raw_path)
                    assemble_video.normalize_clip(
                        raw_path, norm_path, padded,
                        offset=footage.get("best_offset", 0.0),
                        source_duration=footage.get("duration", 0.0),
                        target_width=target_width, target_height=target_height,
                    )
                else:
                    # Раньше здесь было continue — молча пропускало шот, отчего
                    # видео отставало от полной озвучки НАЧИНАЯ с этого места
                    # (не только в конце), а mux_audio() с -shortest потом тихо
                    # обрезал звук по итоговой укороченной длительности видео.
                    # См. assemble_video.generate_placeholder_clip.
                    self.log_line.emit(f"    нет footage для {scene['id']} — вставляю плейсхолдер {padded}с")
                    assemble_video.generate_placeholder_clip(norm_path, padded,
                                                              target_width=target_width, target_height=target_height)
                    placeholder_count += 1
                    placeholder_seconds += duration
                clip_paths.append(norm_path)
                durations.append(padded)

            if not clip_paths:
                self.finished_ok.emit(False, "Нет ни одного клипа для сборки")
                return

            if placeholder_count:
                self.log_line.emit(
                    f"ВНИМАНИЕ: {placeholder_count} шот(ов) без видео заменены заглушкой "
                    f"({placeholder_seconds:.1f} сек суммарно) — проверьте footage.json"
                )

            self.log_line.emit("Склеиваю клипы с кроссфейдом...")
            concat_path = os.path.join(work_dir, "concat.mp4")
            assemble_video.build_crossfade_chain(clip_paths, durations, concat_path)

            self.log_line.emit("Накладываю озвучку...")
            if self.subtitles_path and os.path.exists(self.subtitles_path):
                muxed_path = os.path.join(work_dir, "muxed.mp4")
                assemble_video.mux_audio(concat_path, self.audio_path, muxed_path)
                self.log_line.emit("Прожигаю субтитры (движок OasisSubtitleStudio)...")
                assemble_video.burn_subtitles(muxed_path, self.subtitles_path, self.out_path,
                                               style_slug=self.style_slug)
            else:
                assemble_video.mux_audio(concat_path, self.audio_path, self.out_path)

            self.finished_ok.emit(True, self.out_path)
        except Exception as e:
            self.finished_ok.emit(False, f"Ошибка сборки: {e}")
