"""
book_to_script.py

Модуль "Мозг" пайплайна: берёт исходный текст (книгу, подборку рассекреченных
дел, архивный материал), делит его на самостоятельные истории под отдельные
видео и пишет сценарий закадрового текста для одной из них с учётом заданной
длительности готового ролика.

Не решает, откуда взят исходный текст — это отдельный вопрос источников
(FBI Vault, CIA FOIA Reading Room, общественное достояние и т.п.). Модуль
работает с уже добытым текстом.

Выходной формат script.json совместим с audio_script_aligner.py — сцены
можно сразу прогонять через сверку с озвучкой.

Использование:
    export DEEPSEEK_API_KEY=...
    python book_to_script.py \
        --book source.txt \
        --duration 20 \
        --language ru \
        --out-dir ./project_001
"""

import argparse
import json
import os
from typing import List, Dict

from openai import OpenAI  # DeepSeek API OpenAI-совместим
from dotenv import load_dotenv

load_dotenv()  # подхватывает .env из текущей папки — export больше не нужен

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL_DEFAULT = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
CHARS_PER_MINUTE_DEFAULT = 900  # эмпирический темп речи, откалибруйте под свой TTS-голос


def get_client() -> OpenAI:
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("Задайте переменную окружения DEEPSEEK_API_KEY")
    return OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL)


def _strip_json_fence(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw
        if raw.endswith("```"):
            raw = raw[: -3]
    return raw.strip()


# ---------- 1. Разбивка источника на самостоятельные истории ----------

SPLIT_SYSTEM_PROMPT = """Ты — редактор документальных YouTube/TikTok каналов о расследованиях,
заговорах и исторических тайнах. На вход получаешь текст книги или архивного
источника (например, рассекреченное дело). Твоя задача — найти в нём
самостоятельные истории, каждая из которых тянет на отдельное 15-25-минутное
документальное видео: имеет завязку, развитие и развязку, не требует знания
остального текста источника.

Верни СТРОГО JSON без markdown и пояснений, массив объектов:
[
  {"id": "story_001", "title": "...", "summary": "2-3 предложения о чём история",
   "source_excerpt": "ключевая цитата или маркер, где в тексте искать этот кусок"}
]
Если в тексте меньше содержательных историй, чем кажется на первый взгляд —
не выдумывай, верни столько, сколько реально есть."""


def split_book_into_stories(book_text: str, client: OpenAI, model: str = DEEPSEEK_MODEL_DEFAULT) -> List[Dict]:
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SPLIT_SYSTEM_PROMPT},
            {"role": "user", "content": book_text},
        ],
        temperature=0.3,
        max_tokens=4000,  # с запасом под список историй с summary — не сама книга, а метаданные по ней
        # deepseek-v4-flash с 2026-07-24 по умолчанию работает в thinking mode (reasoning_effort=high):
        # рассуждения уходят в отдельное поле reasoning_content и съедают max_tokens ДО того, как
        # модель напишет финальный JSON в content — из-за этого content может прийти пустым. Нам
        # для структурированного JSON-вывода думающий режим не нужен, отключаем явно.
        extra_body={"thinking": {"type": "disabled"}},
    )
    raw = _strip_json_fence(response.choices[0].message.content)
    if not raw:
        finish_reason = response.choices[0].finish_reason
        raise RuntimeError(
            f"DeepSeek вернул пустой ответ (finish_reason={finish_reason!r}). "
            f"Если finish_reason == 'length' — не хватило max_tokens, увеличьте лимит."
        )
    return json.loads(raw)


# ---------- 2. Сценарий одной истории с учётом длительности ----------

CONTENT_MODE_BLOCKS = {
    "documentary": """РЕЖИМ: документальное расследование реальных событий (секретные материалы,
нераскрытые дела). Точность важна, но не первостепенна — интрига и тайна вовлекают
зрителя активнее, чем протокольная сухость. Если по теме существуют реальные,
обсуждаемые версии/теории — можно их упомянуть, но ВСЕГДА с явной пометкой
("по одной из версий...", "некоторые исследователи считают...", "существует теория,
что..."), никогда не выдавая догадку за установленный факт. НЕ придумывай новые
теории от себя — используй только те версии, которые реально существуют и
обсуждаются в связи с этим делом.""",

    "historical": """РЕЖИМ: историческое повествование о реальных событиях и людях.
Фактическая точность обязательна — не выдумывай события, даты, имена, не приписывай
вымышленных версий. Но подача обязана быть увлекательной: выбирай самый
драматичный, человеческий ракурс на реальные факты, не превращай в сухую хронологию.""",

    "storytelling": """РЕЖИМ: художественный пересказ литературного произведения в
формате устного сторителлинга. Сохраняй сюжет, характеров и ключевые события
первоисточника, но подавай эмоционально и живо, как рассказ у костра, а не конспект.
Здесь нет "недостоверных" деталей за пределами сюжета — это признанная
художественная адаптация, дополнять и художественно окрашивать можно свободно.""",
}

