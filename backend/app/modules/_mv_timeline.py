"""MV 镜头时间轴规划（1.1.0）——纯算法，不含任何 prompt

为什么单独成模块、不写进 mv_director agent：
  ① 这是硬约束求解（每镜 ∈ [4,12] 秒、切点必须落在歌词句边界、首尾相接总和=歌长），
     交给 LLM 做算术+约束满足非常不可靠；代码算才能保证一定成立。
  ② 不含 prompt ⇒ 不属于护城河。放进 agent 会被打包剥壳器连体剥掉，
     而前端校对界面改完歌词轴后要**本地即时重算**镜头预览，不该为此往返云端。

分工：这里定「每个镜头占哪段时间、盖住哪几句词」，agent 只负责给定好的窗口填画面内容。

🔴 4~12 秒的来历：Seedance 2.0 的硬钳制，三处代码一致
   （_octopus_cloud_video.py / _antchain_video.py / image_to_video.py）。
   歌词一句常只有 2~3 秒，所以必须按乐句合并，不能一句一镜。
"""
import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

MIN_SHOT = 4.0
MAX_SHOT = 12.0

# 各段落的目标镜头时长：副歌切得密（情绪推进快），主歌用长镜（叙事铺陈），
# 纯器乐段最长（没有词要卡，长镜更耐看）。用户选的「混合」策略就落在这张表上。
SECTION_TARGET = {
    "intro":        10.0,
    "outro":        10.0,
    "interlude":    10.0,
    "instrumental": 10.0,
    "verse":         8.0,
    "pre_chorus":    6.0,
    "chorus":        4.5,
    "bridge":        7.0,
}
DEFAULT_TARGET = 7.0


def _target_for(section_type: str) -> float:
    return SECTION_TARGET.get((section_type or "").strip().lower(), DEFAULT_TARGET)


def normalize_sections(sections: Optional[List[Dict]], duration: float) -> List[Dict]:
    """把 LLM 给的段落表整理成**无缝覆盖 [0, duration]** 的有序段落。

    LLM 常见毛病：段落有缝、有重叠、末段不到歌尾、start/end 是字符串。全在这里兜住。
    """
    out: List[Dict] = []
    for s in sections or []:
        try:
            st = max(0.0, min(duration, float(s.get("start", 0))))
            en = max(0.0, min(duration, float(s.get("end", 0))))
        except (TypeError, ValueError):
            continue
        if en - st < 0.5:
            continue
        out.append({"type": (s.get("type") or "verse").strip().lower(), "start": st, "end": en})

    if not out:
        return [{"type": "verse", "start": 0.0, "end": duration}]

    out.sort(key=lambda x: x["start"])
    fixed: List[Dict] = []
    cursor = 0.0
    for s in out:
        st = max(s["start"], cursor)          # 消重叠
        if st >= duration:
            break
        if st - cursor > 0.5:                 # 补缝：归给前一段，没有前一段就自成一段
            if fixed:
                fixed[-1]["end"] = st
            else:
                fixed.append({"type": "intro", "start": cursor, "end": st})
        fixed.append({"type": s["type"], "start": st, "end": min(s["end"], duration)})
        cursor = fixed[-1]["end"]
    if not fixed:
        return [{"type": "verse", "start": 0.0, "end": duration}]
    fixed[-1]["end"] = duration               # 末段必须顶到歌尾
    return [s for s in fixed if s["end"] - s["start"] > 0.05]


def _lines_in(lines: List[Dict], start: float, end: float) -> List[Dict]:
    """归属规则按**句子起点**算：一句词只属于一个镜头，不会被两个镜头同时认领。"""
    return [l for l in lines if start - 0.01 <= l["start"] < end]


def _boundary(ln: Dict, nxt: Optional[Dict], sec_end: float, cur: float) -> float:
    """本句之后的切点，并夹进 [MIN_SHOT, MAX_SHOT] 允许的范围。

    🔴 切点默认取「本句结束 ~ 下句开始」的中点，落在停顿正中。
       不切在下句 start 上：那会削掉起音（ASR 的起音检测本就偏晚，再切就更秃）。
       够不够 4 秒时，允许在停顿区间内前后挪——挪一下就能成立的，
       不必为了凑时长硬塞下一整句词进来。
    """
    if nxt is None:
        return sec_end
    early = ln["end"] + 0.05
    late = max(early, nxt["start"] - 0.05)
    cut = (ln["end"] + nxt["start"]) / 2
    if cut - cur < MIN_SHOT:
        cut = min(late, cur + MIN_SHOT)
    elif cut - cur > MAX_SHOT:
        cut = max(early, cur + MAX_SHOT)
    return cut


