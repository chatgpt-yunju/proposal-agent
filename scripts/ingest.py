"""命令行入库工具：python scripts/ingest.py --tier global <文件...>"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db
from app.kb.models import Scope
from app.kb.router import KnowledgeBase


def main() -> None:
    ap = argparse.ArgumentParser(description="将文件入库到指定三级知识库层级")
    ap.add_argument("--tier", choices=["global", "project", "chapter"], required=True)
    ap.add_argument("--project-id", default=None, help="project/chapter 级必填")
    ap.add_argument("--chapter-id", default=None, help="chapter 级必填")
    ap.add_argument("files", nargs="+", help="要入库的文档路径（txt/md/csv/pdf/docx）")
    a = ap.parse_args()

    try:
        Scope(tier=a.tier, project_id=a.project_id, chapter_id=a.chapter_id).validate()
    except ValueError as e:
        print(f"参数错误：{e}")
        sys.exit(1)

    db.init_db()
    kb = KnowledgeBase()
    total = 0
    for f in a.files:
        n = kb.ingest_file(f, a.tier, a.project_id, a.chapter_id)
        print(f"  + {f} -> {n} chunks")
        total += n
    print(f"已入库：{total} chunks；当前统计：{kb.stats()}")


if __name__ == "__main__":
    main()
