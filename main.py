import shutil
import tempfile
from fastapi import FastAPI, File, UploadFile, HTTPException, Form
import os
import re
import subprocess
import json
import logging
import openai
from dotenv import load_dotenv
from docx import Document
from docx.document import Document as _Document
from docx.oxml.text.paragraph import CT_P
from docx.oxml.table import CT_Tbl
from docx.table import _Cell, Table
from docx.text.paragraph import Paragraph
from pdf2image import convert_from_path
import base64
from fastapi.staticfiles import StaticFiles

from app.auto_parser import split_questions_logic, pipeline_mcq, \
    build_rows_with_placeholders, clean_math_and_sub, pipeline_matching

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()
openai.api_key = os.getenv("OPENAI_API_KEY")

app = FastAPI()


from app.promt import GLOBAL_SYSTEM_PROMPT, GLOBAL_FIX_PROMPT

PROMPT = GLOBAL_SYSTEM_PROMPT

BASE_DIR = os.path.dirname(__file__)
STATIC_DIR = os.path.join(BASE_DIR, "static")
IMG_DIR = os.path.join(STATIC_DIR, "img")
os.makedirs(IMG_DIR, exist_ok=True)
app.mount(
    "/img",
    StaticFiles(directory=os.path.join(STATIC_DIR, "img")),
    name="img",
)

def iter_block_items(parent):
    if isinstance(parent, _Document):
        parent_element = parent.element.body
    elif isinstance(parent, _Cell):
        parent_element = parent._tc
    else:
        raise ValueError("Unsupported parent type")
    for child in parent_element.iterchildren():
        if isinstance(child, CT_P):
            yield Paragraph(child, parent)
        elif isinstance(child, CT_Tbl):
            yield Table(child, parent)


async def send_image_to_gpt(image_path: str) -> dict:
    logger.info(f"Sending image to GPT: {image_path}")
    with open(image_path, "rb") as f:
        image_bytes = f.read()
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    messages = [
        {"role": "system", "content": PROMPT},
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}]},
    ]
    functions = [
        {
            "name": "return_json",
            "description": "Return the parsed questions as JSON matching the schema.",
            "parameters": {
                "type": "object",
                "properties": {"questions": {"type": "array", "items": {"type": "object"}}},
                "required": ["questions"]
            }
        }
    ]
    try:
        resp = openai.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            temperature=0.0,
            functions=functions,
            function_call={"name": "return_json"}
        )
        logger.info("Received GPT response with function call")
    except Exception as e:
        logger.error(f"OpenAI request failed: {e}")
        return {"questions": [], "error": "OpenAI request failed"}

    # Access function call args
    func_call = resp.choices[0].message.function_call
    if not func_call or not func_call.arguments:
        logger.error("No function_call in GPT response")
        return {"questions": [], "error": "No function_call in GPT response"}
    args = func_call.arguments
    try:
        parsed = json.loads(args)
        logger.info("Successfully parsed JSON from GPT")
        return parsed
    except Exception as e:
        logger.error(f"JSON parse error: {e}, raw args: {args}")
        return {"questions": [], "error": "Invalid JSON from GPT", "raw": args}

SYSTEM_PROMPT = GLOBAL_FIX_PROMPT

def fix_math_json(input_data: dict) -> dict:
    functions = [
        {
            "name": "return_json",
            "description": "Return the parsed questions as JSON matching the schema.",
            "parameters": {
                "type": "object",
                "properties": {"questions": {"type": "array", "items": {"type": "object"}}},
                "required": ["questions"]
            }
        }
    ]
    response = openai.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(input_data, ensure_ascii=False)}
        ],
        temperature=0,
            functions=functions,
            function_call={"name": "return_json"}
    )
    args = response.choices[0].message.function_call.arguments
    try:
        corrected_json = json.loads(args)
        return corrected_json
    except json.JSONDecodeError:
        raise ValueError("Ответ от GPT не является валидным JSON:", args)

