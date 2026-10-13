"""Phase B：从**真 agent** 生成「客户端壳」—— 把 prompt 工程整体剥掉。

护城河的最后一步：Phase A 让所有含 prompt 的方法都在云端执行（@cloud_method），
客户端**永不执行它们的 body**；于是分发包里根本不需要那些 body。本脚本按这个事实生成壳：

  · 带 `@cloud_method` 的方法 → 只留**签名 + pass**（调用时装饰器路由云端）
  · `KEEP_LOCAL` 里的方法      → **原样保留**（经依赖分析确认自包含、无 LLM、无 prompt）
  · system_prompt / 模块级大字符串常量 / 其余私有 prompt helper → **全部丢弃**

**真源永不改动**：只把壳写进 staging 目录，供打包使用（见 desktop/build_desktop.py）。

用法：
    python desktop/gen_agent_stubs.py <staging_backend_dir>
"""
import ast
import os
import sys

# 客户端仍需本地执行的非秘密方法（依赖分析确认：零 LLM、零 prompt、自包含）
KEEP_LOCAL = {
    "director": {"_extract_collected_info"},
}

# 类级长字符串（>120 字符）视为 prompt → 清空
_STR_SECRET_LEN = 120

HEADER = '''"""{cls} —— 客户端壳（护城河剥离，自动生成，勿手改）。

真实实现（prompt 工程）**只在 Octopus 云端**。本机永不执行下面这些方法体：
`@cloud_method` 会在 cloud_client 模式把调用整体路由到云端执行。
生成器：desktop/gen_agent_stubs.py
"""
'''


def _indent(text: str, n: int = 4) -> str:
    pad = " " * n
    return "\n".join((pad + ln) if ln.strip() else ln for ln in text.splitlines())


def gen_stub(src_path: str, agent_key: str) -> str:
    with open(src_path, "r", encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)

    # 1) 模块级 import 原样保留（只取 import 语句 → 模块级 prompt 常量天然被丢掉）
    imports = []
    for n in tree.body:
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            seg = ast.get_source_segment(src, n)
            if seg:
                imports.append(seg)
    # 壳一定会用到这两个（兜底补的 execute 也带 @cloud_method）；原文件没有就补上
    if not any("agent_runtime import cloud_method" in i for i in imports):
        imports.append("from app.core.agent_runtime import cloud_method")
    if not any("base_agent import BaseAgent" in i for i in imports):
        imports.append("from app.agents.base.base_agent import BaseAgent")

    classes = [n for n in tree.body if isinstance(n, ast.ClassDef)]
    if not classes:
        raise SystemExit(f"{src_path}: 找不到 class")
    cls = classes[-1]                      # agent 类（可能前面有辅助类）
    bases = ", ".join(ast.unparse(b) for b in cls.bases) or "BaseAgent"
    keep = KEEP_LOCAL.get(agent_key, set())

    parts = []
    n_cloud = n_keep = n_drop = 0

    # 2) 类级属性：**只保留短字面量**。凡是长字符串（prompt）或引用了模块级常量/表达式
    #    （如 `system_prompt = DIRECTOR_SYSTEM_PROMPT`，那常量已被丢弃 → 保留会 NameError）
    #    一律丢弃；system_prompt 丢掉后自动继承 BaseAgent 的通用默认值，客户端也用不到它。
    has_name = False
    for n in cls.body:
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
            tname = n.targets[0].id
            v = n.value
            keep_it = False
            if isinstance(v, ast.Constant):
                keep_it = not (isinstance(v.value, str) and len(v.value) > _STR_SECRET_LEN)
            elif isinstance(v, (ast.List, ast.Tuple, ast.Dict, ast.Set)):
                seg0 = ast.get_source_segment(src, n) or ""
                keep_it = len(seg0) < 200 and all(
                    isinstance(e, ast.Constant) for e in ast.walk(v) if isinstance(e, (ast.Constant, ast.Name))
                )
            if keep_it:
                parts.append("    " + (ast.get_source_segment(src, n) or f"{tname} = None"))
                if tname == "name":
                    has_name = True
            else:
                n_drop += 1
    if not has_name:
        parts.insert(0, f'    name = "{agent_key}"')

    # 3) 方法
    emitted_execute = False
    for n in cls.body:
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        decos = [ast.unparse(d) for d in n.decorator_list]
        if any("cloud_method" in d for d in decos):
            n.body = [ast.Pass()]           # 剥掉 body（含 docstring）
            parts.append(_indent(ast.unparse(n)))
            n_cloud += 1
            if n.name == "execute":
                emitted_execute = True
        elif n.name in keep:
            seg = ast.get_source_segment(src, n)
            if seg:
                parts.append("    " + seg)   # 首行补缩进，其余行原缩进已对
                n_keep += 1
        else:
            n_drop += 1                      # 私有 prompt helper 等 → 丢弃

    # 4) BaseAgent.execute 是 abstractmethod：壳里必须有 execute，否则类无法实例化。
    #    （如 router/module_router 的 execute 没被装饰 → 上面被丢弃了）
    if not emitted_execute:
        parts.append(
            "    @cloud_method\n"
            "    async def execute(self, task=None, context=None):\n"
            "        pass          # 真实实现在云端；本机不执行"
        )
        n_cloud += 1

    if not parts:
        parts.append("    pass")

    out = (
        HEADER.format(cls=cls.name)
        + "\n".join(imports)
        + "\n\n\n"
        + f"class {cls.name}({bases}):\n"
        + "\n\n".join(parts)
        + "\n"
    )
    return out, (n_cloud, n_keep, n_drop)


AGENTS = {
    "art_director": "art_director/art_director_agent.py",
    "audio_director": "audio_director/audio_director_agent.py",
    "director": "director/director_agent.py",
    "module_router": "module_router/module_router_agent.py",
    "mv_director": "mv_director/mv_director_agent.py",
    "publisher": "publisher/publisher_agent.py",
    "router": "router/router_agent.py",
    "screenwriter": "screenwriter/screenwriter_agent.py",
    "storyboard": "storyboard/storyboard_agent.py",
}


def main():
    if len(sys.argv) < 2:
        raise SystemExit("用法: python desktop/gen_agent_stubs.py <staging_backend_dir>")
    staging_backend = os.path.abspath(sys.argv[1])
    agents_dir = os.path.join(staging_backend, "app", "agents")
    if not os.path.isdir(agents_dir):
        raise SystemExit(f"staging agents 目录不存在: {agents_dir}")

    total_before = total_after = 0
    for key, rel in AGENTS.items():
        path = os.path.join(agents_dir, rel.replace("/", os.sep))
        if not os.path.isfile(path):
            print(f"  !! 跳过（不存在）: {rel}")
            continue
        before = os.path.getsize(path)
        stub, (c, k, d) = gen_stub(path, key)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(stub)
        after = os.path.getsize(path)
        total_before += before
        total_after += after
        print(f"  {key:15s} {before:>7,}B → {after:>6,}B  "
              f"(云端方法{c} 保留本地{k} 丢弃{d})  剥掉 {100*(1-after/before):.0f}%")
    print(f"\n合计 {total_before:,}B → {total_after:,}B，剥掉 {100*(1-total_after/total_before):.1f}%")


if __name__ == "__main__":
    main()
