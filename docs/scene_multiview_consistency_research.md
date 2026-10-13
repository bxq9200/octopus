# 场景多机位一致性 — 技术调研与落地方案

> 日期：2026-07-06 ｜ 触发：0.7.4 导演层落地后暴露的新痛点 ｜ 调研方式：4 路独立子 agent 并行 web 调研后综合

## 一、问题定义

一个场景美术**只生成一张图**，该场景下所有分镜（shot 8 / 9 / 10 …）复用这一张 →「**换角色不换景**」，无法表现同一房间的不同机位（正打/反打、圆桌这一侧 vs 对面另一侧），场景显得重复、不够丰富。

用户提出两个候选方向：
- **A) 分镜后按需 outpainting 扩图**：根据分镜脚本在已有场景图上扩出缺失的机位/角度。
- **B) 美术阶段直接出 360° 全景图**：之后各分镜从全景裁剪不同视角，天然保持同景一致。

约束：贴合火山栈（Seedream 图 / Seedance 2.0 视频 / 即梦），尽量小改现有架构。

---

## 二、四路调研结论（精炼）

### 路 1 — 360° 全景图（方向 B）
- 生成室内 360° 全景已成熟：**Blockade Labs Skybox AI**（8K equirect + 深度图，有 API，商用最省事）、**腾讯 HunyuanWorld / HY-World 2.0**（开源，图/文→360，可导 mesh/3DGS）、**阿里 Diffusion360**（ModelScope 开源，中文友好）。
- **equirectangular → 透视裁剪是确定性几何**（`py360convert.e2p(yaw, pitch, fov)`），同一张全景按不同角度裁，**天然保证"同房间、不同机位、跨镜一致"**。
- **硬伤**：全景是**球心单点**视角——只能换"**朝向**"（转头看四周，正打/反打≈180° yaw 可近似），**不能换"站位"**（圆桌这侧走到对面是空间平移，全景做不出真视差）。
- 分辨率偏软，但**它只当机位构图底图**，送进现有 Seedream 图生图环节插角色+升质时被吸收。
- Seedream 4.0 宽高比可到 21:9，但**无原生 equirect/360 模式**，不能直接产出可等距裁剪的球面全景。

### 路 2 — outpainting / 反打 / 单图3D（方向 A）
- **核心洞察（已证实）**：outpainting 本质只做"画框边界外延"，补的是同侧墙/家具延伸，**几何视点不变 → 原理上做不出 180° 反打**。桌子对面是从未观测的全新视角，属**新视角合成（NVS）**，不是画面外扩。**方向 A 用 outpainting 求反打 = 死路。**
- 真解法两条：
  - **（推荐）指令式换机位**：**Qwen-Image-Edit-2511 Multiple-Angles LoRA**（开源、可自部署 / ComfyUI）——单图→96 离散机位（方位 0–315°、俯仰 −30–60°、远近三档，**含 180° back view**），训练自 3DGS 一致性渲染，换角度时布局/物体基本不变，一步图生图出底板。**当前投入产出比最高。** 闭源画质兜底：**Nano Banana Pro（Gemini 3 Pro Image）**。
  - **单图重建 3D**：腾讯 HunyuanWorld / Stable Virtual Camera 等，几何最严谨但产出是 3D 资产需再渲染、偏室外全景、工程量大，对 2D 漫剧底板属**过度工程**，列后续。
- **心态校准**：反打图只是给 Seedream 再融角色的**底板**，对面墙"真不真"无所谓，只要风格/光照/家具语言一致即可。验收锁"跨镜一致"，不追反打真实性。

### 路 3 — 业界/竞品 + 学术
- **趋势已明**：主流工具都在把"环境/地点"做成**可复用参考资产**，与角色参考图并列：
  - **Runway Gen-4 References + "coverage"**：纯 2D 路线最强，显式支持"把一个场景当参考、从不同角度补拍不穿帮"。
  - **可灵 Kling Elements / Vidu 多实体参考 / Google Veo Ingredients**：场景/背景/道具可作参考实体，Elements 还能传同场景多角度打包成"资产"。
