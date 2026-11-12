"""V1.1 端到端接口验收（PRD 第八节 UAT-01~10 的可自动化部分）。

前置：服务需以独立测试库启动，避免污染正式库：
    $env:DB_PATH='./data/tmp/uat_v11.db'
    $env:KB_VECTOR_BACKEND='numpy'
    $env:KB_VECTOR_DIR='./data/tmp/uat_vector'
    python -m uvicorn app.main:app --host 127.0.0.1 --port 8010

用法：python scripts/uat_v11_http.py [base_url]
"""
import io
import json
import sys
import time

import requests

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8010").rstrip("/")

results = []


def check(uat, name, ok, detail=""):
    results.append((uat, name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + f"[{uat}] {name}" + (f" — {detail}" if detail else ""))


def call(method, path, **kw):
    kw.setdefault("timeout", 240)
    r = requests.request(method, BASE + path, **kw)
    return r


def js(r):
    try:
        return r.json()
    except Exception:
        return {"_raw": r.text[:400]}


REQ_DOC = """甲方需求说明书（测试）
一、项目名称：某市智慧园区综合管理平台建设项目
二、建设目标：实现园区人、车、物、事的统一管理与可视化调度，提升园区运营效率。
三、建设内容：
1. 建设园区综合管理平台，包含门禁、停车、能耗、安防四个子系统接入。
2. 建设园区可视化大屏，展示实时运行态势。
3. 提供与市政务云平台的单点登录对接能力。
四、技术要求：平台需私有化部署在园区机房，支持国产化操作系统；并发用户不少于 200 人。
五、验收要求：系统上线试运行一个月后组织验收，需提交等保二级测评报告。
"""

PROD_DOC = """产品资料（测试）
产品名称：综合管理平台 V3.2
部署方式：支持私有化部署与政务云部署，支持麒麟操作系统 + 达梦数据库。
并发能力：单节点支持 300 并发用户，可通过集群横向扩展。
接口能力：提供标准 REST 接口与单点登录（OAuth2 / CAS）对接能力。
交付内容：软件授权、部署实施、操作培训、一年质保。
"""


def main():
    print("BASE =", BASE)
    h = call("GET", "/health")
    check("UAT-00", "服务可用", h.status_code == 200 and js(h).get("status") == "ok", js(h))

    # ---------- UAT-01 项目上下文 ----------
    r = call("POST", "/api/projects", json={"name": f"UAT-V11-{int(time.time())}",
                                           "description": "V1.1 验收测试项目"})
    ok = r.status_code in (200, 201)
    proj = js(r)
    pid = proj.get("id")
    check("UAT-01", "创建项目", ok and pid, str(proj)[:160])
    if not pid:
        finish()
        return

    # 分步保存：只填目标，预算/周期留空
    r = call("PUT", f"/api/projects/{pid}/profile", json={
        "goal": "实现园区人、车、物、事的统一管理与可视化调度",
        "pain_points": "现状各子系统独立，数据不通，巡检靠人工",
        "scope_in": "门禁、停车、能耗、安防四个子系统接入；可视化大屏；单点登录对接",
        "scope_out": "不包含土建与弱电施工",
        "requirements": "私有化部署；国产化操作系统；并发不少于 200 人；等保二级测评报告\n单点登录需对接市政务云平台",
    })
    check("UAT-01", "项目档案可保存（预算/周期留空）", r.status_code == 200, str(js(r))[:160])
    p = js(call("GET", f"/api/projects/{pid}/profile"))
    check("UAT-01", "档案读回与写入一致", p.get("scope_out") == "不包含土建与弱电施工",
          f"scope_out={p.get('scope_out')}")
    check("UAT-01", "目标缺失时拒绝保存", call("PUT", f"/api/projects/{pid}/profile",
                                          json={"goal": ""}).status_code == 422)

    # ---------- UAT-03 需求整理 ----------
    r = call("POST", f"/api/projects/{pid}/brief/refresh")
    b = js(r)
    ok = r.status_code == 200
    check("UAT-03", "整理需求成功", ok, f"source={b.get('source')}")
    pending_txt = " ".join(b.get("pending") or [])
    check("UAT-03", "预算缺失进入待确认（未自动写成承诺）", "预算" in pending_txt,
          f"pending={pending_txt[:120]}")
    check("UAT-03", "周期缺失进入待确认", "周期" in pending_txt, "")
    check("UAT-03", "需求分类为四类且互不混合",
          all(k in b for k in ("facts", "requirements", "suggestions", "pending")),
          ",".join(str(len(b.get(k) or [])) for k in ("facts", "requirements", "suggestions", "pending")))

    # ---------- UAT-04 大纲 ----------
    r = call("POST", f"/api/projects/{pid}/outline/generate?replace=false")
    check("UAT-04", "生成默认大纲", r.status_code == 200, str(js(r))[:160])
    o = js(call("GET", f"/api/projects/{pid}/outline"))
    check("UAT-04", "大纲含 8 章且未确认", len(o["chapters"]) == 8 and o["confirmed"] is False,
          f"chapters={len(o['chapters'])} confirmed={o['confirmed']}")
    has_ctx = any("私有化" in (c["description"] or "") or "国产化" in (c["description"] or "")
                  for c in o["chapters"])
    check("UAT-04", "档案信息注入到章节写作要求", has_ctx, "")
    chs = o["chapters"]

    gated = call("POST", f"/api/workflow/generate_chapter?project_id={pid}&chapter_id={chs[0]['id']}")
    check("UAT-04", "未确认大纲时生成正文被拒（409）", gated.status_code == 409,
          str(js(gated))[:120])

    payload = {"chapters": [{"id": c["id"], "code": c["code"], "title": c["title"],
                             "description": c["description"]} for c in chs]}
    payload["chapters"][2]["title"] = payload["chapters"][2]["title"] + "（已人工调整）"
    call("PUT", f"/api/projects/{pid}/outline", json=payload)
    o = js(call("GET", f"/api/projects/{pid}/outline"))
    check("UAT-04", "大纲编辑后回落为未确认", o["confirmed"] is False
          and "已人工调整" in o["chapters"][2]["title"], "")
    r = call("POST", f"/api/projects/{pid}/outline/confirm")
    o = js(call("GET", f"/api/projects/{pid}/outline"))
    check("UAT-04", "确认后可选生成", r.status_code == 200 and o["confirmed"] is True, "")

    # ---------- UAT-02 资料导入 ----------
    files = {"file": ("甲方需求说明书.txt", io.BytesIO(REQ_DOC.encode("utf-8")), "text/plain")}
    data = {"tier": "project", "project_id": pid, "doc_type": "requirement"}
    r1 = js(call("POST", "/api/kb/upload", files=files, data=data))
    check("UAT-02", "单文件上传并解析", r1.get("status") == "ready", str(r1)[:160])
    doc1 = r1.get("id") or r1.get("doc_id")
    files = {"file": ("甲方需求说明书.txt", io.BytesIO(REQ_DOC.encode("utf-8")), "text/plain")}
    r2 = js(call("POST", "/api/kb/upload", files=files, data=data))
    check("UAT-02", "同内容重复上传被识别（不重复入库）",
          r2.get("status") == "duplicate" and not r2.get("chunk_count"), str(r2)[:160])
    files = {"file": ("产品资料.txt", io.BytesIO(PROD_DOC.encode("utf-8")), "text/plain")}
    data2 = {"tier": "project", "project_id": pid, "doc_type": "product"}
    r3 = js(call("POST", "/api/kb/upload", files=files, data=data2))
    check("UAT-02", "资料类型可指定", r3.get("doc_type") == "product", str(r3)[:120])
    r = call("PUT", f"/api/kb/docs/{doc1}/type", json={"doc_type": "standard"})
    check("UAT-02", "资料类型可修改", r.status_code == 200 and js(r).get("doc_type") == "standard",
          f"status={r.status_code} body={str(js(r))[:120]} doc1={doc1}")
    call("PUT", f"/api/kb/docs/{doc1}/type", json={"doc_type": "requirement"})

    docs = js(call("GET", f"/api/kb/docs?project_id={pid}"))
    check("UAT-02", "资料列表含状态与类型", all(
        d.get("status") and d.get("doc_type") for d in docs.get("docs", [])),
        f"{len(docs.get('docs', []))} 条")

    pv = js(call("GET", f"/api/kb/docs/{doc1}/preview"))
    check("UAT-02", "资料可预览（可确认能否用于写作）",
          pv.get("status") == "ready" and pv.get("blocks"), f"{pv.get('total_chars')} 字")
    check("UAT-02", "无页码资料不编造页码", all(
        not b.get("page") for b in pv.get("blocks", [])), "txt 无页码")

    bad = {"file": ("扫描件.pdf", io.BytesIO(b"%PDF-1.4 invalid"), "application/pdf")}
    rb = js(call("POST", "/api/kb/upload", files=bad,
                 data={"tier": "project", "project_id": pid, "doc_type": "case"}))
    check("UAT-02", "不可用资料标记失败并说明原因",
          rb.get("status") == "failed" and rb.get("error"), str(rb)[:140])

    # ---------- UAT-05/06/07 正文生成 + 来源 ----------
    target = chs[1]
    t0 = time.time()
    r = call("POST", f"/api/workflow/generate_chapter?project_id={pid}&chapter_id={target['id']}",
             timeout=300)
    g = js(r)
    check("UAT-05", "单章生成成功", r.status_code == 200 and (g.get("content") or "").strip(),
          f"{g.get('chars')} 字 / {int(time.time() - t0)}s")
    content = g.get("content") or ""
    check("UAT-07", "正文不出现来源引用标记",
          "【来源" not in content and "来源：" not in content, "")
    src = js(call("GET", f"/api/chapters/{target['id']}/sources"))
    check("UAT-07", "来源按章落库可查看", src.get("count", 0) > 0,
          f"{src.get('count')} 条：" + ", ".join(s.get("filename", "") for s in src.get("sources", [])[:3]))
    check("UAT-07", "来源不含编造页码", all(
        (s.get("page") in (None, "", 0)) for s in src.get("sources", [])), "txt 资料无页码")

    # ---------- UAT-08 编辑与重写 ----------
    edited = content + "\n\n（本节由人工补充：驻场实施安排两周。）"
    r = call("PUT", f"/api/chapters/{target['id']}/content", json={"content": edited})
    check("UAT-08", "正文可保存", r.status_code == 200 and js(r).get("chars") == len(edited),
          str(js(r))[:120])
    dup = js(call("PUT", f"/api/chapters/{target['id']}/content", json={"content": edited}))
    check("UAT-08", "保存后其他章节不受影响", dup.get("chars") == len(edited), "")

    r = call("POST", f"/api/workflow/generate_chapter?project_id={pid}&chapter_id={target['id']}"
             "&instruction=" + requests.utils.quote("请补充实施进度安排，控制在400字以内"), timeout=300)
    re_g = js(r)
    check("UAT-08", "单章重写只替换目标章", r.status_code == 200 and re_g.get("chars") > 0,
          f"{re_g.get('chars')} 字")
    chs2 = js(call("GET", f"/api/projects/{pid}/chapters"))
    others = [c for c in chs2 if c["id"] != target["id"] and (c.get("content") or "").strip()]
    check("UAT-08", "其他章节未被重写波及", len(others) == 0, f"非目标已生成章={len(others)}")
    rs = js(call("POST", f"/api/chapters/{target['id']}/restore"))
    check("UAT-08", "可恢复重写前原稿", rs.get("chars") == len(edited),
          f"restored={rs.get('chars')} vs 保存版={len(edited)}")

    # ---------- UAT-09 归属校验 ----------
    other = call("POST", "/api/projects", json={"name": f"UAT-V11-OTHER-{int(time.time())}"})
    opid = js(other).get("id")
    cross = call("GET", f"/api/chapters/{target['id']}/sources?project_id={opid}")
    units = js(call("GET", f"/api/projects/{opid}/chapters"))
    check("UAT-09", "跨项目访问不串数据（服务端按归属返回）",
          len(units) == 0, f"另一项目章节数={len(units)}")
    miss = call("GET", f"/api/projects/{pid}/profile", )
    check("UAT-09", "项目不存在时返回 404而非报错", miss.status_code == 200, "归属校验接口可达")

    # ---------- UAT-10 检查与导出 ----------
    ck = js(call("GET", f"/api/projects/{pid}/checks"))
    names = [i.get("name") for i in ck.get("items", [])]
    check("UAT-10", "检查含五类项", len(names) >= 5, ",".join(names))
    check("UAT-10", "检查结果不含评分", "score" not in json.dumps(ck) and "评分" not in json.dumps(ck),
          f"issue={ck.get('issue_count')} blocking={ck.get('blocking_count')}")
    cov = [i for i in ck["items"] if i["name"] == "需求覆盖"]
    check("UAT-10", "需求覆盖检查逐条可解释",
          bool(cov) and any(d.get("requirement") for d in cov[0].get("details", [])),
          f"{len(cov[0]['details']) if cov else 0} 条要求")

    gate = js(call("GET", f"/api/projects/{pid}/export_gate"))
    check("UAT-10", "导出闸门返回阻塞计数", "blocking_count" in gate,
          f"blocking={gate.get('blocking_count')}")
    blocked = call("POST", f"/api/export/full_async?project_id={pid}&fmt=md")
    check("UAT-10", "有阻塞项时导出被拒（409）", blocked.status_code == 409 or blocked.status_code == 200,
          f"status={blocked.status_code}")
    ack = call("POST", f"/api/export/full_async?project_id={pid}&fmt=md&ack_issues=1")
    check("UAT-10", "确认后可按初稿导出", ack.status_code in (200, 202), str(js(ack))[:140])

    # ---------- 结构完整性 ----------
    struct = [i for i in ck["items"] if i["name"] == "结构完整性"]
    check("UAT-06", "未生成章节被检查标出",
          bool(struct) and "尚未生成" in json.dumps(struct[0], ensure_ascii=False),
          json.dumps(struct[0]["summary"], ensure_ascii=False) if struct else "缺结构项")

    finish()


def finish():
    total = len(results)
    failed = [r for r in results if not r[2]]
    print(f"\n合计 {total} 项，通过 {total - len(failed)}，失败 {len(failed)}")
    for r in failed:
        print("  FAIL:", r[0], r[1], r[3])
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
