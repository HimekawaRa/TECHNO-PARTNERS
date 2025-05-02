import re, sys
from playwright.sync_api import sync_playwright
from docx import Document

def split_questions(doc_path):
    doc = Document(doc_path)
    chunks, current = [], []
    header = re.compile(r'^\d+\.?\s*задани', re.IGNORECASE)
    for p in doc.paragraphs:
        t = p.text.strip()
        if header.match(t):
            if current: chunks.append(current)
            current = [t]
        else:
            if current: current.append(t)
    if current: chunks.append(current)
    return chunks

def render_with_playwright(chunks, out_dir, prefix="question"):
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        for i, chunk in enumerate(chunks, 1):
            html = "<html><head><meta charset='utf-8'><style>"
            html += "body{font-family:Arial;padding:20px;}p{margin:0 0 10px}"
            html += "</style></head><body>"
            html += "".join(f"<p>{line}</p>" for line in chunk)
            html += "</body></html>"
            page.set_content(html, wait_until="networkidle")
            path = f"{out_dir}\\{prefix}{i}.png"
            page.screenshot(path=path, full_page=True)
            print(f"[{i}/{len(chunks)}] saved → {path}")
        browser.close()

def main():
    if len(sys.argv)<2:
        print("Usage: python test.py path/to/input.docx"); sys.exit(1)
    doc_path = sys.argv[1]
    out_dir = r"C:\Users\user\PycharmProjects\TECHNO-PARTNERS"
    chunks = split_questions(doc_path)
    render_with_playwright(chunks, out_dir)

if __name__=="__main__":
    main()
