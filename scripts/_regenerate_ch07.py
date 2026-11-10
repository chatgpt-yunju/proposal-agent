"""针对第 07 章「规划分区与管控」的定向重生成：
从运行中的服务取检索引用 -> deepseek-flash 生成 -> 拼接回 md -> 重新导出 docx/pdf。
若再次走降级路径则中止，不覆盖原产物。
"""
import os
import re
import sys

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

API = "http://127.0.0.1:8000"
BASE = "https://deepseek.yunjunet.cn/v1"
KEY = os.environ.get("LLM_API_KEY", "")
MODEL = "deepseek-flash"
PROJ = "p_6f84565655cb"
CH = "c_c735624fd254"
OUT_MD = r"D:\fangsheng.yunjunet.cn\proposal-agent\data\output\砀山县矿产资源规划_方案.md"

# 1. 检索引用（走运行中服务的 API，避免 Qdrant 双进程争锁）
r = httpx.post(f"{API}/api/kb/retrieve",
               json={"query": "规划分区与管控", "project_id": PROJ, "chapter_id": CH, "top_k": 6},
               timeout=30)
r.raise_for_status()
citations = r.json()["citations"]
print("召回引用:", len(citations))
if not citations:
    print("ABORT: 无检索引用")
    sys.exit(1)

# 2. 模型生成
from app.llm.chat import ApiChat  # noqa: E402

chat = ApiChat(BASE, KEY, MODEL)
out = chat.generate_chapter(
    chapter={"code": "07", "title": "规划分区与管控", "description": ""},
    project_name="砀山县矿产资源规划",
    citations=citations,
)
print("生成字数:", len(out), "| 含来源标注:", "【来源" in out)
if "模型响应超时" in out:
    print("ABORT: 生成再次失败，未覆盖原产物")
    sys.exit(1)

# 3. 拼接回 md
md = open(OUT_MD, encoding="utf-8").read()
start = re.search(r"(?m)^## 07 ", md).start()
end = re.search(r"(?m)^## 08 ", md).start()
md_new = md[:start] + out.strip() + "\n\n" + md[end:]
open(OUT_MD, "w", encoding="utf-8").write(md_new)

# 4. 重新导出 docx/pdf
from app.export import export_proposal  # noqa: E402

export_proposal(md_new, OUT_MD[:-3] + ".docx", OUT_MD[:-3] + ".pdf")

# 5. 验证
sec = md_new[re.search(r"(?m)^## 07 ", md_new).start():re.search(r"(?m)^## 08 ", md_new).start()]
print("验证 -> 新第07章字数:", len(sec), "| fallback标记:", "模型响应超时" in sec,
      "| 含来源标注:", "【来源" in sec)
