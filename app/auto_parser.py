import logging
import os
from typing import List, Dict
import time
import pypandoc
from fastapi import FastAPI
from docx import Document
from docx.document import Document as _Document
from docx.oxml.text.paragraph import CT_P
from docx.oxml.table import CT_Tbl
from docx.table import Table as _Table
from docx.text.paragraph import Paragraph
import re

app = FastAPI()
logger = logging.getLogger(__name__)

def iter_block_items(parent):
    if isinstance(parent, _Document):
        body = parent.element.body
    else:
        raise ValueError("Unsupported parent")
    for child in body.iterchildren():
        if isinstance(child, CT_P):
            yield Paragraph(child, parent)
        elif isinstance(child, CT_Tbl):
            yield _Table(child, parent)

def split_docx_into_questions(input_path: str, output_dir: str) -> list[str]:
    os.makedirs(output_dir, exist_ok=True)
    doc = Document(input_path)
    header_re = re.compile(r'^\d+\.?\s*задани', re.IGNORECASE)
    blocks = list(iter_block_items(doc))
    starts = [i for i, b in enumerate(blocks)
              if isinstance(b, Paragraph) and header_re.match(b.text.strip())]
    if not starts:
        raise ValueError("Заголовки заданий не найдены")
    ranges = [
        (s, starts[idx+1]-1 if idx+1 < len(starts) else len(blocks)-1)
        for idx, s in enumerate(starts)
    ]
    out_paths = []
    for num, (s, e) in enumerate(ranges, start=1):
        part = Document(input_path)
        body = part.element.body
        elems = list(body)
        for i in range(len(elems)-1, -1, -1):
            if i < s or i > e:
                if elems[i].tag.endswith('}sectPr'):
                    continue
                body.remove(elems[i])
        out_file = os.path.join(output_dir, f"question{num}.docx")
        part.save(out_file)
        out_paths.append(out_file)
    return out_paths

def split_questions_logic(src: str) -> list[dict]:
    """
    Принимает путь к сохранённому docx (src),
    возвращает список вопросов вида [{"number": .., "text": ..}, ...],
    причём все картинки из документа будут извлечены в <tmp>/media,
    а в тексте Markdown ссылки на них заменены на /img/<docx_name>/<filename>.
    """
    tmp = os.path.dirname(src)
    docname = os.path.splitext(os.path.basename(src))[0]

    # 1) разбиваем на части
    parts_dir = os.path.join(tmp, "parts")
    part_paths = split_docx_into_questions(src, parts_dir)

    # 2) готовим папку, куда Pandoc будет извлекать медиа
    media_dir = os.path.join(tmp, "media")
    os.makedirs(media_dir, exist_ok=True)

    questions: list[dict] = []
    for idx, path in enumerate(part_paths, start=1):
        # 3) конвертим этот кусочек в Markdown, извлекая все картинки
        md = pypandoc.convert_file(
            path,
            to="markdown+tex_math_dollars",
            format="docx",
            extra_args=[
                "--wrap=none",
                f"--extract-media={media_dir}"
            ],
        ).strip()

        # 4) заменяем все ссылки на media/... → /img/<docname>/...
        md = normalize_image_links(md, docname)

        questions.append({
            "number": idx,
            "text": md,
        })

    return questions

LETTER_TO_INDEX = {"A": 0, "B": 1, "C": 2, "D": 3}