@app.post("/convert-and-send/", tags=["GPT Parser"])
async def convert_docx_to_images_and_send(file: UploadFile = File(...)):
    logger.info("/convert-and-send/ called")
    contents = await file.read()
    filename_base = os.path.splitext(file.filename or "uploaded")[0]
    input_path = f"{filename_base}.docx"
    with open(input_path, "wb") as f:
        f.write(contents)
    logger.info(f"Saved uploaded docx to {input_path}")

    tasks_dir = "tasks_docs"
    images_dir = "sent_images"
    os.makedirs(tasks_dir, exist_ok=True)
    os.makedirs(images_dir, exist_ok=True)

    doc = Document(input_path)
    pattern = re.compile(r'^\d+\.?\s*задани', flags=re.IGNORECASE)
    all_blocks = list(iter_block_items(doc))
    task_indices = [i for i, b in enumerate(all_blocks) if isinstance(b, Paragraph) and pattern.match(b.text.strip())]
    logger.info(f"Found task indices: {task_indices}")
    if not task_indices:
        return {"error": "В загруженном файле не найдены заголовки заданий."}

    task_ranges = []
    for i, start in enumerate(task_indices):
        end = task_indices[i+1] - 1 if i+1 < len(task_indices) else len(all_blocks)-1
        task_ranges.append((start, end))
    logger.info(f"Task ranges: {task_ranges}")

    image_paths = []
    for num, (s, e) in enumerate(task_ranges, start=1):
        doc_full = Document(input_path)
        body = doc_full.element.body
        elems = list(body)
        for idx in range(len(elems)-1, -1, -1):
            if idx < s or idx > e:
                if elems[idx].tag.endswith('}sectPr'): continue
                body.remove(elems[idx])
        task_doc = os.path.join(tasks_dir, f"task{num}.docx")
        doc_full.save(task_doc)
        logger.info(f"Saved task {num} docx to {task_doc}")

        try:
            subprocess.run([
                "libreoffice",
                "--headless", "--convert-to", "pdf", task_doc,
                "--outdir", tasks_dir
            ], check=True)
            logger.info(f"Converted task {num} to PDF")
        except FileNotFoundError:
            return {"error": "LibreOffice не установлена или недоступна в PATH."}

        pdf_file = os.path.join(tasks_dir, f"task{num}.pdf")
        pages = convert_from_path(pdf_file, dpi=200)
        img = pages[0]
        img_file = os.path.join(images_dir, f"task{num}.png")
        img.save(img_file)
        logger.info(f"Saved image for task {num}: {img_file}")
        image_paths.append(img_file)
        os.remove(pdf_file)

    os.remove(input_path)
    logger.info("Removed original docx")

    all_questions = []
    for path in image_paths:
        logger.info(f"Processing GPT for image {path}")
        resp = await send_image_to_gpt(path)
        for q in resp.get("questions", []):
            if q.get("error"): continue
            ca = q.get("correct_answer", "")
            if isinstance(ca, str) and "|" in ca:
                q["correct_answer"] = ca.split("|",1)[0].strip()
            for k,v in q.get("options",{}).items():
                if not v.startswith("$"): q["options"][k] = f"${v}$"
            expl = q.get("explanation","")
            if expl:
                lines = expl.split("\n")
                for i,ln in enumerate(lines):
                    if re.search(r"[=+\-^]|\\frac", ln) and not ln.startswith("$"):
                        lines[i] = f"${ln}$"
                q["explanation"] = "\n".join(lines)
            all_questions.append(q)
    logger.info(f"Total questions parsed: {len(all_questions)}")
    logger.info(
        "Calling fix_math_json with raw questions:\n" + json.dumps({"questions": all_questions}, ensure_ascii=False,
                                                                   indent=2))
    fixed = fix_math_json({"questions": all_questions})
    return fixed



