import os
import re
import aspose.words as aw

def split_and_render(docx_path: str, out_dir: str):
    os.makedirs(out_dir, exist_ok=True)

    doc = aw.Document(docx_path)
    header_re = re.compile(r'^\d+\.?\s*задани', re.IGNORECASE)

    # разбиваем на блоки параграфов
    blocks = []
    cur = []
    for para in doc.get_child_nodes(aw.NodeType.PARAGRAPH, True):
        text = para.to_string(aw.SaveFormat.TEXT).strip()
        if header_re.match(text):
            if cur:
                blocks.append(cur)
            cur = [para]
        else:
            if cur and text:
                cur.append(para)
    if cur:
        blocks.append(cur)

    # рендерим каждый блок
    for idx, paras in enumerate(blocks, start=1):
        new_doc = aw.Document()
        section = new_doc.sections[0]
        body = section.body

        # очистка body
        while body.has_child_nodes:
            body.remove_child(body.first_child)

        # копируем параграфы
        for p in paras:
            imported = new_doc.import_node(p, True)
            body.append_child(imported)

        # сохраняем в PNG
        out_png = os.path.join(out_dir, f"q{idx}.png")
        new_doc.save(out_png, aw.SaveFormat.PNG)
        print(f"[renderer] q{idx}.png saved")