def transform_questions(items):
    results = []
    for item in items:
        raw_text = item.get('text', '')
        if not raw_text:
            continue  # пропускаем, если текст пустой

        # 1. Предобработка: убираем \r, разбиваем по \n, отфильтровываем пустые строки
        text = raw_text.replace('\r', '')
        lines = [ln for ln in text.split('\n') if ln.strip() != '']

        # 2. Удаление медиа-вставок (изображений и т.п.)
        filtered_lines = []
        for ln in lines:
            low = ln.lower()
            # Пропускаем строки, которые выглядят как вставки изображений/медиа
            if low.startswith('![') or low.startswith('<img') or 'data:image' in low:
                continue
            if any(ext in low for ext in ('.png', '.jpg', '.jpeg', '.gif', '.bmp')):
                # Если строка содержит ссылку на изображение (например, заканчивается на .png или содержит http-ссылку на картинку)
                if 'http' in low or low.strip().endswith(('.png', '.jpg', '.jpeg', '.gif', '.bmp')):
                    continue
            filtered_lines.append(ln)
        lines = filtered_lines

        # Инициализируем поля результата по умолчанию
        qid = item.get('id')  # идентификатор задания
        result_item = {
            "id": qid,
            "id_predmet": 1,
            "subject": {"name": "Математика", "namekz": "Математика"},
            "vopros": "",
            "temy": "",
            "temyname": "",
            "temyid": "",
            "target": "",
            "tip": 1,
            "texty": "",
            "otvety": [],
            "pravOtv": [],
            "exp": ""
        }

        # 3. Поиск начала вопроса (строка с "N задание") и сбор строк вопроса
        question_lines = []
        start_idx = 0
        for idx, ln in enumerate(lines):
            # Ищем шаблон "число. задание" или "число задание" (опционально после '>' символа)
            match = re.match(r'^\s*(?:>\s*)?(\d+\.?\s*задание\.?)(.*)', ln, flags=re.IGNORECASE)
            if match:
                # Разделяем строку на часть после "задание"
                trailing_text = match.group(2)
                if trailing_text:
                    # Если на той же строке есть текст вопроса после "задание", добавляем его
                    question_lines.append(trailing_text.strip())
                # Отметим, что вопрос начинается со следующей строки после заголовка
                start_idx = idx + 1
                break

        # Собираем оставшиеся строки вопроса до начала вариантов ответов или служебной секции
        for idx in range(start_idx, len(lines)):
            ln = lines[idx]
            # Прерываем, если встретили начало вариантов или другую секцию
            if re.match(r'^\s*[A-F]\)', ln) or ln.startswith(
                    ("Правильный ответ", "Объяснение:", "Раздел:", "Тема:", "Цель:", "Балл:", "Учебник:")):
                break
            question_lines.append(ln)

        # Функция для объединения строк с сохранением формата LaTeX
        def combine_preserving_latex(line_list):
            combined = ""
            in_latex_block = False
            for i, ln in enumerate(line_list):
                stripped = ln.strip()
                # Если строка сама по себе начинается и заканчивается на $$ (однострочная формула)
                if stripped.startswith("$$") and stripped.endswith("$$"):
                    combined += stripped
                    if i != len(line_list) - 1:
                        combined += " "
                    continue
                # Если начинается $$ (многострочная формула)
                if stripped.startswith("$$") and not stripped.endswith("$$"):
                    in_latex_block = True
                    combined += stripped + "\n"
                    continue
                # Если заканчивается $$ (конец многострочной формулы)
                if stripped.endswith("$$") and in_latex_block:
                    combined += stripped
                    in_latex_block = False
                    if i != len(line_list) - 1:
                        combined += " "
                    continue
                # Если начинается окружение \begin{...}
                if stripped.startswith("\\begin{"):
                    in_latex_block = True
                    combined += ln + "\n"
                    continue
                # Если заканчивается окружение \end{...}
                if in_latex_block and stripped.startswith("\\end{"):
                    combined += ln
                    in_latex_block = False
                    if i != len(line_list) - 1:
                        combined += " "
                    continue
                # Если внутри блока LaTeX, просто добавляем строку с переводом
                if in_latex_block:
                    combined += ln + "\n"
                    continue
                # Обычное объединение вне LaTeX-блока: добавляем строку с пробелом
                combined += stripped
                if i != len(line_list) - 1:
                    combined += " "
            return combined.strip()

        # Получаем итоговый текст вопроса
        result_item["vopros"] = combine_preserving_latex(question_lines)

        # 4. Извлечение вариантов ответов
        answers_lines = []
        # Найдём индекс первой строки, похожей на "A) ...", чтобы знать откуда начинаются варианты
        answer_start = None
        for idx, ln in enumerate(lines):
            if re.match(r'^\s*[A-F]\)', ln):
                answer_start = idx
                break
        # Соберём все строки вариантов ответа, пока не встретим служебную метку
        if answer_start is not None:
            for idx in range(answer_start, len(lines)):
                ln = lines[idx]
                if ln.startswith(("Правильный ответ", "Объяснение:", "Раздел:", "Тема:", "Цель:", "Балл:", "Учебник:")):
                    break
                answers_lines.append(ln)

        # Разбираем список answers_lines на отдельные варианты
        structured_answers = []
        current_option = None
        current_text = ""
        for ln in answers_lines:
            option_match = re.match(r'^\s*([A-F])\)\s*(.*)', ln)
            if option_match:
                # Начало нового варианта (буква и текст)
                if current_option is not None:
                    # Сохраняем предыдущий вариант перед началом нового
                    structured_answers.append((current_option, current_text.strip()))
                current_option = option_match.group(1)  # буква варианта (A, B, ...)
                current_text = option_match.group(2)  # текст варианта (после "A)")
            else:
                # Продолжение текста текущего варианта (если вариант занимает несколько строк)
                if current_option is not None:
                    current_text += " " + ln.strip()
        # Добавляем последний вариант, если он есть
        if current_option is not None:
            structured_answers.append((current_option, current_text.strip()))

        # Сортируем варианты по букве (на случай, если вышли из порядка)
        structured_answers.sort(key=lambda x: x[0])
        # Извлекаем тексты вариантов в список
        answers_texts = [text for _, text in structured_answers]
        # Если вариантов меньше 4, дополняем пустыми строками
        while len(answers_texts) < 4:
            answers_texts.append("")
        # Ограничиваем до 6 вариантов максимум (если вдруг больше, хотя не ожидается более 6)
        if len(answers_texts) > 6:
            answers_texts = answers_texts[:6]
        result_item["otvety"] = answers_texts

        # 5. Извлечение правильного ответа (список индексов)
        for ln in lines:
            if ln.startswith("Правильный ответ"):
                # Берём часть после двоеточия
                parts = ln.split(":", 1)
                letters_part = parts[1] if len(parts) > 1 else ""
                # Ищем все буквы A-F (латинские) в этой части, без учёта регистра
                letters = re.findall(r'[A-F]', letters_part, flags=re.IGNORECASE)
                result_item["pravOtv"] = [ord(letter.upper()) - ord('A') for letter in letters]
                break

        # 6. Извлечение объяснения (exp)
        exp_lines = []
        for idx, ln in enumerate(lines):
            if ln.startswith("Объяснение:"):
                # Если после "Объяснение:" на той же строке есть текст, включаем его
                content_after_colon = ln.split("Объяснение:", 1)[1].strip()
                if content_after_colon:
                    exp_lines.append(content_after_colon)
                # Добавляем следующие строки пояснения
                j = idx + 1
                while j < len(lines):
                    nxt = lines[j]
                    # Останавливаемся, когда встретили следующую служебную секцию
                    if nxt.startswith(("Учебник:", "Раздел:", "Тема:", "Цель:", "Балл:")):
                        break
                    exp_lines.append(nxt)
                    j += 1
                break
        result_item["exp"] = combine_preserving_latex(exp_lines) if exp_lines else ""

        # 7. Извлечение раздела (temy)
        for ln in lines:
            if ln.startswith("Раздел:"):
                result_item["temy"] = ln.split("Раздел:", 1)[1].strip()
                break

        # 8. Извлечение темы (temyname и temyid)
        for ln in lines:
            if ln.startswith("Тема:"):
                tema_content = ln.split("Тема:", 1)[1].strip()
                # Проверяем шаблон "число-..." или "число ...", чтобы выделить ID
                m = re.match(r'^(\d+)[\-\u2013\s]+(.*)', tema_content)
                if m:
                    result_item["temyid"] = m.group(1).strip()
                    result_item["temyname"] = m.group(2).strip()
                else:
                    # Если идентификатор не указан, вся строка - название темы
                    result_item["temyid"] = ""
                    result_item["temyname"] = tema_content
                break

        # 9. Извлечение цели (target)
        for idx, ln in enumerate(lines):
            if ln.startswith("Цель:"):
                # Берём текст после "Цель:"
                target_content = ln.split("Цель:", 1)[1].strip()
                target_lines = [target_content] if target_content else []
                # Собираем последующие строки до следующей служебной метки
                j = idx + 1
                while j < len(lines):
                    nxt = lines[j]
                    if nxt.startswith(("Балл:", "Учебник:", "Раздел:", "Тема:")):
                        break
                    target_lines.append(nxt)
                    j += 1
                result_item["target"] = combine_preserving_latex(target_lines) if target_lines else ""
                break

        # Добавляем сформированный словарь задания в результирующий список
        results.append(result_item)
    return results




