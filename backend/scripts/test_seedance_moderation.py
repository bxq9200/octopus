"""
快速测试 Seedance 图生视频是否会触发 PrivacyInformation 审核。

用法:
  cd backend
  python scripts/test_seedance_moderation.py <image_path> [prompt]

示例:
  python scripts/test_seedance_moderation.py D:/test/shiba.png "镜头缓慢推近，狗狗摇尾巴"
  python scripts/test_seedance_moderation.py D:/test/shiba.png

只创建任务并打印响应（含错误码与 message），不会等待视频生成完毕。
若返回 4xx 含 PrivacyInformation/Sensitive 等关键词，说明被审核拦截。
"""
import sys
import os
import base64
import json
from pathlib import Path

# 让脚本能 import app
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from app.core.config import settings


def to_data_url(path: str) -> str:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(p)
    ext = p.suffix.lower()
    mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".png": "image/png", ".webp": "image/webp"}.get(ext, "image/png")
    b64 = base64.b64encode(p.read_bytes()).decode("utf-8")
    return f"data:{mime};base64,{b64}"


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    image_path = sys.argv[1]
    prompt = sys.argv[2] if len(sys.argv) > 2 else "镜头缓慢推进，主体轻微动作"

    api_key = settings.SEEDANCE_API_KEY
    base_url = settings.SEEDANCE_BASE_URL
    model = settings.SEEDANCE_MODEL

    if not api_key or not base_url:
        print("[ERR] SEEDANCE_API_KEY 或 SEEDANCE_BASE_URL 未配置")
        sys.exit(2)

    first_frame = to_data_url(image_path)
    img_size_kb = len(first_frame) * 3 // 4 // 1024  # base64 解码后约略大小

    payload = {
        "model": model,
        "content": [
            {"type": "text", "text": prompt},
            {
                "type": "image_url",
                "image_url": {"url": first_frame},
                "role": "first_frame",
            },
        ],
        "ratio": "1:1",
        "duration": 5,
        "resolution": "720p",
        "generate_audio": False,
        "watermark": False,
    }

    print(f"[INFO] image     = {image_path} (~{img_size_kb} KB)")
    print(f"[INFO] prompt    = {prompt}")
    print(f"[INFO] model     = {model}")
    print(f"[INFO] endpoint  = {base_url}/contents/generations/tasks")
    print("[INFO] 创建任务中...")

    with httpx.Client(timeout=60.0) as client:
        resp = client.post(
            f"{base_url}/contents/generations/tasks",
            json=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )

    print(f"\n[HTTP {resp.status_code}]")
    try:
        data = resp.json()
        print(json.dumps(data, indent=2, ensure_ascii=False))
    except Exception:
        print(resp.text[:2000])

    # 简易判定
    body = resp.text
    if resp.status_code >= 400:
        if "PrivacyInformation" in body or "Privacy" in body:
            print("\n[结论] ❌ 触发 PrivacyInformation 审核")
        elif "Sensitive" in body or "sensitive" in body:
            print("\n[结论] ❌ 触发敏感内容审核")
        elif "moderation" in body.lower():
            print("\n[结论] ❌ 触发其他审核策略")
        else:
            print("\n[结论] ❌ 其他错误（非审核类）")
    else:
        print("\n[结论] ✅ 通过创建（任务已入队，未触发首帧/prompt 审核）")
        task_id = data.get("id") if isinstance(data, dict) else None
        if task_id:
            print(f"        task_id = {task_id}")
            print(f"        如需观察过程审核，可查询 GET {base_url}/contents/generations/tasks/{task_id}")


if __name__ == "__main__":
    main()
