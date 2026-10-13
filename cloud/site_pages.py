"""主站页面：落地页 `/` + 用户文档 `/docs/*`。

放云端而不是打进客户端的理由：**下载链接要能点、文档改了不用重新发版**。
纯静态 HTML（无外部依赖、无 CDN），直接由 FastAPI 返回。
"""
import os

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["site"])

# 下载地址：默认指向开源仓库；自部署可用环境变量 SITE_DOWNLOAD_URL 改。
# 🔴 **发新版只改这三行** —— 官网下载按钮、客户端「检查更新」都读它，改完重启云端即可推送更新。
DOWNLOAD_VERSION = "1.0.0"
RELEASE_NOTES = "首个正式版：对话式创作全流程、角色跨镜锁脸、支付宝充值、新手引导。"
DOWNLOAD_URL = os.environ.get("SITE_DOWNLOAD_URL", "https://github.com/bxq9200/octopus")

_BASE_CSS = """
*{margin:0;padding:0;box-sizing:border-box}
body{background:#07070b;color:#e6e6ef;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;line-height:1.7;-webkit-font-smoothing:antialiased}
a{color:#5eead4;text-decoration:none}
a:hover{text-decoration:underline}
.wrap{max-width:920px;margin:0 auto;padding:0 24px}
.nav{border-bottom:1px solid #ffffff14;position:sticky;top:0;background:#07070bee;backdrop-filter:blur(8px);z-index:10}
.nav .wrap{display:flex;align-items:center;gap:14px;height:60px}
.logo{display:flex;align-items:center;gap:9px;font-weight:700;letter-spacing:.5px;color:#fff}
.logo span{font-size:20px}
.nav .sp{flex:1}
.nav a{color:#9a9ab0;font-size:14px}
.nav a:hover{color:#e6e6ef;text-decoration:none}
.btn{display:inline-block;background:linear-gradient(135deg,#22d3ee,#6366f1);color:#04040a!important;font-weight:700;
     padding:13px 30px;border-radius:11px;font-size:15px;transition:.2s;border:none;cursor:pointer}
.btn:hover{text-decoration:none;transform:translateY(-1px);box-shadow:0 8px 28px #6366f159}
.btn.sm{padding:8px 18px;font-size:13px;border-radius:8px}
.btn.ghost{background:none;border:1px solid #ffffff24;color:#e6e6ef!important;font-weight:500}
.btn.ghost:hover{border-color:#5eead4;box-shadow:none}
h1{font-size:44px;line-height:1.25;letter-spacing:-.5px;margin-bottom:18px;color:#fff}
h2{font-size:25px;margin:52px 0 16px;color:#fff;letter-spacing:-.2px}
h3{font-size:17px;margin:26px 0 10px;color:#fff}
p{color:#b4b4c8;margin-bottom:14px}
.muted{color:#7a7a90;font-size:13px}
code{background:#ffffff0f;padding:2px 7px;border-radius:5px;font-family:ui-monospace,Consolas,monospace;font-size:.88em;color:#5eead4}
pre{background:#0d0d14;border:1px solid #ffffff14;border-radius:10px;padding:15px;overflow-x:auto;margin:14px 0}
pre code{background:none;padding:0;color:#c3c3d8}
table{width:100%;border-collapse:collapse;margin:16px 0;font-size:14px}
th,td{text-align:left;padding:11px 12px;border-bottom:1px solid #ffffff12}
th{color:#8a8aa0;font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.5px}
td{color:#c3c3d8;vertical-align:top}
ul,ol{color:#b4b4c8;margin:0 0 14px 22px}
li{margin-bottom:7px}
.card{background:#ffffff07;border:1px solid #ffffff12;border-radius:13px;padding:22px;margin:14px 0}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:14px;margin:22px 0}
.note{border-left:3px solid #5eead4;background:#5eead40d;padding:13px 17px;border-radius:0 9px 9px 0;margin:16px 0;color:#c3c3d8;font-size:14px}
.warn{border-left-color:#fbbf24;background:#fbbf240d}
footer{border-top:1px solid #ffffff12;margin-top:80px;padding:28px 0 44px;color:#5a5a70;font-size:13px}
"""

