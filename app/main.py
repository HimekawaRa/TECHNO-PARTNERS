import os
import shutil
import tempfile
import logging
import json
from pathlib import Path
from typing import Union, List, Dict

import pypandoc
import openai
import uvicorn
from fastapi import FastAPI, File, UploadFile, HTTPException
from pydantic import BaseModel, Field, ValidationError
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
# Pydantic Models
# -----------------------
class Section(BaseModel):
    id: int
    name: str

class Topic(BaseModel):
    id: int
    name: str

class Question(BaseModel):
    question: str
    options: Dict[str, str]
    section: Section
    topic: Topic
    subject: str
    class_: str = Field(..., alias="class")
    quarter: int
    language: str
    task_form: str
    explanation: str
    textbook: str
    goal: str
    points: int
    correct_answer: Union[str, List[str], Dict[str, str]]
    level: str
    direction: str

class ParsedData(BaseModel):
    questions: List[Question]

# -----------------------
# Helper functions
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

def replace_double_dashes(obj):
    if isinstance(obj, dict):
        return {k: replace_double_dashes(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [replace_double_dashes(v) for v in obj]
    elif isinstance(obj, str):
        return obj.replace("--", "-")
    return obj

def count_dollars(text):
    return text.count("$")

def has_unclosed_dollar(data):
    if isinstance(data, dict):
        return any(has_unclosed_dollar(v) for v in data.values())
    elif isinstance(data, list):
        return any(has_unclosed_dollar(item) for item in data)
    elif isinstance(data, str):
        return count_dollars(text=data) % 2 != 0
    return False

# -----------------------
# FastAPI app
# -----------------------
app = FastAPI(
    title="DOCX/HTML → Markdown+LaTeX → GPT JSON Parser",
    description="Uploads a .docx or .html, converts via pandoc to markdown with LaTeX math, then calls OpenAI to extract structured JSON."
)

SYSTEM_PROMPT = """
You are an assistant that receives a Markdown document containing text and LaTeX math (delimited by $...$ or \\(...\\)),
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
      "explanation":"<string>",  // Use '\\n' for each original line break in the explanation!
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
- Preserve ALL LaTeX math exactly as in the Markdown input, including both $...$ and \\(...\\) delimiters.
- In the "explanation" field, always use '\\n' for every original line break in the explanation. Do NOT join all lines together.
- The "explanation" field must preserve both formulas and regular explanation text.
- Do not omit or summarize textbook-style phrases like "Учебник Алгебра и начала анализа. Абылкасымова А.Е...."
- Always keep math formulas inside `$...$` or `\\(...\\)`, and plain text outside math mode.
- Example: `Объяснение: $M[X] = x_1 p_1 + ... = 3.4$\nУчебник Алгебра и начала анализа. Абылкасымова А.Е....`
- The explanation field is not only math — it must include regular textual content such as textbook references, descriptive sentences, etc., exactly as in the original.
- The "options" field must be an **object** with option labels ("A", "B", etc.) as keys, and the answer text as values.
- If a field is missing, set it to an empty string, empty object, or zero as appropriate.
- If you see a mathematical equation or expression, always wrap it entirely in a single pair of `$...$`. NEVER forget the closing `$`.
Additional instructions:
- Never change minus, dash, or hyphen characters to double hyphens (`--`). Keep single minuses for subtraction or negative numbers.
- If you see any mathematical formula, expression, or a variable with an exponent (such as x^2, a_n, sin(x), \frac{1}{x}, $M[X] = x_1 p_1 + x_2 p_2 + ...$ etc.), you MUST always wrap the entire formula in LaTeX math mode using dollar signs ($...$). For example, x^2 should be written as $x^2$, and x^3 - 12x + 1 as $x^3 - 12x + 1$. This applies to ALL fields in the JSON: question, options, explanation, etc. Never output a math formula as plain text without $...$.
- Do NOT invent or modify formulas; copy all math content exactly as in the input.
- Do NOT output Markdown code blocks (no ```json).
- Your output must be directly parseable as JSON.
- All formulas that starts from $ should end by $.
- At the end of each field (question, explanation, options, etc.), automatically ensure that:
  - If there is an unclosed LaTeX math expression starting with `$`, it MUST be closed with a matching `$`.
  - If a `$`-delimited expression is opened and not closed, append a closing `$` at the correct position before any non-math content starts (like textbook references or punctuation).

IMPORTANT:
- Every LaTeX formula must always be enclosed with matching delimiters:
  - Either use `$...$` for inline math, or `\\(...\\)` — but never mix them within one formula.
  - Never leave a formula with only one `$` or only one `\\(` — it must be closed.
- For example, this is VALID: `$f(x) = x^2 + 2x + 1$` or `\\(a_n = \\frac{1}{n}\\)`.
- This is INVALID: `$f(x) = x^2 + 2x + 1`, `\\(a_n = \\frac{1}{n}$`, or raw formula without any math delimiters.
- If you see a mathematical equation or expression, always wrap it entirely in a single pair of `$...$`. NEVER forget the closing `$`.
- NEVER output an incomplete LaTeX expression or leave math expressions outside of math mode.
- Any mathematical equation (even long ones like expectations, probability formulas, or expressions with ⋅, +, -, =, fractions, variables like x_1, p_1, etc.) MUST be fully enclosed in $...$ — even if they span multiple terms.
- Example: M[X] = x_1 p_1 + x_2 p_2 + ... = 3.4 MUST be converted into $M[X] = x_1 p_1 + x_2 p_2 + x_3 p_3 + x_4 p_4 + x_5 p_5 = 1\\cdot0.1 + 2\\cdot0.2 + 3\\cdot0.1 + 4\\cdot0.4 + 5\\cdot0.2 = 0.1 + 0.4 + 0.3 + 1.6 + 1 = 3.4$
- NEVER leave such formulas outside of LaTeX math mode. The entire equation — from M[X] = ... to the result — must be enclosed in a single pair of dollar signs $...$.

Example (for one question):

{
  "questions": [
    {
      "question": "Задана функция $f(x) = x + \\frac{1}{x}$. Укажите промежутки убывания данной функции.",
      "options": {
        "A": "[-1; 0)∪(0; 1]",
        "B": "[-1; 1]",
        "C": "(-∞; -1]∪[1; +∞)",
        "D": "(-∞; 0)∪(0; +∞)"
      },
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
      "explanation": "$f(x) = x + \\frac{1}{x}$;\n$f'(x) = 1 - \\frac{1}{x^2}$;\n$f'(x) = 0$;\n$1 - \\frac{1}{x^2} = 0$;\nx = ±1, x ≠ 0\n$y' < 0$ при $x\\in[-1; 0)\\cup(0; 1]$.\nТ.к. функция определена при $x=-1$ и $x=1$, эти значения включены в промежутки убывания.",
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

@app.post("/parse-json")
async def parse_document(file: UploadFile = File(...)):
    suffix = Path(file.filename).suffix.lower()
    if suffix not in {".docx", ".html", ".htm"}:
        raise HTTPException(status_code=400, detail="Unsupported file type")

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        tmp.write(await file.read())
        tmp.close()

        fmt = "html" if suffix in {".html", ".htm"} else "docx"
        markdown = convert_to_markdown_with_latex(tmp.name, fmt)

        MAX_RETRIES = 4
        retries = 0

        while True:
            response = openai.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": markdown}
                ],
                temperature=0.0
            )
            parsed_str = response.choices[0].message.content or ""

            try:
                parsed_dict = json.loads(parsed_str)
                parsed_dict = replace_double_dashes(parsed_dict)

                validated = ParsedData.model_validate(parsed_dict)
                if has_unclosed_dollar(parsed_dict):
                    raise ValueError("Unclosed $ math expression found")
                return validated

            except (ValidationError, ValueError, json.JSONDecodeError) as ve:
                logger.warning("Retry due to: %s", ve)
                if retries >= MAX_RETRIES:
                    raise HTTPException(status_code=422, detail="Validation failed after retries")
                retries += 1

    finally:
        os.unlink(tmp.name)

@app.get("/")
async def root():
    return {"message": "Upload .docx or .html to /parse-json"}

if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
