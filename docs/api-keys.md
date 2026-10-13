# 怎么获取 API Key

Octopus 开源版不连任何平台：**大模型、图片、视频、音乐全部用你自己的账号**（业内叫 BYOK）。
好处是按官方原价付费、没有中间商，额度完全由你掌控。

## 需要配几个？

| 类型 | 用来干嘛 | 必须吗 | 推荐 |
| --- | --- | --- | --- |
| **大模型** | 导演 / 编剧 / 分镜的"大脑" | ✅ 必须 | [DeepSeek](#deepseek)（最便宜、免梯子） |
| **图片·文生图** | 画场景、道具 | ✅ 必须 | [智谱](#zhipu) |
| **图片·Seedream** | 画角色三视图、镜头关键帧 | ✅ 必须 | [火山方舟](#seedream) |
| **视频** | 把关键帧变成视频 | ✅ 必须（二选一） | [火山方舟 Seedance](#seedance) 或 [本地 ComfyUI](#comfyui) |
| **音乐** | 背景音乐 | ⚪ 可选 | [ElevenLabs](#elevenlabs) |

图片要配 **两个**：智谱画场景道具、Seedream 画角色和关键帧，分工不同、都得有。
Seedream 和 Seedance 是同一个火山方舟账号、同一个 key，开通两个模型就行。
音乐不配也能正常出片，只是成片没有 BGM。

## 大概要花多少钱

做一集短剧（4-6 个镜头）的 API 成本大致是：

| 项目 | 一集大概 | 说明 |
| --- | --- | --- |
| 大模型（DeepSeek） | 几毛钱 | 充 10-20 元能用很久 |
| 图片（智谱 + Seedream） | 1-3 元 | 按张计费，几分钱一张 |
| 视频（Seedance） | 视镜头总时长而定 | 按秒计费，以火山方舟官方价格为准 |
| 视频（本地 ComfyUI） | 不花钱 | 需要一块大显存的 N 卡，吃电费和时间 |
| 音乐（ElevenLabs） | 订阅制 $5/月起 | 可不配 |

以上为估算，实际以各厂商计费为准。

<a id="deepseek"></a>

## ① DeepSeek（大模型 · 推荐新手）

**为什么推荐**：国内直连不用梯子、注册最简单、价格最低。

1. 打开 [platform.deepseek.com](https://platform.deepseek.com)，用手机号注册登录
2. 左侧菜单 →「**API keys**」→「创建 API key」→ 起个名字 → 创建
3. 复制出来的 `sk-` 开头的字符串

   ⚠️ 只显示这一次，关掉就看不到了，先存好
4. 左侧「**充值**」充 10-20 元（新账号可能有赠送额度）
5. 回到 Octopus → 系统设置 → 大模型 → 服务商选 **DeepSeek** → 粘贴 key → 保存 → 点「测试 LLM 连接」

## ① 备选：Kimi / OpenAI

**Kimi**：[platform.moonshot.cn](https://platform.moonshot.cn) →
手机号注册 →「API Key 管理」→ 新建 → 复制 `sk-` 串。国内直连，新号一般有免费额度。

**OpenAI**：[platform.openai.com/api-keys](https://platform.openai.com/api-keys) →
Create new secret key → 复制。**需要科学上网 + 绑卡充值**，国内用起来最麻烦。

<a id="zhipu"></a>

## ② 智谱 GLM（图片 · 画场景和道具）

1. 打开 [open.bigmodel.cn](https://open.bigmodel.cn)，手机号注册登录
2. 完成**实名认证**（会送免费额度）
3. 右上角头像 →「**API Keys**」→ 复制那串 key

   形如 `xxxxxxxx.xxxxxxxx`，中间有个点，这是正常的
4. Octopus → 系统设置 → 图片生成 → 服务商选 **智谱** → 粘贴 → 保存 → 测试

<a id="seedream"></a>

## ③ 火山方舟 Seedream（图片 · 画角色和关键帧）

1. 打开 [console.volcengine.com/ark](https://console.volcengine.com/ark)，注册并**实名认证**
2. 左侧「**开通管理**」→ 找到 **Doubao-Seedream** 系列 → 点「**开通服务**」

   ⚠️ **这一步最容易漏！** 很多人只创建了 key 却没开通模型，结果 key 是有的、调用却一直报错。务必确认显示"已开通"。
3. 左侧「**API Key 管理**」→「创建 API Key」→ 复制 `ark-` 开头的串
4. 账户里充点钱（出图按张计费，几分钱一张）
5. Octopus → 系统设置 → 图片生成 → 找到「**Seedream Key（火山方舟）**」→ 粘贴 → 保存

<a id="seedance"></a>

## ④ 火山方舟 Seedance（视频）

和上一步是**同一个控制台、同一个 key**。

1. 在 [火山方舟控制台](https://console.volcengine.com/ark)「**开通管理**」里，再把 **Doubao-Seedance** 系列开通
2. Octopus → 系统设置 → 视频生成 → 服务商选「**火山方舟 Seedance**」→「火山方舟 API Key」粘贴同一个 `ark-` key → 保存

视频按秒计费，是整条流程里最花钱的一步。建议先拿一两个镜头试跑，满意了再批量生成。

<a id="comfyui"></a>

## ④ 备选：本地 ComfyUI（视频 · 不花钱）

有一块大显存的 N 卡，可以完全不用云端视频服务。

1. 装好 [ComfyUI](https://github.com/comfyanonymous/ComfyUI) 和要用的视频模型（Wan 2.2 / LTX-2.3 / MiniMax H3）
2. Octopus → 系统设置 → 本地 ComfyUI → 地址填 `http://127.0.0.1:8188`
3. 视频生成 → 服务商选「**ComfyUI**」

各模型的注意事项见 [README 的本地视频生成一节](../README.md#本地视频生成comfyui可选)。

<a id="elevenlabs"></a>

## ⑤ ElevenLabs（音乐 · 可选）

不配这个也能正常出片，只是成片没有背景音乐。

1. 打开 [elevenlabs.io](https://elevenlabs.io) 注册（需要科学上网）
2. **订阅 Starter（$5/月）或以上** —— 免费版没有音乐生成权限
3. 头像 → Settings → **API Keys** →「Create API Key」
4. 创建时**勾上 Music 相关权限**

   ⚠️ 不勾的话调用会报 `missing_permission`
5. 复制 `sk_` 开头的串 → Octopus → 系统设置 → 音频生成 → 粘贴

## 常见问题

### Key 存在哪？会不会被上传？

**只存在你自己电脑上**，开源版没有任何服务器。
key 存在本机 SQLite 里（源码运行在 `backend/octopus.db`，桌面版在 `%LOCALAPPDATA%\Octopus`），
敏感字段加密，主密钥在 `~/.octopus/master.key`。

### 填完点「测试」失败怎么办？

- **智谱 / DeepSeek**：检查有没有实名认证、账户有没有余额
- **Seedream / Seedance**：99% 是**没开通对应模型**（见上面「开通管理」那一步）
- **OpenAI / Google / ElevenLabs**：检查科学上网是否全局生效
- key 前后有没有多余的空格 / 引号（复制时容易带上）

### 可以几家混着用吗？

可以。大模型选一家、文生图选一家、视频选一家，Seedream 单独配，互不影响。随时能在设置里切换。

[← 回到使用教程](guide.md)
