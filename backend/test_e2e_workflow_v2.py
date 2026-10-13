#!/usr/bin/env python3
"""
端到端测试：完整对话 → 方案 → 确认 → 执行流程（带轮询）
"""
import asyncio
import httpx
import json

BASE_URL = "http://localhost:8000"

async def poll_node_status(client, project_id, node_id, max_wait=60):
    """轮询节点状态直到完成或超时"""
    for i in range(max_wait):
        resp = await client.get(f"{BASE_URL}/api/workflow/status/{project_id}")
        status = resp.json()
        nodes = status.get("nodes", [])
        for n in nodes:
            if n.get("id") == node_id or n.get("node_id") == node_id:
                if n.get("status") in ["completed", "confirmed", "failed"]:
                    return n
        await asyncio.sleep(1)
    return None

async def test_full_workflow():
    print("=" * 70)
    print("🐙 Octopus 端到端流程测试（带轮询）")
    print("=" * 70)
    
    async with httpx.AsyncClient(timeout=60) as client:
        # Step 1: 初始对话
        print("\n📍 Step 1: 初始对话")
        print("-" * 50)
        
        resp = await client.post(f"{BASE_URL}/api/workflow/chat", json={
            "message": "我想做一个关于翼装飞行的短视频",
            "context": {}
        })
        data = resp.json()
        project_id = data.get("project_id")
        print(f"Project ID: {project_id}")
        print(f"Type: {data.get('type')}")
        
        # Step 2: 回答追问
        print("\n📍 Step 2: 回答追问")
        print("-" * 50)
        
        resp = await client.post(f"{BASE_URL}/api/workflow/chat", json={
            "message": "内容来源是真实飞行体验，风格要硬核刺激，时长30秒，比例9:16发抖音，情绪是肾上腺素飙升的感觉",
            "project_id": project_id,
            "context": {}
        })
        data = resp.json()
        print(f"Type: {data.get('type')}")
        print(f"Workflow nodes: {len(data.get('workflow_nodes', []))} 个")
        for node in data.get("workflow_nodes", []):
            print(f"  - {node.get('node_name')} ({node.get('node_id')})")
        
        # Step 3: 确认方案
        print("\n📍 Step 3: 确认方案")
        print("-" * 50)
        
        resp = await client.post(f"{BASE_URL}/api/workflow/confirm-plan", json={
            "project_id": project_id
        })
        data = resp.json()
        print(f"Type: {data.get('type')}")
        print(f"Message: {data.get('message', '')}")
        node_id = data.get("node_id")
        
        # Step 4: 轮询等待剧本生成完成
        print("\n📍 Step 4: 等待剧本生成完成（轮询）")
        print("-" * 50)
        
        node = await poll_node_status(client, project_id, node_id, max_wait=60)
        if node:
            print(f"✅ 节点状态: {node.get('status')}")
            result = node.get("result", {})
            print(f"剧本标题: {result.get('script_title', 'N/A')}")
            print(f"场景数: {len(result.get('scenes', []))}")
            print(f"角色数: {len(result.get('characters', []))}")
        else:
            print("❌ 等待超时或节点未找到")
        
        # Step 5: 查询 DAG
        print("\n📍 Step 5: 查询 DAG 结构")
        print("-" * 50)
        
        resp = await client.get(f"{BASE_URL}/api/workflow/dag/{project_id}")
        dag = resp.json()
        print(f"Nodes: {len(dag.get('nodes', []))}")
        print(f"Edges: {len(dag.get('edges', []))}")
        
        print("\n" + "=" * 70)
        print("✅ 端到端测试完成！")
        print("=" * 70)

if __name__ == "__main__":
    asyncio.run(test_full_workflow())
