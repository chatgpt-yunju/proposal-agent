"""探测 deepseek-flash 响应结构：message 字段、finish_reason、是否 reasoning_content。"""
import os, httpx, json
url="https://deepseek.yunjunet.cn/v1/chat/completions"
key=os.environ.get("LLM_API_KEY", "")
payload={"model":"deepseek-flash","temperature":0.3,"max_tokens":4096,
 "messages":[{"role":"system","content":"你是规划编写专家，中文输出。"},
             {"role":"user","content":"用200字说明绿色矿山建设的主要任务。"}]}
r=httpx.post(url,headers={"Authorization":f"Bearer {key}","Content-Type":"application/json"},
             json=payload,timeout=240)
print("HTTP",r.status_code)
d=r.json()
ch=d["choices"][0]
msg=ch["message"]
print("message keys:",list(msg.keys()))
print("finish_reason:",ch.get("finish_reason"))
print("content len:",len(msg.get("content") or ""))
print("reasoning_content len:",len(msg.get("reasoning_content") or ""))
print("content head:",(msg.get("content") or "")[:200])