def wrap_raw(raw_item) -> dict:
    """
    raw_item может быть либо строкой, либо dict с ключом 'text'.
    Сохраняем и сам объект, и чистую строку в state.
    """
    if isinstance(raw_item, dict) and "text" in raw_item:
        text = raw_item["text"]
    elif isinstance(raw_item, str):
        text = raw_item
    else:
        # на всякий случай приводим к строке
        text = str(raw_item)
    return {"raw": raw_item, "text": text}


def extract_number_and_vopros(state: dict) -> dict:
    text = state["text"]
    logger.debug("STEP1: raw text:\n%s", text)

    lines = [l.strip() for l in text.replace("\r", "").split("\n")]
    logger.debug("STEP1: total %d lines", len(lines))

    num = None
    vopros = "вопрос не опознан"

    pattern = re.compile(
        r'^\s*(?:>\s*)?(\d+)(?:\\\.)?\.?\s*задание\.?', flags=re.IGNORECASE
    )

    for i, ln in enumerate(lines):
        m = pattern.match(ln) or re.match(r'^\s*(?:>\s*)?(\d+)\s*задание', ln, flags=re.IGNORECASE)
        if m:
            num = int(m.group(1))
            tail = ln[m.end():].strip()
            logger.debug("STEP1: matched number=%r, raw tail=%r", num, tail)

            if tail:
                vopros = tail
                logger.debug("STEP1: vopros taken from same line: %r", vopros)
            else:
                # 🔽 Собираем строки до первой опции A)–F)
                vopros_lines = []
                for j in range(i + 1, len(lines)):
                    if re.match(r'^[A-FА-Я]\\?\)', lines[j]):  # начало блока ответов
                        break
                    if lines[j]:
                        vopros_lines.append(lines[j])
                if vopros_lines:
                    vopros = " ".join(vopros_lines).strip()
                    logger.debug("STEP1: vopros assembled from lines: %r", vopros)
            break

    if num is None:
        logger.warning("STEP1: номер задания не найден, оставляем None")
    else:
        logger.info("STEP1: извлечён номер=%s, вопрос=%r", num, vopros)

    new = state.copy()
    new.update({"number": num, "vopros": vopros})
    return new