_NAV = """
<div class="nav"><div class="wrap">
  <a class="logo" href="/"><span>🐙</span> Octopus</a>
  <div class="sp"></div>
  <a href="/docs/guide">使用教程</a>
  <a href="/docs/keys">获取 API Key</a>
  <a href="/docs/comfyui">进阶</a>
  <a href="__DL__">下载</a>
</div></div>
""".replace("__DL__", DOWNLOAD_URL)


# 备案信息 —— **法规要求网站必须公示**，支付渠道（支付宝/微信）审核也会检查。
# 自部署时填你自己的：SITE_COMPANY / SITE_ICP_NO / SITE_POLICE_NO / SITE_POLICE_CODE。留空就不显示。
COMPANY = os.environ.get("SITE_COMPANY", "")
ICP_NO = os.environ.get("SITE_ICP_NO", "")
POLICE_NO = os.environ.get("SITE_POLICE_NO", "")
POLICE_CODE = os.environ.get("SITE_POLICE_CODE", "")


def _beian_html() -> str:
    parts = []
    if ICP_NO:
        parts.append(f'<a href="https://beian.miit.gov.cn/" target="_blank" rel="noreferrer" '
                     f'style="color:#5a5a70">{ICP_NO}</a>')
    if POLICE_NO:
        parts.append(f'<a href="https://beian.mps.gov.cn/#/query/webSearch?code={POLICE_CODE}" '
                     f'target="_blank" rel="noreferrer" style="color:#5a5a70">{POLICE_NO}</a>')
    return " &nbsp;·&nbsp; ".join(parts)


def _page(title: str, body: str) -> str:
    return f"""<!doctype html><html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>{_BASE_CSS}</style></head>
<body>{_NAV}{body}
<footer><div class="wrap">
  <div>© {COMPANY or 'Octopus'} &nbsp;·&nbsp; <a href="/">首页</a> &nbsp;·&nbsp; <a href="/docs/comfyui">转视角教程</a></div>
  <div style="margin-top:9px;font-size:12px">{_beian_html()}</div>
</div></footer>
</body></html>"""


# ═══════════════════════════════════════════════════════
# 落地页
# ═══════════════════════════════════════════════════════

_LANDING = f"""
<div class="wrap" style="padding-top:80px;padding-bottom:20px;text-align:center">
  <h1>用一句话，<br>生成一整集短剧</h1>
  <p style="font-size:18px;max-width:620px;margin:0 auto 34px">
    Octopus 是对话式 AI 视频创作工具。你只管说想拍什么，
    导演、编剧、美术、分镜、配乐各司其职，一路做到成片。
  </p>
  <a class="btn" href="{DOWNLOAD_URL}">下载 Windows 版 · v{DOWNLOAD_VERSION}</a>
  &nbsp;&nbsp;
  <a class="btn ghost" href="/docs/guide">先看使用教程</a>
  <p class="muted" style="margin-top:14px">双击安装，约 120 MB · Windows 10/11 64 位</p>
</div>

<div class="wrap">
  <div class="grid">
    <div class="card"><h3>💬 一句话起片</h3><p class="muted" style="margin:0">
      说“拍个 2 岁小姑娘和小猫玩”，AI 导演会追问你想要的调性、时长、横竖屏，然后自动往下做。</p></div>
    <div class="card"><h3>🎭 角色不串脸</h3><p class="muted" style="margin:0">
      角色立绘入库锚定，跨镜头保持同一张脸；连贯镜头用上一镜尾帧续接，画面接得上。</p></div>
    <div class="card"><h3>🎬 分镜到成片</h3><p class="muted" style="margin:0">
      剧本 → 美术设定 → 分镜 → 关键帧 → 视频 → 配乐合成，每一步都能看、能改、能重跑。</p></div>
    <div class="card"><h3>🔑 用你自己的 Key</h3><p class="muted" style="margin:0">
      大模型、图片、音频都填你自己的 API Key，按量付费不经手第三方。视频生成走平台中转。</p></div>
  </div>

  <h2>怎么开始</h2>
  <div class="card">
    <ol style="margin-bottom:0">
      <li><b>下载安装</b> —— 双击安装包，不需要管理员权限，装完自动打开。</li>
      <li><b>注册登录</b> —— 邮箱收验证码即可，新账号送体验额度。</li>
      <li><b>填 API Key</b> —— 首次启动的向导里配大模型和图片生成（都是你自己的账号）。</li>
      <li><b>说一句话</b> —— 剩下的交给 AI。</li>
    </ol>
  </div>

  <h2>常见问题</h2>
  <h3>要花多少钱？</h3>
  <p>两部分：<b>你自己的 API 用量</b>（大模型/图片，按各家官方价，直接付给厂商）+
     <b>视频生成的平台额度</b>（按视频秒数计费，账号内可查余额与流水）。</p>
  <h3>需要显卡吗？</h3>
  <p>不需要。生成都在云端。只有可选的
     <a href="/docs/comfyui">场景转视角</a> 进阶功能要本地 N 卡。</p>
  <h3>支持 Mac 吗？</h3>
  <p>目前只有 Windows 版。</p>
  <h3>作品版权归谁？</h3>
  <p>归你。作品文件都存在你自己电脑上（<code>%LOCALAPPDATA%\\Octopus</code>）。</p>

  <div style="text-align:center;margin:60px 0 20px">
    <a class="btn" href="{DOWNLOAD_URL}">立即下载 · v{DOWNLOAD_VERSION}</a>
  </div>
</div>
"""


