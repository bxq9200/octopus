# -*- coding: utf-8 -*-
"""MV 链路端到端冒烟：start_mv → 歌曲解析 → 确认 → 自动推进到 MV 剧本。

只跑新增的两个阶段（美术/关键帧/视频会真出图花钱，不在冒烟范围）。
会打真 LLM + 真 ASR（花钱），所以叫 _manual_，不进自动化回归。
跑法： cd backend && OCTOPUS_DEPLOYMENT_MODE=local python tests/_manual_mv_e2e.py
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.user_settings_service import user_settings          # noqa: E402
from app.engine.interactive_executor import InteractiveExecutor   # noqa: E402
from app.engine.workflow_state import workflow_state, NodeStatus  # noqa: E402

PID = 'mv_smoke_test'
SONG = '/output/uploads/mvtest/song_intro.mp3'      # 20 秒器乐前奏 + 人声，67.8 秒


async def wait_node(pid, node_id, timeout=300):
    """等节点跑完（COMPLETED / FAILED）。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        n = workflow_state.get_node(pid, node_id) or {}
        st = n.get('status')
        if st in (NodeStatus.COMPLETED, NodeStatus.FAILED, 'completed', 'failed'):
            return n
        await asyncio.sleep(1.0)
    raise TimeoutError(f'节点 {node_id} 超时未完成')


async def main():
    await user_settings.hydrate()
    workflow_state.create_project(PID, name='MV 冒烟测试')

    msgs = []
    ex = InteractiveExecutor(PID, ws_callback=lambda m: msgs.append(m))

    print('【1】start_mv ...')
    r = await ex.start_mv({
        'audio_path': SONG,
        'type': 'narrative',
        'style_refs': [],
        'style_preset': '国风水墨，青灰调',
        'user_brief': '',
    })
    node_id = r['node']['id']
    print(f"    建节点 {node_id}  phase={r['phase']}")

    proj = workflow_state.get_project(PID)
    print(f"    项目 config.mode = {(proj.get('config') or {}).get('mode')}")
    print(f"    项目 phase       = {proj.get('phase')}")

    print('\n【2】等歌曲解析跑完（ASR 切片 + 曲式分析 + 风格锁定）...')
    node = await wait_node(PID, node_id)
    if node.get('status') in (NodeStatus.FAILED, 'failed'):
        print('    ✗ 失败:', node.get('error'))
        return
    res = node.get('result') or {}
    song = res.get('song') or {}
    print(f"    时长 {res.get('duration')}s | 歌词 {len(res.get('lyrics') or [])} 句 "
          f"| ASR 切 {res.get('asr_slices')} 片")
    print(f"    主题: {song.get('theme')}")
    print(f"    段落 {len(song.get('sections') or [])} 段:")
    for s in (song.get('sections') or []):
        print(f"      {s.get('type'):<11} {float(s.get('start',0)):6.1f}-{float(s.get('end',0)):6.1f}")
    print(f"    风格: {str((res.get('style') or {}).get('style_prompt'))[:70]}")

    print('\n【3】确认节点 → 应自动推进到 MV 剧本 ...')
    adv = await ex.confirm_node(node_id)
    print(f"    返回 type={adv.get('type')} phase={adv.get('phase')}")
    if adv.get('type') != 'new_node':
        print('    ✗ 没有推进到下一阶段！返回：', str(adv)[:300])
        return
    nxt = adv['node']
    print(f"    新节点 {nxt['id']}  type={nxt['type']}  label={nxt['label']}")
    assert nxt['type'] == 'mv_script_design', f"推进到了错误的阶段：{nxt['type']}"

    print('\n【4】等 MV 剧本跑完 ...')
    n2 = await wait_node(PID, nxt['id'])
    if n2.get('status') in (NodeStatus.FAILED, 'failed'):
        print('    ✗ 失败:', n2.get('error'))
        return
    sc = n2.get('result') or {}
    print(f"    logline: {sc.get('logline')}")
    print(f"    角色 {len(sc.get('characters') or [])} 个: "
          f"{'、'.join(c.get('name','') for c in (sc.get('characters') or []))}")
    print(f"    场景 {len(sc.get('scenes') or [])} 个: "
          f"{'、'.join(s.get('name','') for s in (sc.get('scenes') or []))}")
    print(f"    style 已透传给美术: {bool(sc.get('style'))}")

    print('\n【5】本地试算镜头切分（不调模型）...')
    from app.modules._mv_timeline import plan_shot_windows, verify_windows
    w = plan_shot_windows(res['lyrics'], song.get('sections'), res['duration'])
    print(f"    {len(w)} 镜 | 体检: {verify_windows(w, res['duration']) or 'OK 全部合规'}")
    for x in w:
        words = '｜'.join(l['text'] for l in x['lyrics'])[:34] or '（器乐）'
        print(f"      #{x['index']:<2} {x['start']:6.2f}-{x['end']:6.2f} ({x['duration']:5.2f}s)"
              f" [{x['section']:<9}] {words}")

    print('\n✅ MV 链路冒烟通过：start_mv → 歌曲解析 → 自动推进 → MV 剧本')

asyncio.run(main())
