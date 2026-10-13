# 🐙 Octopus — 对话式 AI 视频创作

> 说一句你想拍什么，Agent 帮你写剧本、出美术、拆分镜、生成视频、合成成片。

Octopus 把一条视频的制作拆成一串可以逐步确认的阶段，铺在一张无限画布上：每个阶段由一个专门的 Agent 负责，每个节点都能确认、重跑、修改参数或直接编辑结果。你随时可以插手，也可以一路点"确认"让它自己跑完。

> ⚠️ 作者已停止开发，代码按现状（as-is）开源，欢迎 fork 自用或二次开发。

## 功能

**四种创作模式**

| 模式 | 适合 | 说明 |
|---|---|---|
| 简易 | 一句话出片 | 尽量少打扰，自动推进 |
| 进阶 | 想自己把关每一步 | 每个阶段停下来等确认，可逐镜手动生成 |
| 专家 | 分集漫剧 / 长篇改编 | 剧集 → 总设定 → 分集三层结构，角色、场景、道具跨集复用 |
| MV | 给一首歌出 MV | 导入歌曲与歌词，镜头切点卡在歌词句边界，成片总长 = 歌曲长度，音轨用原曲 |

**创作流水线**

```
对话（导演）→ 方案确认 → 剧本（编剧）
   → 美术（角色三视图 / 场景 / 道具）
   → 分镜（用 @ 引用美术资产）
   → 关键帧 → 视频（按分镜逐镜生成）
   → 合成（ffmpeg 拼接 + 转场 + 字幕 + 配乐）→ 发布
```

**一致性**：分镜里用 `@角色名#服装`、`@场景名#机位`、`@道具名#状态` 引用美术阶段的产物，同一个角色、场景跨镜头保持一致。

**可接入的服务**（都在设置页里填，按需选用）

| 环节 | 支持 |
|---|---|
| 大模型 | DeepSeek、Kimi、任意 OpenAI 兼容接口 |
| 出图 | 火山 Seedream、智谱 GLM-Image、Google Imagen |
| 视频 | 火山方舟 Seedance 2.0、蚂蚁数科渠道 Seedance、阿里云百炼（DashScope）、**本地 ComfyUI**（Wan 2.2 / LTX-2.3 / MiniMax H3） |
| 配乐 / 配音 | ElevenLabs Music、Mubert；旁白用 edge-tts |
| 歌词识别（MV） | 智谱 GLM-ASR（可选；主路径是创作者自己导入歌词） |
| 发布 | YouTube、TikTok |
| 存储（可选） | 阿里云 OSS —— 部分视频服务要求首帧是公网 URL 时才需要 |

## 快速开始

### 准备

