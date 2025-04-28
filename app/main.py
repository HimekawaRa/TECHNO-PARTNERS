# -*- coding: utf-8 -*-
import io
import os
import json
from typing import Dict, Any, List
from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import docx # pip install python-docx
import requests # pip install requests
# Для асинхронных запросов (лучше для FastAPI):
# import httpx # pip install httpx

# --- НАСТРОЙКИ ---
# !!! ВАЖНО: Замените на ваш реальный API ключ !!!
# Лучше всего загружать ключ из переменной окружения:
# AI_API_KEY = os.getenv("GOOGLE_API_KEY")
# Если переменная не задана, можно временно использовать ключ напрямую (НЕ РЕКОМЕНДУЕТСЯ для продакшена):
AI_API_KEY = "AIzaSyAa4_ILzOQMYbagymHh2d4t1z8P0mf-Pqs"


# Выберите модель и метод
MODEL_NAME = "gemini-1.5-flash-latest" # Актуальная быстрая модель
METHOD = "generateContent"
AI_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:{METHOD}?key={AI_API_KEY}"
# ---------------

app = FastAPI(title="AI DOCX Question Processor")

# Настройка CORS для разрешения запросов с локального HTML файла
origins = [
    "http://localhost",
    "http://localhost:8080", # Добавьте порт, если используете сервер для HTML
    "http://127.0.0.1",
    "http://127.0.0.1:8080",
    "null", # Для локальных файлов (file://)
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def extract_text_from_docx(content: bytes) -> str:
    """Извлекает полный текст из содержимого DOCX файла."""
    try:
        if not content:
            raise ValueError("Received empty content for DOCX extraction.")
        document = docx.Document(io.BytesIO(content))
        full_text = [para.text for para in document.paragraphs if para.text.strip()] # Игнорируем пустые параграфы
        if not full_text:
             raise ValueError("DOCX file contains no text paragraphs.")
        return '\n\n'.join(full_text) # Используем двойной перенос для лучшего разделения
    except Exception as e:
        print(f"Error extracting text from docx: {e}")
        # Проверяем, является ли файл действительно DOCX
        if "File is not a zip file" in str(e):
             raise HTTPException(status_code=400, detail=f"Invalid DOCX file format.")
        raise HTTPException(status_code=500, detail=f"Could not read DOCX file content: {e}")

async def process_text_with_ai(text_content: str) -> List[Dict[str, Any]]:
    """
    Отправляет текст и промпт в Google AI API и получает JSON.
    """
    if not AI_API_KEY or AI_API_KEY == "ВАШ_API_КЛЮЧ_ЗДЕСЬ":
         raise HTTPException(status_code=500, detail="Google AI API Key is not configured on the server.")

    headers = {
        "Content-Type": "application/json",
    }

    # Промпт для ИИ (можно доработать)
    prompt = f"""
    Проанализируй следующий текст, который является набором учебных заданий (например, по математике).
    Извлеки КАЖДОЕ задание отдельно. Для каждого задания сформируй JSON-объект со следующими полями:
    - "question": (string) Полный текст вопроса. Обязательное поле.
    - "options": (object) JSON-объект (словарь) с вариантами ответа. Ключ - буква варианта (A, B, C...). Значение - текст варианта. Если вариантов нет (например, задание на сопоставление или открытый ответ), должно быть пустым объектом {{}}. Обязательное поле.
    - "explanation": (string) Текст объяснения решения, если он есть.
    - "correct_answer": (string) Строка с правильным(и) ответом(ами) или ключом к ответу.
    - "difficulty_level": (string) Уровень сложности (например, "A", "B", "C"), если указан.
    - "section": (object) Объект вида {{ "id": number, "name": "string" }}, если указан раздел.
    - "topic": (object) Объект вида {{ "id": number, "name": "string" }}, если указана тема.
    - "subject": (string) Название предмета, если указано.
    - "class_": (string) Класс или группа, если указаны.
    - "score": (number) Количество баллов за задание, если указано.

    КРАЙНЕ ВАЖНЫЕ ПРАВИЛА ФОРМАТИРОВАНИЯ ОТВЕТА:
    1.  Твой ответ ДОЛЖЕН быть валидным JSON-массивом (списком объектов Python), где каждый элемент - это JSON-объект одного задания.
    2.  НЕ ДОБАВЛЯЙ никакого описательного текста до или после JSON-массива. Только сам массив, начинающийся с `[` и заканчивающийся `]`.
    3.  ВСЕГДА используй двойные кавычки `"` для всех ключей и ВСЕХ строковых значений в JSON. Одинарные кавычки НЕДОПУСТИМЫ.
    4.  ЭКРАНИРОВАНИЕ СИМВОЛОВ ВНУТРИ СТРОК JSON:
        * Если внутри строки JSON нужно использовать символ двойной кавычки `"`, экранируй его как `\\"`.
        * Если внутри строки JSON нужно использовать символ обратной косой черты `\\`, экранируй его как `\\\\`. Это ОСОБЕННО ВАЖНО для разметки MathJax/LaTeX! Например, `\\frac` в тексте вопроса должно стать `"\\\\frac"` внутри JSON-строки. `\\(` должно стать `"\\\\("`. `\\[` должно стать `"\\\\["`.
        * Символы новой строки внутри одного строкового значения JSON представляй как `\\n`.
    5.  MATHJAX/LATEX: Для ВСЕХ математических формул и символов используй разметку MathJax: `\\(` и `\\)` для inline-формул, `\\[` и `\\]` для display-формул. Помни правило 4 про экранирование `\\` внутри JSON-строк!
    6.  Если какое-то необязательное поле отсутствует в тексте задания, не включай его ключ в JSON-объект для этого задания.

    Текст для анализа:
    ---
    {text_content}
    ---
    """

    # Формируем тело запроса (JSON payload)
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.5,
            "maxOutputTokens": 8192
        },
         "safetySettings": [ # Настройки безопасности (можно настроить)
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
        ]
    }

    print(f"Sending request to AI API URL: {AI_API_URL.split('?')[0]}...") # Не логгируем ключ

    try:
        # Используем синхронный requests. Для высокой нагрузки лучше перейти на async httpx
        response = requests.post(AI_API_URL, headers=headers, json=payload, timeout=240) # Увеличим таймаут еще больше
        response.raise_for_status() # Проверка на HTTP ошибки (4xx, 5xx)

        print(f"AI API Response Status: {response.status_code}")
        ai_response_json = response.json()

        # Анализ ответа от API Gemini
        if not ai_response_json.get('candidates'):
            # Если кандидатов нет, возможно, промпт был заблокирован целиком
            prompt_feedback = ai_response_json.get('promptFeedback', {})
            block_reason = prompt_feedback.get('blockReason', 'UNKNOWN')
            safety_ratings = prompt_feedback.get('safetyRatings', [])
            raise HTTPException(status_code=400, detail=f"AI prompt blocked. Reason: {block_reason}. Safety ratings: {safety_ratings}")

        # Извлекаем сгенерированный контент
        try:
             # Проверяем finishReason перед извлечением текста
             finish_reason = ai_response_json['candidates'][0].get('finishReason', 'UNKNOWN')
             if finish_reason not in ['STOP', 'MAX_TOKENS']: # Допускаем остановку по токенам
                  safety_ratings = ai_response_json['candidates'][0].get('safetyRatings', [])
                  # Если не STOP, то что-то пошло не так (SAFETY, RECITATION, OTHER)
                  raise HTTPException(status_code=500, detail=f"AI generation finished unexpectedly. Reason: {finish_reason}. Safety ratings: {safety_ratings}")

             generated_content = ai_response_json['candidates'][0]['content']['parts'][0]['text']
        except (KeyError, IndexError, TypeError) as e:
            print(f"Error extracting content from AI response structure: {e}")
            print(f"AI Response (structure causing error): {ai_response_json}")
            raise HTTPException(status_code=500, detail="Could not extract generated text from AI response.")

        # Парсим строку с JSON
        try:
            questions_list = json.loads(generated_content)
        except json.JSONDecodeError as e:
            print(f"Error parsing generated content string as JSON: {e}")
            print(f"Content that failed parsing:\n{generated_content}")
            # Попытка "очистить" JSON, если ИИ добавил ```json ... ```
            if generated_content.strip().startswith("```json"):
                 clean_content = generated_content.strip()[7:-3].strip()
                 try:
                     questions_list = json.loads(clean_content)
                     print("Successfully parsed after cleaning ```json``` markers.")
                 except json.JSONDecodeError:
                      print("Parsing failed even after cleaning markers.")
                      raise HTTPException(status_code=500, detail=f"AI returned invalid JSON string, even after cleaning: {e}")
            else:
                 raise HTTPException(status_code=500, detail=f"AI returned invalid JSON string: {e}")


        # Проверяем, что результат - это список
        if not isinstance(questions_list, list):
            print(f"Parsed data is not a list: {type(questions_list)}")
            raise HTTPException(status_code=500, detail="AI did not return the expected list structure.")

        print(f"Successfully parsed AI response. Found {len(questions_list)} questions.")
        return questions_list

    except requests.exceptions.Timeout:
         print("Error: AI API request timed out.")
         raise HTTPException(status_code=504, detail="Request to AI service timed out.")
    except requests.exceptions.RequestException as e:
        print(f"Error calling AI API: {e}")
        # Проверяем на ошибки аутентификации (401, 403)
        if e.response is not None:
             if e.response.status_code == 401 or e.response.status_code == 403:
                  raise HTTPException(status_code=401, detail=f"Authentication error with AI service. Check your API Key. Status: {e.response.status_code}")
             elif e.response.status_code == 429:
                  raise HTTPException(status_code=429, detail=f"Rate limit exceeded for AI service. Please try again later. Status: {e.response.status_code}")
        raise HTTPException(status_code=502, detail=f"Could not connect to AI service: {e}")
    except HTTPException as he:
        # Пробрасываем HTTP ошибки, которые мы сгенерировали сами
        raise he
    except Exception as e:
        print(f"An unexpected error occurred in process_text_with_ai: {e}")
        import traceback
        traceback.print_exc() # Печатаем полный traceback для диагностики
        raise HTTPException(status_code=500, detail=f"An unexpected internal error occurred: {e}")