# Шаг 2: temy, temyid, temyname
def extract_temy(state: dict) -> dict:
    temy_id = temy_name = podtemy_id = podtemy_name = None

    for ln in state.get("text", "").splitlines():
        # 1) Обрабатываем «Раздел:»
        if ln.startswith("Раздел:"):
            raw = ln.split("Раздел:", 1)[1].strip()
            # вытаскиваем номер и имя секции
            m0 = re.match(r'^(\d+)[\-\u2013\s]*(.+)$', raw)
            if m0:
                temy_id   = m0.group(1)
                temy_name = m0.group(2).strip().rstrip('.')
            else:
                temy_name = raw.rstrip('.')

        # 2) Обрабатываем «Тема:» и затем выходим из цикла
        elif ln.startswith("Тема:"):
            raw = ln.split("Тема:", 1)[1].strip()
            m1 = re.match(r'^(\d+)[\-\u2013\s]+(.+)', raw)
            if m1:
                podtemy_id   = m1.group(1)
                podtemy_name = m1.group(2).strip().rstrip('.')
            else:
                podtemy_name = raw.rstrip('.')
            break

    # Собираем результат
    new_state = state.copy()
    new_state.update({
        "temy_id":      temy_id,
        "temy_name":    temy_name,
        "podtemy_id":   podtemy_id,
        "podtemy_name": podtemy_name,
    })
    return new_state

