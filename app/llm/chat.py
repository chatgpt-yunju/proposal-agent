"""可插拔的方案写作 LLM：`api`（OpenAI 兼容 /chat/completions）或 `local`（离线兜底，结构化组装检索内容）。

接口统一为 generate_chapter(...)：给定章节规格 + 三级知识库检索引用（+ 可选审查问题），
返回该章节的 Markdown 正文。生产用 api 获得真正智能生成；local 用于离线跑通工作流与测试。
"""
from __future__ import annotations

from typing import Dict, List, Optional, Protocol

import httpx


class ChatProvider(Protocol):
    def generate_chapter(
        self,
        *,
        chapter: Dict,
        project_name: str,
        citations: List[Dict],
        issues: Optional[List[str]] = None,
    ) -> str:
        ...


class ApiChat:
    """OpenAI 兼容 Chat Completions 客户端（可接豆包 / DeepSeek / 本地 vLLM 等）。"""

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 240.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self._last_reasoning = ""
        if not (base_url and api_key and model):
            raise ValueError("LLM_PROVIDER=api 需配置 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL")

    def _fallback(self, chapter: Dict, project_name: str, citations: List[Dict]) -> str:
        """模型不可用时的降级：用检索到的知识库要点结构化组装，保证方案不崩、章节不丢。"""
        code, title = chapter.get("code", ""), chapter.get("title", "")
        lines = [f"## {code} {title}", "",
                 "> 说明：本章模型响应超时，暂由知识库检索要点组装，可稍后重跑获取成文。", ""]
        for c in citations:
            text = (c.get("text") or "").strip()
            if text:
                lines.append(f"- {text[:300]}（【来源：{c.get('source', '')}】）")
        return "\n".join(lines)

    def generate_chapter(self, *, chapter: Dict, project_name: str,
                         citations: List[Dict], issues: Optional[List[str]] = None) -> str:
        if not citations:
            # 只返回纯正文：标题由调用方统一拼接，避免出现双重标题
            return "（未检索到可引用的知识库内容，待补充入库后重跑。）"

        ctx_lines = []
        for i, c in enumerate(citations, 1):
            src = f"{c.get('source','')}({c.get('tier','')}级)"
            ctx_lines.append(f"[{i}] {src}：{c.get('text','')[:300]}")
        context = "\n".join(ctx_lines)

        extra = ""
        if issues:
            extra = "\n针对以下审查问题补足与修正：\n" + "\n".join(f"- {x}" for x in issues)

        system = (
            "你是资深国土空间规划方案编写专家，撰写政府正式实施方案。"
            "风格要求：完全仿照政府公文/工程实施方案的写作风格，语言正式严谨。\n"
            "严格遵守以下规则：\n"
            "1. 只输出正文内容，绝对不要输出任何标题、小标题、编号；\n"
            "2. 绝对不要在正文里出现【来源：xxx】、[来源: xxx]、（来源：xxx）之类的任何引用标注；\n"
            "3. 不要分小节，就直接写连贯的正文内容；\n"
            "4. 篇幅根据章节内容灵活调整：重要章节可以写得充实详细，一般性章节写得简洁精炼，整体保持政府公文的篇幅惯例；\n"
            "5. 内容要和参考资料里的事实、数据一致，不要编造，不要啰嗦重复；\n"
            "6. 若本节需要罗列多项并列数据（项目清单、面积与投资明细、指标对比、分年度安排等），"
            "必须输出标准 Markdown 表格：第一行为表头，第二行为 | --- | 分隔行，"
            "其后每行一条记录、单元格用 | 分隔；表格下方再补一段文字说明。没有并列数据时不要强行造表；\n"
            "7. 表格上方必须单独一行写表名，格式为「表：<表名>」，例如「表：历口镇现状人口结构统计表」，"
            "表名要具体到能看懂内容，不要自己写表号（表号由系统按章节顺序自动生成）；\n"
            "8. 正文里提到表格时同样不要写具体表号（不要出现「见表1-3-7」这类写法），"
            "用「见下表」「具体见下文表格」表述即可，避免与系统自动生成的表号对不上"
        )
        user = (
            f"项目：{project_name}\n"
            f"待撰写章节：{chapter.get('title','')}\n"
            f"章节要求：{chapter.get('description','（依据项目资料自行组织）')}\n"
            f"{extra}\n"
            f"参考资料（直接引用其中的事实和数据，不要标注来源）：\n{context}\n"
            "请直接输出该章节的正文内容，不要带任何标题，仿照政府实施方案的公文风格，篇幅根据内容需要灵活调整。"
        )

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.3,
            "max_tokens": 8192,
        }

        last_err: Optional[Exception] = None
        for attempt in range(3):
            try:
                resp = httpx.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                    json=payload,
                    timeout=self.timeout,
                )
                if resp.status_code >= 500:
                    last_err = Exception(f"HTTP {resp.status_code}: {resp.text[:200]}")
                    continue
                resp.raise_for_status()
                msg = resp.json()["choices"][0]["message"]
                content = (msg.get("content") or "").strip()
                reasoning = (msg.get("reasoning_content") or "").strip()
                if not content:
                    last_err = Exception("模型返回空正文（token 预算被思考占用）")
                    continue
                self._last_reasoning = reasoning
                return content
            except (httpx.TimeoutException, httpx.TransportError, KeyError, IndexError) as e:
                last_err = e
                continue
        print(f"[ApiChat] 章节 {chapter.get('code')} 模型连续失败，降级组装：{last_err}")
        return self._fallback(chapter, project_name, citations)

    def complete(self, *, system: str, user: str, max_tokens: int = 4096,
                 temperature: float = 0.3, retries: int = 2) -> str:
        """通用一次性补全：给「摘要 / 前言」这类非章节请求用。失败抛 RuntimeError，由调用方降级。"""
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        last_err: Optional[Exception] = None
        for _ in range(max(1, retries)):
            try:
                resp = httpx.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                    json=payload,
                    timeout=self.timeout,
                )
                if resp.status_code >= 400:
                    # 网关偶发 404/429/5xx 都重试；401/403 是鉴权问题，重试无意义
                    if resp.status_code in (401, 403):
                        raise RuntimeError(f"模型鉴权失败：HTTP {resp.status_code}")
                    last_err = Exception(f"HTTP {resp.status_code}: {resp.text[:200]}")
                    continue
                resp.raise_for_status()
                msg = resp.json()["choices"][0]["message"]
                content = (msg.get("content") or "").strip()
                if not content:
                    last_err = Exception("模型返回空正文")
                    continue
                self._last_reasoning = (msg.get("reasoning_content") or "").strip()
                return content
            except (httpx.TimeoutException, httpx.TransportError, KeyError, IndexError) as e:
                last_err = e
                continue
        raise RuntimeError(f"模型调用失败：{last_err}")

    def complete_stream(self, *, system: str, user: str, max_tokens: int = 8192,
                        temperature: float = 0.3, retries: int = 2, on_chunk=None) -> str:
        """流式一次性补全（前言/摘要用）：边生成边收，网关不会因为长时间无数据而 503。

        on_chunk(delta) 可选：每收到一段增量就回调，供上层把生成过程实时推给前端。
        """
        import json as _json
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        last_err: Optional[Exception] = None
        for _ in range(max(1, retries)):
            parts: List[str] = []
            try:
                with httpx.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                    json=payload,
                    timeout=self.timeout,
                ) as resp:
                    if resp.status_code >= 400:
                        resp.read()
                        if resp.status_code in (401, 403):
                            raise RuntimeError(f"模型鉴权失败：HTTP {resp.status_code}")
                        last_err = Exception(f"HTTP {resp.status_code}: {resp.text[:200]}")
                        continue
                    for line in resp.iter_lines():
                        if not line.startswith("data: "):
                            continue
                        data = line[6:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = _json.loads(data)
                            delta = chunk["choices"][0].get("delta") or {}
                        except (_json.JSONDecodeError, KeyError, IndexError):
                            continue
                        c = delta.get("content") or ""
                        if c:
                            parts.append(c)
                            if on_chunk is not None:
                                try:
                                    on_chunk(c)
                                except Exception:
                                    pass
                text = "".join(parts).strip()
                if not text:
                    last_err = Exception("模型返回空正文")
                    continue
                return text
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_err = e
                continue
        raise RuntimeError(f"模型调用失败：{last_err}")

    async def generate_chapter_stream(self, *, chapter: Dict, project_name: str,
                                      citations: List[Dict], issues: Optional[List[str]] = None):
        """流式生成章节，逐块 yield 返回 (delta_text, reasoning_delta)。"""
        if not citations:
            # 只返回纯正文：标题由调用方统一拼接，避免出现双重标题
            yield ("（未检索到可引用的知识库内容，待补充入库后重跑。）", "")
            return

        ctx_lines = []
        for i, c in enumerate(citations, 1):
            src = f"{c.get('source','')}({c.get('tier','')}级)"
            ctx_lines.append(f"[{i}] {src}：{c.get('text','')[:300]}")
        context = "\n".join(ctx_lines)

        extra = ""
        if issues:
            extra = "\n针对以下审查问题补足与修正：\n" + "\n".join(f"- {x}" for x in issues)

        system = (
            "你是资深国土空间规划方案编写专家，撰写政府正式实施方案。"
            "风格要求：完全仿照政府公文/工程实施方案的写作风格，语言正式严谨。\n"
            "严格遵守以下规则：\n"
            "1. 只输出正文内容，绝对不要输出任何标题、小标题、编号；\n"
            "2. 绝对不要在正文里出现【来源：xxx】、[来源: xxx]、（来源：xxx）之类的任何引用标注；\n"
            "3. 不要分小节，就直接写连贯的正文内容；\n"
            "4. 篇幅根据章节内容灵活调整：重要章节可以写得充实详细，一般性章节写得简洁精炼，整体保持政府公文的篇幅惯例；\n"
            "5. 内容要和参考资料里的事实、数据一致，不要编造，不要啰嗦重复；\n"
            "6. 若本节需要罗列多项并列数据（项目清单、面积与投资明细、指标对比、分年度安排等），"
            "必须输出标准 Markdown 表格：第一行为表头，第二行为 | --- | 分隔行，"
            "其后每行一条记录、单元格用 | 分隔；表格下方再补一段文字说明。没有并列数据时不要强行造表；\n"
            "7. 表格上方必须单独一行写表名，格式为「表：<表名>」，例如「表：历口镇现状人口结构统计表」，"
            "表名要具体到能看懂内容，不要自己写表号（表号由系统按章节顺序自动生成）；\n"
            "8. 正文里提到表格时同样不要写具体表号（不要出现「见表1-3-7」这类写法），"
            "用「见下表」「具体见下文表格」表述即可，避免与系统自动生成的表号对不上"
        )
        user = (
            f"项目：{project_name}\n"
            f"待撰写章节：{chapter.get('title','')}\n"
            f"章节要求：{chapter.get('description','（依据项目资料自行组织）')}\n"
            f"{extra}\n"
            f"参考资料（直接引用其中的事实和数据，不要标注来源）：\n{context}\n"
            "请直接输出该章节的正文内容，不要带任何标题，仿照政府实施方案的公文风格，篇幅根据内容需要灵活调整。"
        )

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.3,
            "max_tokens": 8192,
            "stream": True,
        }

        import json
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json=payload,
            ) as resp:
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                        delta = chunk["choices"][0]["delta"]
                        content = delta.get("content", "")
                        reasoning = delta.get("reasoning_content", "")
                        if content or reasoning:
                            yield (content, reasoning)
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue


class LocalChat:
    """离线兜底：把三级知识库检索到的引用结构化组装为章节正文（确定性、可追溯、非 LLM）。"""

    def complete(self, *, system: str, user: str, max_tokens: int = 4096,
                 temperature: float = 0.3, retries: int = 2) -> str:
        raise RuntimeError("当前为离线模式（LLM_PROVIDER 不是 api），无法自动起草前言/摘要")

    def complete_stream(self, *, system: str, user: str, max_tokens: int = 8192,
                        temperature: float = 0.3, retries: int = 2, on_chunk=None) -> str:
        raise RuntimeError("当前为离线模式（LLM_PROVIDER 不是 api），无法自动起草前言/摘要")

    def generate_chapter(self, *, chapter: Dict, project_name: str,
                         citations: List[Dict], issues: Optional[List[str]] = None) -> str:
        title = chapter.get("title", "")
        code = chapter.get("code", "")
        lines = [f"## {code} {title}", ""]
        if issues:
            lines.append(f"> 修订说明：{'; '.join(issues)}")
            lines.append("")
        lines.append(
            f"本章基于项目《{project_name}》与三级知识库检索内容编制，"
            f"围绕“{chapter.get('description','本项目的相关章节要求')}”展开如下："
        )
        lines.append("")
        if not citations:
            lines.append("（本章暂无可引用知识，待补充入库后重跑。）")
        else:
            for c in citations:
                src = c.get("source", "")
                tier = c.get("tier", "")
                text = c.get("text", "").strip()
                if text:
                    lines.append(f"- {text}（【来源：{src}，{tier}级】）")
        lines.append("")
        lines.append("以上要点均检索自本项目知识库，引用可回溯至原始文档。")
        return "\n".join(lines)


def build_chat() -> ChatProvider:
    from ..config import settings

    if settings.llm_provider == "api":
        return ApiChat(settings.llm_base_url, settings.llm_api_key, settings.llm_model)
    return LocalChat()
