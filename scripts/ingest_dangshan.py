"""砀山县矿产资源规划：建项目/章节 + 按 全局/项目/章节 三级批量入库。

章节结构参考用户提供的祁门县实施方案正文（公文化 11 章骨架）+ 矿产资源规划业务映射。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db
from app.config import settings
from app.kb.models import Scope
from app.kb.router import KnowledgeBase

BASE = r"D:\fangsheng.yunjunet.cn\砀山县矿产资源规划基础资料\砀山县矿产资源规划基础资料"
DOC_TXT = r"D:\fangsheng.yunjunet.cn\proposal-agent\data\tmp\doc_txt"
QIMEN = r"D:\fangsheng.yunjunet.cn\祁门县全域土地综合整治工程实施方案_正文_v4_公文版_20261014.pdf"
PROJ_NAME = "砀山县矿产资源规划"

CHAPTERS = [
    ("01", "基本情况"),
    ("02", "总体要求"),
    ("03", "矿产资源现状与形势分析"),
    ("04", "矿产资源勘查规划"),
    ("05", "矿产资源开发利用与保护"),
    ("06", "绿色矿山建设与矿山地质环境保护"),
    ("07", "规划分区与管控"),
    ("08", "重大工程与实施安排"),
    ("09", "投资估算及资金筹措"),
    ("10", "预期效益"),
    ("11", "规划实施保障"),
]


def main():
    db.init_db()

    # 1. 项目
    proj = next((p for p in db.list_projects() if p["name"] == PROJ_NAME), None)
    if not proj:
        proj = db.create_project(PROJ_NAME, "安徽省宿州市砀山县矿产资源总体规划（2026—2030年）")
    pid = proj["id"]
    print(f"[项目] {proj['name']} id={pid}")

    # 2. 章节
    existing = {c["code"]: c for c in db.list_chapters(pid)}
    for code, title in CHAPTERS:
        if code not in existing:
            db.create_chapter(pid, code, title, "")
            print(f"[章节] 新增 {code} {title}")
    ch = {c["code"]: c for c in db.list_chapters(pid)}

    kb = KnowledgeBase()

    def ingest(path, tier, chapter_code=None):
        pid_ = None if tier == "global" else pid
        cid = ch[chapter_code]["id"] if chapter_code else None
        label = os.path.basename(path)
        doc_id = os.path.splitext(label)[0]
        try:
            n = kb.ingest_file(path, tier, pid_, cid, doc_id=doc_id, source_label=label)
            tgt = f"{tier}{(':'+chapter_code) if chapter_code else ''}"
            print(f"  [{tgt:12}] {label} -> {n} 块")
            return n
        except Exception as e:
            print(f"  [跳过] {label}: {e}")
            return 0

    # 文件归集：每个文件只入一次
    jobs = []  # (abs_path, tier, chapter_code|None)

    def scan(subdir, tier, chapter_code=None, exts=None):
        exts = exts or (".pdf", ".docx", ".xlsx", ".doc")
        d = os.path.join(BASE, subdir)
        if not os.path.isdir(d):
            return
        for root, _, files in os.walk(d):
            for f in files:
                if f.startswith("~$") or f.lower().startswith("."):
                    continue
                ext = os.path.splitext(f)[1].lower()
                if ext not in exts:
                    continue
                path = os.path.join(root, f)
                # .doc 用 LibreOffice 已转换的 txt 替代
                if ext == ".doc":
                    base = os.path.splitext(f)[0]
                    txt = os.path.join(DOC_TXT, base + ".txt")
                    if os.path.exists(txt):
                        jobs.append((txt, tier, chapter_code))
                    else:
                        print(f"  [无转换] {f}（未找到 {base}.txt）")
                    continue
                jobs.append((path, tier, chapter_code))

    # 全局知识库：编制指南 + 法规 + 案例
    scan("04编制指南", "global")
    scan("07法律法规", "global")
    scan("08案例材料", "global")
    # 祁门公文模板（案例）
    jobs.append((QIMEN, "global", None))

    # 项目知识库：上一轮规划 + 矿业权 + 上位规划 + 三区三线
    scan("01上一轮规划材料，砀山县矿产资源规划21-25年", "project")
    scan("02矿产公司提供的矿业权规划内容", "project")
    scan("03上位规划", "project")
    scan("06砀山县三区三线和变更调查数据", "project")

    # 章节知识库（强关联章节的文件，从 project 移入章节级避免重复）
    ch03 = os.path.join(BASE, "05砀山县国土空间总体规划",
                        "砀山县国土空间总体规划2021-2035年.pdf")
    ch04 = os.path.join(DOC_TXT, "3专题1 砀山县矿产资源调查评价与勘查.txt")
    ch05 = os.path.join(BASE, "01上一轮规划材料，砀山县矿产资源规划21-25年",
                        "规划文本", "4专题2 砀山县供需和开发利用与保护.docx")
    ch07 = os.path.join(BASE, "02矿产公司提供的矿业权规划内容",
                        "砀山县矿产资源勘查开采区划坐标.docx")
    for path, cc in ((ch03, "03"), (ch04, "04"), (ch05, "05"), (ch07, "07")):
        if os.path.exists(path):
            jobs.append((path, "chapter", cc))

    # 去重（同一文件只入一次）
    seen, uniq = set(), []
    for path, tier, cc in jobs:
        key = (os.path.basename(path), tier, cc)
        if key in seen:
            continue
        seen.add(key)
        uniq.append((path, tier, cc))

    print(f"\n=== 开始入库 {len(uniq)} 个文件 ===")
    total = 0
    from collections import Counter
    tier_count = Counter()
    for path, tier, cc in uniq:
        total += ingest(path, tier, cc)
        tier_count[tier] += 1
    print(f"\n[汇总] 共入库 {total} 块；按层级：{dict(tier_count)}；项目 id={pid}")
    stats = kb.stats()
    print(f"[统计] tiers={stats.get('tiers')}")


if __name__ == "__main__":
    main()
