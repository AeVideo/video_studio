"""
tools/audio_capture/capture_core.py

Логика захвата системного звука через PulseAudio/PipeWire-Pulse — без
какой-либо привязки к GUI. Вынесена отдельно от recorder_gui.py специально
для того, чтобы её можно было вызвать программно (например, из будущей
Playwright/Camoufox-автоматизации HeyGen: открыть страницу, нажать
предпросмотр голоса, синхронно начать/остановить запись) — там tkinter не
нужен и не должен тянуться как зависимость, важна только сама
последовательность действий.

Раньше вся эта логика была внутри методов класса RecorderApp (recorder.py)
вперемешку с обновлением полей интерфейса — здесь она отделена и не имеет
побочных эффектов на UI.

Системные зависимости (НЕ ставятся через pip — apt/системный пакетный
менеджер):
    sudo apt install pulseaudio-utils ffmpeg
"""
import re
import subprocess
import tempfile
import time
from typing import List, Optional, Tuple

from pydub import AudioSegment
from pydub.silence import detect_leading_silence

ALL_SYSTEM_AUDIO = "Весь системный звук"


def get_monitor_source() -> Tuple[int, str]:
    """Находит monitor-источник дефолтного sink (весь системный звук).
    Возвращает (index, name)."""
    try:
        default_sink = subprocess.check_output(["pactl", "get-default-sink"], text=True).strip()
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        raise RuntimeError(f"Не удалось получить дефолтный sink: {e}")

    try:
        out = subprocess.check_output(["pactl", "list", "short", "sources"], text=True)
    except FileNotFoundError:
        raise RuntimeError("Команда 'pactl' не найдена. Установите пакет pulseaudio-utils.")
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Ошибка при опросе источников звука: {e}")

    expected_name = f"{default_sink}.monitor"
    monitors: List[Tuple[int, str]] = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and ".monitor" in parts[1]:
            try:
                idx = int(parts[0])
            except ValueError:
                continue
            monitors.append((idx, parts[1]))

    if not monitors:
        raise RuntimeError(
            "Не найден monitor-источник. Проверьте вывод команды:\n"
            "  pactl list short sources"
        )

    for idx, name in monitors:
        if name == expected_name:
            return idx, name
    return monitors[0]


def list_active_streams() -> List[Tuple[int, str]]:
    """Возвращает список активных аудио-потоков приложений (sink-inputs):
    [(index, "Firefox — Playback"), ...]"""
    try:
        out = subprocess.check_output(["pactl", "list", "sink-inputs"], text=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        return []

    apps = []
    blocks = out.split("Sink Input #")[1:]
    for block in blocks:
        first_line = block.splitlines()[0].strip()
        try:
            idx = int(first_line)
        except ValueError:
            continue

        name_match = re.search(r'application\.name\s*=\s*"([^"]+)"', block)
        media_match = re.search(r'media\.name\s*=\s*"([^"]+)"', block)
        app_name = name_match.group(1) if name_match else "Неизвестное приложение"
        media_name = media_match.group(1) if media_match else ""

        label = app_name if not media_name or media_name == app_name else f"{app_name} — {media_name}"
        apps.append((idx, label))
    return apps


def get_recording_source_index() -> Optional[int]:
    """Индекс источника (Source: N), к которому подключён самый свежий
    клиент записи (наш parecord) — для проверки, что запись реально идёт
    с нужного устройства, а не, например, с микрофона по ошибке."""
    try:
        out = subprocess.check_output(["pactl", "list", "source-outputs"], text=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None

    blocks = out.split("Source Output #")[1:]
    if not blocks:
        return None

    last_block = blocks[-1]
    src_match = re.search(r'^\s*Source:\s*(\d+)', last_block, re.MULTILINE)
    return int(src_match.group(1)) if src_match else None


def trim_silence(sound: AudioSegment, silence_thresh_db: float, chunk_size: int = 10) -> AudioSegment:
    """Обрезает тишину в начале и в конце аудиосегмента."""
    def leading_silence_len(audio):
        return detect_leading_silence(audio, silence_threshold=silence_thresh_db, chunk_size=chunk_size)

    start_trim = leading_silence_len(sound)
    end_trim = leading_silence_len(sound.reverse())
    duration = len(sound)

    if start_trim + end_trim >= duration:
        return sound[0:0]  # весь файл оказался тишиной

    return sound[start_trim: duration - end_trim]


class RecordingSession:
    """Программный (без GUI) запуск/остановка записи системного звука —
    то, что будущая Playwright/Camoufox-автоматизация сможет вызвать вокруг
    клика по кнопке предпросмотра голоса:

        session = RecordingSession()
        session.start()
        page.click("кнопка предпросмотра")
        page.wait_for_selector("аудио доиграло")   # условно
        raw_path = session.stop()
        clean = trim_silence(AudioSegment.from_wav(raw_path), silence_thresh_db=-40.0)
        clean.export(out_path, format="mp3")

    Каждый шаг — то же самое, что делает RecorderApp в recorder_gui.py, но
    без единой зависимости от tkinter.
    """

    def __init__(self, source_label: Optional[str] = None):
        self.monitor_index, self.monitor_source = get_monitor_source()
        self.source_label = source_label  # None или ALL_SYSTEM_AUDIO -> весь системный звук
        self._process: Optional[subprocess.Popen] = None
        self._raw_path: Optional[str] = None
        self._log_path: Optional[str] = None

    def start(self) -> None:
        streams = {label: idx for idx, label in list_active_streams()}
        self._raw_path = tempfile.mktemp(prefix="sysaudio_", suffix=".wav")
        self._log_path = tempfile.mktemp(prefix="sysaudio_", suffix=".log")

        cmd = ["parecord", "--device", self.monitor_source, "--file-format=wav", self._raw_path]
        if self.source_label and self.source_label != ALL_SYSTEM_AUDIO and self.source_label in streams:
            cmd = ["parecord", "--monitor-stream", str(streams[self.source_label]),
                   "--device", self.monitor_source, "--file-format=wav", self._raw_path]

        log_f = open(self._log_path, "w")
        try:
            self._process = subprocess.Popen(cmd, stdout=log_f, stderr=log_f)
        except FileNotFoundError:
            raise RuntimeError("Команда 'parecord' не найдена. Установите пакет pulseaudio-utils.")

    def verify_source(self) -> Tuple[bool, Optional[int]]:
        """True, если запись реально идёт с ожидаемого monitor-источника."""
        actual = get_recording_source_index()
        return (actual == self.monitor_index), actual

    def stop(self) -> str:
        """Останавливает запись, возвращает путь к сырому .wav (без обрезки
        тишины — это отдельный шаг через trim_silence, вызывающий код сам
        решает, применять ли его)."""
        if self._process:
            self._process.terminate()
            try:
                self._process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._process.kill()
        time.sleep(0.3)  # даём parecord дописать файл на диск
        return self._raw_path