# Шаг 3: otvety
def extract_otvety(state: dict) -> dict:
    """
    Извлекает ровно 4 варианта ответов A)–D) из state["text"].
    Убирает ведущие '>' и пробелы, поддерживает многострочные варианты,
    останавливается при встрече 'Правильный ответ', 'Объяснение:' и т.п.
    """
    import re
    text = state["text"]
    raw_lines = text.split("\n")

    # Транслитерация кириллицы → латиница (только первые буквы вариантов)
    translit = {"А": "A", "В": "B", "С": "C", "D": "D", "E": "E", "F": "F"}
    lines = [ln.translate(str.maketrans(translit)).lstrip("> ").rstrip() for ln in raw_lines]

    # 2) Найдём начало блока — первая строка, которая выглядит как "X)" или "X\)"
    start_idx = None
    start_re = re.compile(r'^\s*[A-F]\\?\)\s*')  # теперь только латинские буквы
    for i, ln in enumerate(lines):
        if start_re.match(ln):
            start_idx = i
            logger.debug("STEP3: options start at line %d: %r", i, ln)
            break

    opts = []
    if start_idx is not None:
        # 3) Собираем строки до первой «служебной» метки
        block = []
        for ln in lines[start_idx:]:
            if re.match(r'^(Правильный ответ|Объяснение:|Раздел:|Тема:|Цель:|Балл:)', ln):
                logger.debug("STEP3: hit end-of-options at %r", ln)
                break
            block.append(ln)

        # 4) Парсим внутри блока: новая опция при встрече "X)" или "X\)"
        curr = None
        opt_re = re.compile(r'^\s*([A-F])\\?\)\s*(.*)')
        for ln in block:
            m = opt_re.match(ln)
            if m:
                if curr is not None:
                    opts.append(curr.strip())
                curr = m.group(2).strip()
                logger.debug("STEP3: new option %r: %r", m.group(1), curr)
            else:
                if curr is not None:
                    curr += " " + ln.strip()
        if curr is not None:
            opts.append(curr.strip())

    logger.info("STEP3: final otvety = %r", opts)
    new = state.copy()
    new["otvety"] = opts
    return new
#
# def extract_prav_otv(state: dict) -> dict:
#     """
#     Извлекает правильные ответы из текста, включая A|, B|, ..., даже если таких несколько.
#     """
#     text = state.get("text", "")
#     text_cleaned = text.replace("\r", "").replace("\n", " ")  # на случай переноса
#
#     # Ищем все буквы A–F перед \|, как в "A\|", "B\|"
#     found = re.findall(r'([A-F])\\\|', text_cleaned, flags=re.IGNORECASE)
#     logger.debug("STEP4: found letters = %r", found)
#
#     # Преобразуем в индексы
#     mapping = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4, "F": 5}
#     indexes = [mapping[letter.upper()] for letter in found if letter.upper() in mapping]
#
#     logger.info("STEP4: pravOtv indexes = %r", indexes)
#
#     new = state.copy()
#     new["pravOtv"] = indexes
#     return new

