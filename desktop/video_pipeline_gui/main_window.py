"""
gui/main_window.py

Владеет ProjectManifest и всеми QThread-воркерами; экраны (gui/screens.py)
сами ничего не запускают, только эмитят сигналы сюда. Вся оркестрация —
в одном месте, чтобы не потерять, кто в какой момент что вызывает.
"""

import os
import sys
from typing import Optional

from PyQt5.QtWidgets import QMainWindow, QStackedWidget, QMessageBox, QWidget, QVBoxLayout
from PyQt5.QtCore import QTimer

from core.manifest import ProjectManifest
from asr import bridge as oasis_studio_bridge
from desktop.video_pipeline_gui.status_bar import StatusBar
from desktop.video_pipeline_gui.stage_tabs import StageTabBar
from desktop.video_pipeline_gui.screens import (
    NewProjectScreen, CaseBrowserScreen, VolumeSelectionScreen, ScriptScreen, NarrationScreen,
    SubtitlesScreen, MatchingScreen, AssemblyScreen,
)
from desktop.video_pipeline_gui.workers import (
    IndexLoadWorker, VolumeListWorker, CaseScrapeWorker,
    ScriptWorker, SubtitlesWorker, ShotExpansionWorker, MatchWorker, AssembleWorker,
    RegenerateShotWorker, ElevenLabsWorker,
)