SCRIPT_SYSTEM_PROMPT_TEMPLATE = """Ты пишешь закадровый текст видеоролика на языке и
длительность, которые придут в пользовательском сообщении вместе с материалом.

{mode_block}

СТРУКТУРА ДЛЯ УДЕРЖАНИЯ ЗРИТЕЛЯ (это не опционально, это требование):

1. ХУК: первая сцена НЕ должна начинаться со спокойной завязки (дата, место,
   обычный рейс/день). Начни с вопроса или дерзкого утверждения, которое сразу
   создаёт интригу — то, ради чего зритель досмотрит до конца. Например, не
   "В 1971 году самолёт вылетел из Портленда...", а "Как человек смог исчезнуть
   с 200 000 долларов, спрыгнув с самолёта — и почему его не могут найти уже
   50 лет?" Проверь: если первая сцена звучит как энциклопедическая статья —
   перепиши.

2. ОТКРЫТАЯ ПЕТЛЯ: где-то в середине истории добавь фразу, которая намекает
   на важную деталь, но не раскрывает её — и явно вернись к ней в развязке.
   Например: "...но одна улика, найденная спустя годы, поставила всё под
   сомнение" — а в одной из последних сцен эту улику назвать и объяснить.
   Это даёт зрителю причину не уходить в середине.

Раздели текст на сцены — смысловые куски по 2-4 предложения, которые дальше
будут озвучены целиком. Не пиши ремарки в скобках внутри самого текста
озвучки — визуальный ряд идёт ОТДЕЛЬНЫМ полем, не смешивай их.

Для каждой сцены дополнительно составь список из 3-6 визуальных описаний —
конкретных, буквальных кадров, которые реально можно найти на стоковом видео
(интерьеры, действия, предметы, места), в том порядке, в котором они должны
идти на экране во время озвучки этой сцены. Каждое описание — короткая
фраза (не предложение), например "Ночной аэропорт", "Мужчина в очках и
плаще в салоне самолёта", "Стюардесса наклоняется к пассажиру".

ЛЕКСИКА ЭПОХИ, А НЕ ЯВНЫЙ ГОД: если история происходит в определённый
исторический период — выражай эпоху словами, которые ей присущи, а не
явным указанием года/десятилетия ("1970-е", "в 1971 году" и т.п.) внутри
самого визуального описания. Например, для эпохи дирижаблей — "дирижабль",
"причальная мачта", а не "летательный аппарат 1930-х"; для эпохи телеграфа
— "телеграфный аппарат", "оператор с наушниками", а не "устройство связи
1900-х". Так поиск по стоку и архивам естественно попадает в нужную
эпоху через сам предмет/явление, а не через отдельное слово-год, которое
на практике часто не совпадает с тем, как эпоха размечена в метаданных
источников, и режет выдачу в ноль на буквальном совпадении.

КРИТИЧЕСКИ ВАЖНО — ты знаешь всю историю целиком, используй это: визуальные
описания должны соответствовать ТОЧНО тому, что происходит в сюжете именно
в этот момент, а не тому, что ассоциативно напрашивается. Если сцена просто
упоминает предмет (например, "потребовал четыре парашюта") — это НЕ повод
показывать связанное с ним драматическое действие (прыжок с парашютом),
если оно происходит в сюжете позже. Показывай то действие или предмет,
который история описывает именно в этом месте таймлайна, не забегая вперёд
и не отставая от повествования.

Верни СТРОГО JSON без markdown, массив объектов:
[{{"id": "scene_001", "text": "...", "visuals": ["...", "...", "..."]}}]"""

# Каждый режим — отдельная ПОЛНОСТЬЮ статичная строка, посчитанная один раз
# при импорте — так кэширование DeepSeek работает в пределах одного режима
# (в рамках одной рубрики канала), просто не кросс-режимно.
SCRIPT_SYSTEM_PROMPTS = {
    mode: SCRIPT_SYSTEM_PROMPT_TEMPLATE.format(mode_block=block)
    for mode, block in CONTENT_MODE_BLOCKS.items()
}


