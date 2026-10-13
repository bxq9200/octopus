# 场景转视角（Qwen 多角度）安装指南

> **这是一个可选的进阶功能。不装也能正常用 Octopus。**
>
> 装了之后，同一个场景可以生成**真正换机位**的画面（比如圆桌对面的反打），
> 而不只是"同一面墙换个取景角度"。

---

## 一、先搞清楚要不要装

Octopus 生成场景机位板有两种方式：

| | 效果 | 要求 |
|---|---|---|
| **Seedream（默认）** | 同一空间换取景/机位高度，**朝向同一面墙** | 零配置，开箱即用 |
| **Qwen 多角度（本文）** | **真 180° 反打 / 换机位** | 本地 N 卡 + 装 ComfyUI + 下 ~30GB 模型 |

**建议**：
- 只是想出片 → **不用装**，默认的 Seedream 够用
- 拍对话戏、需要正反打、有 12GB 以上显存的 N 卡 → 值得装

**硬件底线**：NVIDIA 显卡，显存 **12GB 起**（16GB+ 更稳，24GB 最舒服）。A 卡 / 核显 / Mac 暂不支持。

---

## 二、装 ComfyUI

去官网下 ComfyUI（Windows 一键包即可）：<https://www.comfy.org/download>

装完先启动一次，确认浏览器能打开 `http://127.0.0.1:8188`，然后关掉，去下模型。

> 如果显存不足 16GB，等下你要用 GGUF 版模型，需要额外装个插件：
> ComfyUI 里 **Manager → Custom Nodes → 搜 `ComfyUI-GGUF` → 安装 → 重启**。

---

## 三、下模型

模型都从 [HuggingFace](https://huggingface.co) 下（国内打不开可用镜像 `hf-mirror.com`）。
下完**按下表放进 ComfyUI 对应文件夹**，放错目录会加载不出来。

### 必装（4 个）

| 文件 | 从哪下 | 放到 ComfyUI 的 | 大小 |
|---|---|---|---|
| `qwen_image_edit_2511_fp8mixed.safetensors` | `Comfy-Org/Qwen-Image-Edit_ComfyUI` → `split_files/diffusion_models/` | `models/diffusion_models/` | ~20GB |
| `qwen_2.5_vl_7b_fp8_scaled.safetensors` | `Comfy-Org/Qwen-Image_ComfyUI` → `split_files/text_encoders/` | `models/text_encoders/` | ~8GB |
| `qwen_image_vae.safetensors` | 同上 → `split_files/vae/` | `models/vae/` | ~250MB |
| **`qwen-image-edit-2511-multiple-angles-lora.safetensors`** | `fal/Qwen-Image-Edit-2511-Multiple-Angles-LoRA` | `models/loras/` | 295MB |

> 最后那个**多角度 LoRA 是关键**，没它就没有"换机位"能力。

### 强烈推荐（提速 3-5 倍）

| 文件 | 从哪下 | 放到 | 大小 |
|---|---|---|---|
| `Qwen-Image-Edit-2511-Lightning-8steps-V1.0-bf16.safetensors` | `lightx2v/Qwen-Image-Edit-2511-Lightning` | `models/loras/` | 850MB |

不加它：一张图 40–70 秒。加了：**10–20 秒**。

### 显存不够 20GB 的话（替代主模型）

不下上表第一个 20GB 的 fp8，改下 GGUF 量化版：

| 显存 | 下这个 | 放到 | 大小 |
|---|---|---|---|
| 16GB | `qwen-image-edit-2511-Q5_K_M.gguf` | `models/unet/` | 14.9GB |
| 12GB | `qwen-image-edit-2511-Q4_K_S.gguf` | `models/unet/` | 12.1GB |

来源仓库：`QuantStack/Qwen-Image-Edit-GGUF` 或 `unsloth/Qwen-Image-Edit-2511-GGUF`

> ⚠️ **GGUF 必须放 `models/unet/`**（不是 diffusion_models），且要先装 `ComfyUI-GGUF` 插件。
> ⚠️ **别选 Q8_0**（21.8GB）—— 它和文本编码器挤不下，反而更慢。

---

## 四、在 Octopus 里打开

模型下完、ComfyUI 启动着（`http://127.0.0.1:8188` 能打开），然后：

1. Octopus → **系统设置 → 本地 ComfyUI**
   - **ComfyUI 地址**：默认 `http://127.0.0.1:8188`，一般不用改
   - 点 **「测试 ComfyUI 连接」** → 显示已连接 + 显卡型号 = 通了
2. Octopus → **系统设置 → 图片生成**
   - **场景机位板生成器** → 选 **「Qwen 多角度（本地 ComfyUI·真换机位）」**
3. **重跑美术**（老项目要重跑才生效）

搞定。之后生成场景机位板就会走本地 ComfyUI。

---

## 五、常见问题

**Q：测试连接失败 / 连不上**
确认 ComfyUI 正在运行、浏览器能打开 `http://127.0.0.1:8188`。如果 ComfyUI 装在 WSL 或另一台机器，把设置里的地址改成对应 IP。

**Q：生成很慢（一张要好几分钟）**
八成没装 Lightning LoRA，或者显存不够在跑 lowvram。装上 Lightning、或换更小的 GGUF 量化版。

**Q：出来的图还是没换机位 / 报错了**
Octopus 有兜底：**Qwen 失败会自动回退 Seedream**，不会卡住出片流程。所以看到"没换机位"通常是回退了 —— 检查 ComfyUI 那边有没有报错（缺模型、显存不够）。

**Q：反打的画面细节不对**
反打属于"新视角合成"，模型看不见的地方靠脑补。**空场景、简单构图效果最好**（Octopus 正是先出空场景机位板、再融角色进去，就是为了这个）。人多、遮挡重的镜头容易露馅。

**Q：能同时跑视频生成吗？**
尽量别 —— 两个都吃显存，会互相挤。建议出机位板时先别跑本地视频。

**Q：不想用了怎么关？**
设置 → 图片生成 → 场景机位板生成器 → 切回 **Seedream**。

---

## 六、进阶：换自己的工作流

Octopus 自带了一份转视角工作流，一般不用管。

如果你想自己调质量/步数/LoRA 强度：在 ComfyUI 里改好参数 → **导出为 API 格式**（保存(API格式)）→
Octopus **系统设置 → 本地 ComfyUI → 转视角工作流(.json)** 填你的文件路径。

Octopus 是**运行时读这个文件**的，所以你在 ComfyUI 里调完重新导出，下次生成就用新参数，不用重启。

> 工作流里必须有 `LoadImage`（喂原图）、`KSampler`、正向 `CLIPTextEncode`（写角度提示词）这几个节点，
> Octopus 按节点类型自动定位并填参数。