- **真能"换机位不换景"的终局**：**先建一个 3D 场景，再在稳定的虚拟片场里任选机位抽帧**（World Labs Marble / 腾讯 HunyuanWorld 开源）——所有镜头共享同一几何+光照，从根上消除漂移。当下可作前期/参考板用。
- 学术：**CineMaster**（3D 感知可控 T2V，场景摆 bbox + 定相机轨迹）、**STAGE / VMem / ShotDirector**（多镜锚定 + 跨机位几何记忆）。

### 路 4 — 火山栈原生能力
- **火山原生没有"多机位一致"按钮**；`asset://` 素材库**只存人像**（虚拟人像+真人脸），**存不了房间/场景**——想复用场景只能自己在业务层存锚图 URL。
- 但有三样 **"官方有、我们可能没接满" 的 Seedream 参数，全在已接的 ark 接口内、不用新渠道**：
  1. **组图 `sequential_image_generation:auto` + `options.max_images:N`**：一次请求让模型在**同一上下文**里出同场景 N 个机位，一致性天然优于逐张单发。**几乎零成本，最该先做。**
  2. **多参考图数组**（`image` 可数组，4.5 最多 10 张）：把"场景锚图"当参考喂每个机位。
  3. **自定义超宽 `size`**（宽高比范围 [1/16, 16]）：出近全景宽幅再裁。
- Seedance 2.0 `reference_image`（≤9 张）有"环境/背景槽"，能锁**单段视频内**的场景（注意首帧与多图参考互斥），但**解决不了跨静态分镜对齐**。
- 即梦/Dreamina 产品端有智能画布（扩图/局部重绘）、故事分镜，但**开放 API 走的就是同一套火山方舟**，无额外 3D/全景专用接口。

---

## 三、核心收敛（四路共识）