from config.paths import PROJECTS_ROOT  # см. config/paths.py — раньше был хардкод ~/projects/video-pipeline/gui_projects


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("OASIS Video Combine")
        self.resize(900, 700)

        self.manifest: Optional[ProjectManifest] = None
        self._visited_stage_tabs = set()
        self._worker = None  # держим ссылку на активный воркер, чтобы не удалился раньше времени

        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)

        self.status_bar_widget = StatusBar()
        central_layout.addWidget(self.status_bar_widget)

        self.stage_tabs = StageTabBar()
        self.stage_tabs.stage_clicked.connect(self._on_stage_tab_clicked)
        central_layout.addWidget(self.stage_tabs)

        self.stack = QStackedWidget()
        central_layout.addWidget(self.stack)

        self.setCentralWidget(central)

        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self.status_bar_widget.refresh)
        self._status_timer.start(5000)  # обновление раз в 5 сек — subtitle_service может подняться позже
        self.status_bar_widget.refresh()

        self.screen_new_project = NewProjectScreen()
        self.screen_case_browser = CaseBrowserScreen()
        self.screen_volume_selection = VolumeSelectionScreen()
        self.screen_script = ScriptScreen()
        self.screen_narration = NarrationScreen()
        self.screen_subtitles = SubtitlesScreen()
        self.screen_matching = MatchingScreen()
        self.screen_assembly = AssemblyScreen()

        for w in [self.screen_new_project, self.screen_case_browser, self.screen_volume_selection,
                  self.screen_script, self.screen_narration, self.screen_subtitles,
                  self.screen_matching, self.screen_assembly]:
            self.stack.addWidget(w)

        # Экран -> ключ вкладки в StageTabBar. Экраны до появления
        # manifest.json (новый проект/браузер дел/выбор томов) ни на что
        # не отображаются — вкладки для них не подсвечиваются.
        # screen_subtitles (вычитка/транскрипция) сознательно отображается
        # на ту же вкладку «Озвучка», что и screen_narration — отдельной
        # вкладки под неё нет по просьбе свести всё к 4 вкладкам.
        self._screen_to_stage_tab = {
            self.screen_script: "script",
            self.screen_narration: "narration",
            self.screen_subtitles: "narration",
            self.screen_matching: "video_matching",
            self.screen_assembly: "assembly",
        }

        self.screen_new_project.create_requested.connect(self._on_create_project)
        self.screen_new_project.browse_archive_requested.connect(self._on_browse_archive)
        self.screen_new_project.resume_requested.connect(self._resume_project)
        self.screen_case_browser.case_selected.connect(self._on_case_selected)
        self.screen_case_browser.back_requested.connect(lambda: self._goto(self.screen_new_project))
        self.screen_volume_selection.download_requested.connect(self._on_volumes_selected)
        self.screen_volume_selection.back_requested.connect(lambda: self._goto(self.screen_case_browser))
        self.screen_script.continue_requested.connect(self._goto_narration)
        self.screen_narration.audio_selected.connect(self._on_audio_selected)
        self.screen_narration.auto_narration_requested.connect(self._on_auto_narration_requested)
        self.screen_subtitles.start_requested.connect(self._on_start_subtitles)
        self.screen_subtitles.continue_requested.connect(self._on_continue_to_matching)
        self.screen_matching.start_requested.connect(self._on_start_matching)
        self.screen_matching.regenerate_requested.connect(self._on_regenerate_shot)
        self.screen_matching.continue_requested.connect(self._enter_assembly_screen)
        self.screen_assembly.start_requested.connect(self._on_start_assembly)
        self.screen_assembly.back_to_matching_requested.connect(self._on_back_to_matching)

        self._refresh_recent_projects()
        self._goto(self.screen_new_project)

    def _refresh_recent_projects(self):
        """Сканирует PROJECTS_ROOT на manifest.json — без этого при любом
        перезапуске (падение/обновление кода/просто закрытие окна) весь
        прогресс проекта был недостижим из GUI, хотя manifest.json и все
        промежуточные файлы (сценарий, аудио, srt, shots_footage.json)
        уже лежали на диске."""
        projects = []
        if os.path.isdir(PROJECTS_ROOT):
            for name in sorted(os.listdir(PROJECTS_ROOT)):
                project_dir = os.path.join(PROJECTS_ROOT, name)
                manifest_path = os.path.join(project_dir, "manifest.json")
                if not os.path.isfile(manifest_path):
                    continue
                try:
                    m = ProjectManifest.load(project_dir)
                except Exception:
                    continue
                stage = m.next_pending_stage() or "готово"
                status = m.get_stage(stage)["status"] if stage != "готово" else "done"
                label = m.data.get("source", {}).get("case_title") or \
                    os.path.basename(m.data.get("source", {}).get("path", "")) or name
                projects.append({
                    "project_dir": project_dir, "label": label,
                    "stage": stage, "status": status,
                    "updated_at": m.data.get("updated_at", ""),
                })
        projects.sort(key=lambda p: p["updated_at"], reverse=True)
        self.screen_new_project.set_recent_projects(projects)

    def _on_back_to_matching(self):
        """Кнопка «← Назад к подбору видео» на экране Сборки. Нужна потому,
        что визард линейный без штатной навигации назад — а резюмирование
        проекта, где ВСЕ этапы (включая саму сборку — final_video.mp4 уже
        существовал с самого первого прогона) отмечены done, ведёт сразу на
        экран Сборки в обход экрана подбора и его баннера об устаревших
        данных. Это единственный выход туда в такой ситуации."""
        footage_path = self.manifest.get_stage("video_matching").get("output")
        if footage_path and os.path.exists(footage_path):
            self.screen_matching.set_done(footage_path, stale=True)
        else:
            self.screen_matching.status_label.setText("Файл подбора не найден — начните подбор заново")
        self._goto(self.screen_matching)

    def _enter_assembly_screen(self):
        """Общая настройка AssemblyScreen — вызывается и при резюмировании
        проекта, и при обычном переходе «Дальше — сборка» из подбора видео,
        и теперь ещё при каждом клике по вкладке «Монтаж».

        aspect_ratio/стиль подтягиваются из manifest каждый раз — это
        безобидно, они и так синхронизируются обратно в manifest при
        нажатии «Собрать видео». А вот subtitles_path НЕ перечитываем,
        если поле уже непустое: раньше при возврате на этот экран (через
        вкладки или «Дальше — сборка» повторно) путь, вручную поправленный
        через «Обзор…», тихо стирался обратно на значение из manifest —
        приходилось указывать путь заново каждый раз."""
        aspect_ratio = self.manifest.data["config"].get("aspect_ratio", "16:9")
        self.screen_assembly.set_aspect_ratio(aspect_ratio)
        self.screen_assembly.set_style_options(oasis_studio_bridge.list_style_presets())
        if not self.screen_assembly.subtitles_path_edit.text().strip():
            srt_path = self.manifest.get_stage("transcription").get("output")
            self.screen_assembly.set_subtitles_path(srt_path or "")
        output = self.manifest.get_stage("assembly").get("output")
        if output and os.path.exists(output):
            self.screen_assembly.set_done(output)
        self._goto(self.screen_assembly)

    def _resume_project(self, project_dir: str):
        """Восстанавливает проект с первого незавершённого этапа — не
        трогая то, что уже сделано (сценарий/озвучка/субтитры/подбор видео
        остаются на диске и просто подхватываются, а не генерируются заново)."""
        try:
            self.manifest = ProjectManifest.load(project_dir)
            self._visited_stage_tabs = set()
        except Exception as e:
            QMessageBox.critical(self, "Ошибка загрузки проекта", str(e))
            return

        stage = self.manifest.next_pending_stage()
        config = self.manifest.data.get("config", {})
        source = self.manifest.data.get("source", {})

        if stage is None:
            self._enter_assembly_screen()
            return

        if stage == "script":
            if source.get("type") == "fbi_archive":
                combined_path = self.manifest.path_for("combined_source.txt")
                if not os.path.exists(combined_path):
                    # скрапинг не был доведён до конца в прошлый раз — начинаем
                    # заново с выбора томов, а не со всего мастера сначала
                    self._pending_case_url = source["case_url"]
                    self._pending_project_dir = project_dir
                    self.screen_volume_selection.set_loading()
                    self._goto(self.screen_volume_selection)
                    worker = VolumeListWorker(source["case_url"])
                    worker.finished_ok.connect(self._on_volumes_listed)
                    self._worker = worker
                    worker.start()
                    return
                book_path = combined_path
            else:
                book_path = source.get("path")

            self.manifest.set_stage("script", "running")
            worker = ScriptWorker(
                book_path, config.get("duration_minutes"), config.get("language"),
                project_dir, content_mode=config.get("content_mode", "documentary"),
            )
            worker.finished_ok.connect(self._on_script_done)
            self._worker = worker
            worker.start()
            return

        if stage == "narration":
            self._goto_narration()
            return

        if stage == "transcription":
            self._goto(self.screen_subtitles)
            return

        if stage == "shot_expansion":
            srt_path = self.manifest.get_stage("transcription").get("output")
            if srt_path and os.path.exists(srt_path):
                self.screen_subtitles.set_done(srt_path)
            self._goto(self.screen_subtitles)
            return

        if stage == "video_matching":
            self._goto(self.screen_matching)
            return

        if stage == "assembly":
            footage_path = self.manifest.get_stage("video_matching").get("output")
            if footage_path and os.path.exists(footage_path):
                self.screen_matching.set_done(footage_path, stale=True)
            self._goto(self.screen_matching)
            return

    def _goto(self, widget):
        self.stack.setCurrentWidget(widget)
        stage_key = self._screen_to_stage_tab.get(widget)
        if stage_key:
            self._visited_stage_tabs.add(stage_key)
        self._sync_stage_tabs(current=stage_key)

    def _sync_stage_tabs(self, current: Optional[str] = None):
        """Единая точка синхронизации вкладок с manifest — вызывается из
        _goto() при КАЖДОМ переключении экрана, так что не нужно вручную
        расставлять вызовы по всем ~15 обработчикам, которые меняют экран.

        Кликабельны: всё, что в manifest уже done; ВСЁ, что уже посещалось
        в этой сессии (self._visited_stage_tabs) — даже если этап так и не
        завершился успешно (например сборка упала с ошибкой) — иначе уйдя
        посмотреть другую вкладку и вернувшись назад можно обнаружить, что
        вкладка с незавершённым/упавшим этапом молча погасла; плюс сам
        текущий экран."""
        if not self.manifest:
            self.stage_tabs.set_reachable_stages(set())
            self.stage_tabs.set_current_stage("")
            return
        reachable = set(self._visited_stage_tabs)
        for tab_key in ("script", "narration", "video_matching", "assembly"):
            manifest_stage = tab_key  # ключи вкладок совпадают с ключами STAGES в manifest
            if self.manifest.get_stage(manifest_stage)["status"] == "done":
                reachable.add(tab_key)
        if current:
            reachable.add(current)
        self.stage_tabs.set_reachable_stages(reachable)
        self.stage_tabs.set_current_stage(current or "")

    def _on_stage_tab_clicked(self, stage_key: str):
        """Клик по вкладке — переиспользует уже существующую логику входа
        на каждый этап (та же, что при резюмировании), чтобы не заводить
        второй набор навигационных путей вдобавок к _resume_project."""
        if not self.manifest:
            return
        if stage_key == "script":
            self._goto(self.screen_script)
        elif stage_key == "narration":
            self._goto(self.screen_narration)
        elif stage_key == "video_matching":
            self._on_back_to_matching()  # уже делает set_done(..., stale=True) + _goto
        elif stage_key == "assembly":
            self._enter_assembly_screen()

    # ---------- 1 -> 2: новый проект -> сценарий ----------

    # ---------- 1 -> 1б: новый проект (архив) -> браузер дел ----------

    def _on_browse_archive(self, language, duration_minutes, content_mode, aspect_ratio):
        self._pending_language = language
        self._pending_duration = duration_minutes
        self._pending_content_mode = content_mode
        self._pending_aspect_ratio = aspect_ratio
        self._goto(self.screen_case_browser)

        worker = IndexLoadWorker()
        worker.finished_ok.connect(self._on_index_loaded)
        self._worker = worker
        worker.start()

    def _on_index_loaded(self, success, result):
        if not success:
            self.screen_case_browser.set_error(result)
            return
        self.screen_case_browser.set_index(result)

    def _on_case_selected(self, case_title, case_url):
        project_id = case_title.lower().replace(" ", "-")[:50]
        project_dir = os.path.join(PROJECTS_ROOT, project_id)
        os.makedirs(project_dir, exist_ok=True)

        self._visited_stage_tabs = set()
        self.manifest = ProjectManifest.create(
            project_dir,
            source={"type": "fbi_archive", "case_title": case_title, "case_url": case_url},
            config={"language": self._pending_language, "duration_minutes": self._pending_duration,
                    "content_mode": self._pending_content_mode, "aspect_ratio": self._pending_aspect_ratio},
        )
        self._pending_case_url = case_url
        self._pending_project_dir = project_dir

        self.screen_volume_selection.set_loading()
        self._goto(self.screen_volume_selection)

        worker = VolumeListWorker(case_url)
        worker.finished_ok.connect(self._on_volumes_listed)
        self._worker = worker
        worker.start()

    def _on_volumes_listed(self, success, result):
        if not success:
            self.screen_volume_selection.set_error(result)
            return
        self.screen_volume_selection.set_volumes(result)

    def _on_volumes_selected(self, selected_indices, max_ocr_pages):
        worker = CaseScrapeWorker(
            self._pending_case_url, self._pending_project_dir,
            selected_indices=selected_indices, max_ocr_pages=max_ocr_pages,
        )
        worker.finished_ok.connect(self._on_case_scraped)
        self._worker = worker
        worker.start()

    def _on_case_scraped(self, success, result):
        if not success:
            QMessageBox.critical(self, "Ошибка скачивания дела", result)
            return
        # result — combined_source.txt; дальше используем как обычный book_path
        self.manifest.set_stage("script", "running")
        worker = ScriptWorker(
            result, self.manifest.data["config"]["duration_minutes"],
            self.manifest.data["config"]["language"], self.manifest.project_dir,
            content_mode=self.manifest.data["config"].get("content_mode", "documentary"),
        )
        worker.finished_ok.connect(self._on_script_done)
        self._worker = worker
        worker.start()

    def _on_create_project(self, book_path, language, duration_minutes, content_mode, aspect_ratio):
        project_id = os.path.splitext(os.path.basename(book_path))[0]
        project_dir = os.path.join(PROJECTS_ROOT, project_id)

        self._visited_stage_tabs = set()
        self.manifest = ProjectManifest.create(
            project_dir,
            source={"type": "book", "path": book_path},
            config={"language": language, "duration_minutes": duration_minutes,
                    "content_mode": content_mode, "aspect_ratio": aspect_ratio},
        )
        self.manifest.set_stage("script", "running")

        worker = ScriptWorker(book_path, duration_minutes, language, project_dir, content_mode=content_mode)
        worker.finished_ok.connect(self._on_script_done)
        self._worker = worker
        worker.start()

    def _on_script_done(self, success, result):
        if not success:
            QMessageBox.critical(self, "Ошибка сценария", result)
            self.manifest.set_stage("script", "error", error=result)
            return
        self.manifest.set_stage("script", "done", output=result)
        self.screen_script.load_script(result)
        self._goto(self.screen_script)

    # ---------- 2 -> 3: сценарий -> озвучка ----------

    def _goto_narration(self):
        narration_path = self.manifest.path_for("narration_text.txt")
        self.screen_narration.load_narration_text(narration_path)
        self.manifest.set_stage("narration", "waiting_for_user")
        self._goto(self.screen_narration)

    # ---------- 3 -> 4: озвучка -> субтитры ----------

    def _on_audio_selected(self, audio_path):
        self.manifest.data["source"]["audio_path"] = audio_path
        self.manifest.set_stage("narration", "done", output=audio_path)
        self._goto(self.screen_subtitles)

    def _on_auto_narration_requested(self, voice_id: str):
        narration_path = self.manifest.path_for("narration_text.txt")
        out_path = self.manifest.path_for("narration.mp3")

        worker = ElevenLabsWorker(narration_path, voice_id, out_path)
        worker.log_line.connect(lambda line: self.screen_narration.status_label.setText(line))
        worker.finished_ok.connect(self._on_auto_narration_done)
        self._worker = worker
        worker.start()

    def _on_auto_narration_done(self, success, result):
        if not success:
            QMessageBox.critical(self, "Ошибка автоматической озвучки", result)
            self.screen_narration.status_label.setText(f"Ошибка: {result}")
            return
        self.screen_narration.status_label.setText(f"Готово: {result}")
        self._on_audio_selected(result)  # дальше — тот же путь, что и при ручной загрузке

    def _on_start_subtitles(self):
        audio_path = self.manifest.data["source"].get("audio_path")
        narration_path = self.manifest.path_for("narration_text.txt")
        language = self.manifest.data["config"]["language"]

        self.manifest.set_stage("transcription", "running")
        worker = SubtitlesWorker(audio_path, narration_path, language=language)
        worker.log_line.connect(self.screen_subtitles.append_log)
        worker.progress.connect(self.screen_subtitles.set_progress)
        worker.finished_ok.connect(self._on_subtitles_done)
        self._worker = worker
        worker.start()

    def _on_subtitles_done(self, success, result):
        if not success:
            self.screen_subtitles.set_error(result)
            self.manifest.set_stage("transcription", "error", error=result)
            return
        self.manifest.set_stage("transcription", "done", output=result)
        self.screen_subtitles.set_done(result)

    # ---------- 4 -> 5: субтитры -> развёртка в шоты -> подбор видео ----------

    def _on_continue_to_matching(self):
        script_path = self.manifest.get_stage("script")["output"]
        srt_path = self.manifest.get_stage("transcription")["output"]
        audio_path = self.manifest.data["source"].get("audio_path")

        self.manifest.set_stage("shot_expansion", "running")
        worker = ShotExpansionWorker(script_path, srt_path, audio_path, self.manifest.project_dir)
        worker.finished_ok.connect(self._on_shots_ready)
        self._worker = worker
        worker.start()

    def _on_shots_ready(self, success, result):
        if not success:
            QMessageBox.critical(self, "Ошибка разбивки на шоты", result)
            self.manifest.set_stage("shot_expansion", "error", error=result)
            return
        self.manifest.set_stage("shot_expansion", "done", output=result)
        self._goto(self.screen_matching)

    # ---------- 5 -> 6: подбор видео -> сборка ----------

    def _on_start_matching(self):
        shots_path = self.manifest.get_stage("shot_expansion")["output"]
        out_path = self.manifest.path_for("shots_footage.json")

        pexels_key = os.environ.get("PEXELS_API_KEY")
        pixabay_key = os.environ.get("PIXABAY_API_KEY")
        if not pexels_key:
            QMessageBox.critical(self, "Нет ключа", "PEXELS_API_KEY не задан (проверьте .env)")
            return

        aspect_ratio = self.manifest.data["config"].get("aspect_ratio", "16:9")
        self.manifest.set_stage("video_matching", "running")
        worker = MatchWorker(shots_path, out_path, pexels_key, pixabay_key=pixabay_key,
                              aspect_ratio=aspect_ratio)
        worker.progress.connect(self.screen_matching.set_progress)
        worker.finished_ok.connect(self._on_matching_done)
        self._worker = worker
        worker.start()

    def _on_matching_done(self, success, result):
        if not success:
            self.screen_matching.set_error(result)
            self.manifest.set_stage("video_matching", "error", error=result)
            return
        self.manifest.set_stage("video_matching", "done", output=result)
        self.screen_matching.set_done(result, stale=False)

    def _on_regenerate_shot(self, shot_id: str):
        footage_path = self.manifest.get_stage("video_matching")["output"]
        pexels_key = os.environ.get("PEXELS_API_KEY")
        pixabay_key = os.environ.get("PIXABAY_API_KEY")
        aspect_ratio = self.manifest.data["config"].get("aspect_ratio", "16:9")

        self.screen_matching.status_label.setText(f"Перегенерирую {shot_id}...")
        worker = RegenerateShotWorker(footage_path, shot_id, pexels_key, pixabay_key=pixabay_key,
                                       aspect_ratio=aspect_ratio)
        worker.finished_ok.connect(self._on_shot_regenerated)
        self._worker = worker
        worker.start()

    def _on_shot_regenerated(self, success, result):
        if not success:
            QMessageBox.critical(self, "Ошибка перегенерации", result)
            return
        footage_path = self.manifest.get_stage("video_matching")["output"]
        self.screen_matching.set_done(footage_path)  # перечитываем файл, обновляем список

    # ---------- 6: сборка ----------

    def _on_start_assembly(self, aspect_ratio, burn_subtitles, style_slug, subtitles_path):
        footage_path = self.manifest.get_stage("video_matching")["output"]
        audio_path = self.manifest.data["source"].get("audio_path")
        out_path = self.manifest.path_for("final_video.mp4")

        # aspect_ratio выбран в AssemblyScreen на этот конкретный прогон
        # сборки — сохраняем его в manifest, чтобы при резюмировании (и в
        # video_matching при повторном подборе) экран открывался с тем же
        # значением, а не откатывался к дефолту 16:9.
        self.manifest.data["config"]["aspect_ratio"] = aspect_ratio
        self.manifest.save()

        # subtitles_path приходит прямо из видимого поля в AssemblyScreen —
        # не из manifest.json заново. Раньше здесь повторно читался путь из
        # manifest.get_stage("transcription")["output"], который на практике
        # успел разойтись с реальным расположением файла (файлы переносили
        # вручную) — это и привело к молчаливой сборке без прожига без
        # единого объяснения почему. Экран уже провалидировал путь перед
        # тем как включить чекбокс, здесь просто доверяем этому выбору.
        if burn_subtitles and (not subtitles_path or not os.path.exists(subtitles_path)):
            QMessageBox.warning(self, "Субтитры не найдены",
                                 f"Путь не существует: {subtitles_path!r} — "
                                 f"собираю видео без прожига субтитров.")
            subtitles_path = None
        elif not burn_subtitles:
            subtitles_path = None

        self.manifest.set_stage("assembly", "running")
        worker = AssembleWorker(footage_path, audio_path, out_path, aspect_ratio=aspect_ratio,
                                 subtitles_path=subtitles_path, style_slug=style_slug or None)
        worker.log_line.connect(self.screen_assembly.append_log)
        worker.finished_ok.connect(self._on_assembly_done)
        self._worker = worker
        worker.start()

    def _on_assembly_done(self, success, result):
        if not success:
            self.screen_assembly.set_error(result)
            self.manifest.set_stage("assembly", "error", error=result)
            return
        self.manifest.set_stage("assembly", "done", output=result)
        self.screen_assembly.set_done(result)
