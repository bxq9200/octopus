"""歌词导入与对齐（1.1.0 MV）

🔴 为什么需要这条路：
   GLM-ASR 对着歌唱基本听不出词。剥掉伴奏后有明显改善（一首儿歌 8 句 → 36 句），
   但仍只覆盖 32%，且错字很多（「风儿拍拍手」被听成「老师点点头」）。
   而从人声轨检测「哪一刻在唱」却非常准——同一首歌能检出 89% 的演唱时长。

   于是分工变成：**时间从音频来（准），文字从创作者来（准），模型一步都不参与猜。**
   创作者做 MV 时几乎必然有自己的歌词，这比任何 ASR 都可靠。

两种导入：
  · LRC —— 自带逐句时间戳，直接解析，零猜测。
  · 纯文本 —— 按人声段落顺序对齐；段落粒度会**按贴进来的行数自适应挑选**，
    行数本身就是最好的先验（贴了 40 行就找出 40 个乐句，而不是 22 个或 54 个）。
"""
import logging
import math
import re
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# LRC 时间标签：[mm:ss.xx] / [mm:ss:xx] / [mm:ss]，一行可以有多个（重复段共用歌词）
_LRC_TAG = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
# 元数据标签 [ti:] [ar:] [al:] [by:] [offset:] —— 不是歌词
_LRC_META = re.compile(r"^\[[a-zA-Z#]+:")


def parse_lrc(content: str, duration: float) -> List[Dict]:
    """解析 LRC。返回 [{start,end,text}]，按时间排序。

    end 取下一句的 start（LRC 本身不带结束时间）；最后一句给到歌尾。
    """
    items: List[Tuple[float, str]] = []
    for raw in (content or "").splitlines():
        line = raw.strip()
        if not line or _LRC_META.match(line):
            continue
        tags = _LRC_TAG.findall(line)
        if not tags:
            continue
        text = _LRC_TAG.sub("", line).strip()
        if not text:
            continue
        for mm, ss, frac in tags:
            t = int(mm) * 60 + int(ss)
            if frac:
                # 两位是百分秒、三位是毫秒
                t += int(frac) / (100.0 if len(frac) <= 2 else 1000.0)
            items.append((round(t, 2), text))

    if not items:
        return []

    items.sort(key=lambda x: x[0])
    out: List[Dict] = []
    for i, (t, text) in enumerate(items):
        nxt = items[i + 1][0] if i + 1 < len(items) else duration
        end = min(nxt, duration)
        if end - t < 0.2:                     # 同一时刻多行（合唱/翻译）——跳过重复
            continue
        out.append({"start": t, "end": round(end, 2), "text": text})
    return out


# 🔴 Suno / 常见歌词稿都带段落标记（[Verse 1] [Pre-Chorus] [Chorus] [Bridge]…）。
#    这是白送的**歌曲结构**——下游卡点分镜正需要它（副歌切得密、主歌用长镜），
#    原本要靠 LLM 从时间戳猜。别当垃圾丢掉。
_SECTION_ALIASES = {
    "intro": "intro", "前奏": "intro",
    "verse": "verse", "主歌": "verse",
    "pre-chorus": "pre_chorus", "prechorus": "pre_chorus", "pre chorus": "pre_chorus",
    "导歌": "pre_chorus", "预副歌": "pre_chorus",
    "chorus": "chorus", "副歌": "chorus", "hook": "chorus",
    "final chorus": "chorus", "last chorus": "chorus", "尾副歌": "chorus",
    "bridge": "bridge", "桥段": "bridge",
    "interlude": "interlude", "间奏": "interlude",
    "instrumental": "interlude", "solo": "interlude",
    "outro": "outro", "尾奏": "outro", "ending": "outro",
}
# 整行就是个标记：[Verse 1] / (副歌) / 【Chorus】/ Verse 2:
_SECTION_LINE = re.compile(r"^[\[\(【（]?\s*([A-Za-z一-鿿][A-Za-z \-一-鿿]*?)"
                           r"\s*\d*\s*[\]\)】）]?\s*[:：]?\s*$")


