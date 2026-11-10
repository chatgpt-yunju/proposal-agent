"""M5 冒烟：TestClient 走通 静态前端 + 写作流生成 + 导出 Word/PDF。"""
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from app.config import settings

BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "samples")
for p in (settings.resolved_vector_dir, settings.resolved_db_path):
    if os.path.isdir(p):
        shutil.rmtree(p, ignore_errors=True)
    elif os.path.exists(p):
        os.remove(p)

from app.main import app  # noqa: E402

with TestClient(app) as c:
    # 静态前端
    idx = c.get("/")
    assert idx.status_code == 200 and "写方案智能体" in idx.text, "前端未正常返回"
    print("frontend /: 200 OK (包含页面标题)")

    # 建项目/章节
    proj = c.post("/api/projects", json={"name": "XX大学智慧校园", "description": "demo"}).json()
    pid = proj["id"]
    c1 = c.post(f"/api/projects/{pid}/chapters", json={"code": "03", "title": "技术架构方案"}).json()
    c2 = c.post(f"/api/projects/{pid}/chapters", json={"code": "07", "title": "项目实施计划"}).json()

    # 三级入库
    for tier, d, extra in (("global", "global", {}),
                           ("project", "project", {"project_id": pid}),
                           ("chapter", "chapter", {"project_id": pid, "chapter_id": c1["id"]})):
        files = [("files", (fname, open(os.path.join(BASE, d, fname), "rb"), "text/markdown"))
                 for fname in os.listdir(os.path.join(BASE, d))]
        c.post("/api/kb/ingest", data={"tier": tier, **extra}, files=files)
    print("三级入库完成")

    # 写作流生成
    r = c.post("/api/workflow/generate", json={"project_id": pid}).json()
    assert r["proposal"].strip() and len(r["chapters"]) == 2, "写作流未生成方案"
    assert all(os.path.exists(x) for x in (r["md_path"], r["docx_path"], r["pdf_path"])), "产物文件缺失"
    print(f"generate: {len(r['chapters'])} 章, 迭代 {r['iterations']} 轮, proposal={len(r['proposal'])}字")

    # 导出
    for fmt in ("docx", "pdf", "md"):
        resp = c.get(f"/api/export/{pid}/{fmt}")
        assert resp.status_code == 200, f"导出 {fmt} 失败"
        assert len(resp.content) > 0
        print(f"export {fmt}: {resp.status_code} ({len(resp.content)}B)")

    print("M5_OK")
