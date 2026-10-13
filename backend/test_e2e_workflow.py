#!/usr/bin/env python3
"""
端到端测试：完整对话 → 方案 → 确认 → 执行流程
"""
import asyncio
import httpx
import json

BASE_URL = "http://localhost:8000"

async def test_full_workflow():
    print("=" * 70)
    print("🐙 Octopus 端到端流程测试")
    print("=" * 70)
    
    async with httpx.AsyncClient(timeout=60) as client:
        # Step 1: 初始对话（提供模糊需求）
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
        print(f"Message: {data.get('message', '')[:200]}...")
        
        if data.get("type") == "ask":
            # Step 2: 回答追问（快速通过 checklist）
            print("\n📍 Step 2: 回答追问")
            print("-" * 50)
            
            # 一次性提供所有信息
            resp = await client.post(f"{BASE_URL}/api/workflow/chat", json={
                "message": "内容来源是真实飞行体验，风格要硬核刺激，时长30秒，比例9:16发抖音，情绪是肾上腺素飙升的感觉",
                "project_id": project_id,
                "context": {}
            })
            data = resp.json()
            print(f"Type: {data.get('type')}")
            print(f"Message: {data.get('message', '')[:300]}...")
            
            # 检查是否生成了方案
            if data.get("type") == "plan":
                print(f"\n✅ 方案已生成！")
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
        print(f"Node ID: {data.get('node_id')}")
        print(f"Phase: {data.get('phase')}")
        
        # Step 4: 等待并查询状态
        print("\n📍 Step 4: 查询项目状态")
        print("-" * 50)
        
        await asyncio.sleep(2)  # 等待节点执行
        
        resp = await client.get(f"{BASE_URL}/api/workflow/status/{project_id}")
        status = resp.json()
        
        if "error" in status:
            print(f"❌ Error: {status['error']}")
        else:
            project = status.get("project", {})
            nodes = status.get("nodes", [])
            print(f"Project phase: {project.get('phase')}")
            print(f"Total nodes: {len(nodes)}")
            for node in nodes:
                print(f"  - {node.get('label')} ({node.get('type')}): {node.get('status')}")
        
        # Step 5: 查询 DAG 结构
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