@app.post("/split-multiple-choice-questions/", tags=["Python Parser"])
async def split_questions_api(
    file: UploadFile = File(...),
    subject_name: str = Form(..., description="Название предмета"),
    subject_namekz: str = Form(..., description="Название предмета на казахском"),
    language: str = Form("рус", description="Язык задания"),
    klass: str = Form(..., description="Класс, например '10 ЕМН'"),
    tip: int = Form(1, description="Тип задания (целое число)")
):
    tmp = tempfile.mkdtemp()
    filename = (file.filename or "input.docx").replace(' ', '_')
    src = os.path.join(tmp, filename)
    docname = os.path.splitext(filename)[0]
    try:
        # 1) Сохраняем загруженный .docx во временную папку
        with open(src, "wb") as f:
            f.write(await file.read())

        # 2) Разбираем документ на вопросы и извлекаем медиа
        try:
            raw_list = split_questions_logic(src)

            # Перемещаем извлечённые изображения в static/img/<docname>
            media_dir = os.path.join(tmp, "media")
            target_img_dir = os.path.join(IMG_DIR, docname)
            os.makedirs(target_img_dir, exist_ok=True)

            if os.path.isdir(media_dir):
                for root, _, files in os.walk(media_dir):
                    for fname in files:
                        src_img = os.path.join(root, fname)
                        fname_clean = fname.replace(' ', '_')
                        dst_img = os.path.join(target_img_dir, fname_clean)
                        shutil.copy2(src_img, dst_img)

        except Exception as e:
            logger.error("Ошибка при split_questions_logic: %s", e)
            raise HTTPException(status_code=400, detail=str(e))

        # 3) Прогоним через пайплайн для multiple choice
        states = [pipeline_mcq(raw_item) for raw_item in raw_list]

        # 4) Собираем итоговые строки
        subject = {"name": subject_name, "namekz": subject_namekz}
        db_rows = build_rows_with_placeholders(states, subject, language, klass, tip)

        # 5) Чистим LaTeX/математические выражения
        for row in db_rows:
            row["vopros"] = clean_math_and_sub(row.get("vopros", ""))
            row["exp"] = clean_math_and_sub(row.get("exp", ""))
            row["otvety"] = [clean_math_and_sub(opt) for opt in row.get("otvety", [])]

        return {"questions": db_rows}

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

@app.post("/split-matching-questions/", tags=["Python Parser"])
async def split_questions_api(
    file: UploadFile = File(...),
    subject_name: str = Form(..., description="Название предмета"),
    subject_namekz: str = Form(..., description="Название предмета на казахском"),
    language: str = Form("рус", description="Язык задания"),
    klass: str = Form(..., description="Класс, например '10 ЕМН'"),
    tip: int = Form(1, description="Тип задания (целое число)")
):
    tmp = tempfile.mkdtemp()
    filename = (file.filename or "input.docx").replace(' ', '_')
    src = os.path.join(tmp, filename)
    docname = os.path.splitext(filename)[0]
    try:
        # 1) Сохраняем загруженный .docx во временную папку
        with open(src, "wb") as f:
            f.write(await file.read())

        # 2) Разбираем документ на вопросы и извлекаем медиа
        try:
            raw_list = split_questions_logic(src)

            # Перемещаем все извлечённые Pandoc'ом медиа-файлы в static/img/<docname>
            media_dir = os.path.join(tmp, "media")
            target_img_dir = os.path.join(IMG_DIR, docname)
            os.makedirs(target_img_dir, exist_ok=True)

            if os.path.isdir(media_dir):
                for root, _, files in os.walk(media_dir):
                    for fname in files:
                        src_img = os.path.join(root, fname)
                        fname_clean = fname.replace(' ', '_')
                        dst_img = os.path.join(target_img_dir, fname_clean)
                        shutil.copy2(src_img, dst_img)

        except Exception as e:
            logger.error("Ошибка при split_questions_logic: %s", e)
            raise HTTPException(status_code=400, detail=str(e))

        # 3) Прогоним через нашу ML-пайплайн логику
        states = [pipeline_matching(raw_item) for raw_item in raw_list]

        # 4) Собираем итоговые строки
        subject = {"name": subject_name, "namekz": subject_namekz}
        db_rows = build_rows_with_placeholders(states, subject, language, klass, tip)

        # 5) Чистим математические выражения
        for row in db_rows:
            row["vopros"] = clean_math_and_sub(row.get("vopros", ""))
            row["exp"]    = clean_math_and_sub(row.get("exp", ""))
            for group_name, opts in row.get("otvety", {}).items():
                for key, val in list(opts.items()):
                    opts[key] = clean_math_and_sub(val)

        return {"questions": db_rows}

    finally:
        shutil.rmtree(tmp, ignore_errors=True)