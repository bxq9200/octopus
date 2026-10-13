# -*- coding: utf-8 -*-
"""MV 镜头时间轴规划的回归测试。

时间轴一旦算错，整支 MV 的画面就跟歌错位，而且事后极难看出是哪一步坏的
（成片"感觉有点飘"但说不清）。所以这里把**产出契约**逐条钉死：
首尾相接、无缝无叠、每镜 4~12 秒、每句词恰好归属一个镜头。

跑法： cd backend && python -m pytest tests/test_mv_timeline.py -q
      或直接 python tests/test_mv_timeline.py（会打印每个用例的切分结果）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.modules._mv_timeline import (  # noqa: E402
    MAX_SHOT, MIN_SHOT, plan_shot_windows, verify_windows,
)


def _lines(spec):
    """[(start, end, text), ...] → 歌词轴"""
    return [{"start": s, "end": e, "text": t} for s, e, t in spec]


def _evenly(start, end, step, gap=0.6, prefix="词"):
    """造一段等间隔歌词，模拟规整的主歌/副歌"""
    out, t, i = [], start, 1
    while t + step <= end:
        out.append({"start": round(t, 2), "end": round(t + step - gap, 2), "text": f"{prefix}{i}"})
        t += step
        i += 1
    return out


def _check(name, shots, duration, lines, *, verbose=False):
    problems = verify_windows(shots, duration)
    assert not problems, f"[{name}] 契约违规: {problems}"

    # 每句歌词恰好归属一个镜头 —— 漏了就是画面没词可对，重了就是同一句被两个镜头抢
    assigned = [ly["text"] for sh in shots for ly in sh["lyrics"]]
    assert len(assigned) == len(set(assigned)), f"[{name}] 有歌词被重复分配"
    missing = {l["text"] for l in lines} - set(assigned)
    assert not missing, f"[{name}] 有 {len(missing)} 句歌词没被任何镜头认领: {list(missing)[:5]}"

    total = sum(sh["duration"] for sh in shots)
    assert abs(total - duration) < 0.05, f"[{name}] 镜头总长 {total} != 歌长 {duration}"

    if verbose:
        print(f"\n── {name} ──  {len(shots)} 镜 / {duration}s")
        for sh in shots:
            words = "｜".join(l["text"] for l in sh["lyrics"])[:46] or "（器乐）"
            print(f"   #{sh['index']:<3}{sh['start']:>7.2f}-{sh['end']:>7.2f} "
                  f"({sh['duration']:>5.2f}s) [{sh['section']:<9}] {words}")
    return shots


# ── 用例 1：真实 ASR 产出（47.9 秒，12 句）──────────────────────────
REAL_LINES = _lines([
    (0.20, 3.30, "清晨的光落在安静的河面上"), (4.50, 7.20, "她独自走过空荡的长街"),
    (8.30, 11.20, "城市在细雨里沉沉睡去"),    (12.20, 15.20, "明天我们会找到回家的路"),
    (16.30, 19.50, "风吹过山岗吹过旧时的窗"), (20.50, 23.40, "那些说过的话都散在风里"),
    (24.50, 27.20, "我把心事折成一只纸船"),   (28.43, 31.53, "让它顺着流水漂向远方"),
    (32.63, 35.33, "夜色温柔像母亲的手掌"),   (36.43, 39.23, "轻轻覆盖所有未愈的伤"),
    (40.33, 43.13, "如果时间可以倒着流淌"),   (44.23, 46.83, "我愿意再走一次那条路"),
])


def test_real_asr_output(verbose=False):
    d = 47.86
    secs = [{"type": "verse", "start": 0, "end": 24}, {"type": "chorus", "start": 24, "end": 47.86}]
    shots = _check("真实ASR·主歌+副歌", plan_shot_windows(REAL_LINES, secs, d), d, REAL_LINES, verbose=verbose)
    # 副歌该比主歌切得密：这是用户选的「混合」策略的核心，退化了就等于没做
    verse = [s for s in shots if s["section"] == "verse"]
    chorus = [s for s in shots if s["section"] == "chorus"]
    if verse and chorus:
        av, ac = sum(s["duration"] for s in verse) / len(verse), sum(s["duration"] for s in chorus) / len(chorus)
        assert ac < av, f"副歌镜头({ac:.1f}s)没有比主歌({av:.1f}s)短，混合策略失效"


# ── 用例 2：完整 4 分钟歌曲结构 ────────────────────────────────────
def test_full_song(verbose=False):
    d = 245.0
    lines = (_evenly(20, 60, 4.0, prefix="主歌A") + _evenly(62, 92, 2.2, prefix="副歌A")
             + _evenly(100, 140, 4.0, prefix="主歌B") + _evenly(142, 172, 2.2, prefix="副歌B")
             + _evenly(180, 205, 3.0, prefix="桥段") + _evenly(207, 235, 2.2, prefix="副歌C"))
    secs = [
        {"type": "intro", "start": 0, "end": 20}, {"type": "verse", "start": 20, "end": 60},
        {"type": "chorus", "start": 60, "end": 95}, {"type": "interlude", "start": 95, "end": 100},
        {"type": "verse", "start": 100, "end": 140}, {"type": "chorus", "start": 140, "end": 175},
        {"type": "bridge", "start": 175, "end": 205}, {"type": "chorus", "start": 205, "end": 237},
        {"type": "outro", "start": 237, "end": 245},
    ]
    _check("完整4分钟", plan_shot_windows(lines, secs, d), d, lines, verbose=verbose)


# ── 用例 3：LLM 给的段落表是脏的（缝/重叠/越界/字符串）────────────
def test_dirty_sections(verbose=False):
    d = 120.0
    lines = _evenly(10, 110, 3.5)
    dirty = [
        {"type": "intro", "start": "0", "end": "10"},      # 字符串
        {"type": "verse", "start": 8, "end": 45},          # 与上一段重叠 2 秒
        {"type": "chorus", "start": 50, "end": 80},        # 与上一段有 5 秒缝
        {"type": "verse", "start": 80, "end": 999},        # 越界
        {"type": "outro", "start": 200, "end": 260},       # 整段在歌尾之外
        {"type": "bridge", "start": 60, "end": 60.2},      # 时长几乎为 0
    ]
    _check("脏段落表", plan_shot_windows(lines, dirty, d), d, lines, verbose=verbose)


# ── 用例 4：没有段落表（LLM 没给 / 解析失败）─────────────────────
def test_no_sections(verbose=False):
    d = 100.0
    lines = _evenly(2, 98, 3.0)
    _check("无段落表", plan_shot_windows(lines, None, d), d, lines, verbose=verbose)


# ── 用例 5：纯器乐，一句词都没有 ───────────────────────────────────
def test_instrumental_only(verbose=False):
    d = 180.0
    secs = [{"type": "intro", "start": 0, "end": 40}, {"type": "interlude", "start": 40, "end": 180}]
    _check("纯器乐", plan_shot_windows([], secs, d), d, [], verbose=verbose)


# ── 用例 6：极密副歌（每句 1.5 秒，远短于 4 秒下限）────────────────
def test_dense_chorus(verbose=False):
    d = 60.0
    lines = _evenly(0, 60, 1.5, gap=0.2, prefix="快词")
    secs = [{"type": "chorus", "start": 0, "end": 60}]
    shots = _check("极密副歌", plan_shot_windows(lines, secs, d), d, lines, verbose=verbose)
    assert all(s["duration"] >= MIN_SHOT - 0.01 for s in shots), "密集歌词没有被合并到 4 秒以上"


# ── 用例 7：超长纯器乐段（必须被劈成多个 ≤12 秒镜头）──────────────
def test_long_instrumental_block(verbose=False):
    d = 95.0
    lines = _lines([(2.0, 5.0, "开场一句")])
    secs = [{"type": "verse", "start": 0, "end": 8}, {"type": "interlude", "start": 8, "end": 95}]
    shots = _check("超长器乐段", plan_shot_windows(lines, secs, d), d, lines, verbose=verbose)
    assert all(s["duration"] <= MAX_SHOT + 0.01 for s in shots)


# ── 用例 8：退化输入不许崩 ────────────────────────────────────────
def test_degenerate_inputs():
    assert plan_shot_windows([], [], 0) == []
    assert plan_shot_windows(None, None, -5) == []
    tiny = plan_shot_windows(_lines([(0, 2, "就一句")]), None, 3.0)
    assert len(tiny) == 1 and abs(tiny[0]["duration"] - 3.0) < 0.01   # 短于下限：整支一镜，不崩
    # 歌词越界（ASR 偶尔给出超过音频长度的 end）不该让契约失效
    over = plan_shot_windows(_lines([(0, 5, "a"), (50, 90, "b")]), None, 40.0)
    assert not verify_windows(over, 40.0)


if __name__ == "__main__":
    for fn in (test_real_asr_output, test_full_song, test_dirty_sections, test_no_sections,
               test_instrumental_only, test_dense_chorus, test_long_instrumental_block):
        fn(verbose=True)
    test_degenerate_inputs()
    print("\n全部用例通过 ✓")