1. **outpainting 求反打是死路**——反打是新视角合成（NVS），不是画框外延。方向 A 要改成"指令式转机位/3D 渲染"。
2. **业界统一趋势**：「一场一图」→「**场景参考资产 scene reference asset**」，与角色参考图并列。这与 Octopus 已有的**角色 asset 范式同构**，是最自然的升级方向。
3. **终局是"先建 3D 场景再抽机位"**（HunyuanWorld/Marble），但对 2D 漫剧底板是过度工程；**当下最优 = "场景机位板 → Seedream 融角色"**。
4. **火山原生没有多机位引擎**，但 Seedream 组图/多参考图/超宽 size 三参数（已接 ark 内）可零成本先做一版。
5. **无论哪条路，产出都汇入现有 Seedream 图生图关键帧环节**，架构侵入小；且与 0.7.4 的 `shot_type`（establish/master/single/ots/**reverse**）、`reverse_of`、`screen_position` 字段**天然对接**——这些机位语义正好用来选/生成对应的场景机位板。

---

## 四、分层落地方案

### Tier 0 — 今天就能做（零新依赖，全在已接 ark Seedream）
把「一场一图」升级为「**一场多机位板**」：
- 每个地点先出一张"场景主锚图"；再用 **`sequential_image_generation:auto` + `max_images:N`** 一次性生成该场景 N 个典型机位板（establish / 圆桌正打 / 圆桌反打 / 过肩 …），prompt 列出各机位并强调"保持墙面/家具/灯光一致"。
- 或：各机位走图生图，把锚图作参考图 + prompt 描述机位。
- 业务层存这些"场景板"URL 当**可复用场景资产**（自建场景库，因 asset:// 存不了场景）。
- 分镜时按 `shot_type` / `reverse_of` 选最接近的场景板 + 角色参考一起喂 Seedream 关键帧。
- **收益**：直接缓解"shot 8/9/10 同一张图"的重复感；**局限**：纯 prompt 驱动换机位，真反打几何会漂，但作底板可接受。

### Tier 1 — 中期（开源可自部署，专治反打）
- 接入 **Qwen-Image-Edit-2511 Multiple-Angles LoRA**（本地 ComfyUI，咱们有 4090 + ComfyUI 环境）做"**分镜后按需转机位**"：场景锚图 →（离散机位 token，含 180° 反打）→ 反打底板 → 现有 Seedream 融角色链路。
- 这是用户方向 A 的**真正可行版**（替代做不到的 outpainting）。先小规模验证室内杂场景质量，不行用 Nano Banana Pro 兜底。

### Tier 2 — 可选（几何最严谨，高频复用地点 / 前期美术）
- **360°全景**（方向 B）：HunyuanWorld/Skybox/Diffusion360 出全景 → `py360convert` 按角度裁 → 天生同景一致。适合"转朝向多、站位固定"的场景。
- 或更彻底：**HunyuanWorld 单图→3D 场景**，精确相机位渲染任意机位板，真几何一致、直解"圆桌对面"。工程量大 + 3D 资产管理，留给高频复用地点或未来大版本。

---

## 五、与现有架构对接点

- 在**「美术」与「关键帧」之间插入一层「场景机位板（scene view board）」**——无论 Tier 0/1/2，产出都是"某机位的场景底图"，最后统一汇入现有 Seedream 图生图关键帧环节融入 @角色 + 升质。
- **复用 @引用 机制**：场景板像角色 asset 一样被 shot 引用（`@场景#机位`？可对齐现有 `@角色#服装` 范式）。
- **用 0.7.4 导演层字段驱动**：`shot_type`（establish/master/single/two_shot/ots/reverse/insert/reaction）、`reverse_of`、`screen_position`、`eyeline_target` 决定每个 shot 该选/生成哪个机位板。
- **场景资产存储**：业务层自建（存锚图/机位板 URL），因火山 asset:// 只存人像。

---

## 六、建议路线

**先 Tier 0**（小成本验证"每场多机位板"能否缓解重复感，纯 Seedream 参数）→ **反打质量不够再上 Tier 1**（Qwen 多角度 LoRA，本地 ComfyUI 跑，专治反打）→ **高频复用地点/大版本再考虑 Tier 2**（HunyuanWorld 3D）。

---

## 七、参考链接

**360°全景**
- Skybox AI：https://www.blockadelabs.com/ ｜ 8K Model 3：https://www.blockadelabs.com/post/skybox-ai-model-3-brings-8k-generated-360-panoramic-worlds-to-all
- 腾讯 HunyuanWorld：https://github.com/Tencent-Hunyuan/HunyuanWorld-1.0 ｜ HY-World 2.0：https://github.com/Tencent-Hunyuan/HY-World-2.0
- 阿里 Diffusion360：https://www.modelscope.cn/models/damo/cv_diffusion_text-to-360panorama-image_generation/summary ｜ 论文 https://arxiv.org/abs/2311.13141
- py360convert：https://github.com/sunset1995/py360convert ｜ equilib：https://github.com/haruishi43/equilib

**换机位 / NVS**
- Qwen 多角度 LoRA：https://huggingface.co/fal/Qwen-Image-Edit-2511-Multiple-Angles-LoRA ｜ https://huggingface.co/dx8152/Qwen-Edit-2509-Multiple-angles ｜ demo https://huggingface.co/spaces/linoyts/Qwen-Image-Edit-Angles
- Nano Banana Pro：https://blog.google/technology/ai/nano-banana-pro/
- Stable Virtual Camera：https://github.com/Stability-AI/stable-virtual-camera

**业界/竞品**
- Runway Gen-4：https://runwayml.com/research/introducing-runway-gen-4
- World Labs Marble（影视案例）：https://www.worldlabs.ai/case-studies/creative-film
- Kling O1/Elements：https://help.scenario.com/articles/3906786894-kling-o1-family-the-essentials
- Vidu Ref-to-Video：https://www.prnewswire.com/news-releases/vidu-launches-q2-reference-to-video-302590002.html
- Veo 3.1 Ingredients：https://blog.google/innovation-and-ai/technology/ai/veo-3-1-ingredients-to-video/
- Genie 3：https://deepmind.google/blog/genie-3-a-new-frontier-for-world-models/

**学术**
- CineMaster：https://arxiv.org/abs/2502.08639 ｜ STAGE：https://arxiv.org/abs/2512.12372 ｜ VMem：https://arxiv.org/pdf/2506.18903

**火山官方文档**
- Seedream 教程：https://www.volcengine.com/docs/82379/1330310 ｜ Seedream 4.0：https://www.volcengine.com/docs/82379/1824718
- Seedance 2.0：https://www.volcengine.com/docs/82379/2291680 ｜ 官方 https://research.doubao.com/zh/seedance2_0
