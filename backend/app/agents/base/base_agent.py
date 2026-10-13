"""
Agent 基类 — 所有业务 Agent 的抽象基类

职责：
1. 定义 Agent 生命周期（初始化→接收任务→执行→返回结果）
2. 提供 LLM 调用能力
3. 支持工具调用（Tool Use）
4. 状态管理和日志
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Callable
from dataclasses import dataclass, field
from enum import Enum
import json
import logging
import time
import uuid

from openai import AsyncOpenAI

from app.core.config import settings

logger = logging.getLogger(__name__)


class AgentStatus(Enum):
    IDLE = "idle"           # 空闲
    THINKING = "thinking"   # 思考中
    EXECUTING = "executing" # 执行中
    DONE = "done"           # 完成
    ERROR = "error"         # 错误


@dataclass
class AgentMessage:
    """Agent 间通信消息"""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    from_agent: str = ""           # 发送方
    to_agent: str = ""             # 接收方（空表示广播）
    msg_type: str = "task"         # task | result | query | notify
    content: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    context: Dict[str, Any] = field(default_factory=dict)  # 上下文传递


@dataclass
class AgentResult:
    """Agent 执行结果"""
    success: bool
    data: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    logs: List[str] = field(default_factory=list)
    duration_ms: int = 0


class BaseAgent(ABC):
    """
    Agent 基类
    
    子类需要实现：
    - name: Agent 名称
    - description: Agent 职责描述
    - system_prompt: 系统提示词（老白你来填）
    - execute(): 核心执行逻辑
    """
    
    # 子类覆盖
    name: str = "base"
    description: str = "基础Agent"
    system_prompt: str = "你是一个通用的AI助手。"

    def __init__(self):
        self.status = AgentStatus.IDLE
        # client 在每次需要时按当前 user_settings 动态构造（支持配置热更）
        self._client_cache: Optional[AsyncOpenAI] = None
        self._client_cache_key: Optional[tuple] = None
        self.message_history: List[AgentMessage] = []
        self._tool_handlers: Dict[str, Callable] = {}
        # 实例级工具注册表（避免类级共享状态）
        self.tools: List[Dict[str, Any]] = []
        # 0.6.5: 最近一次 LLM 调用的 usage（被 executor 层消费后清空），用于成本统计
        self._last_usage: Optional[Dict[str, Any]] = None

    @property
    def client(self) -> AsyncOpenAI:
        """按当前 user_settings 返回 LLM client；配置变了自动重建"""
        from app.core.runtime_config import active_llm
        cfg = active_llm()
        cache_key = (cfg.provider, cfg.api_key, cfg.base_url)
        if self._client_cache_key != cache_key or self._client_cache is None:
            # 0.7.0：显式 timeout + max_retries。默认是 600s + 重试 2 次（最坏 ~30 分钟傻等），
            # 大输出（storyboard max_tokens=16384）一旦 LLM 慢/卡会让节点长时间"执行中"无反馈。
            # 300s 单次 + 重试 1 次 = 最坏 ~10 分钟就失败，节点转 FAILED 让用户重跑。
            common = dict(timeout=300.0, max_retries=1)
            if cfg.provider == "kimi":
                self._client_cache = AsyncOpenAI(
                    api_key=cfg.api_key,
                    base_url=cfg.base_url,
                    default_headers={"User-Agent": "KimiCLI/1.30.0"},
                    **common,
                )
            else:
                self._client_cache = AsyncOpenAI(
                    api_key=cfg.api_key,
                    base_url=cfg.base_url,
                    **common,
                )
            self._client_cache_key = cache_key
        return self._client_cache

    @property
    def model(self) -> str:
        """获取当前使用的模型"""
        from app.core.runtime_config import active_llm
        return active_llm().model
    
    # ── 核心接口 ──
    
    async def run(self, task: Dict[str, Any], context: Dict[str, Any] = None) -> AgentResult:
        """
        Agent 入口：接收任务 → 执行 → 返回结果
        
        Args:
            task: 任务参数
            context: 上下文（来自上游 Agent 的结果）
        
        Returns:
            AgentResult: 执行结果
        """
        start = time.time()
        self.status = AgentStatus.THINKING
        
        try:
            # 前置处理
            prepared_task = self._preprocess(task, context)

            # 核心执行（子类实现）
            self.status = AgentStatus.EXECUTING
            # 0.8 缝0：agent 上云插口。必须在这里分叉——execute() 里才拼 prompt，
            # 到 llm_chat 就晚了（那时 prompt 已成型，护城河护不住）。local 恒走下面的本地分支。
            from app.core import agent_runtime
            if agent_runtime.should_run_remote():
                result_data = await agent_runtime.run_remote(
                    self, "execute", {"task": prepared_task, "context": context or {}},
                )
                agent_runtime.apply_usage(self, result_data)
            else:
                result_data = await self.execute(prepared_task, context)
            
            # 后置处理
            final_result = self._postprocess(result_data)
            
            self.status = AgentStatus.DONE
            return AgentResult(
                success=True,
                data=final_result,
                duration_ms=int((time.time() - start) * 1000)
            )
            
        except Exception as e:
            self.status = AgentStatus.ERROR
            return AgentResult(
                success=False,
                error=str(e),
                duration_ms=int((time.time() - start) * 1000)
            )
    
    @abstractmethod
    async def execute(self, task: Dict[str, Any], context: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        子类必须实现的核心执行逻辑

        Args:
            task: 预处理后的任务
            context: 上下文

        Returns:
            执行结果字典
        """
        pass

    # ── 进阶模式对话式修改入口 ──

    async def chat(
        self,
        message: str,
        summary: str = "",
        project_meta: Optional[Dict[str, Any]] = None,
        target_nodes: Optional[List[Dict[str, Any]]] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """对话式修改入口。0.6.0 阶段 ②b 引入。

        子类可 override `_chat_system_prompt()` 给更专业的提示，
        多数情况下默认实现已够（统一 JSON 输出 → executor 应用）。

        返回: {"reply": str, "node_updates": [{node_id, params_patch, should_rerun, reason}]}
        """
        # 0.8 缝0：agent 上云插口。**必须在 _build_chat_prompt 之前**——
        # _chat_system_prompt() 和拼装逻辑同样是护城河，拼完再分叉就等于白发一遍。
        from app.core import agent_runtime
        if agent_runtime.should_run_remote():
            try:
                result = await agent_runtime.run_remote(self, "chat", {
                    "message": message,
                    "summary": summary,
                    "project_meta": project_meta or {},
                    "target_nodes": target_nodes or [],
                    "attachments": attachments or [],
                })
                agent_runtime.apply_usage(self, result)
                return result
            except Exception as e:
                # 与下面本地失败路径同一种友好返回，别把异常抛给前端对话框
                logger.warning("%s.chat 云端调用失败: %s", self.name, e)
                return {"reply": f"我这边出了点小问题：{str(e)[:120]}", "node_updates": []}

        user_prompt = self._build_chat_prompt(
            message, summary, project_meta or {}, target_nodes or [], attachments or [],
        )
        try:
            # 有附件 → 走 vision（LLM 真看到图），无附件 → 走 text
            if attachments:
                # 收集 target_nodes 里"原节点的 image_url" 作为 Photo 1（让 vision LLM 看到原角色/场景）
                # 顺序跟 _retry_result_child 一致：[原节点图, *用户附件]
                anchor_image_urls: List[str] = []
                for tn in (target_nodes or []):
                    if not isinstance(tn, dict):
                        continue
                    res = tn.get("result") or {}
                    img = res.get("image_url") or ""
                    if img and img not in anchor_image_urls:
                        anchor_image_urls.append(img)
                    if len(anchor_image_urls) >= 2:
                        break

                user_image_urls = [a.get("url") for a in attachments if a.get("url")]
                # 合并：anchor 在前（Photo 1+），用户附件在后（Photo N+）；去重；总数 ≤5
                vision_image_urls: List[str] = []
                for u in anchor_image_urls + user_image_urls:
                    if u and u not in vision_image_urls:
                        vision_image_urls.append(u)
                vision_image_urls = vision_image_urls[:5]

                try:
                    response = await self.llm_chat_with_vision(
                        text_prompt=user_prompt,
                        image_urls=vision_image_urls,
                        system=self._chat_system_prompt(),
                        temperature=0.5,
                        json_mode=True,
                    )
                except Exception as ve:
                    logger.warning("%s.chat vision 失败，降级 text-only: %s", self.name, ve)
                    response = await self.llm_generate(
                        prompt=user_prompt,
                        system=self._chat_system_prompt(),
                        temperature=0.5,
                        json_mode=True,
                    )
            else:
                response = await self.llm_generate(
                    prompt=user_prompt,
                    system=self._chat_system_prompt(),
                    temperature=0.5,
                    json_mode=True,
                )
        except Exception as e:
            logger.warning("%s.chat LLM 调用失败: %s", self.name, e)
            return {"reply": f"我这边出了点小问题：{str(e)[:120]}", "node_updates": []}

        try:
            data = json.loads(response)
        except json.JSONDecodeError:
            import re as _re
            m = _re.search(r"```(?:json)?\s*(.*?)\s*```", response, _re.DOTALL)
            if m:
                try:
                    data = json.loads(m.group(1))
                except json.JSONDecodeError:
                    return {"reply": response.strip()[:300], "node_updates": []}
            else:
                return {"reply": response.strip()[:300], "node_updates": []}

        # 规范化 node_updates
        updates = data.get("node_updates") or []
        if not isinstance(updates, list):
            updates = []
        norm_updates: List[Dict[str, Any]] = []
        for u in updates:
            if not isinstance(u, dict):
                continue
            nid = u.get("node_id")
            if not nid:
                continue
            norm_updates.append({
                "node_id": str(nid),
                "params_patch": u.get("params_patch") or {},
                "should_rerun": bool(u.get("should_rerun", False)),
                "reason": (u.get("reason") or "").strip()[:200],
            })

        return {
            "reply": (data.get("reply") or "").strip()[:400],
            "node_updates": norm_updates,
        }

    def _chat_system_prompt(self) -> str:
        """子类 override 以给更专业的指引。默认通用版。"""
        return f"""你是 Octopus 的 {self.description}。
创作者正在跟你对话，希望对当前的节点做修改、查询，或闲聊。

## 输出格式（严格 JSON，必须包含 reply 和 node_updates）
{{
  "reply": "简短回应（≤80 字，第一人称，像同事对话）",
  "node_updates": [
    {{"node_id": "节点id", "params_patch": {{"字段名": "新值"}}, "should_rerun": true, "reason": "为什么这样改"}}
  ]
}}

## 规则
- 创作者明确要求修改某节点 → 输出对应 node_updates，params_patch 给出要 merge 的字段
- should_rerun=true 时节点会自动清 result 重跑（适合需要重新出图/出视频的情况）
- should_rerun=false 时只改 params 不重跑（适合改文案、标签等不需要重新生成的场景）
- 不属于本 agent 职责 / 用户只是闲聊 → node_updates 为空数组
- 改动会影响下游的话，reply 里**简短**提示一句（不要长篇大论）
"""

    def _build_chat_prompt(
        self,
        message: str,
        summary: str,
        project_meta: Dict[str, Any],
        target_nodes: List[Dict[str, Any]],
        attachments: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        meta_block = json.dumps(project_meta, ensure_ascii=False, indent=2)[:800]
        nodes_block = json.dumps(target_nodes, ensure_ascii=False, indent=2)[:3000] if target_nodes else "（无具体目标节点 — 你可以给 reply 但 node_updates 多半为空）"
        summary_line = f"\n## Router 解读\n{summary}" if summary else ""
        # 创作者附件（参考图）—— 按上传顺序编号，方便用"图 1 / 图二"指代各自职能
        attach_block = ""
        if attachments:
            atts = attachments[:5]
            lines = [
                f"  Photo {i + 2}: {a.get('url', '')} （文件: {a.get('name', '?')}）"
                for i, a in enumerate(atts)
            ]
            urls_array_str = "[" + ", ".join(f'"{a.get("url", "")}"' for a in atts) + "]"
            attach_block = (
                "\n## 创作者附件 + Photo 编号约定（关键）\n"
                + "vision 调用时图片的 Photo 编号：\n"
                + "  Photo 1: 上方「目标节点」里 result.image_url 那张原图（原角色立绘 / 原场景图）\n"
                + "\n".join(lines)
                + "\n\n## 写 Seedream prompt 时严格使用 `Photo 1 / Photo 2 / Photo 3` 这套术语\n"
                + "（这是 Seedream 4.0 训练时的标准用语，能最准识别每张图的角色）\n"
                + "\n常见模板（imperative + Edit clothing only）：\n"
                + "  · 单图换装：`Dress the person in Photo 1 in the [实际看到的衣服描述] from Photo 2.`\n"
                + "    `Preserve face, hair, and identity from Photo 1. Edit clothing only.`\n"
                + "  · 多图组合：`Edit the person in Photo 1: dress them in [描述] from Photo 2 and [描述] from Photo 3.`\n"
                + "    `Preserve face/hair/body identity from Photo 1. Edit only the specified items.`\n"
                + "  · 场景嵌入：`Edit the scene in Photo 1: add [描述] from Photo 2 at [位置].`\n"
                + "    `Preserve original scene composition. Only add the new element.`\n"
                + "\n创作者说「图 1 / 第一张」指的是 **Photo 2**（用户上传的第一张）；「图 2」对应 Photo 3，依次类推。\n"
                + f"\n在 params_patch 加：reference_image_urls: {urls_array_str}\n"
                + "数组顺序必须和上面 Photo 2/3/... 编号一致（Photo 1 后端会自动塞，不要重复）"
            )
        return f"""## 项目元数据
{meta_block}

## 目标节点（创作者想修改的节点；params/result 已裁剪）
{nodes_block}{summary_line}{attach_block}

## 创作者最新消息
{message}

请按 system prompt 的 JSON schema 输出。"""
    
    # ── LLM 调用封装 ──
    
    async def llm_chat(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.5,
        response_format: str = None,
        tools: List[Dict] = None,
        max_tokens: int = 8192,  # 0.6.5: 4000→8192 — storyboard / art 大输出 JSON 经常超 4000 被截断
    ) -> Dict[str, Any]:
        """
        调用 LLM 进行对话
        
        Args:
            messages: 消息列表
            temperature: 温度
            response_format: "json" | None
            tools: 工具定义列表
            max_tokens: 最大token数
        """
        # 2026-06-12: Kimi 平台对 kimi-for-coding 强制 temperature=0.6，传其它值一律 400
        # （"invalid temperature: only 0.6 is allowed"，Kimi Code 文档·请求格式错误）。
        # 在总漏斗统一钳制——别在几十个调用点逐个改；非 kimi 模型不受影响。
        if "kimi" in str(self.model or "").lower():
            temperature = 0.6
        kwargs = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "extra_body": {"thinking": {"type": "disabled"}}
        }
        
        if response_format == "json":
            kwargs["response_format"] = {"type": "json_object"}
        
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        
        response = await self.client.chat.completions.create(**kwargs)

        result = {
            "content": response.choices[0].message.content,
            "role": response.choices[0].message.role,
        }

        # 0.6.5: 捕获 token usage 用于成本统计
        from app.core.runtime_config import active_llm
        cfg = active_llm()
        self._last_usage = self._extract_usage(response, cfg.provider, cfg.model)
        if self._last_usage:
            result["usage"] = self._last_usage

        # 处理工具调用
        if hasattr(response.choices[0].message, 'tool_calls') and response.choices[0].message.tool_calls:
            result["tool_calls"] = [
                {
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": json.loads(tc.function.arguments)
                }
                for tc in response.choices[0].message.tool_calls
            ]

        return result

    @staticmethod
    def _extract_usage(response, provider: str, model: str) -> Optional[Dict[str, Any]]:
        """0.6.5: 从 OpenAI 兼容 response.usage 提取标准化 usage dict。"""
        u = getattr(response, "usage", None)
        if u is None:
            return None
        pt = getattr(u, "prompt_tokens", 0) or 0
        ct = getattr(u, "completion_tokens", 0) or 0
        if not pt and not ct:
            return None
        return {
            "provider": provider,
            "model": model,
            "kind": "llm",
            "prompt_tokens": int(pt),
            "completion_tokens": int(ct),
        }
    
    async def llm_generate(
        self,
        prompt: str,
        system: str = None,
        temperature: float = 0.5,
        json_mode: bool = False,
        max_tokens: int = 8192,  # 0.6.5: 大输出 caller（storyboard / art / review）可显式传更高
    ) -> str:
        """简化版：单轮生成"""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        result = await self.llm_chat(
            messages=messages,
            temperature=temperature,
            response_format="json" if json_mode else None,
            max_tokens=max_tokens,
        )
        return result["content"]

    # ── Vision 调用（0.6.0 阶段 ②e）──
    # 让 LLM 真正"看到"创作者上传的参考图，避免脑补衣服/物体细节

    async def llm_chat_with_vision(
        self,
        text_prompt: str,
        image_urls: List[str],
        system: Optional[str] = None,
        temperature: float = 0.5,
        json_mode: bool = False,
        max_tokens: int = 4000,
    ) -> str:
        """vision 调用：text + 图片走 OpenAI 兼容的 multimodal content 格式。

        参数:
            image_urls: 可以是 /output/uploads/xxx 这种本地路径，会自动转 base64 data URL
        失败处理:
            - 所有图都转 data URL 失败 → 降级回 self.llm_generate（text-only）
            - vision 模型未配置 → AsyncOpenAI 调用会报错，让上层 catch 后降级
        """
        from app.core.runtime_config import vision_llm
        from app.core.paths import image_to_data_url
        from openai import AsyncOpenAI

        cfg = vision_llm()

        # 转每张图为 data URL（本地路径走 base64；http URL 透传）
        image_contents: List[Dict[str, Any]] = []
        for url in (image_urls or [])[:5]:
            if not url:
                continue
            try:
                data_url = await image_to_data_url(url)
                image_contents.append({
                    "type": "image_url",
                    "image_url": {"url": data_url},
                })
            except Exception as e:
                logger.warning("vision: 图 %s 转 data URL 失败，跳过: %s", url, e)

        if not image_contents:
            # 全部转失败 → 降级 text-only，不浪费 vision 调用
            logger.warning("vision: 所有图都不可用，降级 text-only")
            return await self.llm_generate(
                prompt=text_prompt, system=system,
                temperature=temperature, json_mode=json_mode,
            )

        # 构造 multimodal user message
        user_content: List[Dict[str, Any]] = list(image_contents)
        user_content.append({"type": "text", "text": text_prompt})

        messages: List[Dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user_content})

        # 用独立 client（vision 的 base_url 可能跟主 LLM 不同，所以不能复用 self.client）
        if cfg.provider == "kimi":
            client = AsyncOpenAI(
                api_key=cfg.api_key, base_url=cfg.base_url,
                default_headers={"User-Agent": "KimiCLI/1.30.0"},
            )
        else:
            client = AsyncOpenAI(api_key=cfg.api_key, base_url=cfg.base_url)

        # 2026-06-12: 同 llm_chat —— kimi 系模型强制 temperature=0.6（否则 400）
        if "kimi" in str(cfg.model or "").lower():
            temperature = 0.6
        kwargs: Dict[str, Any] = {
            "model": cfg.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "extra_body": {"thinking": {"type": "disabled"}},
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        logger.info(
            "[vision] provider=%s model=%s images=%d, text_prompt_len=%d",
            cfg.provider, cfg.model, len(image_contents), len(text_prompt),
        )

        response = await client.chat.completions.create(**kwargs)
        # 0.6.5: vision 也捕获 usage
        self._last_usage = self._extract_usage(response, cfg.provider, cfg.model)
        return response.choices[0].message.content
    
    # ── 工具系统 ──
    
    def register_tool(self, name: str, handler: Callable, schema: Dict[str, Any]):
        """注册工具"""
        self._tool_handlers[name] = handler
        self.tools.append({
            "type": "function",
            "function": {
                "name": name,
                "description": schema.get("description", ""),
                "parameters": schema.get("parameters", {})
            }
        })
    
    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        """调用已注册的工具"""
        if name not in self._tool_handlers:
            raise ValueError(f"工具 '{name}' 未注册")
        return await self._tool_handlers[name](**arguments)
    
    # ── 消息总线接口 ──
    
    def send_message(
        self,
        to_agent: str,
        msg_type: str,
        content: Dict[str, Any],
        context: Dict[str, Any] = None
    ) -> AgentMessage:
        """发送消息给其他 Agent"""
        msg = AgentMessage(
            from_agent=self.name,
            to_agent=to_agent,
            msg_type=msg_type,
            content=content,
            context=context or {}
        )
        self.message_history.append(msg)
        return msg
    
    def receive_message(self, msg: AgentMessage):
        """接收来自其他 Agent 的消息"""
        self.message_history.append(msg)
    
    # ── 生命周期钩子 ──
    
    def _preprocess(self, task: Dict[str, Any], context: Dict[str, Any] = None) -> Dict[str, Any]:
        """任务预处理（子类可覆盖）"""
        return task
    
    def _postprocess(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """结果后处理（子类可覆盖）"""
        return result
    
    def reset(self):
        """重置状态"""
        self.status = AgentStatus.IDLE
        self.message_history.clear()
