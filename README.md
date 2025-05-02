PROJECT: DOCX → Structured Questions API

DESCRIPTION
-----------
This FastAPI service accepts a DOCX file containing math questions formatted with delimiters,
extracts each question block, parses out the question text and answer options,
and returns a JSON payload with structured question data.


PREREQUISITES
-------------
1. Python 3.8 or newer
2. Pandoc installed and on your PATH
   Download: https://pandoc.org/installing.html

INSTALLATION
------------
# Clone the repo
git clone https://github.com/HimekawaRa/TECHNO-PARTNERS.git
cd TECHNO-PARTNERS

# (Optional) create & activate a virtual environment
python -m venv venv
# Windows: venv\Scripts\activate
# macOS/Linux: source venv/bin/activate

# Install Python dependencies
pip install -r requirements.txt

USAGE
-----
# Start the server
uvicorn app.main:app --reload

# Submit a request
POST http://127.0.0.1:8000/split_by_delimiter
Content-Type: multipart/form-data
Form-Field: file  (the .docx file)

# Example with curl:
curl -X POST "http://127.0.0.1:8000/split_by_delimiter" \
  -F "file=@/path/to/questions.docx"

# Response: JSON with "questions" array
{
  "questions": [
    {
      "question": "1.Задана функция ...",
      "options": "A) ...\nB) ...\nC) ...\nD) ...",
      // (additional parsed fields...)
    },
    ...
  ]
}

FILES
-----
app/
  main.py        — FastAPI application
requirements.txt — pinned Python dependencies


AUTHOR
------
HimekawaRa / TECHNO-PARTNERS
