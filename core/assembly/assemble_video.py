import argparse
import json
import os
import subprocess
import tempfile
from typing import List, Dict, Optional

from asr import bridge as oasis_studio_bridge

TARGET_WIDTH = 1280
TARGET_HEIGHT = 720
TARGET_FPS = 25
CROSSFADE_SEC = 0.5
MIN_CLIP_SIZE = 10 * 1024  # 10 КБ
DOWNLOAD_RETRIES = 2

_NVENC_AVAILABLE: Optional[bool] = None
LIBX264_ARGS = ["-c:v", "libx264", "-preset", "fast", "-crf", "20"]
NVENC_ARGS = ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "20", "-b:v", "0"]


def _has_nvenc() -> bool:
    """Проверяет один раз (кэшируется) РЕАЛЬНЫМ тестовым кодированием, а не
    просто наличием h264_nvenc в списке `ffmpeg -encoders` — тот список
    показывает, что энкодер СКОМПИЛИРОВАН, а не что он реально работает в
    этом конкретном окружении прямо сейчас (драйвер/лимиты/конкретный
    рантайм могут отличаться). На практике наблюдался краш сборки видео
    "на определённом моменте" после нескольких успешно обработанных
    клипов — расследование ffmpeg-логов было недоступно, но именно так
    выглядела бы ситуация "encoders list врёт" или "nvenc падает не на
    любом входе, а на части". Реальный тестовый прогон ловит первый
    случай сразу при старте; add_video_encode_args_with_fallback() ниже
    защищает и от второго — падение конкретной команды с nvenc больше не
    рушит всю сборку, а откатывается на libx264 для этой же команды.

    Отключить принудительно: переменная окружения VIDEO_STUDIO_FORCE_LIBX264=1."""
    global _NVENC_AVAILABLE
    if os.environ.get("VIDEO_STUDIO_FORCE_LIBX264"):
        return False
    if _NVENC_AVAILABLE is None:
        test_cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.1",
        ] + NVENC_ARGS + ["-f", "null", "-"]
        try:
            result = subprocess.run(test_cmd, capture_output=True, text=True, timeout=15)
            _NVENC_AVAILABLE = result.returncode == 0
            if not _NVENC_AVAILABLE:
                print(f"[assemble_video] h264_nvenc не прошёл тестовое кодирование в этом "
                      f"окружении — использую libx264. ffmpeg: {result.stderr.strip()[-300:]}")
        except Exception as e:
            _NVENC_AVAILABLE = False
            print(f"[assemble_video] Проверка h264_nvenc упала ({e}) — использую libx264.")
    return _NVENC_AVAILABLE


def _video_encode_args() -> List[str]:
    """Аргументы кодека для ffmpeg — до этого коммита ВСЕГДА возвращали
    libx264 (программный энкодер), даже на Colab с доступным T4. Это была
    задокументированная, но нереализованная задача с самого первого анализа
    проекта (COLAB_MIGRATION_PLAN.md, TASK G-04) — три предыдущих сессии
    правок её не коснулись, отсюда жалоба "чистый ffmpeg должен летать, а
    еле ползёт": на каждый шот (normalize_clip), плюс склейка crossfade,
    плюс прожиг субтитров — GPU физически не участвовала ни в одном.

    -preset/-crf у libx264 и -preset/-cq у nvenc — РАЗНЫЕ шкалы, нельзя
    просто перенести числа 1:1 (см. предупреждение в самом
    COLAB_MIGRATION_PLAN.md на этот счёт) — подобраны отдельно."""
    return NVENC_ARGS if _has_nvenc() else LIBX264_ARGS


def _run_encode(cmd_prefix: List[str], cmd_suffix: List[str]):
    """Собирает и запускает ffmpeg-команду кодирования видео с автоматическим
    откатом на libx264, если конкретная команда с nvenc падает — даже когда
    _has_nvenc() при старте сказала, что энкодер в целом работает (тестовое
    кодирование 64x64 может пройти, а падение случиться на конкретном
    реальном клипе — другое разрешение/формат/что угодно специфичное для
    именно этого входа). Раньше падение здесь рушило всю сборку целиком."""
    codec_args = _video_encode_args()
    cmd = cmd_prefix + codec_args + cmd_suffix
    try:
        run(cmd)
    except Exception as e:
        if codec_args is NVENC_ARGS:
            print(f"[assemble_video] h264_nvenc упал на этой команде ({e}) — "
                  f"повторяю тот же клип с libx264...")
            run(cmd_prefix + LIBX264_ARGS + cmd_suffix)
        else:
            raise