def write_script_for_story(
    story: Dict,
    source_text: str,
    duration_minutes: int,
    language: str,
    client: OpenAI,
    chars_per_minute: int = CHARS_PER_MINUTE_DEFAULT,
    model: str = DEEPSEEK_MODEL_DEFAULT,
    content_mode: str = "documentary",
) -> List[Dict]:
    if content_mode not in SCRIPT_SYSTEM_PROMPTS:
        raise ValueError(f"Неизвестный content_mode: {content_mode!r}. "
                          f"Доступные: {list(SCRIPT_SYSTEM_PROMPTS)}")
    system_prompt = SCRIPT_SYSTEM_PROMPTS[content_mode]

    target_chars = duration_minutes * chars_per_minute
    # Системный промпт НЕ трогаем параметрами — он должен быть побайтово
    # одинаковым при каждом вызове, иначе DeepSeek не сможет закэшировать
    # префикс и скидка на повторные токены не сработает. Язык/длительность/
    # объём — это динамика, ей место только в user-сообщении, в конце.
    user_content = (
        f"Язык закадрового текста: {language}\n"
        f"Целевая длительность ролика: {duration_minutes} минут "
        f"(~{target_chars} символов текста озвучки, допуск ±10%)\n\n"
        f"История: {story['title']}\n"
        f"Краткое содержание: {story['summary']}\n\n"
        f"Исходный материал:\n{source_text}"
    )
    # Лимит вывода считаем от реальной длительности видео, а не берём
    # фиксированную цифру — 500 токенов достаточно для карточки, но обрежет
    # полноценный 20-минутный сценарий на середине первой же сцены.
    max_output_tokens = max(3000, int(target_chars * 0.9) + 2000)

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        temperature=0.5,
        max_tokens=max_output_tokens,
        # см. комментарий в split_book_into_stories — без этого thinking mode
        # съедает max_output_tokens на рассуждения и content приходит пустым
        extra_body={"thinking": {"type": "disabled"}},
    )
    raw = _strip_json_fence(response.choices[0].message.content)
    if not raw:
        finish_reason = response.choices[0].finish_reason
        raise RuntimeError(
            f"DeepSeek вернул пустой ответ (finish_reason={finish_reason!r}). "
            f"Если finish_reason == 'length' — не хватило max_tokens, увеличьте лимит."
        )
    return json.loads(raw)


# ---------- 3. CLI ----------

def main():
    parser = argparse.ArgumentParser(description="Источник → истории (бэклог) → сценарий первой истории")
    parser.add_argument("--book", required=True, help="Путь к тексту источника (.txt)")
    parser.add_argument("--duration", type=int, default=20, help="Целевая длительность видео, минут")
    parser.add_argument("--language", default="ru", help="Язык закадрового текста: ru/en/de")
    parser.add_argument("--chars-per-minute", type=int, default=CHARS_PER_MINUTE_DEFAULT)
    parser.add_argument("--content-mode", choices=list(SCRIPT_SYSTEM_PROMPTS), default="documentary",
                         help="documentary — расследования с интригой; historical — точность обязательна; "
                              "storytelling — художественный пересказ книги")
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    with open(args.book, "r", encoding="utf-8") as f:
        book_text = f.read()

    client = get_client()

    print("Разбиваю источник на самостоятельные истории...")
    stories = split_book_into_stories(book_text, client)
    print(f"Найдено историй: {len(stories)}")

    if not stories:
        raise RuntimeError("Не удалось выделить ни одной истории — проверьте исходный текст")

    first, backlog = stories[0], stories[1:]

    backlog_path = os.path.join(args.out_dir, "stories_backlog.json")
    with open(backlog_path, "w", encoding="utf-8") as f:
        json.dump(backlog, f, ensure_ascii=False, indent=2)
    print(f"В очередь отправлено историй: {len(backlog)} -> {backlog_path}")

    print(f"Пишу сценарий для: {first['title']}")
    script = write_script_for_story(
        story=first,
        source_text=book_text,  # для MVP отдаём весь текст; дальше можно резать по source_excerpt
        duration_minutes=args.duration,
        language=args.language,
        client=client,
        chars_per_minute=args.chars_per_minute,
        content_mode=args.content_mode,
    )

    total_chars = sum(len(s["text"]) for s in script)
    script_path = os.path.join(args.out_dir, "script.json")
    with open(script_path, "w", encoding="utf-8") as f:
        json.dump(script, f, ensure_ascii=False, indent=2)

    # Чистый текстовый файл без JSON-обвязки — удобно открыть и скопировать для озвучки
    narration_path = os.path.join(args.out_dir, "narration_text.txt")
    with open(narration_path, "w", encoding="utf-8") as f:
        f.write("\n\n".join(s["text"] for s in script))

    target = args.duration * args.chars_per_minute
    print(f"Готово: {len(script)} сцен, {total_chars} символов (цель: {target}, допуск ±10%)")
    print(f"Файлы в {args.out_dir}: stories_backlog.json, script.json, narration_text.txt")


if __name__ == "__main__":
    main()