def _match_section(label: str) -> Optional[str]:
    key = re.sub(r"\s+", " ", (label or "").strip().lower())
    if key in _SECTION_ALIASES:
        return _SECTION_ALIASES[key]
    # 「Chorus 2」「主歌一」这类带序号的，去掉尾部数字/中文数字再试
    key2 = re.sub(r"[\s\d一二三四五六七八九十]+$", "", key).strip()
    return _SECTION_ALIASES.get(key2)


def parse_structured_lyrics(content: str) -> List[Dict]:
    """纯文本歌词 → [{text, section}]。段落标记被提取成 section，不当歌词。

    没有任何标记时所有行都是 verse——不影响对齐，只是分镜少了「副歌切得密」这层信息。
    """
    out: List[Dict] = []
    current = "verse"
    for raw in (content or "").splitlines():
        s = raw.strip()
        if not s:
            continue
        m = _SECTION_LINE.match(s)
        if m:
            sec = _match_section(m.group(1))
            if sec:
                current = sec
                continue                      # 标记行本身不是歌词
        if not re.search(r"[\w一-鿿]", s):   # 纯符号行
            continue
        out.append({"text": s, "section": current})
    return out


def split_plain_lyrics(content: str) -> List[str]:
    """只要文字（对齐时用来数行数）。"""
    return [x["text"] for x in parse_structured_lyrics(content)]


def derive_sections(lines: List[Dict], duration: float) -> List[Dict]:
    """从对齐好的歌词行推出段落表，交给 plan_shot_windows 用。

    连续同 section 的行合成一段；段与段之间的空隙（没人唱的地方）归给前一段，
    保证段落表**无缝覆盖 0~duration**——这正是 normalize_sections 要的形状。
    """
    if not lines:
        return []
    out: List[Dict] = []
    for ln in lines:
        sec = ln.get("section") or "verse"
        if out and out[-1]["type"] == sec:
            out[-1]["end"] = ln["end"]
        else:
            out.append({"type": sec, "start": ln["start"], "end": ln["end"]})

    # 首段从 0 开始（前面通常是前奏，但没标记就并进第一段，不凭空造 intro）
    out[0]["start"] = 0.0
    for i in range(len(out) - 1):
        out[i]["end"] = out[i + 1]["start"]
    out[-1]["end"] = duration
    return [s for s in out if s["end"] - s["start"] > 0.05]


def _similarity(a: str, b: str) -> float:
    """两句中文的相似度 0~1。ASR 的错基本是同音字，字面重合度够用。"""
    from difflib import SequenceMatcher
    ca = re.sub(r"[^\w一-鿿]", "", a or "")
    cb = re.sub(r"[^\w一-鿿]", "", b or "")
    if not ca or not cb:
        return 0.0
    return SequenceMatcher(None, ca, cb).ratio()


_ANCHOR_MIN_SIM = 0.42        # 低于这个就不认，宁可不锚也不能锚错
_ANCHOR_MIN_CHARS = 4         # 太短的句子（「真好」）字面碰巧撞上的概率太高