def extract_prav_otv(state: dict) -> dict:
    import re

    text = state.get("text", "")
    text_cleaned = text.replace("\r", "").replace("\n", " ")

    # Кириллица → латиница
    translit = {
        'А': 'A', 'В': 'B', 'С': 'C', 'Д': 'D', 'Е': 'E', 'Ф': 'F',
        'а': 'A', 'в': 'B', 'с': 'C', 'д': 'D', 'е': 'E', 'ф': 'F'
    }
    for k, v in translit.items():
        text_cleaned = text_cleaned.replace(k, v)

    # Найти все буквы A–F перед | или внутри "Правильный ответ:"
    # Примеры: A|, A |, A]|, A:[ , и т.п.
    found = re.findall(r'Правильн(?:ый|ые)\s+ответ[а-я:\s]*([A-F](?:[,|; ]+[A-F])*)', text_cleaned, flags=re.IGNORECASE)

    letters = []
    if found:
        # Пример: "A, B", "A;B", "A|B"
        raw_ans = found[0]
        letters = re.findall(r'[A-F]', raw_ans)

    # Альтернативный способ, если шаблон выше не сработал:
    if not letters:
        letters = re.findall(r'\b([A-F])\s*\\?\|', text_cleaned)

    mapping = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4, "F": 5}
    indexes = [mapping[l] for l in letters if l in mapping]

    new = state.copy()
    new["pravOtv"] = indexes
    return new

# Шаг 5: exp

def extract_exp(state: dict) -> dict:
    exp_lines = []
    lines = state["text"].split("\n")

    for i, ln in enumerate(lines):
        if ln.startswith("Объяснение:"):
            tail = ln.split("Объяснение:", 1)[1].strip()
            if tail:
                exp_lines.append(tail)
            for nxt in lines[i + 1:]:
                if re.match(r'^(Раздел:|Тема:|Цель:|Балл:|Учебник:)', nxt):
                    break
                exp_lines.append(nxt.strip())
            break

    explanation = " ".join(exp_lines).strip()

    new = state.copy()
    new["exp"] = explanation
    return new

# Шаг 6: target
def extract_target(state: dict) -> dict:
    target_lines = []
    lines = state["text"].split("\n")
    for i, ln in enumerate(lines):
        if ln.startswith("Цель:"):
            tail = ln.split("Цель:",1)[1].strip()
            if tail:
                target_lines.append(tail)
            for nxt in lines[i+1:]:
                if re.match(r'^(Балл:|Учебник:|Раздел:|Тема:)', nxt):
                    break
                target_lines.append(nxt.strip())
            break

    # Сбор из строк
    target = " ".join(target_lines).strip()

    # Убираем ВСЕ символы '>' и leading точку/пробелы
    target = re.sub(r'^[\.\s>]+', '', target)  # удаляем в начале ., >, пробелы
    target = target.replace('>', '')           # удаляем все оставшиеся '>'
    target = re.sub(r'\s{2,}', ' ', target)    # сводим множественные пробелы к одному
    target = target.strip()

    new = state.copy()
    new["target"] = target
    return new

# Собирающая функция
def pipeline_mcq(raw_item) -> dict:
    st = wrap_raw(raw_item)
    st = extract_number_and_vopros(st)
    st = extract_temy(st)
    st = extract_otvety(st)
    st = extract_prav_otv(st)
    st = extract_exp(st)
    st = extract_target(st)
    return st



