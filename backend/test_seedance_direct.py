#!/usr/bin/env python3
"""直接测试 Seedance API 连通性"""
import asyncio
import os
import httpx

API_KEY = os.environ.get("SEEDANCE_API_KEY", "")
BASE_URL = "https://operator.las.cn-beijing.volces.com/api/v1"

async def test_api():
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json"
    }
    
    # 测试1: 创建纯文本视频生成任务（最简单的情况）
    payload = {
        "model": "doubao-seedance-1-5-pro-251215",
        "prompt": "A beautiful sunset over mountains, cinematic shot",
        "ratio": "16:9",
        "duration": 5,
        "resolution": "720p",
        "generate_audio": False
    }
    
    print("=" * 60)
    print("🧪 Seedance API 直连测试")
    print("=" * 60)
    print(f"URL: {BASE_URL}/contents/generations/tasks")
    print(f"Model: {payload['model']}")
    print(f"Key前缀: {API_KEY[:20]}...")
    print("-" * 60)
    
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.post(
                f"{BASE_URL}/contents/generations/tasks",
                headers=headers,
                json=payload
            )
            print(f"Status: {resp.status_code}")
            print(f"Response: {resp.text[:500]}")
            
            if resp.status_code == 200:
                data = resp.json()
                print(f"\n✅ 任务创建成功!")
                print(f"Task ID: {data.get('id')}")
                print(f"Status: {data.get('status')}")
                
                # 轮询查询结果
                task_id = data.get('id')
                if task_id:
                    print("\n⏳ 轮询任务状态...")
                    for i in range(10):
                        await asyncio.sleep(3)
                        poll = await client.get(
                            f"{BASE_URL}/contents/generations/tasks/{task_id}",
                            headers=headers
                        )
                        poll_data = poll.json()
                        status = poll_data.get('status', 'unknown')
                        print(f"  [{i+1}/10] Status: {status}")
                        
                        if status == "succeeded":
                            content = poll_data.get('content', {})
                            video_url = content.get('video_url') if isinstance(content, dict) else None
                            print(f"\n🎉 视频生成完成!")
                            print(f"Video URL: {video_url}")
                            break
                        elif status in ["failed", "cancelled"]:
                            print(f"\n❌ 任务失败: {poll_data}")
                            break
            else:
                print(f"\n❌ API 错误: {resp.status_code}")
                try:
                    err = resp.json()
                    print(f"Error detail: {err}")
                except:
                    print(f"Raw: {resp.text}")
                    
        except Exception as e:
            print(f"\n❌ 请求异常: {e}")
            import traceback
            traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(test_api())
