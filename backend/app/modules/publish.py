"""
发布模块 — 把成品视频 + 封面 + 元数据打包导出到 output/publish/<project_id>/

输入：
  - merged_video_url: 合并后的视频 /output/videos/xxx.mp4
  - cover_image_url:  封面 /output/images/xxx.png（可选）
  - title / description / tags / platform
  - project_id（命名用）

输出：
  - export_path: 导出目录（绝对路径）
  - export_url: /output/publish/<project_id>/（前端可访问）
  - files: { video, cover, metadata }

不做：第三方平台上传。这一步留给将来扩展。
"""

import json
import logging
import os
import shutil
import uuid
from datetime import datetime
from typing import Any, Dict

from app.core.config import settings
from app.core.paths import resolve_output_path
from app.modules.base import BaseModule

logger = logging.getLogger(__name__)


def publish_dir(project_id: str) -> str:
    path = os.path.join(settings.OUTPUT_DIR, "publish", project_id)
    os.makedirs(path, exist_ok=True)
    return path


class PublishModule(BaseModule):
    """发布导出模块（仅本地导出）"""

    name = "publish"
    description = "把成品视频、封面、发布元数据打包导出到 output/publish/<project_id>/"
    category = "advanced"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "项目 ID（用于命名目录）"},
                "merged_video_url": {"type": "string", "description": "成片视频 URL /output/videos/xxx.mp4"},
                "cover_image_url": {"type": "string", "description": "封面 URL（可选）"},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "platform": {"type": "string", "description": "目标平台（仅记录到元数据，不真正上传）"},
                "translations": {
                    "type": "array", "items": {"type": "object"},
                    "description": "0.7.7 多语言译后视频 [{target_language, video_url}]（可选）",
                },
            },
            "required": ["project_id", "merged_video_url", "title"],
        }

    @property
    def output_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "export_path": {"type": "string"},
                "export_url": {"type": "string"},
                "files": {"type": "object"},
                "metadata": {"type": "object"},
            },
        }

    async def run(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        project_id = input_data.get("project_id") or f"unknown_{uuid.uuid4().hex[:8]}"
        video_url = input_data.get("merged_video_url") or ""
        cover_url = input_data.get("cover_image_url") or ""
        title = (input_data.get("title") or "").strip()
        description = (input_data.get("description") or "").strip()
        tags = input_data.get("tags") or []
        platform = input_data.get("platform") or "本地"

        if not video_url:
            raise ValueError("publish: 缺少 merged_video_url")
        if not title:
            raise ValueError("publish: 缺少 title")

        out_dir = publish_dir(project_id)
        files: Dict[str, str] = {}

        # 1. 复制视频
        video_local = resolve_output_path(video_url)
        if not video_local or not os.path.exists(video_local):
            raise ValueError(f"成片视频不存在：{video_url}")
        video_ext = os.path.splitext(video_local)[1] or ".mp4"
        video_dst_name = f"video{video_ext}"
        video_dst = os.path.join(out_dir, video_dst_name)
        shutil.copyfile(video_local, video_dst)
        files["video"] = video_dst_name

        # 1b. 复制多语言译后视频（0.7.7 本地化）→ video_<lang>.mp4，收进 files["translations"]
        trans_files: Dict[str, str] = {}
        for t in (input_data.get("translations") or []):
            lang = (t.get("target_language") or "").strip()
            turl = t.get("video_url")
            if not lang or not turl:
                continue
            t_local = resolve_output_path(turl)
            if not t_local or not os.path.exists(t_local):
                logger.warning("译后视频不存在，跳过 %s: %s", lang, turl)
                continue
            t_ext = os.path.splitext(t_local)[1] or ".mp4"
            t_name = f"video_{lang}{t_ext}"
            shutil.copyfile(t_local, os.path.join(out_dir, t_name))
            trans_files[lang] = t_name
        if trans_files:
            files["translations"] = trans_files

        # 2. 复制封面（可选）
        if cover_url:
            cover_local = resolve_output_path(cover_url)
            if cover_local and os.path.exists(cover_local):
                cover_ext = os.path.splitext(cover_local)[1] or ".png"
                cover_dst_name = f"cover{cover_ext}"
                cover_dst = os.path.join(out_dir, cover_dst_name)
                shutil.copyfile(cover_local, cover_dst)
                files["cover"] = cover_dst_name
            else:
                logger.warning("封面文件不存在：%s", cover_url)

        # 3. 写 metadata.json
        metadata = {
            "project_id": project_id,
            "title": title,
            "description": description,
            "tags": tags,
            "platform": platform,
            "exported_at": datetime.utcnow().isoformat() + "Z",
            "languages": list(trans_files.keys()),  # 0.7.7 一并导出的多语言版
            "files": files,
        }
        metadata_path = os.path.join(out_dir, "metadata.json")
        with open(metadata_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)
        files["metadata"] = "metadata.json"

        # 4. 写一份给人看的简介 .txt（方便复制粘贴到平台）
        readable_path = os.path.join(out_dir, "publish.txt")
        with open(readable_path, "w", encoding="utf-8") as f:
            f.write(f"{title}\n\n")
            f.write(f"{description}\n\n")
            if tags:
                f.write("标签：" + "  ".join(f"#{t}" for t in tags) + "\n")
        files["readable"] = "publish.txt"

        export_url = f"/output/publish/{project_id}"

        return {
            "export_path": out_dir,
            "export_url": export_url,
            "files": files,
            "metadata": metadata,
            "mock": False,
        }
