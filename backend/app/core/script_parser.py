"""剧本文件解析（0.7.0 专家模式）

支持 .txt / .md / .docx。.doc 给出友好错误提示。

调用：
    text = parse_script_bytes(data, filename)
    # 抛 ValueError("不支持 .doc，请另存为 .docx 或 .txt") 当格式不支持
"""
from __future__ import annotations

import os
from io import BytesIO


_SUPPORTED_EXT = {".txt", ".md", ".markdown", ".docx"}


def is_supported_script(filename: str) -> bool:
    ext = os.path.splitext(filename or "")[1].lower()
    return ext in _SUPPORTED_EXT


def parse_script_bytes(data: bytes, filename: str) -> str:
    """把字节内容解析成纯文本（按文件扩展名分发）。

    - .txt / .md / .markdown → UTF-8 decode（errors='replace' 兜底）
    - .docx → python-docx 提段落
    - .doc → 抛 ValueError（提示用户另存）
    - 其他 → 抛 ValueError
    """
    ext = os.path.splitext(filename or "")[1].lower()

    if ext in (".txt", ".md", ".markdown"):
        # 尝试 utf-8 / utf-8-sig（带 BOM）/ gbk（中文常见）
        for enc in ("utf-8-sig", "utf-8", "gbk"):
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                continue
        return data.decode("utf-8", errors="replace")

    if ext == ".docx":
        try:
            from docx import Document
        except ImportError as e:
            raise ValueError("服务器未安装 python-docx，请联系管理员") from e
        try:
            doc = Document(BytesIO(data))
        except Exception as e:
            raise ValueError(f"docx 解析失败：{e}") from e
        # 段落 + 表格里的文字都收一遍
        parts = []
        for p in doc.paragraphs:
            t = (p.text or "").strip()
            if t:
                parts.append(t)
        for table in doc.tables:
            for row in table.rows:
                for cell in row.cells:
                    t = (cell.text or "").strip()
                    if t:
                        parts.append(t)
        return "\n".join(parts)

    if ext == ".doc":
        raise ValueError("暂不支持 .doc 格式，请用 Word 另存为 .docx 或 .txt 后重新上传")

    raise ValueError(f"不支持的文件类型：{ext}（仅 .txt / .md / .docx）")


def preview_text(text: str, max_chars: int = 2000) -> str:
    """返回前 max_chars 字符（带省略号）"""
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "…"