# ═══════════════════════════════════════════════════════
# ComfyUI 转视角教程
# ═══════════════════════════════════════════════════════

_HF = "https://huggingface.co"
_COMFYUI_DOC = f"""
<div class="wrap" style="padding-top:44px">
  <h1 style="font-size:34px">场景转视角安装指南</h1>
  <p>让同一个场景能生成<b>真正换机位</b>的画面（比如圆桌对面的反打），而不只是同一面墙换取景。</p>
  <div class="note"><b>这是可选的进阶功能，不装也能正常用 Octopus。</b>
    默认的 Seedream 方案零配置、开箱即用，只是做不出真 180° 反打。</div>

  <h2>一、先想清楚要不要装</h2>
  <table>
    <tr><th>方案</th><th>效果</th><th>要求</th></tr>
    <tr><td><b>Seedream</b>（默认）</td><td>同一空间换取景、机位高度，但<b>朝向同一面墙</b></td><td>零配置</td></tr>
    <tr><td><b>Qwen 多角度</b>（本文）</td><td><b>真 180° 反打 / 换机位</b></td><td>本地 N 卡 + ComfyUI + 约 30GB 模型</td></tr>
  </table>
  <p><b>建议</b>：只想出片 → 不用装；要拍对话戏、需要正反打，且有 12GB 以上显存的 N 卡 → 值得装。</p>
  <div class="note warn"><b>硬件底线</b>：NVIDIA 显卡，显存 12GB 起（16GB+ 更稳，24GB 最舒服）。
    A 卡 / 核显 / Mac 暂不支持。</div>

  <h2>二、装 ComfyUI</h2>
  <p>去官网下 Windows 一键包：<a href="https://www.comfy.org/download" target="_blank">comfy.org/download</a></p>
  <p>装完先启动一次，确认浏览器能打开 <code>http://127.0.0.1:8188</code>，然后关掉去下模型。</p>
  <div class="note">显存不足 16GB 的话等下要用 GGUF 版模型，需要在 ComfyUI 里装个插件：
    <b>Manager → Custom Nodes → 搜 <code>ComfyUI-GGUF</code> → 安装 → 重启</b>。</div>

  <h2>三、下模型</h2>
  <p>点下面的链接直接去下载页。<b>务必按表放进对应文件夹</b> —— 放错目录是最常见的失败原因。
     国内下载慢可以把网址里的 <code>huggingface.co</code> 换成 <code>hf-mirror.com</code>。</p>

  <h3>必装（4 个）</h3>
  <table>
    <tr><th>文件</th><th>下载</th><th>放到 ComfyUI 的</th><th>大小</th></tr>
    <tr><td><code>qwen_image_edit_2511_fp8mixed.safetensors</code></td>
        <td><a href="{_HF}/Comfy-Org/Qwen-Image-Edit_ComfyUI/tree/main/split_files/diffusion_models" target="_blank">Comfy-Org/Qwen-Image-Edit_ComfyUI ↗</a></td>
        <td><code>models/diffusion_models/</code></td><td>~20GB</td></tr>
    <tr><td><code>qwen_2.5_vl_7b_fp8_scaled.safetensors</code></td>
        <td><a href="{_HF}/Comfy-Org/Qwen-Image_ComfyUI/tree/main/split_files/text_encoders" target="_blank">Comfy-Org/Qwen-Image_ComfyUI ↗</a></td>
        <td><code>models/text_encoders/</code></td><td>~8GB</td></tr>
    <tr><td><code>qwen_image_vae.safetensors</code></td>
        <td><a href="{_HF}/Comfy-Org/Qwen-Image_ComfyUI/tree/main/split_files/vae" target="_blank">同上 · vae ↗</a></td>
        <td><code>models/vae/</code></td><td>~250MB</td></tr>
    <tr><td><b><code>qwen-image-edit-2511-multiple-angles-lora.safetensors</code></b></td>
        <td><a href="{_HF}/fal/Qwen-Image-Edit-2511-Multiple-Angles-LoRA" target="_blank">fal/…Multiple-Angles-LoRA ↗</a></td>
        <td><code>models/loras/</code></td><td>295MB</td></tr>
  </table>
  <div class="note">最后那个<b>多角度 LoRA 是关键</b>，没它就没有“换机位”的能力。</div>

  <h3>强烈推荐：提速 3–5 倍</h3>
  <table>
    <tr><th>文件</th><th>下载</th><th>放到</th><th>大小</th></tr>
    <tr><td><code>Qwen-Image-Edit-2511-Lightning-8steps-V1.0-bf16.safetensors</code></td>
        <td><a href="{_HF}/lightx2v/Qwen-Image-Edit-2511-Lightning" target="_blank">lightx2v/…Lightning ↗</a></td>
        <td><code>models/loras/</code></td><td>850MB</td></tr>
  </table>
  <p>不加：一张图 40–70 秒。加了：<b>10–20 秒</b>。</p>

  <h3>显存不够 20GB：换 GGUF 量化版</h3>
  <p>不下上面第一个 20GB 的 fp8，改下量化版（其余 3 个照下）：</p>
  <table>
    <tr><th>显存</th><th>文件</th><th>放到</th><th>大小</th></tr>
    <tr><td>16GB</td><td><code>qwen-image-edit-2511-Q5_K_M.gguf</code></td><td><code>models/unet/</code></td><td>14.9GB</td></tr>
    <tr><td>12GB</td><td><code>qwen-image-edit-2511-Q4_K_S.gguf</code></td><td><code>models/unet/</code></td><td>12.1GB</td></tr>
  </table>
  <p>下载：<a href="{_HF}/QuantStack/Qwen-Image-Edit-GGUF" target="_blank">QuantStack/Qwen-Image-Edit-GGUF ↗</a>
     或 <a href="{_HF}/unsloth/Qwen-Image-Edit-2511-GGUF" target="_blank">unsloth/…GGUF ↗</a></p>
  <div class="note warn">
    <b>GGUF 必须放 <code>models/unet/</code></b>（不是 diffusion_models），且要先装 <code>ComfyUI-GGUF</code> 插件。<br>
    <b>别选 Q8_0</b>（21.8GB）—— 它和文本编码器挤不下，反而更慢。
  </div>

  <h2>四、在 Octopus 里打开</h2>
  <p>模型下完、ComfyUI 保持运行（<code>http://127.0.0.1:8188</code> 能打开），然后：</p>
  <div class="card"><ol style="margin-bottom:0">
    <li><b>系统设置 → 本地 ComfyUI</b> ——「ComfyUI 地址」保持 <code>http://127.0.0.1:8188</code>，
        点 <b>「测试 ComfyUI 连接」</b>，显示已连接 + 你的显卡型号 = 通了。</li>
    <li><b>系统设置 → 图片生成</b> ——「场景机位板生成器」选
        <b>「Qwen 多角度（本地 ComfyUI·真换机位）」</b>。</li>
    <li><b>重跑美术</b>（老项目要重跑才生效）。</li>
  </ol></div>

  <h2>五、常见问题</h2>
  <h3>测试连接失败</h3>
  <p>确认 ComfyUI 正在运行、浏览器能打开 <code>http://127.0.0.1:8188</code>。
     如果 ComfyUI 装在 WSL 或另一台机器，把设置里的地址改成对应 IP。</p>
  <h3>生成很慢，一张要好几分钟</h3>
  <p>八成是没装 Lightning LoRA，或显存不够在跑 lowvram。装上 Lightning、或换更小的 GGUF 量化版。</p>
  <h3>出来的图还是没换机位</h3>
  <p>Octopus 有兜底：<b>Qwen 失败会自动回退 Seedream</b>，不会卡住出片。所以看到“没换机位”通常是回退了
     —— 去 ComfyUI 那边看有没有报错（缺模型、显存不够）。</p>
  <h3>反打的画面细节不对</h3>
  <p>反打属于“新视角合成”，模型看不见的地方靠脑补，<b>空场景、简单构图效果最好</b>。
     Octopus 正是先出空场景机位板、再把角色融进去，就是为了避开这个问题。人多、遮挡重的镜头容易露馅。</p>
  <h3>能同时跑视频生成吗</h3>
  <p>尽量别 —— 两个都吃显存会互相挤。出机位板时先别跑本地视频。</p>
  <h3>不想用了怎么关</h3>
  <p>系统设置 → 图片生成 → 场景机位板生成器 → 切回 <b>Seedream</b>。</p>

  <h2>六、进阶：用自己的工作流</h2>
  <p>Octopus 自带了一份转视角工作流，一般不用管。想自己调质量/步数/LoRA 强度的话：
     在 ComfyUI 里改好参数 → <b>导出为 API 格式</b>（保存(API格式)）→
     Octopus <b>系统设置 → 本地 ComfyUI → 转视角工作流(.json)</b> 填你的文件路径。</p>
  <p>Octopus 是<b>运行时读这个文件</b>的，所以在 ComfyUI 调完重新导出，下次生成就用新参数，不用重启。</p>
  <div class="note">工作流里必须有 <code>LoadImage</code>（喂原图）、<code>KSampler</code>、
     正向 <code>CLIPTextEncode</code>（写角度提示词）这几个节点，Octopus 按节点类型自动定位并填参数。</div>
</div>
"""


