"""完整诊断：独占运行时，dense/sparse 计数 + 每章召回数与来源层级。"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import db
from app.kb.router import KnowledgeBase

db.init_db()
kb = KnowledgeBase()
st = kb.stats()
print("qdrant_count(持久dense点):", st["qdrant_count"])
print("sparse_count(BM25内存块):", st["sparse_count"])
print("tiers:", st["tiers"])

proj = next(p for p in db.list_projects() if p["name"] == "砀山县矿产资源规划")
print("项目 id:", proj["id"])
total = 0
for ch in db.list_chapters(proj["id"]):
    q = f"{ch['title']} {ch['description']}".strip()
    cites = kb.retrieve(q, proj["id"], ch["id"], top_k=6)
    total += len(cites)
    tiers = sorted({c.tier for c in cites})
    print(f"[{ch['code']}] {ch['title'][:16]} -> {len(cites)} 条 {tiers}")
    for c in cites[:1]:
        print(f"      top rel={round(c.score,3)} [{c.tier}] {c.source[:42]}")
print("合计召回:", total)
