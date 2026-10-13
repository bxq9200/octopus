#!/usr/bin/env python3
"""测试 Seedance 2.0 多模态视频生成 - 舱门开启冲击场景"""
import asyncio
import os
import base64
import httpx
import json

API_KEY = os.environ.get("SEEDANCE_API_KEY", "")
BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"

# 读取图片并转为 base64
def image_to_base64(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()

async def test_seedance_multimodal():
    # 准备图片 - 修正：交换首帧尾帧
    # first_frame.jpg 实际上是尾帧（白光爆发），last_frame.jpg 是首帧（关闭舱门）
    first_frame_b64 = image_to_base64("/data/octopus/backend/output/images/last_frame.jpg")
    last_frame_b64 = image_to_base64("/data/octopus/backend/output/images/first_frame.jpg")
    
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json"
    }
    
    # 详细的提示词 - 舱门开启冲击场景（加强雾气效果）
    prompt = """机舱内部视角，镜头正对关闭状态的巨大金属舱门，中景构图，镜头极慢向前推进，舱门为厚重工业结构，单扇整门，表面有铆钉和金属接缝，细节清晰，环境昏暗，四周有微弱红色指示灯，地面金属板结构。空气中弥漫着浓雾，白雾厚重且浓密，雾气在机舱内缓缓流动，像牛奶一样浓稠，能见度很低，整体压抑感增强。整扇舱门从右向左水平滑开，像超市单侧感应门一样整体平移向左侧，门缝从右侧开始出现并逐渐扩大，极强白光从舱门外爆发进入，画面中心高亮，浓雾被猛烈撕裂并高速向机舱内冲入，形成明显方向性流线，空气被压缩后释放，整体画面对比极强，冲击力极大。
【声音】瞬间0.1秒真空静音 → 紧接巨大风压爆发声，空气撕裂声，低频冲击声同时释放，低频持续增强，远处金属结构轻微共振声，空气被压缩的低沉风声（不是吹，是压迫感）"""
    
    payload = {
        "model": "doubao-seedance-2-0-260128",
        "content": [
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{first_frame_b64}"},
                "role": "first_frame"
            },
            {
                "type": "text",
                "text": prompt
            },
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{last_frame_b64}"},
                "role": "last_frame"
            }
        ],
        "ratio": "9:16",
        "duration": 6,
        "resolution": "720p",
        "generate_audio": True,
        "watermark": False,
        "seed": -1
    }
    
    print("=" * 70)
    print("🎬 Seedance 2.0 多模态视频生成测试")
    print("=" * 70)
    print(f"模型: {payload['model']}")
    print(f"比例: {payload['ratio']}")
    print(f"时长: {payload['duration']}秒")
    print(f"分辨率: {payload['resolution']}")
    print(f"音频生成: {payload['generate_audio']}")
    print(f"首帧图片: {len(first_frame_b64)} chars")
    print(f"尾帧图片: {len(last_frame_b64)} chars")
    print("-" * 70)
    print(f"提示词: {prompt[:100]}...")
    print("-" * 70)
    
    async with httpx.AsyncClient(timeout=60) as client:
        try:
            # 1. 创建任务
            print("\n📤 发送请求...")
            resp = await client.post(
                f"{BASE_URL}/contents/generations/tasks",
                headers=headers,
                json=payload
            )
            print(f"Status: {resp.status_code}")
            
            if resp.status_code != 200:
                print(f"❌ 错误: {resp.text[:500]}")
                return
            
            data = resp.json()
            print(f"\n✅ 任务创建成功!")
            print(f"Task ID: {data.get('id')}")
            print(f"Status: {data.get('status')}")
            
            task_id = data.get('id')
            if not task_id:
                print("❌ 未返回任务ID")
                return
            
            # 2. 轮询查询任务状态
            print("\n⏳ 轮询任务状态...")
            for i in range(30):
                await asyncio.sleep(5)
                
                poll_resp = await client.get(
                    f"{BASE_URL}/contents/generations/tasks/{task_id}",
                    headers=headers
                )
                poll_data = poll_resp.json()
                status = poll_data.get('status', 'unknown')
                
                print(f"  [{i+1}/30] Status: {status}")
                
                if status == "succeeded":
                    content = poll_data.get('content', {})
                    if isinstance(content, dict):
                        video_url = content.get('video_url')
                        print(f"\n🎉 视频生成完成!")
                        print(f"Video URL: {video_url}")
                    else:
                        print(f"\n🎉 完成! Content: {content}")
                    break
                elif status in ["failed", "cancelled"]:
                    print(f"\n❌ 任务失败: {poll_data}")
                    break
            else:
                print("\n⏰ 轮询超时，任务可能还在处理中")
                print(f"Task ID: {task_id} (可稍后查询)")
            
        except Exception as e:
            print(f"\n❌ 异常: {e}")
            import traceback
            traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(test_seedance_multimodal())
