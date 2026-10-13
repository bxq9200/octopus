# Octopus 云端网关

0.9 的云端服务。对应 0.8 在客户端埋好的插口（`backend/app/core/agent_runtime.py` 等）。

## Phase 1 现状（2026-07-21）

已实现 **`POST /v1/agent/run`** —— agent 即服务，端到端跑通。

未实现（后续 Phase）：`/v1/auth/login`、`/v1/video/generate`、`/v1/storage/sts`、`/v1/usage/report`。

## 本机跑（不需要服务器）

```bash
cd octopus/cloud
python -m uvicorn main:app --host 127.0.0.1 --port 9000
```

客户端那边：设置 → **部署模式** 选 `cloud_client`，
设置 → 视频生成 → **Octopus 网关地址** 填 `http://127.0.0.1:9000`。

> ⚠️ 切模式的优先级是 **`user_settings("deploy","mode")` > `config.DEPLOYMENT_MODE`**。
> 只要在设置页动过一次开关，DB 里就有值了，此后 `OCTOPUS_DEPLOYMENT_MODE` 环境变量**再也压不过它**。
> 用脚本切模式时必须同时设两处，否则测的是假的（Phase 1 验证时踩过这个坑）。

## 三个必须记住的设计点

**1. 本服务内部强制 `local` 模式 —— 否则递归调用自己。**
`BaseAgent.run()` / `chat()` 里有 cloud_client 分叉；本服务若也处于 cloud_client，
跑 agent 时会再往"云端"发一次 → 打回自己 → 无限递归。
`main.py` 在 import `app.*` **之前**就把 env 钉成 `local`，并在 import 后再钉一次 settings。

**2. LLM key 用完即弃。**
`llm_config` 随请求转发上来，用 `runtime_config.llm_override` 压进 ContextVar，
只在该请求作用域内有效，**不落库、不写日志**。ContextVar 保证并发请求互不串号。
日志只记 `{agent_type, entry, provider, model, payload_keys}` —— 绝不记 key、绝不记 prompt。

**3. `entry` 两种语义不同。**
- `execute`：客户端已做过 `_preprocess`，云端只跑 `agent.execute()`（调 `run()` 会重复 preprocess）
- `chat`：客户端分叉在 `chat()` 最开头（拼 prompt 之前），云端要跑**完整的 `agent.chat()`**

## agent 代码在哪

**不在这个目录**，仍住 `backend/app/agents/`，这里只 import。
agents 只保留一份，local 模式（开发机）和云端模式跑**同一份代码**，避免"云端出的分镜跟本地不一样"这种漂移 bug。

**护城河靠打包时排除保护**：桌面端构建产物不得包含 `backend/app/agents/` 与 `cloud/`，
只有云端镜像才包含。构建脚本必须带断言 —— 产物里发现 `agents/` 就中止构建。**这条没做到，护城河就是裸的。**

## 部署（未来）

建议单独一台机器部署，
2 核 4G 起步足够：agent 不吃 GPU，视频中转也是**字节不过服务器**（客户端直连下载）。
容器建议设内存限额。