# --- FastAPI Эндпоинт ---
@app.post("/process_docx_ai")
async def process_docx_with_ai(file: UploadFile = File(...)) -> Dict[str, Any]:
    """
    Принимает DOCX файл, извлекает текст, отправляет его в Google AI API
    для анализа и структурирования, возвращает результат в виде JSON.
    """
    print(f"Received file request: filename='{file.filename}', content_type='{file.content_type}'")

    # Проверка типа файла
    if not file.content_type in [
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword", # Старый формат .doc (python-docx может его не поддерживать)
        "application/octet-stream" # Общий тип, если браузер не определил
        ]:
        print(f"Rejected file type: {file.content_type}")
        raise HTTPException(
            status_code=400,
            detail=f"Invalid file type: '{file.content_type}'. Please upload a DOCX file."
        )
    # Дополнительная проверка по расширению файла
    if file.filename and not file.filename.lower().endswith('.docx'):
         print(f"Rejected file extension: {file.filename}")
         raise HTTPException(
            status_code=400,
            detail=f"Invalid file extension. Please upload a file with the .docx extension."
        )


    try:
        content = await file.read()
        print(f"File size: {len(content)} bytes")
        if not content:
            raise HTTPException(status_code=400, detail="Uploaded file is empty.")

        # 1. Извлекаем текст из DOCX
        print("Extracting text from DOCX...")
        text_content = extract_text_from_docx(content)
        print(f"Extracted text length: {len(text_content)} characters.")
        # print(f"Extracted text (first 500 chars):\n{text_content[:500]}\n---") # Для отладки

        # 2. Отправляем текст ИИ для обработки
        print("Processing text with AI...")
        questions_list = await process_text_with_ai(text_content)

        # 3. Возвращаем результат
        print(f"Successfully processed. Returning {len(questions_list)} questions.")
        return {"questions": questions_list}

    except HTTPException as he:
        # Перехватываем и логируем HTTP ошибки перед тем, как вернуть их клиенту
        print(f"HTTP Exception occurred: Status={he.status_code}, Detail={he.detail}")
        raise he
    except Exception as e:
        print(f"Internal Server Error occurred in /process_docx_ai endpoint: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"An internal server error occurred: {e}")

@app.get("/")
async def read_root():
    """ Корневой эндпоинт для проверки работы API """
    return {"message": "AI DOCX Question Processor API is running."}

# --- Запуск приложения (если файл запускается напрямую) ---
if __name__ == "__main__":
    import uvicorn
    print("Starting Uvicorn server...")
    # Запуск uvicorn напрямую из скрипта (удобно для отладки)
    # В продакшене лучше запускать командой: uvicorn main_ai:app --host 0.0.0.0 --port 8000
    uvicorn.run(app, host="127.0.0.1", port=8000)