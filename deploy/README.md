# 云部署说明（写方案智能体）

本目录是 Linux 服务器部署用的一整套东西。代码本身走 GitHub 私密仓库
`chatgpt-yunju/proposal-agent`，服务器不需要从本机拷贝文件。

## 文件

| 文件 | 用途 |
|---|---|
| `deploy.sh` | 一条命令完成：拉代码 → 建 venv → 装依赖 → 起 uvicorn（也支持 update/start/stop/status/logs） |
| `proposal-agent.service` | 正式 Linux 机器上用 systemd 托管（开机自启、崩溃自动重启） |
| `nginx-proposal-agent.conf` | Nginx 反代 + SSE 关缓冲 + 大文件上传 + 长超时 |
| `start.sh` | 仓库根目录；幂等启动 + 等就绪（无 systemd 的环境用它） |
| `keepalive.sh` | 仓库根目录；30s 健康检查，掉线自动拉起 |

## 首次部署

```bash
# 1) 部署（GH_TOKEN 只在拉私有仓库时用，别写进脚本）
GH_TOKEN=<你的 token> bash deploy/deploy.sh install

# 2) 检查
bash deploy/deploy.sh status
curl http://127.0.0.1:8000/api/version
```

`install` 会做的事：克隆到 `~/proposal-agent-src` → rsync 覆盖 `~/proposal-agent/app`
（`data/` 和 `.env` 不动）→ 建 `.venv` → 装依赖 → 中文字体检查 → 后台拉起 uvicorn。

之后更新版本只要：

```bash
GH_TOKEN=<token> bash deploy/deploy.sh update   # 拉最新代码 → 重启 → 健康检查
```

## .env 必须项

`~/proposal-agent/.env`（脚本首次会自动从 `.env.example` 复制一份）：

```ini
APP_HOST=0.0.0.0
APP_PORT=8000
DB_PATH=./data/agent.db

KB_VECTOR_BACKEND=qdrant          # 小库/内存充足用 qdrant；大库或弱内存见下节改 flat
KB_VECTOR_DIR=./data/vector_db
KB_COLLECTION=proposal_kb

EMBEDDING_PROVIDER=local          # 本地 numpy+jieba，无需外部服务；也可改 api 走 OpenAI 兼容接口
EMBEDDING_LOCAL_DIM=8192

LLM_PROVIDER=api
LLM_BASE_URL=https://deepseek.yunjunet.cn/v1
LLM_API_KEY=<必填>
LLM_MODEL=deepseek-flash
```

## 内存：向量库是最大的坑（已实测）

`data/vector_db`（1.68 万条 8192 维向量）在 **qdrant 嵌入式模式**下，`QdrantClient(path=...)`
初始化需要把整个 1.2 GB sqlite 库读进内存并重建索引：

| 机器内存 | 实测结果 |
|---|---|
| 4 GB | 进程 RSS 涨到约 800 MB 后被 OOM kill，`server.log` 0 字节、无 traceback、8000 端口无监听 |
| 4 GB（把库拷到本地 overlay 盘再测） | 初始化 **>150 秒**仍未完成（被 timeout 杀掉），连 `Started server process` 都不打印 |

结论：**这台 4 GB 机器 + 1.2 GB 向量库，qdrant 嵌入模式不可用**，不是等久一点就能起。

### 解法：`KB_VECTOR_BACKEND=flat`（mmap 向量库）

`app/kb/storage.py` 里新增 `FlatStore`：向量存 `vectors.npy`，元数据存 `meta.jsonl`，
用 `numpy.load(mmap_mode='r')` 按需映射。实测同一台 4 GB 机器：

- 加载 **0.4 秒**（原来 >150 秒且失败）
- 服务启动到 `/api/version` 返回 200 共 **5 秒**
- 常驻内存约 **166 MB**（原 qdrant 方案 800 MB 还没起来）
- 全库检索（16829 条 8192 维）一次 **0.1–1.3 秒**，实测能命中「表1-3-1历口镇现状人口结构统计表」等真实内容

从 qdrant 库一次性导成 flat 格式（8 秒，不动原库）：