def _cut_section(sec: Dict, lines: List[Dict]) -> List[Dict]:
    """在单个段落内切镜头。"""
    target = _target_for(sec["type"])
    st, en = sec["start"], sec["end"]
    inner = _lines_in(lines, st, en)
    shots: List[Dict] = []

    if not inner:
        # 纯器乐段：没有词要卡，均分即可
        n = max(1, round((en - st) / target))
        step = (en - st) / n
        for i in range(n):
            shots.append({
                "start": st + i * step,
                "end": st + (i + 1) * step,
                "section": sec["type"],
                "lyrics": [],
            })
        return shots

    cur, acc, i = st, [], 0
    while i < len(inner):
        acc.append(inner[i])
        nxt = inner[i + 1] if i + 1 < len(inner) else None
        if nxt is None:
            shots.append({"start": cur, "end": en, "section": sec["type"], "lyrics": list(acc)})
            break

        cut = _boundary(inner[i], nxt, en, cur)
        # 前瞻一句：再吃一句会变多长。谁更接近该段落的目标时长就选谁。
        # 🔴 少了这步前瞻，贪心只会「凑够 target 就断」，于是副歌(4.5s)和主歌(8s)
        #    都变成两句一镜的 ~8 秒，混合策略退化成没有策略。
        nxt2 = inner[i + 2] if i + 2 < len(inner) else None
        len_now = cut - cur
        len_next = _boundary(nxt, nxt2, en, cur) - cur
        if len_now >= MIN_SHOT and (len_next > MAX_SHOT or
                                    abs(len_now - target) <= abs(len_next - target)):
            shots.append({"start": cur, "end": cut, "section": sec["type"], "lyrics": list(acc)})
            cur, acc = cut, []
        i += 1
    return shots


