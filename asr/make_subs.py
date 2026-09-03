import sys
import os
import re
import subprocess

_NOWIN = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
import json

from asr import style_manager


def get_video_resolution(video_path):
    """Автоматически определяет разрешение видео через ffprobe"""
    if not video_path or not os.path.exists(video_path):
        return 720, 720

    cmd = [
        'ffprobe', '-v', 'quiet', '-print_format', 'json',
        '-show_streams', '-select_streams', 'v:0', video_path
    ]
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=_NOWIN)
        data = json.loads(result.stdout)
        width = int(data['streams'][0]['width'])
        height = int(data['streams'][0]['height'])
        return width, height
    except Exception as e:
        print(f"Предупреждение: Не удалось определить размер видео ({e}). Используем 720x720.")
        return 720, 720


def parse_srt_manually(srt_path):
    with open(srt_path, "r", encoding="utf-8") as f:
        content = f.read().strip()

    blocks = content.split('\n\n')
    subtitles = []

    for block in blocks:
        lines = block.split('\n')
        if len(lines) < 3:
            continue

        time_line = lines[1]
        if "-->" not in time_line:
            continue

        start_str, end_str = time_line.split("-->")
        start_str = start_str.strip().replace(',', '.')
        end_str = end_str.strip().replace(',', '.')

        text = " ".join(lines[2:])
        text = re.sub(re.compile(r'<[^>]+>'), '', text)

        subtitles.append({
            'start': start_str,
            'end': end_str,
            'text': text.strip()
        })
    return subtitles


def convert_time_to_ass(time_str):
    parts = time_str.split(':')
    if len(parts) == 2:
        h = "0"
        m = parts[0]
        s_ms = parts[1]
    else:
        h = str(int(parts[0]))
        m = parts[1]
        s_ms = parts[2]

    s, ms = s_ms.split('.')
    cs = ms[:2]
    return f"{h}:{int(m):02d}:{int(s):02d}.{cs}"


def time_str_to_seconds(t):
    p = t.split(':')
    return float(p[-1]) + int(p[-2]) * 60 + (int(p[-3]) * 3600 if len(p) > 2 else 0)


def compute_default_style(width, height):
    """
    Базовый (авто) стиль по геометрии экрана — та же логика, что была
    зашита в оригинальном скрипте. Используется как основа, поверх
    которой накатывается пользовательский пресет (см. merge_style).
    """
    margin_l = 30
    margin_r = 30
    alignment = 2

    if height > width:
        # ВЕРТИКАЛЬНОЕ (9:16)
        font_size = int(height * 0.032)
        margin_v = int(height * 0.22)
        outline_size = max(2, int(font_size * 0.12))
    elif width > height:
        # ГОРИЗОНТАЛЬНОЕ (16:9)
        font_size = int(height * 0.052)
        margin_v = int(height * 0.08)
        outline_size = max(2, int(font_size * 0.12))
        alignment = 1
        margin_l = 40
    else:
        # КВАДРАТНОЕ (1:1)
        font_size = 34
        margin_v = 60
        outline_size = 4

    return {
        "font_name": "Arial",
        "font_size": font_size,
        "primary_color": "&H00FFFFFF",
        "secondary_color": "&H0000FFFF",
        "outline_color": "&HB0000000",
        "back_color": "&HB0000000",
        "bold": True,
        "alignment": alignment,
        "margin_l": margin_l,
        "margin_r": margin_r,
        "margin_v": margin_v,
        "outline_size": outline_size,
        "karaoke": True,
        # --- Фиксированная плашка под текст ---
        "box_enabled": True,
        "box_width_factor": 0.86,   # ширина плашки = 86% ширины видео
        "box_lines": 2,             # плашка рассчитана ровно на N строк текста
        "box_padding": 16,          # внутренний отступ текста от края плашки, px
        "box_corner_radius": 18,    # радиус скругления углов плашки, px
    }