# ═══════════════════════════════════════════════════════
# 获取 API Key 图文教程（应用内设置页/向导都链到这里，含锚点）
# ═══════════════════════════════════════════════════════

_KEYS_DOC = """
<div class="wrap" style="padding-top:44px">
  <h1 style="font-size:34px">怎么获取 API Key</h1>
  <p>Octopus 的<b>大模型、图片、音乐</b>都用你自己的账号（业内叫 BYOK），
     好处是按官方原价付费、没有中间商加价，你的额度自己完全掌控。</p>
  <div class="note">
    <b>只有「视频生成」用平台额度（credits）</b>，那部分在软件里直接充值即可，不需要注册任何第三方。
  </div>

  <h2>需要配几个？</h2>
  <table>
    <tr><th>类型</th><th>用来干嘛</th><th>必须吗</th><th>推荐</th></tr>
    <tr><td><b>大模型</b></td><td>导演/编剧/分镜的"大脑"</td><td>✅ 必须</td><td><a href="#deepseek">DeepSeek</a>（最便宜、免梯子）</td></tr>
    <tr><td><b>图片·文生图</b></td><td>画场景、道具</td><td>✅ 必须</td><td><a href="#zhipu">智谱</a></td></tr>
    <tr><td><b>图片·Seedream</b></td><td>画角色三视图、镜头关键帧</td><td>✅ 必须</td><td><a href="#seedream">火山方舟</a></td></tr>
    <tr><td><b>音乐</b></td><td>背景音乐</td><td>⚪ 可选</td><td><a href="#elevenlabs">ElevenLabs</a></td></tr>
  </table>
  <div class="note warn">
    图片要配 <b>两个</b>：智谱画场景道具、Seedream 画角色和关键帧，分工不同、都得有。<br>
    音乐不配也能正常出片，只是成片没有 BGM。
  </div>

  <h2>大概要花多少钱</h2>
  <p>做一集短剧（4-6 个镜头）的第三方 API 成本大致是：</p>
  <table>
    <tr><th>项目</th><th>一集大概</th><th>说明</th></tr>
    <tr><td>大模型（DeepSeek）</td><td>几毛钱</td><td>充 10-20 元能用很久</td></tr>
    <tr><td>图片（智谱 + Seedream）</td><td>1-3 元</td><td>按张计费，几分钱一张</td></tr>
    <tr><td>音乐（ElevenLabs）</td><td>订阅制 $5/月起</td><td>可不配</td></tr>
    <tr><td><b>视频（平台 credits）</b></td><td><b>约 15-20 元</b></td><td>0.8 元/秒，软件内充值</td></tr>
  </table>
  <p class="muted">以上为估算，实际以各厂商计费为准。</p>

  <h2 id="deepseek">① DeepSeek（大模型 · 推荐新手）</h2>
  <div class="card">
    <p><b>为什么推荐</b>：国内直连不用梯子、注册最简单、价格最低。</p>
    <ol>
      <li>打开 <a href="https://platform.deepseek.com" target="_blank">platform.deepseek.com ↗</a>，用手机号注册登录</li>
      <li>左侧菜单 →「<b>API keys</b>」→「创建 API key」→ 起个名字 → 创建</li>
      <li>复制出来的 <code>sk-</code> 开头的字符串
          <br><span class="muted">⚠️ 只显示这一次，关掉就看不到了，先粘到记事本存好</span></li>
      <li>左侧「<b>充值</b>」充 10-20 元（新账号可能有赠送额度）</li>
      <li>回到 Octopus → 系统设置 → 大模型 → 服务商选 <b>DeepSeek</b> → 粘贴 key → 保存 → 点「测试 LLM 连接」</li>
    </ol>
  </div>

  <h2>① 备选：Kimi / OpenAI</h2>
  <div class="card">
    <p><b>Kimi</b>：<a href="https://platform.moonshot.cn" target="_blank">platform.moonshot.cn ↗</a> →
       手机号注册 →「API Key 管理」→ 新建 → 复制 <code>sk-</code> 串。国内直连，新号一般有免费额度。</p>
    <p><b>OpenAI</b>：<a href="https://platform.openai.com/api-keys" target="_blank">platform.openai.com/api-keys ↗</a> →
       Create new secret key → 复制。<b>需要科学上网 + 绑卡充值</b>，国内用起来最麻烦。</p>
  </div>

  <h2 id="zhipu">② 智谱 GLM（图片 · 画场景和道具）</h2>
  <div class="card">
    <ol>
      <li>打开 <a href="https://open.bigmodel.cn" target="_blank">open.bigmodel.cn ↗</a>，手机号注册登录</li>
      <li>完成<b>实名认证</b>（会送免费额度）</li>
      <li>右上角头像 →「<b>API Keys</b>」→ 复制那串 key
          <br><span class="muted">形如 <code>xxxxxxxx.xxxxxxxx</code>，中间有个点，这是正常的</span></li>
      <li>Octopus → 系统设置 → 图片生成 → 服务商选 <b>智谱</b> → 粘贴 → 保存 → 测试</li>
    </ol>
  </div>

  <h2 id="seedream">③ 火山方舟 Seedream（图片 · 画角色和关键帧）</h2>
  <div class="card">
    <ol>
      <li>打开 <a href="https://console.volcengine.com/ark" target="_blank">console.volcengine.com/ark ↗</a>，注册并<b>实名认证</b></li>
      <li>左侧「<b>开通管理</b>」→ 找到 <b>Doubao-Seedream</b> 系列 → 点「<b>开通服务</b>」
        <div class="note warn" style="margin:10px 0">
          <b>⚠️ 这一步最容易漏！</b>很多人只创建了 key 却没开通模型，
          结果 key 是有的、调用却一直报错。务必确认 Seedream 显示"已开通"。
        </div>
      </li>
      <li>左侧「<b>API Key 管理</b>」→「创建 API Key」→ 复制 <code>ark-</code> 开头的串</li>
      <li>账户里充点钱（出图按张计费，几分钱一张）</li>
      <li>Octopus → 系统设置 → 图片生成 → 找到「<b>Seedream Key（火山方舟）</b>」→ 粘贴 → 保存</li>
    </ol>
  </div>

  <h2 id="elevenlabs">④ ElevenLabs（音乐 · 可选）</h2>
  <div class="card">
    <p class="muted">不配这个也能正常出片，只是成片没有背景音乐。</p>
    <ol>
      <li>打开 <a href="https://elevenlabs.io" target="_blank">elevenlabs.io ↗</a> 注册（需要科学上网）</li>
      <li><b>订阅 Starter（$5/月）或以上</b> —— 免费版没有音乐生成权限</li>
      <li>头像 → Settings → <b>API Keys</b> →「Create API Key」</li>
      <li>创建时<b>勾上 Music 相关权限</b>
          <br><span class="muted">⚠️ 不勾的话调用会报 missing_permission</span></li>
      <li>复制 <code>sk_</code> 开头的串 → Octopus → 系统设置 → 音频生成 → 粘贴</li>
    </ol>
  </div>

  <h2>常见问题</h2>
  <h3>Key 会不会被上传到你们服务器？</h3>
  <p><b>不会。</b>所有第三方 key 都<b>加密存在你自己电脑上</b>（<code>%LOCALAPPDATA%\\Octopus</code>），
     平台不保存、也看不到。</p>
  <h3>填完点「测试」失败怎么办？</h3>
  <ul>
    <li><b>智谱/DeepSeek</b>：检查有没有实名认证、账户有没有余额</li>
    <li><b>Seedream</b>：99% 是<b>没开通 Doubao-Seedream 模型</b>（见上面第 2 步）</li>
    <li><b>OpenAI/Google/ElevenLabs</b>：检查科学上网是否全局生效</li>
    <li>key 前后有没有多余的空格 / 引号（复制时容易带上）</li>
  </ul>
  <h3>可以几家混着用吗？</h3>
  <p>可以。大模型选一家、文生图选一家，Seedream 单独配，互不影响。随时能在设置里切换。</p>

  <div style="text-align:center;margin:56px 0 20px">
    <a class="btn ghost" href="/docs/guide">← 回到使用教程</a>
  </div>
</div>
"""


