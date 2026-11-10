"""API 冒烟测试：用 TestClient 走通 建项目 → 建章节 → 三级入库 → 检索 → 统计。"""
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from app.config import settings

BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "samples")

# 先清空数据，再创建应用（KB 客户端在导入 app.main 时才打开向量库目录，避免旧目录残留）
for p in (settings.resolved_vector_dir, settings.resolved_db_path):
    if os.path.isdir(p):
        shutil.rmtree(p, ignore_errors=True)
    elif os.path.exists(p):
        os.remove(p)

from app.main import app  # noqa: E402

with TestClient(app) as client:
    print("health:", client.get("/health").json())

    p = client.post("/api/projects", json={"name": "XX大学智慧校园", "description": "demo"}).json()
    pid = p["id"]
    print("project:", pid)

    c1 = client.post(f"/api/projects/{pid}/chapters", json={"code": "03", "title": "技术架构方案"}).json()
    c2 = client.post(f"/api/projects/{pid}/chapters", json={"code": "07", "title": "项目实施计划"}).json()
    print("chapters:", c1["id"], c2["id"])

    for tier, d, extra in (("global", "global", {}),
                           ("project", "project", {"project_id": pid}),
                           ("chapter", "chapter", {"project_id": pid, "chapter_id": c1["id"]})):
        files = [("files", (fname, open(os.path.join(BASE, d, fname), "rb"), "text/markdown"))
                 for fname in os.listdir(os.path.join(BASE, d))]
        r = client.post("/api/kb/ingest",
                        data={"tier": tier, **extra},
                        files=files).json()
        print(f"ingest {tier}:", r)

    r = client.post("/api/kb/retrieve", json={
        "query": "系统采用微服务与容器化部署并满足等保2.0安全要求",
        "project_id": pid, "chapter_id": c1["id"], "top_k": 3,
    }).json()
    print("retrieve tech:", [(c["tier"], c["source"]) for c in r["citations"]])

    print("stats:", client.get("/api/kb/stats").json())
    print("SMOKE_OK")
