import sys
import os
import re
from difflib import SequenceMatcher


def clean_word(word):
    # Очистка слова от знаков препинания для точного сравнения
    return re.sub(r'[^\w\s-]', '', word).lower().strip()


def parse_srt_blocks(srt_content):
    """Возвращает список блоков: (index_line, time_line, [слова])."""
    blocks = []
    for block in srt_content.strip().split('\n\n'):
        lines = block.split('\n')
        if len(lines) < 3:
            continue
        index_line = lines[0]
        time_line = lines[1]
        text = " ".join(lines[2:])
        blocks.append((index_line, time_line, text.split()))
    return blocks


def align_and_fix(srt_path, book_path, output_path, progress_cb=None):
    """
    Сопоставляет распознанный srt с текстом книги-первоисточника и
    подтягивает из книги правильные слова, знаки препинания и заглавные буквы.

    В отличие от простого пословного сопоставления, здесь используется
    ГЛОБАЛЬНОЕ выравнивание последовательностей (как в diff-алгоритмах).
    Это принципиально важно для случаев, когда распознавание речи режет
    одно слово книги на несколько кусков (например, придуманное имя
    "Тристран" услышано как "три страноторно") — пословный поиск такое
    не находит, а выравнивание последовательностей — находит, потому что
    сопоставляет целые УЧАСТКИ произвольной длины, а не слово к слову.
    """
    if not os.path.exists(srt_path) or not os.path.exists(book_path):
        print("Ошибка: Убедитесь, что оба файла (srt и txt) существуют!")
        return False

    with open(book_path, "r", encoding="utf-8") as f:
        book_text = f.read()
    raw_book_words = book_text.split()

    with open(srt_path, "r", encoding="utf-8") as f:
        srt_content = f.read()

    blocks = parse_srt_blocks(srt_content)
    if not blocks:
        print("Ошибка: Не удалось прочитать блоки субтитров.")
        return False

    # --- Строим плоский список слов srt с привязкой к номеру блока ---
    flat_srt = []  # [{'word':..., 'block_idx':..., 'clean':...}]
    for block_idx, (_, _, words) in enumerate(blocks):
        for w in words:
            flat_srt.append({'word': w, 'block_idx': block_idx, 'clean': clean_word(w)})

    # Индексы слов, пригодных для сопоставления (не пустая пунктуация)
    matchable_srt_idx = [i for i, e in enumerate(flat_srt) if e['clean']]
    srt_clean_seq = [flat_srt[i]['clean'] for i in matchable_srt_idx]

    matchable_book_idx = [i for i, w in enumerate(raw_book_words) if clean_word(w)]
    book_clean_seq = [clean_word(raw_book_words[i]) for i in matchable_book_idx]

    print(f"Слов в субтитрах: {len(srt_clean_seq)}, слов в книге: {len(book_clean_seq)}")
    print("Запуск глобального сопоставления текста (может занять некоторое время)...")

    if progress_cb:
        progress_cb(0, 100)

    matcher = SequenceMatcher(None, srt_clean_seq, book_clean_seq, autojunk=False)
    opcodes = matcher.get_opcodes()

    if progress_cb:
        progress_cb(50, 100)  # сам подсчёт выравнивания — самая долгая часть

    # out[i] = None -> слово этого слота пропускается при сборке (склеено с соседним)
    #        = строка -> итоговый текст для этого слота (может содержать несколько слов)
    out = [None] * len(matchable_srt_idx)

    total_opcodes = len(opcodes)
    for op_idx, (tag, i1, i2, j1, j2) in enumerate(opcodes, start=1):
        if tag == 'equal':
            for k in range(i2 - i1):
                book_word = raw_book_words[matchable_book_idx[j1 + k]]
                out[i1 + k] = book_word

        elif tag == 'replace':
            if i2 > i1:
                book_start = matchable_book_idx[j1]
                book_end = matchable_book_idx[j2 - 1] if j2 > j1 else book_start
                replacement_words = raw_book_words[book_start:book_end + 1] if j2 > j1 else []
                out[i1] = " ".join(replacement_words) if replacement_words else flat_srt[matchable_srt_idx[i1]]['word']
                # Остальные слоты этого участка склеиваются в первый — помечаем как пропуск
                for k in range(i1 + 1, i2):
                    out[k] = None

        elif tag == 'delete':
            # Слова есть в srt, но их нет в книге — оставляем как распознал whisper
            for k in range(i1, i2):
                out[k] = flat_srt[matchable_srt_idx[k]]['word']

        # tag == 'insert': слова есть в книге, но их нет в srt — вставлять некуда
        # (нет тайминга под них), поэтому пропускаем.

        if progress_cb and total_opcodes:
            progress_cb(50 + int(op_idx / total_opcodes * 45), 100)

    # --- Собираем блоки обратно, используя посчитанные замены ---
    block_tokens = [[] for _ in blocks]

    matchable_pos = 0
    for flat_idx, entry in enumerate(flat_srt):
        if entry['clean']:
            replacement = out[matchable_pos]
            matchable_pos += 1
            if replacement is not None:
                block_tokens[entry['block_idx']].append(replacement)
        else:
            # Пунктуационный токен — переносим как есть
            block_tokens[entry['block_idx']].append(entry['word'])

    fixed_blocks = []
    for (index_line, time_line, _), tokens in zip(blocks, block_tokens):
        new_text = " ".join(tokens)
        fixed_blocks.append(f"{index_line}\n{time_line}\n{new_text}")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n\n".join(fixed_blocks) + "\n")

    if progress_cb:
        progress_cb(100, 100)

    print(f"Готово! Идеальный файл сохранен в: {output_path}")
    return True


if __name__ == "__main__":
    # Использование: python fix_srt.py music.srt book.txt fixed_music.srt
    srt = sys.argv[1] if len(sys.argv) > 1 else "music.srt"
    book = sys.argv[2] if len(sys.argv) > 2 else "book.txt"
    out = sys.argv[3] if len(sys.argv) > 3 else "fixed_music.srt"

    align_and_fix(srt, book, out)
