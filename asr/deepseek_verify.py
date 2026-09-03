"""
deepseek_verify.py
Пост-проверка вычитанных субтитров через DeepSeek API: сравнивает
КАЖДЫЙ блок с эталонным текстом книги и для каждого блока возвращает
вердикт (ok / suspect) плюс готовый вариант исправления, если что-то не так.
"""

import os
import re
import json

import requests

from asr.fix_srt import parse_srt_blocks

DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-chat"

SYSTEM_PROMPT = (
    "Ты — внимательный вычитчик субтитров аудиокниги. Тебе дан эталонный текст "
    "книги и пронумерованные блоки субтитров, полученные распознаванием речи и "
    "автоматическим сопоставлением с книгой. Для КАЖДОГО блока определи, "
    "соответствует ли его текст правильному отрывку книги по смыслу и словам.\n\n"
    "Если блок полностью корректен — верни status \"ok\" (issue и suggestion можно "
    "не указывать).\n"
    "Если есть искажение (неверное слово, пропуск, лишнее слово, неверное имя "
    "собственное и т.п.) — верни status \"suspect\", короткое пояснение в issue "
    "и исправленный текст блока целиком в suggestion (только сам текст фразы, "
    "без номера и таймкода).\n\n"
    "Отвечай СТРОГО валидным JSON-массивом, без пояснений и markdown-обёртки. "
    "Формат каждого элемента:\n"
    '{"block": <номер>, "status": "ok"|"suspect", "issue": "<коротко>", '
    '"suggestion": "<исправленный текст>"}\n'
    "В массиве должна быть ровно одна запись на каждый номер блока из запроса."
)


def load_api_key(project_dir=None):
    """Читает DEEPSEEK_API_KEY из .env в папке проекта (или из переменной окружения)."""
    project_dir = project_dir or os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(project_dir, ".env")
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                if key.strip() == "DEEPSEEK_API_KEY":
                    return value.strip().strip('"').strip("'")
    return os.environ.get("DEEPSEEK_API_KEY")


def _build_user_prompt(book_text, blocks_chunk):
    lines = [f"ЭТАЛОННЫЙ ТЕКСТ КНИГИ:\n{book_text}\n", "БЛОКИ СУБТИТРОВ ДЛЯ ПРОВЕРКИ:"]
    for num, text in blocks_chunk:
        lines.append(f"{num}: {text}")
    return "\n".join(lines)


def _extract_json_array(raw_text):
    """Достаёт JSON-массив из ответа модели, даже если он обёрнут в ```json ... ```."""
    text = raw_text.strip()
    text = re.sub(r'^```(?:json)?', '', text).strip()
    text = re.sub(r'```$', '', text).strip()
    start = text.find('[')
    end = text.rfind(']')
    if start == -1 or end == -1:
        raise ValueError("В ответе модели не найден JSON-массив")
    return json.loads(text[start:end + 1])


def verify_srt(srt_path, book_path, chunk_size=150, model=DEFAULT_MODEL,
                progress_cb=None, api_key=None):
    """
    Проверяет .srt через DeepSeek, возвращает список результатов по каждому блоку:
    [{"block": int, "status": "ok"/"suspect"/"unknown", "issue": str,
      "suggestion": str, "original": str}, ...]
    """
    api_key = api_key or load_api_key()
    if not api_key:
        raise RuntimeError(
            "Не найден DEEPSEEK_API_KEY. Добавьте его в файл .env в папке проекта: "
            "DEEPSEEK_API_KEY=ваш_ключ"
        )

    if not os.path.exists(srt_path) or not os.path.exists(book_path):
        raise FileNotFoundError("Не найден .srt или файл книги.")

    with open(book_path, "r", encoding="utf-8") as f:
        book_text = f.read()

    with open(srt_path, "r", encoding="utf-8") as f:
        srt_content = f.read()

    blocks = parse_srt_blocks(srt_content)
    numbered_texts = [(i + 1, " ".join(words)) for i, (_, _, words) in enumerate(blocks)]

    if not numbered_texts:
        raise ValueError("В .srt файле не найдено ни одного блока.")

    chunks = [numbered_texts[i:i + chunk_size] for i in range(0, len(numbered_texts), chunk_size)]
    total_chunks = len(chunks)

    results_by_block = {}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    for chunk_idx, chunk in enumerate(chunks, start=1):
        user_prompt = _build_user_prompt(book_text, chunk)
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0,
            "stream": False,
        }

        response = requests.post(DEEPSEEK_API_URL, headers=headers, json=payload, timeout=180)
        response.raise_for_status()
        data = response.json()
        raw_text = data["choices"][0]["message"]["content"]

        try:
            parsed = _extract_json_array(raw_text)
        except (ValueError, json.JSONDecodeError) as e:
            raise RuntimeError(
                f"Не удалось разобрать ответ DeepSeek для блоков "
                f"{chunk[0][0]}-{chunk[-1][0]}: {e}"
            )

        for item in parsed:
            block_num = item.get("block")
            if block_num is None:
                continue
            results_by_block[block_num] = {
                "block": block_num,
                "status": item.get("status", "ok"),
                "issue": item.get("issue", ""),
                "suggestion": item.get("suggestion", ""),
            }

        if progress_cb:
            progress_cb(chunk_idx, total_chunks)

    results = []
    for num, original_text in numbered_texts:
        r = results_by_block.get(num) or {
            "block": num, "status": "unknown",
            "issue": "Нет ответа от модели по этому блоку", "suggestion": "",
        }
        r["original"] = original_text
        results.append(r)

    return results


def apply_corrections(srt_path, output_path, results, accepted_blocks):
    """
    Применяет принятые исправления к .srt файлу и сохраняет как output_path.
    accepted_blocks: множество номеров блоков, для которых нужно применить suggestion.
    """
    with open(srt_path, "r", encoding="utf-8") as f:
        srt_content = f.read()

    blocks = parse_srt_blocks(srt_content)
    corrections = {
        r["block"]: r["suggestion"]
        for r in results
        if r["block"] in accepted_blocks and r.get("suggestion")
    }

    fixed_blocks = []
    for i, (index_line, time_line, words) in enumerate(blocks, start=1):
        text = corrections.get(i, " ".join(words))
        fixed_blocks.append(f"{index_line}\n{time_line}\n{text}")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n\n".join(fixed_blocks) + "\n")

    return output_path
