"""
core/script/calibration.py

Раньше CHARS_PER_MINUTE_DEFAULT в book_to_script.py был захардкожен как 900
с комментарием "эмпирический темп речи, откалибруйте под свой TTS-голос" —
никто это программно не откалибровал, цифра просто угадана. Пока угадана
была близко к реальности — сценарии более-менее совпадали по времени с
целью. Как только реальный темп речи (голос диктора, конкретный TTS,
скорость чтения) разошёлся с этой угаданной цифрой — сценарии на заданную
длительность стали расходиться с фактическим временем озвучки, и никакого
сигнала об этом расхождении нигде не было — цифра 900 использовалась
дальше как ни в чём не бывало.

Этот модуль меряет РЕАЛЬНЫЙ темп речи по уже готовой паре (текст озвучки,
аудиофайл) через ffprobe (уже используется в проекте — см.
desktop/whisper_gui/workers.py, core/assembly/assemble_video.py) и хранит
скользящее среднее в JSON-файле под USER_DATA_DIR (переживает пересоздание
рантайма на Colab, в отличие от дотфайла в ~ — см. config/paths.py).

Использование:
    from core.script.calibration import get_calibrated_chars_per_minute, record_calibration_sample

    # при генерации сценария — вместо жёстко зашитого CHARS_PER_MINUTE_DEFAULT
    chars_per_minute = get_calibrated_chars_per_minute()

    # как только готова реальная озвучка для проекта (текст + аудиофайл)
    record_calibration_sample(narration_text_path, audio_path)
"""

import json
import logging
import os
import subprocess
from typing import Optional

from config.paths import USER_DATA_DIR

logger = logging.getLogger(__name__)

CALIBRATION_FILE = os.path.join(USER_DATA_DIR, "tts_calibration.json")

# Сколько последних замеров хранить и усреднять — не одно последнее число
# (шумно, один странный прогон собьёт калибровку) и не бесконечная история
# (не даст подстроиться, если голос/TTS реально сменили).
MAX_SAMPLES = 20

# Если фактический результат совсем не похож на речь (например, ffprobe не
# смог прочитать длительность, или странно короткий/длинный файл) — не
# засоряем калибровку заведомо мусорным замером.
MIN_PLAUSIBLE_CHARS_PER_MINUTE = 300
MAX_PLAUSIBLE_CHARS_PER_MINUTE = 2500


def _get_audio_duration_seconds(audio_path: str) -> Optional[float]:
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration",
           "-of", "default=noprint_wrappers=1:nokey=1", audio_path]
    try:
        out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
        return float(out.stdout.strip())
    except Exception as e:
        logger.warning(f"Не удалось получить длительность {audio_path} через ffprobe: {e}")
        return None


def _load_samples() -> list:
    if not os.path.exists(CALIBRATION_FILE):
        return []
    try:
        with open(CALIBRATION_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("samples", [])
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Файл калибровки {CALIBRATION_FILE} повреждён ({e}) — начинаю заново")
        return []


def _save_samples(samples: list) -> None:
    with open(CALIBRATION_FILE, "w", encoding="utf-8") as f:
        json.dump({"samples": samples}, f, ensure_ascii=False, indent=2)


def record_calibration_sample(narration_text_path: str, audio_path: str) -> Optional[float]:
    """Измеряет реальные символы/минуту по готовой паре (текст, аудио) и
    добавляет замер в скользящее окно. Возвращает измеренное значение для
    ЭТОГО конкретного файла (не усреднённое) — полезно для лога/диагностики,
    даже если сам замер отбракован как неправдоподобный и не сохранён."""
    if not os.path.exists(narration_text_path):
        logger.warning(f"Калибровка пропущена: нет файла текста {narration_text_path}")
        return None
    if not os.path.exists(audio_path):
        logger.warning(f"Калибровка пропущена: нет аудиофайла {audio_path}")
        return None

    with open(narration_text_path, "r", encoding="utf-8") as f:
        char_count = len(f.read())

    duration_seconds = _get_audio_duration_seconds(audio_path)
    if not duration_seconds or duration_seconds <= 0:
        logger.warning(f"Калибровка пропущена: ffprobe не дал длительность для {audio_path}")
        return None

    measured = char_count / (duration_seconds / 60.0)

    if not (MIN_PLAUSIBLE_CHARS_PER_MINUTE <= measured <= MAX_PLAUSIBLE_CHARS_PER_MINUTE):
        logger.warning(
            f"Калибровочный замер {measured:.0f} симв/мин выглядит неправдоподобно "
            f"(вне диапазона {MIN_PLAUSIBLE_CHARS_PER_MINUTE}-{MAX_PLAUSIBLE_CHARS_PER_MINUTE}) — "
            f"не сохраняю, но и не роняю пайплайн. Проверьте вручную {narration_text_path} и {audio_path}."
        )
        return measured

    samples = _load_samples()
    samples.append(measured)
    samples = samples[-MAX_SAMPLES:]
    _save_samples(samples)
    logger.info(
        f"Калибровка обновлена: {measured:.0f} симв/мин для этого файла "
        f"({char_count} символов / {duration_seconds:.0f} сек), "
        f"скользящее среднее по {len(samples)} замерам: {sum(samples) / len(samples):.0f} симв/мин"
    )
    return measured


def get_calibrated_chars_per_minute(default: int) -> int:
    """Возвращает откалиброванное значение (среднее по накопленным замерам),
    если оно есть, иначе — переданный default (обычно CHARS_PER_MINUTE_DEFAULT
    из book_to_script.py — тот самый угаданный 900, но теперь только как
    отправная точка ДО первой реальной калибровки, а не постоянная величина)."""
    samples = _load_samples()
    if not samples:
        return default
    return int(round(sum(samples) / len(samples)))
