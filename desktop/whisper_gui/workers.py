"""
Фоновые потоки (QThread), чтобы GUI не подвисал во время долгих операций:
распознавание whisper, вычитка по книге, генерация .ass и вжигание в видео.
"""

import os
import subprocess
import sys

from PyQt5.QtCore import QThread, pyqtSignal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_NOWIN = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
from asr import fix_srt
from asr import make_subs
from asr import deepseek_verify


class WhisperWorker(QThread):
    """Запускает `whisper` как подпроцесс и стримит его вывод построчно."""
    log_line = pyqtSignal(str)
    finished_ok = pyqtSignal(bool, str)  # success, message/output_srt_path

    def __init__(self, whisper_exe, input_path, model, language, device,
                 word_timestamps, output_dir, parent=None):
        super().__init__(parent)
        self.whisper_exe = whisper_exe
        self.input_path = input_path
        self.model = model
        self.language = language
        self.device = device
        self.word_timestamps = word_timestamps
        self.output_dir = output_dir
        self._process = None
        self._cancelled = False

    def build_command(self):
        cmd = [
            self.whisper_exe, self.input_path,
            "--model", self.model,
            "--output_format", "srt",
            "--device", self.device,
            "--output_dir", self.output_dir,
        ]
        if self.language and self.language != "Auto":
            cmd += ["--language", self.language]
        if self.word_timestamps:
            cmd += ["--word_timestamps", "True"]
        return cmd

    def run(self):
        cmd = self.build_command()
        self.log_line.emit("Команда: " + " ".join(cmd))
        try:
            self._process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, universal_newlines=True,
                creationflags=_NOWIN
            )
        except FileNotFoundError:
            self.finished_ok.emit(False, f"Не найден исполняемый файл: {self.whisper_exe}")
            return
        except Exception as e:
            self.finished_ok.emit(False, f"Ошибка запуска whisper: {e}")
            return

        for line in self._process.stdout:
            if self._cancelled:
                break
            self.log_line.emit(line.rstrip())

        self._process.wait()

        if self._cancelled:
            self.finished_ok.emit(False, "Отменено пользователем.")
            return

        if self._process.returncode != 0:
            self.finished_ok.emit(False, f"whisper завершился с ошибкой (код {self._process.returncode}).")
            return

        base = os.path.splitext(os.path.basename(self.input_path))[0]
        srt_path = os.path.join(self.output_dir, base + ".srt")
        if os.path.exists(srt_path):
            self.finished_ok.emit(True, srt_path)
        else:
            self.finished_ok.emit(False, "Процесс завершился, но .srt файл не найден.")

    def cancel(self):
        self._cancelled = True
        if self._process and self._process.poll() is None:
            self._process.terminate()


class FixWorker(QThread):
    """Выполняет сопоставление srt с текстом книги (fix_srt.align_and_fix)."""
    progress = pyqtSignal(int, int)
    finished_ok = pyqtSignal(bool, str)

    def __init__(self, srt_path, book_path, output_path, parent=None):
        super().__init__(parent)
        self.srt_path = srt_path
        self.book_path = book_path
        self.output_path = output_path

    def run(self):
        try:
            ok = fix_srt.align_and_fix(
                self.srt_path, self.book_path, self.output_path,
                progress_cb=lambda cur, total: self.progress.emit(cur, total)
            )
        except Exception as e:
            self.finished_ok.emit(False, f"Ошибка вычитки: {e}")
            return

        if ok:
            self.finished_ok.emit(True, self.output_path)
        else:
            self.finished_ok.emit(False, "Не удалось выполнить сопоставление (проверьте пути к файлам).")