def find_anchors(items: List[Dict], asr_lines: List[Dict]) -> List[Tuple[int, float, float]]:
    """把创作者贴的歌词跟 ASR 结果对上，产出锚点 [(行号, start, end)]。

    🔴 为什么要锚点：纯按顺序把 M 行歌词铺到 N 个乐句上，只要检出的乐句里混进一个
       不是唱词的（换气、和声尾音、乐器泛音），从那里开始后面全体错位——实测
       「一颗两颗，三颗四颗」被铺到 15 秒，而 ASR 明确听到它在 28 秒。

       ASR 虽然只听出三成，但**听出来的地方时间是准的**。用它把歌词钉在正确位置，
       中间再按乐句分配，漂移就被限制在两个锚点之间。

    锚点必须**时间单调递增**：贴的歌词是按顺序的，匹配结果也必须是。
    """
    if not asr_lines or not items:
        return []

    duration = max(float(l.get("end", 0)) for l in asr_lines) or 1.0
    global_rate = len(items) / duration          # 平均每秒唱几行

    # ① 收集所有够像的候选配对
    cands: List[Tuple[int, float, float, float]] = []      # (行号, start, end, 相似度)
    for i, it in enumerate(items):
        if len(re.sub(r"[^\w一-鿿]", "", it["text"])) < _ANCHOR_MIN_CHARS:
            continue
        for a in asr_lines:
            sim = _similarity(it["text"], a.get("text", ""))
            if sim >= _ANCHOR_MIN_SIM:
                cands.append((i, float(a["start"]), float(a["end"]), sim))
    if not cands:
        logger.info("[lyrics] 没有够像的锚点候选")
        return []

    cands.sort(key=lambda c: (c[0], c[1]))

    # ② 动态规划挑一条**整体最优的单调链**。
    #    🔴 不能贪心。副歌会重复三遍，贪心「在剩下的里挑最像的」会一步跳到最后一遍——
    #       实测第一次副歌的首句被锚到了 170 秒（真值 ~30 秒），中间 140 秒的歌词全被拉伸。
    #    评分 = 相似度之和 − 语速突变惩罚。歌词行号和时间大体成正比，
    #    某两个锚点之间「每秒唱的行数」偏离全局越远，越可能是配错了。
    n = len(cands)
    dp = [c[3] for c in cands]
    prev = [-1] * n
    for k in range(n):
        ik, tk = cands[k][0], cands[k][1]
        for j in range(k):
            ij, tj = cands[j][0], cands[j][1]
            if ij >= ik or tj >= tk:            # 必须同时单调
                continue
            local_rate = (ik - ij) / max(0.1, tk - tj)
            ratio = local_rate / global_rate if global_rate > 0 else 1.0
            # 快 3 倍或慢 3 倍都严惩；log 让快慢对称
            penalty = 0.6 * abs(math.log(max(1e-3, ratio)))
            score = dp[j] + cands[k][3] - penalty
            if score > dp[k]:
                dp[k], prev[k] = score, j

    end = max(range(n), key=lambda k: dp[k])
    chain: List[int] = []
    while end >= 0:
        chain.append(end)
        end = prev[end]
    chain.reverse()

    anchors = [(cands[k][0], cands[k][1], cands[k][2]) for k in chain]
    logger.info("[lyrics] 锚点 %d 个（候选 %d / 歌词 %d 行 / ASR %d 句）",
                len(anchors), n, len(items), len(asr_lines))
    return anchors


def align_with_anchors(items: List[Dict], phrases: List[Tuple[float, float]],
                       anchors: List[Tuple[int, float, float]],
                       duration: float) -> List[Dict]:
    """锚点分段 + 段内按乐句铺开。没有锚点就退回纯乐句对齐。"""
    if not anchors:
        return align_to_phrases(items, phrases, duration)

    out: List[Dict] = []
    # 用锚点把歌词切成若干区间：[上一锚点行, 本锚点行)
    bounds = [(-1, 0.0)] + [(idx, st) for idx, st, _ in anchors] + [(len(items), duration)]
    for k in range(len(bounds) - 1):
        i0, t0 = bounds[k]
        i1, t1 = bounds[k + 1]
        chunk = items[max(0, i0):i1] if k == 0 else items[i0:i1]
        if not chunk:
            continue
        # 该区间可用的乐句
        inner = [p for p in phrases if p[0] >= t0 - 0.05 and p[1] <= t1 + 0.05]
        seg = align_to_phrases(chunk, inner, max(0.5, t1 - t0))
        if not inner:
            # 没有乐句就在区间内均分（而不是 align_to_phrases 的整首均分）
            step = (t1 - t0) / len(chunk)
            seg = [{"start": round(t0 + n * step, 2), "end": round(t0 + (n + 1) * step, 2),
                    "text": c["text"], "section": c.get("section") or "verse"}
                   for n, c in enumerate(chunk)]
        # 被锚住的那一行必须真的落在锚点时刻上。区间内第一个乐句未必正好从锚点开始
        # （检测到的乐句可能晚几秒），不钉的话锚点就白找了。
        if seg and k > 0:
            seg[0]["start"] = round(t0, 2)
            if seg[0]["end"] <= seg[0]["start"]:
                seg[0]["end"] = round(t0 + 1.0, 2)
        out.extend(seg)

    out.sort(key=lambda x: x["start"])
    # 消重叠：锚点区间边界处偶尔会挨着
    for i in range(len(out) - 1):
        if out[i]["end"] > out[i + 1]["start"]:
            out[i]["end"] = round(max(out[i]["start"] + 0.2, out[i + 1]["start"]), 2)
    return out


