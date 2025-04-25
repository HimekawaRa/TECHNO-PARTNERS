# -*- coding: utf-8 -*-
import os
import re
import tempfile
from typing import Dict, Any, List
from fastapi import FastAPI, File, UploadFile, HTTPException
import pypandoc

app = FastAPI(title="Raw Question Splitter by Delimiter")

def split_markdown_by_delimiter(md_content: str, delimiter: str) -> Dict[str, str]:
    """
    Splits the Markdown content into raw blocks based on a specific delimiter
    and returns all blocks in a dictionary.
    """
    escaped_delimiter = re.escape(delimiter)
    split_pattern = re.compile(escaped_delimiter, re.DOTALL)
    parts = split_pattern.split(md_content)
    blocks: List[str] = []
    for i, part in enumerate(parts):
        block = part
        # Re-add delimiter for all but the last block
        if i < len(parts) - 1:
            block += delimiter
        if block.strip():
            blocks.append(block.strip())
    return {f"block{idx+1}": blocks[idx] for idx in range(len(blocks))}


async def split_markdown_by_delimiter_final(file: UploadFile) -> Dict[str, str]:
    """
    Reads DOCX from UploadFile, converts to Markdown, strips intro, splits by delimiter,
    and returns all raw blocks.
    """
    tmp_path = None
    delimiter = '\\|\\|\r\n\r\n'
    try:
        # Save upload to temp docx
        with tempfile.NamedTemporaryFile(delete=False, suffix=".docx") as tmp:
            content = await file.read()
            tmp.write(content)
            tmp_path = tmp.name
        # Convert to Markdown
        md_content = pypandoc.convert_file(
            tmp_path,
            to='markdown',
            format='docx',
            extra_args=['--wrap=none']
        )
        # Strip everything before first question ('1.' or '1)')
        m = re.search(r'^\s*1[\.\)]', md_content, re.MULTILINE)
        if m:
            md_content = md_content[m.start():]
        # Split into all blocks
        return split_markdown_by_delimiter(md_content, delimiter)
    except Exception as e:
        print(f"Error processing file: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)


def parse_options_dict(options_str: str) -> Dict[str, str]:
    lines = options_str.splitlines()
    opts: Dict[str, str] = {}
    current_key = None
    buffer: List[str] = []
    for line in lines:
        m = re.match(r'^([A-F])\\\)\s*(.*)', line)
        if m:
            if current_key:
                opts[current_key] = ' '.join(buffer).strip()
            current_key = m.group(1)
            buffer = [m.group(2).strip()]
        elif current_key:
            buffer.append(line.strip())
    if current_key:
        opts[current_key] = ' '.join(buffer).strip()
    return opts


def extract_metadata(rest: str) -> Dict[str, Any]:
    meta: Dict[str, Any] = {}
    m_sec = re.search(r'Раздел:\s*(\d+)-([^\r\n]+)', rest)
    if m_sec:
        meta['section'] = {'id': int(m_sec.group(1)), 'name': m_sec.group(2).strip()}
    m_topic = re.search(r'Тема:\s*(\d+)-([^\r\n]+)', rest)
    if m_topic:
        meta['topic'] = {'id': int(m_topic.group(1)), 'name': m_topic.group(2).strip()}
    for field, key in [
        (r'Предмет:\s*([^\r\n]+)', 'subject'),
        (r'Класс:\s*([^\r\n]+)', 'class'),
        (r'Четверть:\s*(\d+)', 'quarter'),
        (r'Язык:\s*([^\r\n]+)', 'language'),
        (r'Форма задания:\s*([^\.]+)\.', 'task_type'),
        (r'Объяснение:\s*([\s\S]*?)Учебник', 'explanation'),
        (r'Цель:\s*([\s\S]*?)\r\n', 'learning_goals'),
        (r'Балл:\s*(\d+)', 'score'),
        (r'Правильный ответ:\s*([A-F])', 'correct_answer'),
        (r'Уровень:\s*([A-C])', 'difficulty_level'),
        (r'Направленность:\s*([\d\|]+)', 'direction')
    ]:
        m = re.search(field, rest)
        if m:
            val = m.group(1).strip().replace('|','')
            meta[key] = int(val) if key=='score' else val
    return meta


def format_block(item: Dict[str, str]) -> Dict[str, Any]:
    question = item['question'].replace('$','').strip()
    opts = parse_options_dict(item['options'])
    meta = extract_metadata(item.get('rest',''))
    return {
        'question': question,
        'options': opts,
        **meta
    }

@app.post("/split_by_delimiter")
async def split_by_delimiter_endpoint(file: UploadFile = File(...)) -> Dict[str, Any]:
    raw_blocks = await split_markdown_by_delimiter_final(file)
    parsed_items = []
    for block in raw_blocks.values():
        idx = block.find('A\\)')
        q = block[:idx].strip()
        end = block.find('Раздел:')
        opts = block[idx:end].strip() if end!=-1 else block[idx:].strip()
        rest = block[end:].strip() if end!=-1 else ''
        parsed_items.append({'question': q, 'options': opts, 'rest': rest})
    questions = [format_block(v) for v in parsed_items]
    return {'questions': questions}