# Соотношение сторон готового видео. 16:9 — старое поведение (по умолчанию).
# 9:16 — вертикальное, для TikTok/Reels/Shorts. Разрешение внутри каждой
# ориентации фиксировано — этого достаточно для стокового b-roll, гнаться
# за 4K смысла нет, источники сами редко выше.
ASPECT_RATIOS = {
    "16:9": (1280, 720),
    "9:16": (720, 1280),
}
DEFAULT_ASPECT_RATIO = "16:9"


def run(cmd: List[str]):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        combined = result.stderr.strip() + "\n" + result.stdout.strip()
        raise RuntimeError(f"Команда упала:\n{' '.join(cmd)}\n{combined[-3000:]}")


def _cleanup(*paths: str):
    """Безопасно удаляет переданные файлы. Ошибки игнорируются."""
    for p in paths:
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except OSError:
            pass


def _is_valid_video(path: str) -> bool:
    """Проверяет, что файл существует, имеет достаточный размер и является валидным видео (через ffprobe)."""
    if not os.path.exists(path):
        return False
    if os.path.getsize(path) < MIN_CLIP_SIZE:
        return False
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=codec_type",
                "-of", "csv=p=0",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0 and "video" in result.stdout.lower()


def download_clip(url: str, dest: str):
    if os.path.exists(dest) and _is_valid_video(dest):
        return

    for attempt in range(DOWNLOAD_RETRIES):
        if os.path.exists(dest):
            _cleanup(dest)

        cmd = ["curl", "-L", "--fail", "-s", "-o", dest, url]
        try:
            run(cmd)
        except RuntimeError as e:
            _cleanup(dest)
            if attempt == DOWNLOAD_RETRIES - 1:
                raise ValueError(f"Не удалось скачать видео (HTTP ошибка): {e}") from e
            continue

        if os.path.getsize(dest) < MIN_CLIP_SIZE:
            _cleanup(dest)
            if attempt == DOWNLOAD_RETRIES - 1:
                raise ValueError("Скачанный файл слишком мал или битый")
            continue

        if not _is_valid_video(dest):
            _cleanup(dest)
            if attempt == DOWNLOAD_RETRIES - 1:
                raise ValueError("Скачанный файл не является валидным видео")
            continue

        return  # успех

    # Сюда не должны попадать, но на всякий случай
    raise ValueError("Не удалось скачать видео")


def generate_placeholder_clip(dest: str, duration: float,
                               target_width: int = TARGET_WIDTH, target_height: int = TARGET_HEIGHT,
                               color: str = "gray20"):
    """Клип-заглушка для шота без подобранного видео — тёмно-серый фон
    точно нужной длительности вместо того, чтобы молча пропустить шот.

    Раньше run_assembly_stage() при отсутствии footage делал `continue`,
    пропуская шот целиком — видео-дорожка получалась короче полной
    озвучки НЕ только в конце, а с этого самого места, и весь материал
    ПОСЛЕ пропущенного шота сдвигался относительно narration, оставаясь
    рассинхронизированным до самого конца. mux_audio() с -shortest потом
    просто тихо обрезает лишний хвост звука на итоговой (укороченной из-за
    пропусков) длительности видео — то, что выглядело как "обрыв на
    полуслове в конце", на самом деле означает рассинхрон, начавшийся
    гораздо раньше, просто заметный по факту только на границе.

    Плейсхолдер сохраняет тайминги видео=аудио всегда, и виден на глаз —
    сразу понятно, где не хватило покрытия по видео, а не только слышно
    заметно на слух, что что-то не так к концу ролика."""
    cmd_prefix = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-f", "lavfi", "-i", f"color=c={color}:s={target_width}x{target_height}:r={TARGET_FPS}:d={duration}",
    ]
    try:
        _run_encode(cmd_prefix, [dest])
    except Exception:
        _cleanup(dest)
        raise


def normalize_clip(src: str, dest: str, duration: float, offset: float = 0.0, source_duration: float = 0.0,
                    target_width: int = TARGET_WIDTH, target_height: int = TARGET_HEIGHT):
    """Приводит клип к единому разрешению/fps/без звука и точно нужной длительности
    (зацикливает, если исходника не хватает). Если offset задан и клипа хватает
    по длине после сдвига — стартуем с лучшего момента, а не с начала.
    target_width/target_height задают итоговое соотношение сторон (см.
    ASPECT_RATIOS) — по умолчанию старое поведение 1280×720."""
    if os.path.exists(dest) and _is_valid_video(dest):
        return
    if os.path.exists(dest):
        _cleanup(dest)

    if not os.path.exists(src):
        raise ValueError(f"Исходный файл {src} не существует")
    if not _is_valid_video(src):
        raise ValueError(f"Исходный файл {src} не является валидным видео")

    effective_offset = 0.0
    if offset and source_duration and (source_duration - offset) >= duration:
        effective_offset = offset

    cmd_prefix = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    if effective_offset:
        cmd_prefix += ["-ss", str(effective_offset)]
    cmd_prefix += [
        "-stream_loop", "-1", "-i", src,
        "-t", str(duration),
        "-vf", f"scale={target_width}:{target_height}:force_original_aspect_ratio=increase,"
               f"crop={target_width}:{target_height},fps={TARGET_FPS}",
        "-an",
    ]
    try:
        _run_encode(cmd_prefix, [dest])
    except Exception:
        _cleanup(dest)
        raise