# ═══════════════════════════════════════════════════════
# 使用教程（应用内「使用帮助」入口指向这里）
# ═══════════════════════════════════════════════════════

_GUIDE_DOC = f"""
<div class="wrap" style="padding-top:44px">
  <h1 style="font-size:34px">使用教程</h1>
  <p>从装好软件到导出成片，完整走一遍。第一次用大概需要 20 分钟准备（主要花在注册各家账号上），
     之后每次创作就是"说一句话"的事。</p>

  <h2>第一步：安装并登录</h2>
  <div class="card">
    <ol>
      <li>下载 <a href="{DOWNLOAD_URL}">Octopus 安装包</a>，双击安装（不需要管理员权限）</li>
      <li>启动后会自动打开浏览器界面</li>
      <li><b>邮箱注册登录</b> —— 填邮箱 → 收验证码 → 登录。新账号赠送体验额度</li>
    </ol>
    <p class="muted" style="margin-bottom:0">软件本体和你的作品都在自己电脑上，只有视频生成会走平台。</p>
  </div>

  <h2>第二步：配置 API Key</h2>
  <div class="card">
    <p>首次登录会进入<b>配置向导</b>，跟着两步填就行：</p>
    <ol>
      <li><b>大模型</b> —— 导演/编剧/分镜的大脑</li>
      <li><b>图片生成</b> —— 智谱（画场景道具）+ Seedream（画角色关键帧），<b>两个都要</b></li>
    </ol>
    <p>每家怎么注册、key 在哪找，看这篇：<a href="/docs/keys"><b>怎么获取 API Key →</b></a></p>
    <p class="muted" style="margin-bottom:0">音乐（ElevenLabs）是可选的，跳过也能出片，只是没 BGM。
       之后随时可以在「系统设置」里补。</p>
  </div>

  <h2>第三步：说一句话，开始创作</h2>
  <div class="card">
    <p>在左边聊天框里描述你想拍什么，比如：</p>
    <pre><code>拍一个 2 岁小姑娘和小猫在客厅玩耍的温馨短片，30 秒左右，竖屏</code></pre>
    <p>AI 导演会追问几个问题（时长、调性、横竖屏等），聊清楚之后它就自动往下做了。</p>
    <p class="muted" style="margin-bottom:0">💡 说得越具体越好：角色几个人、什么场景、什么情绪、多长时间。</p>
  </div>

  <h2>第四步：跟着流程一步步确认</h2>
  <p>右边画布会长出一条流水线，<b>每一步都要你点「确认」才会往下走</b> —— 不满意就点「重跑」。</p>
  <table>
    <tr><th>阶段</th><th>产出</th><th>你要做的</th></tr>
    <tr><td><b>剧本</b></td><td>标题、一句话梗概、分场</td><td>看一眼，不满意让 AI 改</td></tr>
    <tr><td><b>美术设定</b></td><td>角色三视图、场景图、道具图</td><td>确认每张图；不喜欢就重跑那一张</td></tr>
    <tr><td><b>分镜</b></td><td>每个镜头的画面描述、时长、运镜</td><td>确认镜头表</td></tr>
    <tr><td><b>关键帧</b></td><td>每个镜头的首帧画面</td><td>确认；这步决定了视频长什么样</td></tr>
    <tr><td><b>视频</b></td><td>每个镜头的视频片段</td><td>确认；这一步消耗 credits</td></tr>
    <tr><td><b>合成</b></td><td>拼接 + 配乐的成片</td><td>导出</td></tr>
  </table>
  <div class="note">
    <b>连续画面怎么保证接得上？</b>相邻镜头会自动用上一镜的<b>最后一帧</b>当下一镜的起始画面，
    所以动作是连贯的。角色的脸也会被锁定，不会越拍越不像。
  </div>

  <h2>第五步：拿到成片</h2>
  <p>合成完成后可以直接下载。所有素材和成片都存在你自己电脑上：</p>
  <pre><code>%LOCALAPPDATA%\\Octopus\\output\\</code></pre>
  <p class="muted">按 Win+R 输入这个路径就能打开。</p>

  <h2>关于费用</h2>
  <table>
    <tr><th>花在哪</th><th>付给谁</th><th>大概</th></tr>
    <tr><td>大模型、图片、音乐</td><td><b>各厂商</b>（用你自己的 key）</td><td>一集几元</td></tr>
    <tr><td>视频生成</td><td><b>Octopus 平台</b>（credits）</td><td>0.8 元/秒</td></tr>
  </table>
  <p>credits 在软件里点「充值」用支付宝购买。余额在左侧栏随时可见，
     不够时会提醒你。</p>

  <h2>常见问题</h2>
  <h3>生成的角色前后长得不一样</h3>
  <p>确认<b>美术阶段的角色三视图</b>已经生成并确认过 —— 后续所有镜头都以它为准锁脸。
     如果三视图本身就不理想，先把它重跑到满意再往下走。</p>
  <h3>视频变成了一张静态图 / 提示"占位"</h3>
  <p>多半是 <b>credits 不够</b>被降级了。看左侧栏余额，充值后在那个镜头上点「重跑」即可。</p>
  <h3>某一步卡住不动</h3>
  <p>先看该节点有没有报错提示。常见原因：某家 API key 失效/欠费、网络不通。
     去「系统设置」对应分类点「测试连接」能快速定位。</p>
  <h3>想改中间某一步</h3>
  <p>直接在那个节点点「重跑」。注意<b>改了上游会让下游失效</b>（比如重跑分镜，后面的关键帧和视频要重做）。</p>
  <h3>作品版权归谁</h3>
  <p>归你。文件都在你自己电脑上，平台不保存你的作品。</p>

  <h2>进阶</h2>
  <p>想让同一个场景出<b>真正的反打机位</b>（而不只是换个取景角度），
     可以接本地 ComfyUI：<a href="/docs/comfyui">场景转视角安装指南 →</a>（可选，需要 N 卡）</p>

  <div style="text-align:center;margin:56px 0 20px">
    <a class="btn" href="{DOWNLOAD_URL}">下载 Octopus</a>
    &nbsp;&nbsp;
    <a class="btn ghost" href="/docs/keys">获取 API Key</a>
  </div>
</div>
"""


@router.get("/v1/app/latest")
async def app_latest():
    """客户端「检查更新」问这里。发新版只需改文件顶部 DOWNLOAD_VERSION/RELEASE_NOTES 并重启云端。"""
    return {
        "version": DOWNLOAD_VERSION,
        "url": DOWNLOAD_URL,
        "notes": RELEASE_NOTES,
        "size_mb": 120,
    }


@router.get("/docs/keys", response_class=HTMLResponse)
async def docs_keys() -> str:
    return _page("怎么获取 API Key — Octopus", _KEYS_DOC)


@router.get("/docs/guide", response_class=HTMLResponse)
async def docs_guide() -> str:
    return _page("使用教程 — Octopus", _GUIDE_DOC)


@router.get("/", response_class=HTMLResponse)
async def landing() -> str:
    return _page("Octopus — 对话式 AI 视频创作", _LANDING)


@router.get("/docs/comfyui", response_class=HTMLResponse)
async def docs_comfyui() -> str:
    return _page("场景转视角安装指南 — Octopus", _COMFYUI_DOC)