```bash
cd ~/proposal-agent
./.venv/bin/python - <<'PY'
import sqlite3, pickle, json, numpy as np
SRC = 'file:./data/vector_db/collection/proposal_kb/storage.sqlite?mode=ro'
OUT = './data/vector_db_flat'
import os; os.makedirs(OUT, exist_ok=True)
c = sqlite3.connect(SRC, uri=True)
n = c.execute("select count(*) from points").fetchone()[0]
V = np.zeros((n, 8192), dtype=np.float32); meta = open(OUT + '/meta.jsonl', 'w', encoding='utf-8')
for i, (pid, blob) in enumerate(c.execute("select id, point from points")):
    pt = pickle.loads(blob); V[i] = np.asarray(pt.vector, dtype=np.float32)
    p = pt.payload or {}
    meta.write(json.dumps({'id': str(pt.id), 'row': i, 'tier': p.get('tier'),
        'project_id': p.get('project_id'), 'chapter_id': p.get('chapter_id'),
        'doc_id': p.get('doc_id'), 'source': p.get('source'),
        'title': p.get('title'), 'text': p.get('text') or ''}, ensure_ascii=False) + '\n')
meta.close(); np.save(OUT + '/vectors.npy', V)
print('exported', n)
PY
```

然后 `.env` 改两行即可（原 qdrant 库保留不删，随时可切回）：

```ini
KB_VECTOR_BACKEND=flat
KB_VECTOR_DIR=./data/vector_db_flat
```

flat 模式下新增的知识库内容会追加到 `extra.npy` / `extra.jsonl`，每 200 条自动合并进主库。

## 没有 systemd 的环境（沙箱 / 容器）

ByteFaaS 沙箱里 PID 1 是 `dumb-init`，`systemctl` 不可用，`proposal-agent.service` 装不了。
用仓库根目录的两个脚本代替：

```bash
bash start.sh        # 幂等启动：已在跑就直接返回，否则拉起并轮询最多 60s 等就绪
bash keepalive.sh    # 前台循环，30s 一次健康检查，掉线自动 bash start.sh
# 常驻：setsid nohup bash keepalive.sh > data/keepalive.log 2>&1 < /dev/null &
```

## 端口与反代

- 应用自己监听 `0.0.0.0:8000`，本机内直接 `curl 127.0.0.1:8000` 即可。
- 沙箱环境的平台 Nginx（`/opt/gem/nginx.conf`，端口 8079）自带端口透传：Host 头写成
  `8000-<任意>` 就会转发到本机 8000，实测 `curl -H "Host: 8000-localhost" http://127.0.0.1:8079/api/version` 返回 200。
- 正式服务器上建议用 `nginx-proposal-agent.conf` 反代到 80/443，别直接暴露 8000。

## SSE 必须关缓冲（代码里已处理）

平台/自建 Nginx 默认会缓冲 `text/event-stream`，导致「前言/摘要起草」「章节生成步骤」一直转圈不出字。
`app/main.py` 两个 SSE 端点都已返回：

```
Cache-Control: no-cache
X-Accel-Buffering: no
Connection: keep-alive
```

`X-Accel-Buffering: no` 是 Nginx 认的关缓冲头，比改配置更可靠（自建 Nginx 那段配置也关了一遍）。

## 部署后自检清单

```bash
bash deploy/deploy.sh status                        # 进程 + /api/version + /api/projects
curl -s localhost:8000/api/kb/stats                 # 知识库条数（当前是 16829，global 6777）
curl -s localhost:8000/api/projects | head -c 200   # 项目列表
fc-list :lang=zh | wc -l                            # 中文字体数量，为 0 则 PDF 中文会丢
```

浏览器打开 `<域名或 IP>:8000`，确认：项目列表正常、点「生成」弹出第三方模型数据风险提示、
生成章节时步骤弹窗逐条刷新（SSE 通）、导出 Word/PDF 与模板版式一致。

## 常见问题

- **进程一起就消失、日志 0 字节**：OOM 或向量库初始化卡死。先查 `free -m` / `dmesg | grep -i oom`，
  然后把 `KB_VECTOR_BACKEND` 换成 `flat`（见上）。
- **整篇生成/前言摘要一直转圈不出字**：Nginx 缓冲没关。用 `nginx-proposal-agent.conf` 里那段 SSE 配置。
- **PDF 中文是方块或空白**：服务器没中文字体，`sudo apt-get install -y fonts-noto-cjk`。
- **上传模板报 413**：`client_max_body_size` 没放大。
- **pip 装不上**：本环境清华镜像整站 403，只能 `pip install -i https://pypi.org/simple -r requirements.txt`。
- **端口访问不到**：确认防火墙/安全组放行 8000（或只放行 Nginx 的 80/443，让反代转发）。
