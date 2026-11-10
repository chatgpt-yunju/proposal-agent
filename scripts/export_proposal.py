"""M4 导出：把已生成的方案 Markdown 转为 Word(.docx) 与 PDF。

用法：.venv\\Scripts\\python.exe scripts\\export_proposal.py <方案.md>
产物：与输入同目录下 <方案>.docx / <方案>.pdf
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.export import export_proposal


def main() -> None:
    if len(sys.argv) < 2:
        print("用法：python scripts\\export_proposal.py <方案.md>")
        sys.exit(1)
    md_path = os.path.abspath(sys.argv[1])
    if not os.path.exists(md_path):
        print(f"文件不存在：{md_path}")
        sys.exit(1)
    with open(md_path, encoding="utf-8") as f:
        md_text = f.read()
    base = os.path.splitext(md_path)[0]
    out = export_proposal(md_text, base + ".docx", base + ".pdf")
    print(f"Word 已导出：{out['docx']}")
    print(f"PDF 已导出：{out['pdf']}")
    print("EXPORT_OK")


if __name__ == "__main__":
    main()
