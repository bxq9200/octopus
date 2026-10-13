# Tier 1 — Qwen-Image-Edit-2511 + Multiple-Angles LoRA 本地安装清单

> 目标：本地 ComfyUI 输入一张室内场景图 → 输出该房间**另一机位/真 180° 反打**的图 → 再喂下游 Seedream 图生图融角色。
> 环境：ComfyUI 在 WSL `Ubuntu-26.04`，路径 `/data/ComfyUI`（Windows 侧 `\\wsl.localhost\Ubuntu-26.04\data\ComfyUI`）；RTX 4090 / 24GB。
> 日期：2026-07-07。

## ① 下载清单（WSL 内 `/data/ComfyUI`，用 `wget` 或 `hf download` 拉到对应子目录）

| 文件 | HuggingFace 来源 | 放置目录 | 大小/档 |
|---|---|---|---|
| `qwen_image_edit_2511_fp8mixed.safetensors` | `Comfy-Org/Qwen-Image-Edit_ComfyUI` → `/split_files/diffusion_models/` | `models/diffusion_models/` | ~20GB fp8 |
| `qwen_2.5_vl_7b_fp8_scaled.safetensors` | `Comfy-Org/Qwen-Image_ComfyUI` → `/split_files/text_encoders/` | `models/text_encoders/` | ~8GB |
| `qwen_image_vae.safetensors` | 同上 → `/split_files/vae/` | `models/vae/` | ~250MB |
| **`qwen-image-edit-2511-multiple-angles-lora.safetensors`** | **`fal/Qwen-Image-Edit-2511-Multiple-Angles-LoRA`** | `models/loras/` | 295MB · Apache-2.0 |
| （加速，推荐）**`Qwen-Image-Edit-2511-Lightning-8steps-V1.0-bf16.safetensors`** | `lightx2v/Qwen-Image-Edit-2511-Lightning` | `models/loras/` | 850MB · 叠加用 |
| （显存吃紧备选）`Qwen_Image_Edit_2511-Q5_K_M.gguf` | `QuantStack/Qwen-Image-Edit-GGUF` 或 `unsloth/Qwen-Image-Edit-2511-GGUF` | `models/unet/` | 14.9GB |

**⚠ 版本对齐（关键）**：LoRA 必须选 **fal 的 2511 版**。别用 `dx8152/Qwen-Edit-2509-Multiple-angles`（那是旧 2509 基座，与 2511 权重不匹配，会糊/失效）。

## ② 触发语法（转机位 / 反打）

格式：`<sks> [方位] [仰角] [距离]`（**必须带 `<sks>` token，否则 LoRA 不激活**；角度关键词写**英文**）。共 96 位姿 = 8 方位 × 4 仰角 × 3 距离。

- **方位**：`front view` / `front-right quarter view` / `right side view` / `back-right quarter view` / **`back view`（=180° 反打/对面）** / `back-left quarter view` / `left side view` / `front-left quarter view`
- **仰角**：`low-angle shot`(仰) / `eye-level shot`(平) / `elevated shot`(30°) / `high-angle shot`(俯)
- **距离**：`close-up` / `medium shot` / `wide shot`

**常用**：
- 反打到对面 → `<sks> back view eye-level shot medium shot`
- 侧机位 → `<sks> right side view eye-level shot medium shot`
- 斜后方 135° → `<sks> back-left quarter view eye-level shot wide shot`
- 反打+俯拍空场景 → `<sks> back view high-angle shot wide shot`

LoRA 强度 **0.8–1.0，起手 0.9**。

## ③ 工作流

- **现成**：`fal` 模型卡 Files 里的 `comfyui-workflow-multiple-angles.json`（拖进 ComfyUI 即用）；或 ComfyUI 官方 2511 模板（docs.comfy.org/tutorials/image/qwen/qwen-image-edit-2511）再插一个 LoRA 节点。
- **节点链**：
  ```
  LoadImage(场景图) ┐
  CLIPLoader(qwen_2.5_vl_7b, type=qwen_image) ┐
  VAELoader(qwen_image_vae) ─────────────────┼→ TextEncodeQwenImageEditPlus(prompt=<sks> 角度)
  UNETLoader(fp8mixed, fp8_e4m3fn) → LoraLoaderModelOnly(multiple-angles, 0.9) → KSampler → VAEDecode → SaveImage
  ```
  GGUF 时把 `UNETLoader` 换成 `UnetLoaderGGUF`（需装 `city96/ComfyUI-GGUF`）。
- **KSampler 参数**：
  - fp8：steps **20**，cfg **4.0**，euler，simple，denoise **1.0**
  - 叠 Lightning LoRA：steps **4–8**，cfg **1.0**（其余不变）

## ④ 4090 推荐配置（最稳一档）

**主力 = `fp8mixed` + Lightning-8step LoRA**。fp8 主体 ~20GB，文本编码器由 ComfyUI 自动分批 offload，24GB 实测可跑；单图 20 步 ~40–70s，叠 Lightning(4–8 步) ~10–20s。
**若 OOM → 换 GGUF `Q5_K_M`（14.9GB，余量足、质量接近）**；Q4_K_S(12.1GB)余量更大。**别选 Q8_0(21.8GB)**（与文本编码器同驻强制 offload，反而更慢）。

## ⑤ 坑

1. **基座 2511 只配 fal 2511 LoRA**，混用会糊。
2. **`<sks>` 前缀不能漏**，角度写英文。
3. **反打是新视角合成，遮挡区靠模型脑补** → 空场景/简单构图（酒店圆桌包厢正合适）效果最好；人多/遮挡重会露馅 → 所以我们的路线是"**空场景出反打板 → 再喂 Seedream 融角色**"，正确。
4. 输入图先 resize 到 ~1MP（长边 1024、保比例），超大图会变形。
5. GGUF 放 `models/unet/` 且必装 ComfyUI-GGUF；fp8 放 `models/diffusion_models/` 用原生 `UNETLoader`，目录别放错。

## 接入 Octopus（下载+验证后）
Octopus 已有 ComfyUI adapter（`backend/app/modules/_comfyui_video.py` 走 `127.0.0.1:8188`）。Tier 1 集成 = 新增一个 `_comfyui_qwen_angle` 调用（提交本工作流 API 版 + 场景图 + `<sks> 角度` prompt → 取输出图），把 `_generate_camera_boards` 里"Seedream 图生图出板"替换/择优为"Qwen 转机位出板"。`cameras[]` / `@场景#机位` 通道不变，只换板生成器。**先在 ComfyUI 自带 UI 用现成 workflow 跑通一张反打，确认效果，再接线。**