- Python 3.11+
- Node.js 18+
- [ffmpeg](https://ffmpeg.org/) 在 `PATH` 里（合成、抽帧、配乐都靠它）

### 启动后端

```bash
cd backend
python -m venv .venv
# Windows: .venv\Scripts\activate      macOS / Linux: source .venv/bin/activate
pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

### 启动前端

```bash
cd frontend
npm install
npm run dev
```

打开 <http://localhost:5173>。本地模式下登录页填任意非空账号密码即可进入。

> Windows 上如果 `npm run dev` 找不到 vite，直接用 `node node_modules/vite/bin/vite.js --port 5173`。

### 填 key

进入后打开**设置**，至少配好：

1. **大模型**（必需）—— 所有 Agent 都靠它
2. **出图**（必需）—— 美术和关键帧
3. **视频**（必需）—— 云端服务的 key，或者本地 ComfyUI 地址

每家怎么注册、key 在哪找：[docs/api-keys.md](docs/api-keys.md)。从启动到出片的完整教程：[docs/guide.md](docs/guide.md)。

key 存在本机 SQLite 里，敏感字段用 Fernet 加密，主密钥在 `~/.octopus/master.key`（首次启动自动生成）。也可以用 `backend/.env` 兜底，模板见 [`backend/.env.example`](backend/.env.example)。

## 本地视频生成（ComfyUI，可选）

不想用云端视频服务，可以接本机的 [ComfyUI](https://github.com/comfyanonymous/ComfyUI)，在设置里把 ComfyUI 地址填成 `http://127.0.0.1:8188`，视频服务选 `comfyui`。按模型文件名自动选择工作流：

- **Wan 2.2** / **LTX-2.3**：常规图生视频
- **MiniMax H3**：音画联合生成，支持图生视频、首尾帧、多参考图。几个实测下来的要点：
  - 稀疏注意力节点 `H3SLAAttention` 来自 [PlagueKind/ComfyUI-PlagueKind-Nodes](https://github.com/PlagueKind/ComfyUI-PlagueKind-Nodes)；需要 `comfy-kitchen >= 0.2.32`，低于这个版本会**静默回退成稠密注意力**（不报错，只是慢）
  - 镜头描述会自动译成英文再交给 H3 —— 中文描述会被它当成台词念出来，人物嘴型跟着乱动
  - 台词 / 歌词走官方的 `<d>[Chinese] …</d>` 对白标签，嘴型才能对上内容
  - 连跑几镜后 ComfyUI 不释放内存，会越跑越慢；Octopus 在提交前检查余量，不够就调 `POST /free` 回收

相关实现在 `backend/app/modules/_comfyui_h3.py` 和 `_comfyui_video.py`，注释里记录了每条结论是怎么测出来的。

## 运行模式

| 模式 | 说明 |
|---|---|
| `local`（默认） | 全部在本机调度，所有 key 自己填。开源版默认就是它 |
| `cloud_client` | Agent 和视频走云端网关、按 credits 计费。需要自行部署 [`cloud/`](cloud/README.md) 并在设置里填网关地址 |

用环境变量 `OCTOPUS_DEPLOYMENT_MODE` 或设置页里的「运行模式」切换。

`cloud/` 是一套完整的云端网关：邮箱验证码登录、Ed25519 授权签发、credits 计费、支付宝充值、管理后台。所有密钥都从服务器上的 `secrets.env` 读，仓库里没有任何密钥。

## 目录结构

```
octopus/
├── backend/
│   ├── app/
│   │   ├── agents/      导演 / 编剧 / 美术 / 分镜 / 音频 / MV / 发布 / 路由
│   │   ├── api/         HTTP 接口（项目、工作流、设置、上传、发布、MV ……）
│   │   ├── engine/      交互式执行器、工作流状态、模块注册
│   │   ├── modules/     出图 / 图生视频 / 合成 / 配乐 / 配音 / 翻译 …… 各 provider 适配
│   │   ├── publishers/  YouTube、TikTok
│   │   └── core/        配置、加密、运行时配置、授权门控
│   ├── tests/
│   └── requirements.txt
├── frontend/            React 18 + TypeScript + Vite + React Flow + Tailwind + Zustand
├── cloud/               可选：自部署的云端网关（登录 / 计费 / 支付 / 管理后台）
├── desktop/             Windows 桌面版打包（PyInstaller + Inno Setup）
└── docs/                设计文档与踩坑记录
```

## 桌面版打包（Windows）

```bash
cd frontend && npm run build && cd ..     # 先构建前端，会被打进包里
pip install pyinstaller
python desktop/build_desktop.py --raw     # 产物在 dist/Octopus/
```

- 一定要带 `--raw`。不带的话脚本会把 Agent 剥成只调云端的空壳（当初商业发行版的做法），本地模式下跑不起来
- `ffmpeg.exe` / `ffprobe.exe` 从 `PATH` 里找，也可以用环境变量 `OCTOPUS_FFMPEG_DIR` 指定目录

再用 [Inno Setup](https://jrsoftware.org/isinfo.php) 编译 `desktop/octopus.iss` 得到安装包。

## 已知问题

- `requirements.txt` 只覆盖核心依赖。以下按需另装：
  - `oss2` —— 用阿里云 OSS 时
  - `demucs` —— MV 模式做人声分离时
  - 蚂蚁数科 AIGC SDK —— 走蚂蚁渠道时；它不在 PyPI，需要从蚂蚁数科开放平台获取
- 发布到 YouTube / TikTok 需要你自己在对应平台注册开发者应用，拿到 OAuth 凭据后填进设置

## 支持作者

如果这个项目帮到了你，可以请作者喝杯咖啡。

<img src="docs/images/wechat_donate.jpg" width="320" alt="微信赞赏码">

## License

[MIT](LICENSE)
