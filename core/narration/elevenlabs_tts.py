"""
elevenlabs_tts.py

Генерация озвучки напрямую через ElevenLabs API — платно, но при
разумном объёме (1 млн символов ≈ $11, это ~16 часов качественной
озвучки) экономит время на ручной прогон через веб-интерфейс.
Поддерживает клонированный голос — просто передайте voice_id вашего клона.

Длинные тексты (сценарий на 20 минут — до ~18000 символов) режутся на
части по границам предложений/абзацев (не разрывая фразы), каждая часть
озвучивается отдельным запросом (у API есть практический лимит на размер
одного запроса), затем части склеиваются в один файл через ffmpeg без
перекодирования.

Установка:
    pip install elevenlabs

Использование:
    export ELEVENLABS_API_KEY=...

    # найти voice_id своего клонированного голоса:
    python elevenlabs_tts.py --list-voices

    # озвучить:
    python elevenlabs_tts.py \
        --text-file test_run/narration_text.txt \
        --voice-id ВАШ_VOICE_ID \
        --out test_run/narration.mp3
"""

import argparse
import os
import re
import subprocess
import tempfile
from typing import List

from dotenv import load_dotenv
from elevenlabs.client import ElevenLabs

load_dotenv()

DEFAULT_MODEL = "eleven_multilingual_v2"  # лучшее качество для многоязычной озвучки
CHUNK_CHAR_LIMIT = 4000  # практический предел на один запрос к API


def get_client() -> ElevenLabs:
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        raise RuntimeError("Задайте ELEVENLABS_API_KEY")
    return ElevenLabs(api_key=api_key)


def list_voices(client: ElevenLabs) -> List[dict]:
    """Список доступных голосов (включая ваш клонированный) — чтобы найти voice_id."""
    response = client.voices.search()
    return [{"voice_id": v.voice_id, "name": v.name} for v in response.voices]


def split_into_chunks(text: str, limit: int = CHUNK_CHAR_LIMIT) -> List[str]:
    """Режет текст на части не длиннее limit, стараясь не разрывать абзацы/предложения."""
    paragraphs = text.split("\n\n")
    chunks = []
    current = ""

    for para in paragraphs:
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) <= limit:
            current = candidate
            continue

        if current:
            chunks.append(current)

        if len(para) <= limit:
            current = para
        else:
            # редкий случай — сам абзац длиннее лимита, режем по предложениям
            sentences = re.split(r"(?<=[.!?])\s+", para)
            current = ""
            for s in sentences:
                cand = f"{current} {s}".strip()
                if len(cand) <= limit:
                    current = cand
                else:
                    if current:
                        chunks.append(current)
                    current = s

    if current:
        chunks.append(current)
    return chunks


def generate_narration(text: str, voice_id: str, out_path: str,
                        model_id: str = DEFAULT_MODEL) -> str:
    client = get_client()
    chunks = split_into_chunks(text)
    print(f"Текст разбит на {len(chunks)} частей для озвучки")

    work_dir = tempfile.mkdtemp(prefix="elevenlabs_")
    chunk_paths = []

    for i, chunk in enumerate(chunks, 1):
        print(f"[{i}/{len(chunks)}] озвучиваю ({len(chunk)} симв.)...")
        audio = client.text_to_speech.convert(
            text=chunk,
            voice_id=voice_id,
            model_id=model_id,
            output_format="mp3_44100_128",
        )
        chunk_path = os.path.join(work_dir, f"chunk_{i:03d}.mp3")
        with open(chunk_path, "wb") as f:
            for piece in audio:
                f.write(piece)
        chunk_paths.append(chunk_path)

    if len(chunk_paths) == 1:
        os.replace(chunk_paths[0], out_path)
        print(f"Готово: {out_path}")
        return out_path

    # Склейка через ffmpeg concat demuxer — без перекодирования, быстро и без потерь.
    concat_list_path = os.path.join(work_dir, "concat_list.txt")
    with open(concat_list_path, "w", encoding="utf-8") as f:
        for p in chunk_paths:
            f.write(f"file '{p}'\n")

    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list_path,
         "-c", "copy", out_path],
        check=True, capture_output=True,
    )
    print(f"Готово: {out_path}")
    return out_path


def main():
    parser = argparse.ArgumentParser(description="Озвучка текста через ElevenLabs API")
    parser.add_argument("--text-file", help="Путь к тексту (например, narration_text.txt)")
    parser.add_argument("--voice-id", help="voice_id голоса (в т.ч. вашего клонированного)")
    parser.add_argument("--out", help="Куда сохранить итоговый .mp3")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--list-voices", action="store_true", help="Показать доступные голоса и выйти")
    args = parser.parse_args()

    if args.list_voices:
        client = get_client()
        for v in list_voices(client):
            print(f"{v['voice_id']}  {v['name']}")
        return

    if not (args.text_file and args.voice_id and args.out):
        parser.error("--text-file, --voice-id и --out обязательны (или используйте --list-voices)")

    with open(args.text_file, "r", encoding="utf-8") as f:
        text = f.read()

    generate_narration(text, args.voice_id, args.out, model_id=args.model)


if __name__ == "__main__":
    main()
