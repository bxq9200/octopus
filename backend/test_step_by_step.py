"""测试逐步节点生成"""
import asyncio
import sys

# 强制重新加载所有模块
for mod in list(sys.modules.keys()):
    if 'app.' in mod:
        del sys.modules[mod]

from app.engine.interactive_executor import InteractiveExecutor
from app.engine.workflow_state import workflow_state

async def test():
    project_id = 'test_proj_009'
    if workflow_state.get_project(project_id):
        workflow_state.projects.pop(project_id, None)
    
    def ws_cb(msg):
        print(f'[WS] {msg["type"]}: {msg.get("message", "")[:50]}')
    
    executor = InteractiveExecutor(project_id=project_id, ws_callback=ws_cb)
    
    # 步骤1: 初始对话
    print('=== 步骤1: 初始对话 ===')
    await executor.handle_initial_chat('小猫温馨短视频，写实，60秒，9:16，抖音，温馨', {})
    
    # 步骤2: 确认方案
    print('\n=== 步骤2: 确认方案 ===')
    await executor.confirm_plan()
    
    nodes = workflow_state.get_all_nodes(project_id)
    print(f'节点数: {len(nodes)}')
    for n in nodes:
        print(f'  - {n["id"]}: {n["label"]} (template_id={n.get("node_id")})')
    
    # 步骤3: 完成并确认剧本
    print('\n=== 步骤3: 完成剧本 + 确认 ===')
    script = nodes[0]
    workflow_state.set_node_status(project_id, script['id'], 'completed')
    workflow_state.update_node(project_id, script['id'], {
        'result': {'script_title': '小猫', 'scenes': []}
    })
    workflow_state.confirm_node(project_id, script['id'])
    
    result = await executor.confirm_script(script['id'])
    print(f'结果: {result["type"]} - {result.get("message", "")}')
    
    nodes = workflow_state.get_all_nodes(project_id)
    print(f'节点数: {len(nodes)}')
    for n in nodes:
        print(f'  - {n["id"]}: {n["label"]} (template_id={n.get("node_id")}, status={n["status"]})')
    
    # 步骤4: 完成并确认美术
    print('\n=== 步骤4: 完成美术 + 确认 ===')
    art = [n for n in nodes if n['label'] == '美术设定'][0]
    workflow_state.set_node_status(project_id, art['id'], 'completed')
    workflow_state.update_node(project_id, art['id'], {
        'result': {'visual_style': '温馨'}
    })
    workflow_state.confirm_node(project_id, art['id'])
    
    result = await executor.confirm_art(art['id'])
    print(f'结果: {result["type"]} - {result.get("message", "")}')
    
    nodes = workflow_state.get_all_nodes(project_id)
    print(f'节点数: {len(nodes)}')
    for n in nodes:
        print(f'  - {n["id"]}: {n["label"]} (template_id={n.get("node_id")}, status={n["status"]})')

asyncio.run(test())