def _split_long(shots: List[Dict]) -> List[Dict]:
    """把 >MAX_SHOT 的镜头劈开。优先劈在内部句边界，没有就均分。"""
    out: List[Dict] = []
    for sh in shots:
        span = sh["end"] - sh["start"]
        if span <= MAX_SHOT:
            out.append(sh)
            continue
        n = int(span // MAX_SHOT) + 1
        lys = sh.get("lyrics") or []
        if len(lys) >= n:                       # 有足够的句边界可用：按句均摊
            per = max(1, len(lys) // n)
            cur = sh["start"]
            for k in range(n):
                chunk = lys[k * per:(k + 1) * per] if k < n - 1 else lys[k * per:]
                if not chunk:
                    continue
                end = sh["end"] if k == n - 1 else (chunk[-1]["end"] + 0.15)
                out.append({**sh, "start": cur, "end": min(end, sh["end"]), "lyrics": chunk})
                cur = out[-1]["end"]
            out[-1]["end"] = sh["end"]
        else:                                   # 器乐长段：均分
            step = span / n
            for k in range(n):
                out.append({**sh,
                            "start": sh["start"] + k * step,
                            "end": sh["start"] + (k + 1) * step,
                            "lyrics": lys if k == 0 else []})
    return out


def _merge_short(shots: List[Dict]) -> List[Dict]:
    """把 <MIN_SHOT 的镜头并进相邻镜头（选更短的那侧，避免把邻居撑爆）。"""
    if len(shots) <= 1:
        return shots
    out = [dict(s) for s in shots]
    i = 0
    while i < len(out):
        span = out[i]["end"] - out[i]["start"]
        if span >= MIN_SHOT or len(out) == 1:
            i += 1
            continue
        prev_len = (out[i - 1]["end"] - out[i - 1]["start"]) if i > 0 else float("inf")
        next_len = (out[i + 1]["end"] - out[i + 1]["start"]) if i + 1 < len(out) else float("inf")
        if prev_len <= next_len:
            out[i - 1]["end"] = out[i]["end"]
            out[i - 1]["lyrics"] = (out[i - 1].get("lyrics") or []) + (out[i].get("lyrics") or [])
            del out[i]
            i = max(0, i - 1)
        else:
            out[i + 1]["start"] = out[i]["start"]
            out[i + 1]["lyrics"] = (out[i].get("lyrics") or []) + (out[i + 1].get("lyrics") or [])
            # 段落归属跟着**词多**的那边走，别让副歌镜头被标成 intro
            if len(out[i].get("lyrics") or []) > len(out[i + 1].get("lyrics") or []):
                out[i + 1]["section"] = out[i]["section"]
            del out[i]
    return out


def plan_shot_windows(lines: List[Dict], sections: Optional[List[Dict]], duration: float) -> List[Dict]:
    """主入口：歌词轴 + 段落表 → 镜头时间窗。

    产出保证（调用方可以直接依赖，不必再校验）：
      · 首镜 start == 0，末镜 end == duration，镜头间首尾相接（无缝无叠）
      · 每镜时长 ∈ [4, 12]，除非整首歌本身就短于 4 秒
      · 每句歌词恰好归属一个镜头
    """
    if duration <= 0:
        return []
    lines = sorted([l for l in (lines or []) if l.get("end", 0) > l.get("start", 0)],
                   key=lambda x: x["start"])
    if duration <= MIN_SHOT:                    # 极短音频：无法满足下限，整支一镜
        return [{"index": 1, "start": 0.0, "end": round(duration, 2),
                 "duration": round(duration, 2), "section": "verse", "lyrics": lines}]

    shots: List[Dict] = []
    for sec in normalize_sections(sections, duration):
        shots.extend(_cut_section(sec, lines))

    # 劈长 → 并短 → 再劈长（并短可能撑出超长镜头）。三轮足够收敛，加上限防死循环。
    for _ in range(3):
        before = len(shots)
        shots = _merge_short(_split_long(shots))
        if len(shots) == before:
            break

    # 首尾对齐 + 消缝：镜头必须严格首尾相接，否则合成时会漂移
    shots[0]["start"] = 0.0
    for i in range(len(shots) - 1):
        shots[i + 1]["start"] = shots[i]["end"]
    shots[-1]["end"] = duration

    out: List[Dict] = []
    for i, sh in enumerate(shots, 1):
        out.append({
            "index": i,
            "start": round(sh["start"], 2),
            "end": round(sh["end"], 2),
            "duration": round(sh["end"] - sh["start"], 2),
            "section": sh.get("section") or "verse",
            "lyrics": sh.get("lyrics") or [],
        })
    return out


def _span(sh: Dict) -> float:
    """镜头时长。时间窗用 `duration`，成品镜头用 `duration_seconds`（下游字段名），
    两种都要认——否则这个体检函数就只能验中间产物，验不了最终交付的东西。"""
    v = sh.get("duration", sh.get("duration_seconds"))
    if v is None:
        v = float(sh.get("end", 0)) - float(sh.get("start", 0))
    return float(v)


def verify_windows(shots: List[Dict], duration: float) -> List[str]:
    """体检：返回问题列表，空列表表示完全合规。给测试和运行期断言共用。"""
    problems: List[str] = []
    if not shots:
        return ["镜头列表为空"]
    if abs(shots[0]["start"]) > 0.01:
        problems.append(f"首镜不是从 0 开始（{shots[0]['start']}）")
    if abs(shots[-1]["end"] - duration) > 0.05:
        problems.append(f"末镜没顶到歌尾（{shots[-1]['end']} vs {duration}）")
    for i in range(len(shots) - 1):
        if abs(shots[i]["end"] - shots[i + 1]["start"]) > 0.01:
            problems.append(f"镜头 {i+1}/{i+2} 之间有缝或重叠")
    if len(shots) > 1:
        for sh in shots:
            span = _span(sh)
            if span < MIN_SHOT - 0.01:
                problems.append(f"镜头 {sh.get('index')} 只有 {span:.2f}s（< {MIN_SHOT}）")
            if span > MAX_SHOT + 0.01:
                problems.append(f"镜头 {sh.get('index')} 长达 {span:.2f}s（> {MAX_SHOT}）")
    return problems
