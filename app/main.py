# main.py
# -*- coding: utf-8 -*-

import os
import shutil
import tempfile
import logging
import json
from pathlib import Path

import pypandoc
import openai
import uvicorn
from fastapi import FastAPI, File, UploadFile, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv

# -----------------------
# Load environment
# -----------------------
load_dotenv()
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_KEY:
    raise RuntimeError("Please set OPENAI_API_KEY in your environment or .env")
openai.api_key = OPENAI_KEY

# -----------------------
# Logging
# -----------------------
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# -----------------------
# Pydantic response model
# -----------------------
class JSONResponseModel(BaseModel):
    parsed: dict

# -----------------------
# System prompt
# -----------------------
SYSTEM_PROMPT = """
You are an assistant that receives a Markdown document containing text and LaTeX math (delimited by \\(...\\)),
and you must output a single JSON object (no extra text) following this schema:

{
  "questions": [
    {
      "question": "<string>",
      "options": { "A": "<string>", "B": "<string>", ... },
      "section":   { "id": <int>, "name": "<string>" },
      "topic":     { "id": <int>, "name": "<string>" },
      "subject":   "<string>",
      "class":     "<string>",
      "quarter":   <int>,
      "language":  "<string>",
      "task_form": "<string>",
      "explanation":"<string>",
      "textbook":  "<string>",
      "goal":      "<string>",
      "points":    <int>,
      "correct_answer":"<string or list>",
      "level":     "<string>",
      "direction": "<string>"
    }
    // ... more question objects
  ]
}

CRITICAL:
- Return ONLY valid JSON, starting with '{' and ending with '}', no markdown fences.
- Preserve all LaTeX math exactly as in the Markdown input.
- Use "options" as an object mapping labels ("A","B", etc.) to their corresponding choice text.
- If a field is missing, set it to an empty string, empty object, or zero as appropriate.

Here is the exact JSON schema example for one question:

{
  "questions": [
    {
      "question": "Задана функция \\(f(x) = x + \\tfrac{1}{x}\\). Укажите промежутки убывания данной функции.",
      "options": [
        "[-1; 0)∪(0; 1]",
        "[-1; 1]",
        "(-∞; -1]∪[1; +∞)",
        "(-∞; 0)∪(0; +∞)"
      ],
      "section": {
        "id": 11,
        "name": "Применение производной"
      },
      "topic": {
        "id": 216,
        "name": "Критические точки и точки экстремума функции"
      },
      "subject": "Алгебра и начала анализа",
      "class": "10 ЕМН",
      "quarter": 4,
      "language": "русский",
      "task_form": "выбор одного правильного из четырех предложенных вариантов ответов",
      "explanation": "\\[f(x) = x + \\tfrac{1}{x}\\]; \\[f'(x) = 1 - \\tfrac{1}{x^2}\\]; \\[f'(x)=0 → x=±1, x≠0\\]. При \\(x∈[-1;0)∪(0;1]\\) функция убывает.",
      "textbook": "Алгебра и начала анализа. Абылкасымова А.Е., Часть 2, §47, стр. 97",
      "goal": "10.4.1.26- Знать необходимое и достаточное условие убывания функции; 10.4.1.27- Находить интервалы убывания",
      "points": 1,
      "correct_answer": "A",
      "level": "A",
      "direction": "3"
    }
  ]
}
"""

# -----------------------
# FastAPI app
# -----------------------
app = FastAPI(
    title="DOCX/HTML → Markdown+LaTeX → GPT JSON Parser",
    description="Uploads a .docx or .html, converts via pandoc to markdown with LaTeX math, then calls OpenAI to extract structured JSON."
)

# -----------------------
# Conversion helper
# -----------------------
def convert_to_markdown_with_latex(path: str, fmt: str) -> str:
    if not shutil.which("pandoc"):
        raise RuntimeError("Pandoc not found in PATH.")
    try:
        md = pypandoc.convert_file(
            path,
            to="markdown+tex_math_dollars",
            format=fmt,
            extra_args=["--wrap=none", "--strip-comments"]
        )
    except Exception as e:
        logger.exception("Pandoc conversion failed")
        raise RuntimeError(f"Pandoc conversion error: {e}")
    return md.replace("\r\n", "\n").replace("\r", "\n").replace("\x0c", "")

# -----------------------
# Endpoint
# -----------------------
@app.post("/parse-json", response_model=JSONResponseModel)
async def parse_document(file: UploadFile = File(..., description="Upload a .docx or .html file")):
    suffix = Path(file.filename).suffix.lower()
    if suffix not in {".docx", ".html", ".htm"}:
        raise HTTPException(status_code=400, detail="Unsupported file type, only .docx/.html")
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        content = await file.read()
        tmp.write(content)
        tmp.close()

        fmt = "html" if suffix in {".html", ".htm"} else "docx"
        markdown = convert_to_markdown_with_latex(tmp.name, fmt)
        logger.info("Converted to Markdown, length=%d", len(markdown))

        # --- Запрос к модели ---
        response = openai.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": markdown}
            ],
            temperature=0.0
        )
        logger.debug("OpenAI raw response object:\n%s", response)

        parsed_str = response.choices[0].message.content or ""
        logger.debug("AI returned (raw):\n%s", parsed_str)

        # Если отрезало по длине, пытаемся продолжить
        if response.choices[0].finish_reason == "length":
            logger.warning("AI JSON truncated, requesting continuation...")
            more = openai.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": "Your previous JSON was truncated. Please continue the JSON object, with no extra text."},
                    {"role": "assistant", "content": parsed_str}
                ],
                temperature=0.0
            )
            logger.debug("Continuation raw:\n%s", more.choices[0].message.content)
            parsed_str += more.choices[0].message.content or ""

        # Пытаемся напрямую распарсить
        try:
            parsed_dict = json.loads(parsed_str)
        except json.JSONDecodeError as e:
            logger.error("Failed to parse JSON from AI: %s", e)
            # Логируем полную «грязную» строку
            logger.debug("Raw AI output for salvage:\n%s", parsed_str)

            # Пытаемся вырезать JSON по первым/последним фигурным скобкам
            raw = parsed_str.strip()
            first = raw.find("{")
            last = raw.rfind("}")
            if first != -1 and last != -1 and last > first:
                candidate = raw[first:last+1]
                logger.debug("Attempting salvage JSON substring:\n%s", candidate)
                try:
                    parsed_dict = json.loads(candidate)
                except Exception as e2:
                    logger.error("Salvage JSON also failed: %s", e2)
                    raise HTTPException(status_code=502, detail="Invalid JSON from AI after salvage")
            else:
                raise HTTPException(status_code=502, detail="Invalid JSON from AI and no JSON delimiters found")

        return {"parsed": parsed_dict}

    except RuntimeError as e:
        logger.error("Processing error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    except openai.OpenAIError as e:
        logger.error("OpenAI API error: %s", e)
        raise HTTPException(status_code=502, detail=f"OpenAI error: {e}")
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass

@app.get("/")
async def root():
    return {"message": "Upload .docx or .html to /parse-json"}

# -----------------------
# Run locally
# -----------------------
if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
