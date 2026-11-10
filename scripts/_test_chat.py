"""单章链路验证：真实检索 + DeepSeek 生成，打印耗时与字数。"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import db
from app.kb.router import KnowledgeBase
from app.llm.chat import ApiChat

db.init_db()
kb = KnowledgeBase()
proj = next(p for p in db.list_projects() if p["name"] == "砀山县矿产资源规划")
ch = next(c for c in db.list_chapters(proj["id"]) if c["code"] == "05")
cites = kb.retrieve(f"{ch['title']} {ch['description']}", proj["id"], ch["id"], top_k=6)
print("召回引用:", len(cites))

chat = ApiChat("https://deepseek.yunjunet.cn/v1",
               os.environ.get("LLM_API_KEY", ""), "deepseek-flash")
t0 = time.time()
out = chat.generate_chapter(chapter=ch, project_name=proj["name"],
                             citations=[c.__dict__ for c in cites])
dt = time.time() - t0
print(f"耗时 {dt:.1f}s | 字数 {len(out)} | 含来源标注 {'【来源' in out}")
print("---- 前 600 字 ----")
print(out[:600])