def extract_matching_number_and_vopros(state: dict) -> dict:
    """
    Извлекает номер и формулировку вопроса (обрезает по линии '----').
    """
    text = state["text"]
    logger.debug("MATCHING STEP1: raw text:\n%s", text)

    lines = [l.strip() for l in text.replace("\r", "").split("\n")]
    num = None
    vopros = "вопрос не опознан"

    # Поиск строки с номером задания
    zadanie_re = re.compile(r"^\s*(\d+)\s*(?:задани[ея]?)", re.IGNORECASE)
    start_line_idx = None

    for i, line in enumerate(lines):
        m = zadanie_re.match(line)
        if m:
            num = int(m.group(1))
            start_line_idx = i
            break

    if start_line_idx is not None:
        vopros_lines = []
        for line in lines[start_line_idx + 1:]:
            if re.search(r"-{5,}", line):  # обрезаем по линии
                break
            if line:
                vopros_lines.append(line)
        vopros = " ".join(vopros_lines).strip(" .:")
        logger.debug("MATCHING STEP1: vopros assembled: %r", vopros)
    else:
        logger.warning("MATCHING STEP1: номер задания не найден")

    return state | {"number": num, "vopros": vopros}


def extract_matching_options(state: dict) -> dict:
    raw = state.get("text", "")
    # 1) Снимаем экранирование точек, скобок и квадратных скобок
    text = (raw
            .replace(r"\.", ".")
            .replace(r"\)", ")")
            .replace(r"\[", "[")
            .replace(r"\]", "]"))

    lines = text.splitlines()
    group1 = {}
    group2 = {}
    in_block = False

    for i, ln in enumerate(lines):
        # 2) Ищем начало блока ("1. ")
        if not in_block:
            if re.match(r"^\s*1[.)]\s+", ln):
                in_block = True
            else:
                continue

        # 3) Останавливаемся, как только встретили заголовок следующего раздела
        if re.match(r"^\s*(Раздел:|Тема:|Объяснение:|Балл:|Правильный ответ:)", ln):
            break

        # 4) Пропускаем чисто декоративные разделительные линии
        if re.match(r"^\s*-{3,}\s*$", ln):
            continue

        # 5) Парсим строки вида "N. <текст>  X) <вариант>"
        m = re.match(r"^\s*(\d+)[\.\)]\s+(.+)$", ln)
        if not m:
            continue

        idx = int(m.group(1)) - 1
        rest = m.group(2).strip()

        # 6) Делим по первому вхождению "A)"/"B)"/…/"E)"
        parts = re.split(r"\s+([A-E])\)\s+", rest, maxsplit=1)
        if len(parts) == 3:
            left, letter, right = parts
            group1[idx] = left.strip()
            group2[idx] = right.strip()
        else:
            group1[idx] = rest


    # 7) Возвращаем обновлённый state с новыми otvety
    new_state = state.copy()
    new_state["otvety"] = {"group1": group1, "group2": group2}
    return new_state

def extract_matching_pravotv(st: dict) -> dict:
    """
    Ищет в raw.text строку 'Правильный ответ:' и парсит пары вида '1-C', '2-A' и т.д.
    Записывает результат в st['pravOtv'] в виде словаря {left_idx: ['ts-right_idx'], ...}.
    """
    text = st.get("raw", {}).get("text", "")
    m = re.search(r"Правильный ответ[:：]\s*(.+)", text)
    if not m:
        st["pravOtv"] = {}
        return st

    parts = re.split(r"[，,]\s*", m.group(1))
    # сколько элементов в group1
    group1 = st.get("otvety", {}).get("group1", {})
    n = len(group1)

    prav = {}
    for part in parts:
        m2 = re.match(r"\s*(\d+)\s*[-:]\s*([A-Z])", part)
        if not m2:
            continue
        left_idx = int(m2.group(1)) - 1           # из "1-C" делаем 0
        letter = m2.group(2)
        right_idx = ord(letter) - ord("A")        # из "C" делаем 2
        if 0 <= left_idx < n:
            prav[str(left_idx)] = [f"ts-{right_idx}"]

    st["pravOtv"] = prav
    return st

def pipeline_matching(raw_item) -> dict:
    st = wrap_raw(raw_item)
    st = extract_matching_number_and_vopros(st)
    st = extract_temy(st)
    st = extract_matching_options(st)
    st = extract_matching_pravotv(st)
    st = extract_exp(st)
    st = extract_target(st)
    return st






