"""连通性测试：DeepSeek 兼容 chat 接口。"""
import os
import httpx

BASE = "https://deepseek.yunjunet.cn/v1"
KEY = os.environ.get("LLM_API_KEY", "")
MODEL = "deepseek-flash"

try:
    r = httpx.post(
        f"{BASE}/chat/completions",
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
        json={"model": MODEL,
              "messages": [{"role": "user", "content": "只回复两个字：正常"}],
              "temperature": 0.3, "max_tokens": 20},
        timeout=30.0,
    )
    print("HTTP", r.status_code)
    if r.status_code == 200:
        data = r.json()
        print("返回模型:", data.get("model"))
        print("内容:", data["choices"][0]["message"]["content"])
        print("usage:", data.get("usage"))
    else:
        print("错误体:", r.text[:800])
except Exception as e:
    print("连接异常:", type(e).__name__, e)