def align_to_phrases(items: List[Dict], phrases: List[Tuple[float, float]],
                     duration: float) -> List[Dict]:
    """把 M 行歌词对到 N 个人声段落上。

    三种情况：
      M == N  一对一，最理想（所以上游会按 M 去挑段落粒度）
      M <  N  段落比行多 → 合并相邻段落
      M >  N  行比段落多 → 在段落内部按字数比例再切

    对不齐时宁可均摊也不丢词：少一行都会让后面全体错位。
    """
    items = [x for x in (items or []) if (x.get("text") or "").strip()]
    if not items:
        return []

    def mk(a: float, b: float, it: Dict) -> Dict:
        return {"start": round(a, 2), "end": round(b, 2),
                "text": it["text"], "section": it.get("section") or "verse"}

    if not phrases:
        # 一个人声段都没检出来（纯器乐？分离失败？）→ 整首均分，让用户在校对界面自己拖
        step = duration / len(items)
        return [mk(i * step, (i + 1) * step, it) for i, it in enumerate(items)]

    m, n = len(items), len(phrases)
    out: List[Dict] = []

    if m == n:
        return [mk(a, b, it) for it, (a, b) in zip(items, phrases)]

    if m < n:
        # 合并：把 n 个段落按顺序切成 m 组，每组首尾即该行的时间
        per = n / m
        for i, it in enumerate(items):
            lo = phrases[int(i * per)][0]
            hi = phrases[min(n - 1, int((i + 1) * per) - 1)][1]
            if hi <= lo:
                hi = phrases[min(n - 1, int(i * per))][1]
            out.append(mk(lo, hi, it))
        return out

    # m > n：按行分配到段落，段内再按字数比例切
    per = m / n
    idx = 0
    for pi, (a, b) in enumerate(phrases):
        take = items[idx:int(round((pi + 1) * per))] if pi < n - 1 else items[idx:]
        if not take:
            continue
        idx += len(take)
        total_chars = sum(max(1, len(x["text"])) for x in take)
        cur = a
        for it in take:
            share = (b - a) * max(1, len(it["text"])) / total_chars
            out.append(mk(cur, min(b, cur + share), it))
            cur += share
    # 分配过程中若有剩余（四舍五入导致），补到最后一段之后
    for it in items[idx:]:
        last = out[-1]["end"] if out else 0.0
        out.append(mk(last, min(duration, last + 2.0), it))
    return out


def import_lyrics(content: str, fmt: str, duration: float,
                  phrases: Optional[List[Tuple[float, float]]] = None,
                  asr_lines: Optional[List[Dict]] = None
                  ) -> Tuple[List[Dict], List[Dict]]:
    """统一入口。返回 (歌词行, 段落表)。

    fmt='lrc'  走时间标签（LRC 通常没有段落标记，段落表会是空的，仍由 LLM 分析）
    fmt='text' 走人声乐句对齐，并把 [Verse]/[Chorus] 这类标记转成段落表
    """
    if fmt == "lrc":
        lines = parse_lrc(content, duration)
        if not lines:
            raise ValueError("这个 LRC 里没解析出带时间标签的歌词行，"
                             "确认一下是不是纯文本歌词（那样请选「纯文本」）。")
        return lines, []

    items = parse_structured_lyrics(content)
    if not items:
        raise ValueError("没解析出任何歌词行。")
    # 先拿 ASR 结果当锚点把歌词钉住，再在锚点之间按乐句铺开
    anchors = find_anchors(items, asr_lines or [])
    lines = align_with_anchors(items, phrases or [], anchors, duration)
    # 只有真的出现过段落标记才给段落表——全是 verse 说明没标记，那还不如让 LLM 去分析
    has_marks = len({x.get("section") for x in items}) > 1
    sections = derive_sections(lines, duration) if has_marks else []
    logger.info("[lyrics] 纯文本对齐：%d 行 ↔ %d 个乐句 / %d 个锚点，段落表 %d 段",
                len(items), len(phrases or []), len(anchors), len(sections))
    return lines, sections