def build_crossfade_chain(clip_paths: List[str], durations: List[float], out_path: str):
    """
    Склеивает клипы через xfade с перекрытием CROSSFADE_SEC между соседними.
    Офсет каждого перехода считается по накопленной длительности уже
    склеенного участка минус накопленные перекрытия — стандартная формула
    для цепочки xfade в ffmpeg.
    """
    n = len(clip_paths)
    if n == 1:
        run(["cp", clip_paths[0], out_path])
        return

    for p in clip_paths:
        if not os.path.exists(p):
            raise ValueError(f"Входной клип не найден: {p}")

    inputs = []
    for p in clip_paths:
        inputs += ["-i", p]

    filter_parts = []
    cumulative = durations[0]
    last_label = "0:v"
    for i in range(1, n):
        offset = max(cumulative - CROSSFADE_SEC, 0)
        next_label = f"v{i}"
        filter_parts.append(
            f"[{last_label}][{i}:v]xfade=transition=fade:duration={CROSSFADE_SEC}:offset={offset}[{next_label}]"
        )
        last_label = next_label
        cumulative += durations[i] - CROSSFADE_SEC

    filter_complex = ";".join(filter_parts)
    cmd_prefix = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y"] + inputs + [
        "-filter_complex", filter_complex,
        "-map", f"[{last_label}]",
    ]
    try:
        _run_encode(cmd_prefix, [out_path])
    except Exception:
        _cleanup(out_path)
        raise


def _escape_ffmpeg_filter_path(path: str) -> str:
    """Экранирование пути для ffmpeg video-filter (ass=/subtitles=).

    Раньше экранировались только backslash и двоеточие (по образцу
    BuildWorker из OasisSubtitleStudio, где это работало — но там ни разу
    не попадался путь с запятой). Запятая в -vf — разделитель ФИЛЬТРОВ в
    цепочке, а не просто "спецсимвол": путь с запятой без экранирования
    обрывает парсинг фильтра ровно на ней ("No option name near..."), и
    именно так и произошло — имя папки проекта в этом кейсе содержит
    запятую буквально (alleged-missing-civil-war-gold-in-dents-run,-elk-c).

    Правильное решение — обернуть значение в одинарные кавычки (так и
    рекомендует сама документация ffmpeg для filtergraph-значений): внутри
    них запятая, двоеточие, слэши — всё literal, экранировать нужно только
    сам символ одинарной кавычки, если он вдруг встретится в пути."""
    normalized = path.replace("\\", "/")
    escaped_quotes = normalized.replace("'", "'\\''")
    return f"'{escaped_quotes}'"


def burn_subtitles(video_path: str, srt_path: str, out_path: str, style_slug: Optional[str] = None):
    """Прожигает субтитры в видео — НЕ через самодельный force_style, а
    через движок самого OasisSubtitleStudio (make_subs.srt_to_animated_ass):
    караоке-подсветка по словам, плашка под текст, авто-размер шрифта под
    геометрию video_path (в т.ч. само определяет верт./гориз. ориентацию —
    отдельно передавать aspect_ratio сюда не нужно, video_path уже в нужном
    разрешении к этому шагу). Требует реэнкод видео (ass= не работает с
    -c:v copy).

    style_slug — имя пресета из style_manager (см.
    oasis_studio_bridge.list_style_presets()). None — чистый авто-расчёт
    по геометрии видео, без пользовательского пресета."""
    if not os.path.exists(video_path):
        raise ValueError(f"Видеофайл не найден: {video_path}")
    if not os.path.exists(srt_path):
        raise ValueError(f"Файл субтитров не найден: {srt_path}")
    if not oasis_studio_bridge.is_available():
        raise RuntimeError(oasis_studio_bridge.error_message())

    ass_path = os.path.splitext(out_path)[0] + ".ass"
    oasis_studio_bridge.make_subs.srt_to_animated_ass(
        srt_path, ass_path, video_path=video_path, style_slug=style_slug,
    )

    ass_arg = _escape_ffmpeg_filter_path(os.path.abspath(ass_path))
    vf = f"ass={ass_arg}"
    if oasis_studio_bridge.BUNDLED_FONTS_DIR:
        # Собственные шрифты приложения не обязаны быть установлены в
        # систему — libass умеет резолвить их из указанной папки напрямую.
        fonts_dir = _escape_ffmpeg_filter_path(os.path.abspath(oasis_studio_bridge.BUNDLED_FONTS_DIR))
        vf += f":fontsdir={fonts_dir}"

    cmd_prefix = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-i", video_path,
        "-vf", vf,
    ]
    try:
        _run_encode(cmd_prefix, ["-c:a", "copy", out_path])
    except Exception:
        _cleanup(out_path)
        raise