def merge_style(default_style, preset, width, height):
    """
    Накатывает пользовательский JSON-пресет поверх авто-расчитанного
    базового стиля. Поля *_fixed имеют приоритет над *_factor,
    которые в свою очередь имеют приоритет над авто-значением.
    """
    style = dict(default_style)
    if not preset:
        return style

    if preset.get("font_name"):
        style["font_name"] = preset["font_name"]

    if preset.get("font_size_fixed"):
        style["font_size"] = int(preset["font_size_fixed"])
    elif preset.get("font_size_factor"):
        style["font_size"] = int(height * float(preset["font_size_factor"]))

    if preset.get("margin_v_fixed") is not None and preset.get("margin_v_fixed") != "":
        style["margin_v"] = int(preset["margin_v_fixed"])
    elif preset.get("margin_v_factor"):
        style["margin_v"] = int(height * float(preset["margin_v_factor"]))

    if preset.get("outline_fixed") is not None and preset.get("outline_fixed") != "":
        style["outline_size"] = int(preset["outline_fixed"])
    elif preset.get("outline_factor"):
        style["outline_size"] = max(2, int(style["font_size"] * float(preset["outline_factor"])))

    for key in ("primary_color", "secondary_color", "outline_color", "back_color"):
        if preset.get(key):
            style[key] = preset[key]

    if "bold" in preset and preset["bold"] is not None:
        style["bold"] = bool(preset["bold"])

    if preset.get("alignment"):
        style["alignment"] = int(preset["alignment"])

    if preset.get("margin_l") is not None:
        style["margin_l"] = int(preset["margin_l"])
    if preset.get("margin_r") is not None:
        style["margin_r"] = int(preset["margin_r"])

    if "karaoke" in preset and preset["karaoke"] is not None:
        style["karaoke"] = bool(preset["karaoke"])

    if "box_enabled" in preset and preset["box_enabled"] is not None:
        style["box_enabled"] = bool(preset["box_enabled"])
    if preset.get("box_width_factor"):
        style["box_width_factor"] = float(preset["box_width_factor"])
    if preset.get("box_lines"):
        style["box_lines"] = int(preset["box_lines"])
    if preset.get("box_padding") is not None:
        style["box_padding"] = int(preset["box_padding"])
    if preset.get("box_corner_radius") is not None:
        style["box_corner_radius"] = int(preset["box_corner_radius"])

    return style


def build_ass_header(width, height, style):
    bold_flag = -1 if style["bold"] else 0
    # WrapStyle 2 = запрещаем автоматический перенос ASS — переносы строк
    # расставляем сами через \N, чтобы текст всегда укладывался в фиксированную плашку.
    wrap_style = 2 if style.get("box_enabled") else 0
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: {wrap_style}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{style['font_name']},{style['font_size']},{style['primary_color']},{style['secondary_color']},{style['outline_color']},{style['back_color']},{bold_flag},0,0,0,100,100,0,0,1,{style['outline_size']},0,{style['alignment']},{style['margin_l']},{style['margin_r']},{style['margin_v']},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _parse_ass_color_parts(ass_color):
    """&HAABBGGRR -> (aa, bb, gg, rr) как двухсимвольные hex-строки."""
    hexpart = ass_color.replace("&H", "").replace("&h", "").rjust(8, "0")
    return hexpart[0:2], hexpart[2:4], hexpart[4:6], hexpart[6:8]


def build_box_geometry(width, height, style):
    """
    Считает фиксированные габариты и позицию плашки под текст.
    Плашка не зависит от длины конкретной фразы — она одна и та же
    для всех субтитров этого видео.
    """
    font_size = style["font_size"]
    box_width = int(width * style["box_width_factor"])
    line_height = int(font_size * 1.35)
    padding = style["box_padding"]
    box_height = line_height * style["box_lines"] + padding * 2

    if style["alignment"] == 1:
        # Снизу слева — прижимаем к левому краю с отступом margin_l
        box_x = style["margin_l"]
    elif style["alignment"] == 3:
        # Снизу справа — прижимаем к правому краю с отступом margin_r
        box_x = width - style["margin_r"] - box_width
    else:
        # Снизу по центру / по центру экрана — центрируем
        box_x = (width - box_width) // 2

    if style["alignment"] == 5:
        # По центру экрана
        box_y = (height - box_height) // 2
    else:
        # Прижато к низу с отступом margin_v
        box_y = height - style["margin_v"] - box_height

    # Оценка максимального числа символов в строке (эвристика для
    # пропорциональных шрифтов: кириллица в среднем ~0.56 ширины font_size на символ)
    usable_width = box_width - padding * 2
    max_chars_per_line = max(6, int(usable_width / (font_size * 0.56)))

    return {
        "box_x": box_x,
        "box_y": box_y,
        "box_width": box_width,
        "box_height": box_height,
        "text_center_x": box_x + box_width // 2,
        "text_center_y": box_y + box_height // 2,
        "max_chars_per_line": max_chars_per_line,
    }