class BuildWorker(QThread):
    """
    Генерирует .ass из .srt (с учётом стиля) и, если запрошено,
    вжигает субтитры в видео через ffmpeg с парсингом прогресса.
    """
    log_line = pyqtSignal(str)
    phase_changed = pyqtSignal(str)
    progress = pyqtSignal(int)  # 0-100
    finished_ok = pyqtSignal(bool, str)

    def __init__(self, srt_path, video_path, style_slug, ass_output_path,
                 burn_into_video, final_output_path, encode_preset="medium", parent=None):
        super().__init__(parent)
        self.srt_path = srt_path
        self.video_path = video_path
        self.style_slug = style_slug
        self.ass_output_path = ass_output_path
        self.burn_into_video = burn_into_video
        self.final_output_path = final_output_path
        self.encode_preset = encode_preset
        self._process = None
        self._cancelled = False

    def _get_duration_seconds(self):
        cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "default=noprint_wrappers=1:nokey=1", self.video_path]
        try:
            out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=_NOWIN)
            return float(out.stdout.strip())
        except Exception:
            return None

    def run(self):
        self.phase_changed.emit("Генерация .ass")
        try:
            ok = make_subs.srt_to_animated_ass(
                self.srt_path, self.ass_output_path, self.video_path,
                style_slug=self.style_slug,
                progress_cb=lambda cur, total: self.progress.emit(int(cur / max(total, 1) * 100))
            )
        except Exception as e:
            self.finished_ok.emit(False, f"Ошибка генерации .ass: {e}")
            return

        if not ok:
            self.finished_ok.emit(False, "Не удалось сгенерировать .ass файл.")
            return

        self.log_line.emit(f"Файл стилей создан: {self.ass_output_path}")

        if not self.burn_into_video:
            self.finished_ok.emit(True, self.ass_output_path)
            return

        self.phase_changed.emit("Вжигание в видео (ffmpeg)")
        self.progress.emit(0)

        duration = self._get_duration_seconds()

        # Абсолютный путь важен: ffmpeg подставляет ass=путь как часть -vf.
        # Известная особенность именно фильтра ass/subtitles на Windows:
        # двоеточие после буквы диска (C:) нужно экранировать ДВОЙНЫМ
        # обратным слэшем, одинарные кавычки для этого фильтра не работают.
        ass_arg = os.path.abspath(self.ass_output_path).replace('\\', '/')
        ass_arg_escaped = ass_arg.replace(':', '\\\\:')

        cmd = [
            "ffmpeg", "-y", "-i", self.video_path,
            "-vf", f"ass={ass_arg_escaped}",
            "-c:v", "libx264", "-preset", self.encode_preset,
            "-c:a", "copy",
            "-progress", "pipe:1", "-nostats",
            self.final_output_path
        ]
        self.log_line.emit("Команда: " + " ".join(cmd))

        try:
            self._process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, universal_newlines=True,
                creationflags=_NOWIN
            )
        except Exception as e:
            self.finished_ok.emit(False, f"Ошибка запуска ffmpeg: {e}")
            return

        for line in self._process.stdout:
            if self._cancelled:
                break
            line = line.strip()
            if line.startswith("out_time_ms=") and duration:
                try:
                    out_time_s = int(line.split("=")[1]) / 1_000_000
                    pct = min(100, int(out_time_s / duration * 100))
                    self.progress.emit(pct)
                except (ValueError, ZeroDivisionError):
                    pass
            elif line.startswith("progress=") and "end" in line:
                self.progress.emit(100)
            elif not line.startswith(("frame=", "bitrate=", "total_size=", "out_time=",
                                       "dup_frames=", "drop_frames=", "speed=", "fps=")):
                self.log_line.emit(line)

        self._process.wait()

        if self._cancelled:
            self.finished_ok.emit(False, "Отменено пользователем.")
            return

        if self._process.returncode != 0:
            self.finished_ok.emit(False, f"ffmpeg завершился с ошибкой (код {self._process.returncode}).")
            return

        self.finished_ok.emit(True, self.final_output_path)

    def cancel(self):
        self._cancelled = True
        if self._process and self._process.poll() is None:
            self._process.terminate()


class VerifyWorker(QThread):
    """Отправляет .srt + текст книги на проверку в DeepSeek, поблочно."""
    progress = pyqtSignal(int, int)
    finished_ok = pyqtSignal(bool, object)  # success, results-list или сообщение об ошибке

    def __init__(self, srt_path, book_path, chunk_size=150, parent=None):
        super().__init__(parent)
        self.srt_path = srt_path
        self.book_path = book_path
        self.chunk_size = chunk_size

    def run(self):
        try:
            results = deepseek_verify.verify_srt(
                self.srt_path, self.book_path, chunk_size=self.chunk_size,
                progress_cb=lambda cur, total: self.progress.emit(cur, total)
            )
        except Exception as e:
            self.finished_ok.emit(False, str(e))
            return
        self.finished_ok.emit(True, results)