def mux_audio(video_path: str, audio_path: str, out_path: str):
    if not os.path.exists(video_path):
        raise ValueError(f"Видеофайл не найден: {video_path}")
    if not os.path.exists(audio_path):
        raise ValueError(f"Аудиофайл не найден: {audio_path}")

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-i", video_path, "-i", audio_path,
        "-map", "0:v", "-map", "1:a",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        out_path,
    ]
    try:
        run(cmd)
    except Exception:
        _cleanup(out_path)
        raise


def main():
    parser = argparse.ArgumentParser(description="Сборка финального видео из подобранных клипов и озвучки")
    parser.add_argument("--footage", required=True, help="scenes_footage.json из pexels_matcher.py")
    parser.add_argument("--audio", required=True, help="Файл озвучки")
    parser.add_argument("--subtitles", required=False, default=None,
                         help="Файл субтитров (.srt) для прожига в видео. Опционально.")
    parser.add_argument("--style-slug", default=None,
                         help="Имя пресета стиля из style_manager OasisSubtitleStudio "
                              "(см. python3 -c \"import oasis_studio_bridge as b; print(b.list_style_presets())\"). "
                              "Не задан — чистый авто-расчёт по геометрии видео.")
    parser.add_argument("--aspect-ratio", choices=list(ASPECT_RATIOS), default=DEFAULT_ASPECT_RATIO,
                         help="Соотношение сторон готового видео (по умолчанию 16:9)")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    target_width, target_height = ASPECT_RATIOS[args.aspect_ratio]

    if args.subtitles and not os.path.exists(args.subtitles):
        raise ValueError(f"Файл субтитров не найден: {args.subtitles}")

    with open(args.footage, "r", encoding="utf-8") as f:
        data = json.load(f)
    scenes = data["scenes"]

    work_dir = tempfile.mkdtemp(prefix="video_assembly_")
    print(f"Рабочая папка: {work_dir}")

    clip_paths: List[str] = []
    durations: List[float] = []

    try:
        for i, scene in enumerate(scenes):
            footage = scene.get("footage")
            if not footage:
                print(f"Пропускаю {scene['id']} — нет подобранного видео")
                continue

            duration = round(scene["end"] - scene["start"], 2)
            if duration <= 0:
                print(f"Пропускаю {scene['id']} — некорректная длительность сцены")
                continue

            raw_path = os.path.join(work_dir, f"raw_{i}.mp4")
            norm_path = os.path.join(work_dir, f"norm_{i}.mp4")

            print(f"[{i+1}/{len(scenes)}] {scene['id']}: скачиваю и привожу к формату ({duration}с)...")
            download_clip(footage["video_link"], raw_path)
            # Кроссфейд склеивает клипы внахлёст на CROSSFADE_SEC — без компенсации
            # итоговое видео короче суммы длительностей на (N-1)*CROSSFADE_SEC.
            # Делаем каждый физический клип длиннее на этот запас заранее.
            padded_duration = round(duration + CROSSFADE_SEC, 2)
            normalize_clip(
                raw_path, norm_path, padded_duration,
                offset=footage.get("best_offset", 0.0),
                source_duration=footage.get("duration", 0.0),
                target_width=target_width, target_height=target_height,
            )

            clip_paths.append(norm_path)
            durations.append(padded_duration)

        if not clip_paths:
            raise RuntimeError("Нет ни одного клипа для сборки")

        concat_path = os.path.join(work_dir, "concat.mp4")
        print("Склеиваю клипы с кроссфейдом...")
        build_crossfade_chain(clip_paths, durations, concat_path)

        print("Накладываю озвучку...")
        if args.subtitles:
            muxed_path = os.path.join(work_dir, "muxed.mp4")
            mux_audio(concat_path, args.audio, muxed_path)
            print("Прожигаю субтитры...")
            burn_subtitles(muxed_path, args.subtitles, args.out, style_slug=args.style_slug)
        else:
            mux_audio(concat_path, args.audio, args.out)

        print(f"Готово: {args.out}")
    except Exception:
        # Удаляем временную папку только если она наша (не переданная пользователем)
        if work_dir and os.path.isdir(work_dir):
            _cleanup(*[os.path.join(work_dir, f) for f in os.listdir(work_dir)])
            try:
                os.rmdir(work_dir)
            except OSError:
                pass
        raise


if __name__ == "__main__":
    main()
