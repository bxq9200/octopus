#!/usr/bin/env python3
"""测试 Seedance 2.0 API 连通性"""
import asyncio
import os
import httpx

API_KEY = os.environ.get("SEEDANCE_API_KEY", "")
BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"

async def test_api():
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "model": "doubao-seedance-2-0-260128",
        "prompt": "A beautiful sunset over mountains, cinematic shot",
        "ratio": "16:9",
        "duration": 5,
        "resolution": "720p"
    }
    
    print("=" * 60)
    print("🧪 Seedance 2.0 API 直连测试")
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
