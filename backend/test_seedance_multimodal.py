#!/usr/bin/env python3
"""测试 Seedance 多模态视频生成模块"""
import asyncio
import sys
sys.path.insert(0, "/data/octopus/backend")

from app.modules.text_to_video import TextToVideoModule

async def test():
    module = TextToVideoModule()
    
    # 测试参数：首帧+尾帧多模态生成
    input_data = {
        "prompt": "A cinematic transition from a peaceful mountain lake at sunrise to a dramatic cliff jump at sunset. The camera slowly pulls back revealing the vast landscape, then accelerates into an adrenaline-filled wingsuit flight through the mountain valley. Epic music builds up, natural lighting, 4K quality, smooth camera movement.",
        "first_frame_url": "/data/octopus/backend/output/images/first_frame.jpg",
        "last_frame_url": "/data/octopus/backend/output/images/last_frame.jpg",
        "ratio": "9:16",
        "duration": 6,
        "resolution": "720p",
        "generate_audio": True,
        "watermark": False,
        "seed": -1,
        "fast_mode": False
    }
    
    print("=" * 60)
    print("🎬 Seedance 多模态视频生成测试")
    print("=" * 60)
    print(f"首帧图片: {input_data['first_frame_url']}")
    print(f"尾帧图片: {input_data['last_frame_url']}")
    print(f"比例: {input_data['ratio']}")
    print(f"时长: {input_data['duration']}秒")
    print(f"分辨率: {input_data['resolution']}")
    print(f"音频生成: {input_data['generate_audio']}")
    print("-" * 60)
    
    try:
        result = await module.run(input_data)
        print("\n✅ 生成成功!")
        print(f"视频路径: {result.get('video_url')}")
        print(f"任务ID: {result.get('task_id')}")
        print(f"模型: {result.get('model')}")
        print(f"Mock模式: {result.get('mock', False)}")
        print(f"时长: {result.get('duration')}秒")
        print(f"比例: {result.get('ratio')}")
        print(f"分辨率: {result.get('resolution')}")
    except Exception as e:
        print(f"\n❌ 生成失败: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(test())
