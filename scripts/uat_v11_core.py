"""V1.1 升级的最小回归：项目档案 → 需求整理 → 大纲 → 章节 → 检查清单。

只测不依赖向量库与模型的纯逻辑部分，用临时 SQLite，跑完即删。
用法：.venv\\Scripts\\python.exe scripts\\uat_v11_core.py
"""
import json
import os
import sys
import tempfile

TMP_DB = os.path.join(tempfile.gettempdir(), "uat_v11_agent.db")
if os.path.exists(TMP_DB):
    os.remove(TMP_DB)
os.environ["DB_PATH"] = TMP_DB
os.environ["KB_VECTOR_BACKEND"] = "numpy"
os.environ["LLM_PROVIDER"] = "local"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db
from app.brief import build_outline, rule_brief, brief_prompt_payload
from app.checks import run_checks

PASS, FAIL = [], []


def ok(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if extra else ""))


db.init_db()

# --- 项目档案：分步保存不清空已有字段（PRD 4.1 验收：刷新后内容仍在）
pid = db.create_project("测试项目", "")["id"]
db.update_project_profile(pid, {"goal": "建成统一政务数据平台"})
db.update_project_profile(pid, {"duration": "三个月"})
prof = db.get_project_profile(pid)
ok("profile 分步保存在库", prof["goal"] == "建成统一政务数据平台" and prof["duration"] == "三个月")

# --- 需求整理：预算/周期缺失必须进待确认，不能自动承诺
brief = rule_brief(prof, "测试项目", [])
ok("待确认含预算缺失", any("预算" in p for p in brief["pending"]))
ok("待确认不含周期(已填)", not any("周期" in p for p in brief["pending"]))
saved = db.update_project_brief(pid, brief)
ok("brief 落库可恢复", saved["facts"] and saved["pending"])

# --- 大纲：按项目实际约束改写章节描述
saved = db.update_project_brief(pid, {**saved, "requirements": ["私有化部署", "三个月实施", "对接现有账号体系"]})
prof = db.update_project_profile(pid, {"deploy_env": "私有化部署", "budget": "300万元"})
outline = build_outline(prof, saved)
ok("大纲八章", len(outline) == 8)
ok("大纲响应部署约束", any("私有化部署" in c["description"] for c in outline))
ok("大纲响应目标", any("政务数据平台" in c["description"] for c in outline))
ok("大纲不是通用空话", len({c["description"] for c in outline}) == 8)

# --- 章节写入 + 大纲门禁字段
for ch in outline:
    db.create_chapter(pid, ch["code"], ch["title"], ch["description"])
ok("大纲默认未确认", db.get_outline_confirmed(pid) is False)
db.set_outline_confirmed(pid, True)
ok("确认后可生成", db.get_outline_confirmed(pid) is True)

chs = db.list_chapters(pid)
cid = chs[0]["id"]
db.update_chapter_content(cid, "本项目按私有化部署方式建设，三个月完成实施，并与现有账号体系对接。" * 3)
db.set_chapter_sources(cid, [{"doc_id": "d_x", "source": "需求书.pdf:第3页",
                              "title": "需求书", "text": "要求私有化部署，三个月完成", "score": 0.8}])
ok("来源按页落库", db.get_chapter_sources(cid)[0]["page"] == 3)

# --- 检查清单：需求覆盖 / 信息缺失 / 来源有效性 / 结构完整性 / 冲突
project = db.get_project(pid)
documents = [{"id": "d_x", "filename": "需求书.pdf", "status": "ready"}]
res = run_checks(project, prof, saved, db.list_chapters(pid), documents)
keys = [x["key"] for x in res["items"]]
ok("检查五类齐全", keys == ["coverage", "missing", "source", "structure", "conflict"], str(keys))
cov = next(x for x in res["items"] if x["key"] == "coverage")
ok("私有化部署判定已响应", any(c["requirement"] == "私有化部署" and c["status"] == "已响应"
                              for c in cov["details"]),
   json.dumps(cov["details"], ensure_ascii=False)[:120])
struct = next(x for x in res["items"] if x["key"] == "structure")
ok("未生成章节计入结构问题", any("尚未生成" in d["message"] for d in struct["details"]))
src = next(x for x in res["items"] if x["key"] == "source")
ok("来源无失效项", all(d.get("level") != "fail" for d in src["details"]))
ok("不出虚假评分", "score" not in json.dumps(res) and "分" not in next(
    x["summary"] for x in res["items"] if x["key"] == "coverage"))

# --- 单章重写留档 + 恢复
db.save_chapter_prev_content(cid, "旧稿正文")
db.update_chapter_content(cid, "新稿正文")
ok("重写保留原稿", db.get_chapter_prev_content(cid) == "旧稿正文")

# --- 写作上下文包含待确认，避免模型替甲方下结论
ctx = brief_prompt_payload(prof, saved)
ok("上下文含待确认", "待确认" in ctx and "建设目标" in ctx)

# --- 资料登记去重
d1 = db.add_kb_doc("d_a", "project", "需求书.pdf", project_id=pid, fingerprint="fp1")
ok("同指纹可被识别为重复",
   db.find_kb_doc_by_fingerprint("fp1", "project", pid, "") is not None)

print("\n==== 结果 ====")
print(f"通过 {len(PASS)}，失败 {len(FAIL)}")
if FAIL:
    print("失败项：" + "、".join(FAIL))
    sys.exit(1)
os.remove(TMP_DB)
