# 这台云主机上的部署现状（2026-11-10）

## 环境事实

- ByteFaaS 函数沙箱（不是常规 ECS）：Ubuntu 22.04.5 / Python 3.12.11 / 4C 4G（cgroup 上限 4 GB）
- 没有 systemd（PID 1 是 dumb-init），`proposal-agent.service` 装不了，改用 `start.sh` + `keepalive.sh`
- 没有公网域名；平台 Nginx 监听 127.0.0.1:8079，Host 头写 `8000-<任意>` 会透传到本机 8000
  （实测 `curl -H "Host: 8000-localhost" http://127.0.0.1:8079/api/version` → 200）
- `/home/user` 是 FUSE 盘（hpvs_fs），`/var/tmp` 在 overlay 本地盘

## 当前运行状态

- 进程：`./.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000`，常驻内存约 165 MB
- 健康检查：`/api/version` 200（前端 index.html 81099 字节，含归档 + 风险弹窗 + 表题）
- 项目：`/api/projects` 13 个（11-06 的旧数据，本机最新的 14 项目/283 已生成章 尚未同步上来）
- 知识库：`/api/kb/stats` = 16829 条（global 6777）；实测检索「历口镇 现状人口 结构 统计」命中
  「表1-3-1历口镇现状人口结构统计表」等真实内容，3.6 秒（含 jieba 首次加载）
- 守护：`keepalive.sh` 每 30 秒探活，掉线自动 `bash start.sh`

## 关键改动（与 qdrant 方案的差异）

`data/vector_db`（qdrant 嵌入式，1.2 GB sqlite）在这台机器上**初始化 >150 秒仍完不成，并被 OOM kill**
（进程 RSS 800 MB 后消失、server.log 0 字节、无 traceback）。所以改成：

- `app/kb/storage.py` 新增 `FlatStore`：`vectors.npy`(526 MB) + `meta.jsonl`(15 MB)，mmap 加载 0.4 秒
- `.env`：`KB_VECTOR_BACKEND=flat`、`KB_VECTOR_DIR=./data/vector_db_flat`
- 原 qdrant 库与 `collection/` 目录原样保留（没删），随时可切回
- `data/vector_db_flat/` 由一次性导出脚本生成（导出 8.3 秒，步骤见 deploy/README.md）
- `app/main.py` 两个 SSE 端点补上 `X-Accel-Buffering: no`，否则平台 Nginx 会缓冲流式输出

## 常用命令

```bash
cd ~/proposal-agent
bash start.sh        # 幂等启动（已跑就直接返回）
bash keepalive.sh    # 前台守护循环
tail -f data/server.log
curl -s localhost:8000/api/kb/stats
```

## 还没做的

- 本机最新数据（本机 agent.db：14 项目 / 283 已生成章节）没上传到云端，云端仍是 11-06 的 13 项目
- 域名 / HTTPS：这台沙箱没有公网入口，只能靠平台透传；要对外域名需放到自有服务器
- 8 GB 以上机器 + qdrant 原方案仍是正式环境的推荐形态