def rounded_rect_path(w, h, radius):
    """Строит ASS drawing-путь скруглённого прямоугольника через кубические кривые."""
    r = max(0, min(radius, w / 2, h / 2))
    if r <= 0:
        return f"m 0 0 l {w} 0 l {w} {h} l 0 {h}"

    def n(v):
        return f"{v:.1f}".rstrip('0').rstrip('.') if isinstance(v, float) else str(v)

    return (
        f"m {n(r)} 0 "
        f"l {n(w - r)} 0 "
        f"b {n(w)} 0 {n(w)} 0 {n(w)} {n(r)} "
        f"l {n(w)} {n(h - r)} "
        f"b {n(w)} {n(h)} {n(w)} {n(h)} {n(w - r)} {n(h)} "
        f"l {n(r)} {n(h)} "
        f"b 0 {n(h)} 0 {n(h)} 0 {n(h - r)} "
        f"l 0 {n(r)} "
        f"b 0 0 0 0 {n(r)} 0"
    )


def build_box_drawing_tag(geo, style):
    """Возвращает override-теги + векторную фигуру для отрисовки фиксированной плашки."""
    aa, bb, gg, rr = _parse_ass_color_parts(style["back_color"])
    x, y, w, h = geo["box_x"], geo["box_y"], geo["box_width"], geo["box_height"]
    radius = style.get("box_corner_radius", 0)
    path = rounded_rect_path(w, h, radius)
    return (
        f"{{\\an7\\pos({x},{y})\\1c&H{bb}{gg}{rr}&\\1a&H{aa}&\\bord0\\shad0\\p1}}"
        f"{path}{{\\p0}}"
    )


def wrap_words_to_lines(words, max_chars_per_line):
    """Жадно раскладывает список слов по строкам не длиннее max_chars_per_line."""
    lines = []
    current = []
    current_len = 0
    for w in words:
        add_len = len(w) + (1 if current else 0)
        if current and current_len + add_len > max_chars_per_line:
            lines.append(current)
            current = [w]
            current_len = len(w)
        else:
            current.append(w)
            current_len += add_len
    if current:
        lines.append(current)
    return lines


def build_pages(words, max_chars_per_line, max_lines):
    """
    Разбивает слова фразы на "страницы" по max_lines строк каждая
    (используется, если фраза слишком длинная для фиксированной плашки —
    тогда она показывается в несколько последовательных карточек,
    а не расползается или обрезается).
    """
    lines = wrap_words_to_lines(words, max_chars_per_line)
    pages = [lines[i:i + max_lines] for i in range(0, len(lines), max_lines)]
    return pages


