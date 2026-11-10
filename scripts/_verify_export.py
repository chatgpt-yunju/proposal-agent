"""校验导出 docx / pdf 的真实体量。"""
import os
base = r"D:\fangsheng.yunjunet.cn\proposal-agent\data\output"
docx = os.path.join(base, "砀山县矿产资源规划_方案.docx")
pdf = os.path.join(base, "砀山县矿产资源规划_方案.pdf")

from docx import Document
d = Document(docx)
paras = [p for p in d.paragraphs if p.text.strip()]
print("DOCX 非空段落:", len(paras), "| 表格数:", len(d.tables),
      "| 总字符:", sum(len(p.text) for p in d.paragraphs))

import fitz
doc = fitz.open(pdf)
txt = "".join(p.get_text() for p in doc)
print("PDF 页数:", doc.page_count, "| 提取字符:", len(txt))
print("PDF 含'来源'标注:", txt.count("来源"), "| 含章节08:", "重大工程" in txt,
      "| 含章节10:", "预期效益" in txt)
doc.close()