def build_rows_with_placeholders(
    states: list[dict],
    subject: dict,
    language: str,
    klass: str,
    tip: int) -> list[dict]:
    """
    Из списка состояний (pipeline output) формирует итоговые строки с заглушками
    для пропущенных номеров.
    """
    rows = []
    last_id = 0
    for state in states:
        curr_id = state.get("number")

        if curr_id is not None:
            # 1) Заглушки для пропущенных вопросов
            for missing in range(last_id + 1, curr_id):
                logger.debug("Placeholder for missing question %d", missing)
                rows.append({
                    "id": missing,
                    "id_predmet": 1,
                    "subject": subject,
                    "language": language,
                    "klass": klass,
                    "vopros": "вопрос не опознан",

                    "temy_id": None,
                    "temy_name": None,
                    "podtemy_id": None,
                    "podtemy_name": None,

                    "target": "",
                    "tip": tip,
                    "texty": "",
                    "otvety": ["", "", "", ""],
                    "pravOtv": [],
                    "exp": "",
                    "raw": None
                })

            # 2) Собственно вопрос
            rows.append({
                "id": curr_id,
                "id_predmet": 1,
                "subject": subject,
                "language": language,
                "klass": klass,
                "vopros": state.get("vopros", "вопрос не опознан"),


                "temy_id": state.get("temy_id"),
                "temy_name": state.get("temy_name"),
                "podtemy_id": state.get("podtemy_id"),
                "podtemy_name": state.get("podtemy_name"),


                "target": state.get("target", ""),
                "tip": tip,
                "texty": "",
                "otvety": state.get("otvety", ["", "", "", ""]),
                "pravOtv": state.get("pravOtv", []),
                "exp": state.get("exp", ""),
                "raw": state.get("raw", None)
            })

            last_id = curr_id

        else:
            # Если номер не найден — просто возвращаем, что есть
            logger.debug("Unnumbered question, adding as-is")
            rows.append({
                "id": None,
                "id_predmet": 1,
                "subject": subject,
                "language": language,
                "klass": klass,
                "vopros": state.get("vopros", "вопрос не опознан"),

                "temy_id": state.get("temy_id"),
                "temy_name": state.get("temy_name"),
                "podtemy_id": state.get("podtemy_id"),
                "podtemy_name": state.get("podtemy_name"),

                "target": state.get("target", ""),
                "tip": tip,
                "texty": "",
                "otvety": state.get("otvety", ["", "", "", ""]),
                "pravOtv": state.get("pravOtv", []),
                "exp": state.get("exp", ""),
                "raw": state.get("raw", None)
            })

    return rows


def clean_math_and_sub(s: str) -> str:
    if not s:
        return s

    # 0) Убираем экранирующие слэши
    s = s.replace('\\', '')

    # 1) Двойные дефисы → одиночный
    s = s.replace('--', '-')

    # 2) Скобки LaTeX-интервалов \[ … \] → [ … ]
    s = s.replace('\[', '[').replace('\]', ']')

    # 3) x^n^ → x^n
    s = re.sub(r'\^(\d+)\^', r'^\1', s)

    # 4) <sub>…</sub> оставляем (или уже есть в exp) — ничего не трогаем

    # 5) Тильды ~…~ → <sub>…</sub>
    s = re.sub(r'~([^~]+?)~', r'<sub>\1</sub>', s)

    # 6) Убираем лишние пробелы перед/после скобок
    s = re.sub(r'\s+\(', ' (', s)
    s = re.sub(r'\)\s+', ') ', s)

    return s.strip()


def normalize_image_links(md: str, docname: str) -> str:
    """
    Ищет в Markdown все ![](path/to/.../имя_файла)
    и превращает их в ![](/img/<docname>/имя_файла)
    """
    return re.sub(
        r'!\[\]\((?:.*?)[\\/]+([^\\/]+)\)',
        rf'![](/img/{docname}/\1)',
        md
    )


