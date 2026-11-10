"""方案文档导出：Markdown → Word(.docx) / PDF。"""
from .blocks import parse_markdown  # noqa: F401
from .docx import md_to_docx
from .pdf import md_to_pdf

__all__ = ["md_to_docx", "md_to_pdf", "parse_markdown"]


def export_proposal(md_text: str, docx_path: str, pdf_path: str, style: dict = None) -> dict:
    """同时导出 Word 与 PDF，返回各自路径。style 传项目采集到的模版样式，缺省用默认公文体例。"""
    md_to_docx(md_text, docx_path, style)
    md_to_pdf(md_text, pdf_path, style)
    return {"docx": docx_path, "pdf": pdf_path}