def srt_to_animated_ass(srt_path, ass_path, video_path=None, style_slug=None, progress_cb=None):
    """
    Основная функция генерации .ass из .srt.

    style_slug: имя файла пресета (без .json) из папки styles/.
                Если None — используется чистый авто-расчёт (как раньше).
    progress_cb: необязательный callback(current, total) для GUI-прогресса.
    """
    if not os.path.exists(srt_path):
        print(f"Ошибка: Файл {srt_path} не найден!")
        return False

    subtitles = parse_srt_manually(srt_path)
    if not subtitles:
        print("Ошибка: Не удалось извлечь фразы.")
        return False

    width, height = get_video_resolution(video_path)
    print(f"Разрешение видео: {width}x{height}")

    default_style = compute_default_style(width, height)

    preset = None
    if style_slug:
        try:
            preset = style_manager.load_style(style_slug)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            print(f"Предупреждение: не удалось загрузить стиль '{style_slug}' ({e}). Используем авто-стиль.")

    style = merge_style(default_style, preset, width, height)

    ass_header = build_ass_header(width, height, style)

    geo = None
    box_tag = ""
    if style["box_enabled"]:
        geo = build_box_geometry(width, height, style)
        box_tag = build_box_drawing_tag(geo, style)

    total = len(subtitles)
    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(ass_header)
        for idx, sub in enumerate(subtitles, start=1):
            words = sub['text'].split()
            if not words:
                if progress_cb:
                    progress_cb(idx, total)
                continue

            block_start_s = time_str_to_seconds(sub['start'])
            block_end_s = time_str_to_seconds(sub['end'])
            block_duration_s = max(0.05, block_end_s - block_start_s)
            total_words_in_block = len(words)

            if not style["box_enabled"]:
                # Старое поведение: одна карточка на блок, авто-перенос ASS
                _write_dialogue_event(
                    f, words, block_start_s, block_end_s, style,
                    geo=None, box_tag=""
                )
                if progress_cb:
                    progress_cb(idx, total)
                continue

            pages = build_pages(words, geo["max_chars_per_line"], style["box_lines"])

            cursor_words = 0
            for page in pages:
                page_words = [w for line in page for w in line]
                page_word_count = len(page_words)

                frac_start = cursor_words / total_words_in_block
                frac_end = (cursor_words + page_word_count) / total_words_in_block
                cursor_words += page_word_count

                page_start_s = block_start_s + frac_start * block_duration_s
                page_end_s = block_start_s + frac_end * block_duration_s

                _write_dialogue_event(
                    f, page_words, page_start_s, page_end_s, style,
                    geo=geo, box_tag=box_tag, lines=page
                )

            if progress_cb:
                progress_cb(idx, total)

    print(f"Успешно сгенерирован: {ass_path}")
    return True


def _write_dialogue_event(f, words, start_s, end_s, style, geo=None, box_tag="", lines=None):
    """Пишет в файл одну (или две — плашка+текст) Dialogue-строки ASS."""
    start = convert_time_to_ass(_seconds_to_time_str(start_s))
    end = convert_time_to_ass(_seconds_to_time_str(end_s))

    total_duration_cs = max(1, int((end_s - start_s) * 100))
    word_count = len(words)
    duration_per_word = max(1, int(total_duration_cs / max(1, word_count)))

    def karaoke_word(w):
        return f"{{\\k{duration_per_word}}}{w}"

    if style["karaoke"]:
        if lines:
            # Печатаем построчно с явными \N между строками (фиксированная плашка)
            rendered_lines = []
            for line in lines:
                rendered_lines.append(" ".join(karaoke_word(w) for w in line))
            animated_text = "\\N".join(rendered_lines)
        else:
            animated_text = " ".join(karaoke_word(w) for w in words)
    else:
        if lines:
            animated_text = "\\N".join(" ".join(line) for line in lines)
        else:
            animated_text = " ".join(words)

    if geo is not None:
        # Плашка (Layer 0) — фиксированного размера, отдельной строкой
        f.write(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{box_tag}\n")
        # Текст (Layer 1) поверх плашки, центрирован внутри неё
        text_tag = f"{{\\an5\\pos({geo['text_center_x']},{geo['text_center_y']})}}"
        f.write(f"Dialogue: 1,{start},{end},Default,,0,0,0,,{text_tag}{animated_text}\n")
    else:
        f.write(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{animated_text}\n")


def _seconds_to_time_str(seconds):
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:06.3f}"


if __name__ == "__main__":
    # Использование: python make_subs.py music.srt [music.mp4] [style_slug]
    srt_file = sys.argv[1] if len(sys.argv) > 1 else "music.srt"
    video_file = sys.argv[2] if len(sys.argv) > 2 else "music.mp4"
    style_arg = sys.argv[3] if len(sys.argv) > 3 else None

    ass_file = srt_file.replace(".srt", ".ass")
    srt_to_animated_ass(srt_file, ass_file, video_file, style_slug=style_arg)
