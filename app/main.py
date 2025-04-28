# -*- coding: utf-8 -*-
import os
import re
import tempfile
from typing import Dict, Any, List
from fastapi import FastAPI, File, UploadFile, HTTPException
import pypandoc

app = FastAPI(title="Raw Question Splitter by Delimiter")


def latex_to_mathjax(text: str) -> str:
    r"""
    Wrap TeX‐style math in MathJax delimiters:
      $$...$$ → \[...\]  (display)
      $...$   → \(...\)  (inline)
    """
    # display math first
    text = re.sub(r"\$\$(.+?)\$\$", r"\\[\1\\]", text, flags=re.DOTALL)
    # inline math
    text = re.sub(r"\$(.+?)\$", r"\\(\1\\)", text, flags=re.DOTALL)
    return text


async def split_markdown_by_delimiter_final(file: UploadFile) -> List[str]:
    """
    1) Save UploadFile to a temp .docx
    2) Convert to Markdown (pandoc, no wrapping)
    3) Trim everything before first "1." or "1)"
    4) Split on newline before a top‐level question number (e.g. "2.", "3.", etc.)
       — but NOT on lines like "10.4.1.27", i.e. only dots not followed by another digit
    5) Return list of question blocks
    """
    tmp_path = None
    try:
        # 1) Save to temp file
        with tempfile.NamedTemporaryFile(delete=False, suffix=".docx") as tmp:
            content = await file.read()
            tmp.write(content)
            tmp_path = tmp.name

        # 2) Convert to markdown
        md = pypandoc.convert_file(
            tmp_path,
            to="markdown",
            format="docx",
            extra_args=["--wrap=none"]
        )

        # 3) Trim before first "1." or "1)"
        m = re.search(r"^\s*1[\.\)]", md, flags=re.MULTILINE)
        if m:
            md = md[m.start():]

        # 4) Split on newline before a question number like "2.", "3.", ... but not "10.4.1.27"
        parts = re.split(r"\n(?=\d+\.(?!\d))", md)

        # 5) Filter out empties
        return [p.strip() for p in parts if p.strip()]

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error parsing DOCX: {e}")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)


def parse_options_dict(options_str: str) -> Dict[str, str]:
    """
    From lines like:
      A) текст...
      B) другой текст...
    build { 'A': 'текст...', 'B': 'другой текст...', ... }
    """
    lines = options_str.splitlines()
    opts: Dict[str, str] = {}
    cur = None
    buf: List[str] = []

    # normalize any escaped braces
    lines = [ln.replace('\\)', ')') for ln in lines]

    for line in lines:
        m = re.match(r'^([A-F])\)\s*(.*)', line)
        if m:
            if cur:
                opts[cur] = " ".join(buf).strip()
            cur = m.group(1)
            buf = [m.group(2).strip()]
        elif cur:
            buf.append(line.strip())
    if cur:
        opts[cur] = " ".join(buf).strip()
    return opts


def extract_metadata(rest: str) -> Dict[str, Any]:
    """
    Extract metadata section from the rest of the block.
    Wrap all non‐numeric fields through latex_to_mathjax.
    """
    meta: Dict[str, Any] = {}

    # Раздел
    m = re.search(r"Раздел:\s*(\d+)-([^\r\n]+)", rest)
    if m:
        meta["section"] = {"id": int(m.group(1)), "name": m.group(2).strip()}

    # Тема
    m = re.search(r"Тема:\s*(\d+)-([^\r\n]+)", rest)
    if m:
        meta["topic"] = {"id": int(m.group(1)), "name": m.group(2).strip()}

    # прочие поля
    fields = [
        (r"Предмет:\s*([^\r\n]+)", "subject"),
        (r"Класс:\s*([^\r\n]+)", "class_"),
        (r"Четверть:\s*(\d+)", "quarter"),
        (r"Язык:\s*([^\r\n]+)", "language"),
        (r"Форма задания:\s*([^\.]+)\.", "task_type"),
        (r"Объяснение:\s*([\s\S]*?)(?:\r?\n\r?\n|$)", "explanation"),
        (r"Цель:\s*([\s\S]*?)(?:\r?\n|$)", "learning_goals"),
        (r"Балл:\s*(\d+)", "score"),
        (r"Правильный ответ:\s*(.+?)(?:\r?\n|$)", "correct_answer"),
        (r"Уровень:\s*([A-C])", "difficulty_level"),
        (r"Направленность:\s*([\d\|]+)", "direction"),
    ]
    for pattern, key in fields:
        m = re.search(pattern, rest)
        if not m:
            continue
        raw = m.group(1).strip()
        if key == "score":
            meta[key] = int(raw)
        else:
            if key == "direction":
                raw = raw.replace("|", "")
            meta[key] = latex_to_mathjax(raw)
    return meta


def format_block(block: str) -> Dict[str, Any]:
    """
    Build a structured dict from one question block.
    """
    # split head (question+options) vs rest (metadata)
    split_at = re.search(r"\r?\nРаздел:", block)
    head = block if not split_at else block[: split_at.start()]
    rest = "" if not split_at else block[split_at.start():]

    # question up to first "A)"
    idx = head.find("\r\n\r\nA\\)")
    q_text = head if idx < 0 else head[:idx]
    opts_text = "" if idx < 0 else head[idx:]

    question = latex_to_mathjax(q_text.strip())
    raw_opts = parse_options_dict(opts_text)
    options = {k: latex_to_mathjax(v) for k, v in raw_opts.items()}

    meta = extract_metadata(rest)

    out: Dict[str, Any] = {"question": question, "options": options}
    for key in [
        "section", "topic", "subject", "class_",
        "quarter", "language", "task_type",
        "learning_goals", "explanation",
        "correct_answer", "difficulty_level",
        "score", "direction"
    ]:
        if key in meta:
            out[key] = meta[key]

    return out


@app.post("/split_by_delimiter")
async def split_by_delimiter_endpoint(file: UploadFile = File(...)) -> Dict[str, Any]:
    blocks = await split_markdown_by_delimiter_final(file)
    questions = [format_block(b) for b in blocks]
    return {"questions": questions}
