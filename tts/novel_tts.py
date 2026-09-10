#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
《碎月长歌：遗迹纪元》 —— 基于 edge-tts 的多角色有声小说合成脚本

功能
  1. 自动切分 Markdown 章节（支持 卷/章 两级标题）
  2. 自动识别对白说话人：前置标注 / 中置插入 / 后置代词回指 / 纯引号延续
  3. 每个角色绑定独立音色 + 语速/音调微调，旁白与对白自动切换
  4. 异步并发合成 + 失败重试 + 断点续传（已生成的分块自动跳过）
  5. 输出：每章一个 MP3（含章节标题片头）+ SRT 字幕 + 角色分配报告

安装依赖
  pip install edge-tts
  # 合并音频需要 ffmpeg（brew install ffmpeg / apt install ffmpeg）

常用命令
  python novel_tts.py --list-voices          # 列出所有可用中文音色
  python novel_tts.py --dry-run              # 只做解析与角色分配，不联网合成（强烈建议先跑）
  python novel_tts.py --chapters 1           # 只合成第一章（试音）
  python novel_tts.py                        # 合成全本
  python novel_tts.py --merge-all            # 额外合并出「全本.mp3」
"""

import argparse
import asyncio
import json
import os
import random
import re
import shutil
import subprocess
import sys
from collections import Counter, OrderedDict
from pathlib import Path

try:
    import edge_tts
except ImportError:
    sys.exit("缺少依赖，请先执行： pip install edge-tts")

# ==========================================================================
# 一、全局配置
# ==========================================================================

SRC_FILE = "碎月长歌：遗迹纪元.md"
OUT_DIR = "/home/khtao/音乐"

MAX_CHARS = 350         # 单次请求的字符上限，超长自动按句断句（建议 250~500）
CONCURRENCY = 4         # 并发数，太大容易被微软限流（建议 3~6）
RETRY = 3               # 失败重试次数
GAP_MS = 320            # 段落之间插入的静音毫秒数（朗读呼吸感）

# ==========================================================================
# 二、音色分配表
#     voice : edge-tts 音色名
#     rate  : 语速  形如 "+10%" / "-5%"
#     pitch : 音调  形如 "+5Hz" / "-8Hz"（必须带 Hz）
#     vol   : 音量  形如 "+0%" / "+20%"
#     gender: 用于「他/她」代词回指消歧
#
#     说明：edge-tts 免费中文音色约 20 个，角色多于音色时，
#           通过「跨卷复用音色 + 语速音调拉开差异」来区分，
#           并确保同一场景内的角色不使用同一音色。
# ==========================================================================

NARRATOR = {
    "voice": "zh-CN-XiaoxiaoNeural", "rate": "+0%", "pitch": "+0Hz", "vol": "+0%",
    "gender": "f", "desc": "主旁白·晓晓，温暖耐听，独占 Xiaoxiao 自然档",
}

# 序章为创世史诗，改用成熟男声（云扬压低）
NARRATOR_EPIC = {
    "voice": "zh-CN-YunyangNeural", "rate": "-12%", "pitch": "-8Hz", "vol": "+5%",
    "gender": "m", "desc": "序章旁白·史诗感，与泽特(Yunxi)不同基底",
}

# ------------------------------------------------------------------------
# 【重要】微软已下线大批中文音色，当前 edge-tts 实测可用仅 6 个：
#     女声：XiaoxiaoNeural(温暖) / XiaoyiNeural(活泼)
#     男声：YunxiNeural(阳光少年) / YunxiaNeural(活泼少年)
#           YunyangNeural(播音腔) / YunjianNeural(成熟浑厚)
#   （若 --list-voices 中仍有 liaoning-XiaobeiNeural / shaanxi-XiaoniNeural，
#    可给阿拉克妮雅等配角启用，见文件末尾「可选方言音色」注释）
#
# 24 个角色 > 6 个音色，因此采用「同基底 + rate/pitch/vol 强变体」区分：
#   · 同章节内绝不出现两个同基底且参数相近的角色
#   · pitch 拉开到 ±20Hz 以上即可明显听辨；超过 ±35Hz 会有金属音，勿再加大
# ------------------------------------------------------------------------

CHARACTERS = OrderedDict([
    # ============ 女声：Xiaoxiao（清冷/空灵/年长） ============
    ("莉莱", {
        "voice": "zh-CN-XiaoxiaoNeural", "rate": "-12%", "pitch": "+14Hz", "vol": "+0%",
        "gender": "f", "desc": "水晶室女——清冷柔软，语速偏慢",
    }),
    ("司里希丝", {
        "voice": "zh-CN-XiaoxiaoNeural", "rate": "-6%", "pitch": "-14Hz", "vol": "+0%",
        "gender": "f", "desc": "深海娜迦——柔和低沉、水汽感",
    }),
    ("丝奎奥克", {
        "voice": "zh-CN-XiaoxiaoNeural", "rate": "-22%", "pitch": "+26Hz", "vol": "+5%",
        "gender": "f", "desc": "古老女神——空灵非人，极慢极高",
    }),
    ("瑞莎莉", {
        "voice": "zh-CN-XiaoxiaoNeural", "rate": "-18%", "pitch": "-20Hz", "vol": "+0%",
        "gender": "f", "desc": "莉莱的导师——年长沉稳",
    }),
    ("无玄", {
        "voice": "zh-CN-XiaoxiaoNeural", "rate": "-20%", "pitch": "+8Hz", "vol": "+0%",
        "gender": "f", "desc": "太虚古神——中性化，冷眼旁观",
    }),

    # ============ 女声：Xiaoyi（清冽/阴柔/娇俏） ============
    ("仙德尔莎", {
        "voice": "zh-CN-XiaoyiNeural", "rate": "-2%", "pitch": "+12Hz", "vol": "+0%",
        "gender": "f", "desc": "天怒长公主/复仇之魂——清冽（与因佩莉娅 pitch 反向拉开 26Hz）",
    }),
    ("因佩莉娅", {
        "voice": "zh-CN-XiaoyiNeural", "rate": "+2%", "pitch": "-14Hz", "vol": "+0%",
        "gender": "f", "desc": "篡位二公主——阴柔压低，表面温柔内里锋利",
    }),
    ("维罗拉", {
        "voice": "zh-CN-XiaoyiNeural", "rate": "+10%", "pitch": "+28Hz", "vol": "+0%",
        "gender": "f", "desc": "破晓辰星——星界造物，明亮天真",
    }),
    ("阿拉克妮雅", {
        "voice": "zh-CN-XiaoyiNeural", "rate": "-10%", "pitch": "-6Hz", "vol": "+0%",
        "gender": "f", "desc": "育母蜘蛛——慵懒妖媚",
    }),
    ("侍女", {
        "voice": "zh-CN-XiaoyiNeural", "rate": "+8%", "pitch": "+20Hz", "vol": "+0%",
        "gender": "f", "desc": "群杂女声",
    }),

    # ============ 男声：Yunxi（少年基底，可塑性最强） ============
    ("扎贡纳斯", {
        "voice": "zh-CN-YunxiNeural", "rate": "-6%", "pitch": "-6Hz", "vol": "+0%",
        "gender": "m", "desc": "天怒法师——深情克制，全书情感主轴",
    }),
    ("尤涅弗", {
        "voice": "zh-CN-YunxiNeural", "rate": "+8%", "pitch": "+10Hz", "vol": "+0%",
        "gender": "m", "desc": "祈求者——学者腔，理性、语速略快带傲气",
    }),
    ("哈斯卡", {
        "voice": "zh-CN-YunxiNeural", "rate": "+16%", "pitch": "+18Hz", "vol": "+10%",
        "gender": "m", "desc": "血矛——狂热、有爆发力",
    }),
    ("斯拉克", {
        "voice": "zh-CN-YunxiNeural", "rate": "+12%", "pitch": "+14Hz", "vol": "+0%",
        "gender": "m", "desc": "鱼人夜行者——年轻狡黠",
    }),
    ("泽特", {
        "voice": "zh-CN-YunxiNeural", "rate": "-22%", "pitch": "-18Hz", "vol": "+0%",
        "gender": "m", "desc": "原初意识守护者——古老、无机质",
    }),
    ("士兵", {
        "voice": "zh-CN-YunxiNeural", "rate": "+12%", "pitch": "+22Hz", "vol": "+15%",
        "gender": "m", "desc": "群杂男声（远景/呐喊）",
    }),

    # ============ 男声：Yunyang（播音腔，成熟） ============
    ("斯温", {
        "voice": "zh-CN-YunyangNeural", "rate": "-14%", "pitch": "-16Hz", "vol": "+0%",
        "gender": "m", "desc": "流浪剑客——低沉寡言，字少而重",
    }),
    ("卡德尔", {
        "voice": "zh-CN-YunyangNeural", "rate": "-8%", "pitch": "-10Hz", "vol": "+0%",
        "gender": "m", "desc": "矮人狙击手——老练、沙哑感",
    }),
    ("伊扎洛", {
        "voice": "zh-CN-YunyangNeural", "rate": "-24%", "pitch": "-26Hz", "vol": "+0%",
        "gender": "m", "desc": "光之守卫——骑白马的衰朽老者，苍老沙哑",
    }),
    ("长老", {
        "voice": "zh-CN-YunyangNeural", "rate": "-18%", "pitch": "-20Hz", "vol": "+0%",
        "gender": "m", "desc": "群杂老者声",
    }),

    # ============ 男声：Yunjian（成熟浑厚，适合威压/阴森） ============
    ("托夫", {
        "voice": "zh-CN-YunjianNeural", "rate": "+16%", "pitch": "+10Hz", "vol": "+20%",
        "gender": "m", "desc": "巨牙海民——豪爽大嗓门，全书喜剧担当",
    }),
    ("玛尔斯", {
        "voice": "zh-CN-YunjianNeural", "rate": "-10%", "pitch": "-20Hz", "vol": "+10%",
        "gender": "m", "desc": "战神——威压感，语速慢而重",
    }),
    ("暗影恶魔", {
        "voice": "zh-CN-YunjianNeural", "rate": "-6%", "pitch": "-24Hz", "vol": "+0%",
        "gender": "m", "desc": "阴森非人（与玛尔斯差 4Hz 但语速相反，且不同卷登场）",
    }),
    ("诺提克", {
        "voice": "zh-CN-YunjianNeural", "rate": "-18%", "pitch": "-14Hz", "vol": "+0%",
        "gender": "m", "desc": "阴沉（与卡德尔不同基底，避免第八章撞音）",
    }),
    ("密探", {
        "voice": "zh-CN-YunjianNeural", "rate": "-8%", "pitch": "-6Hz", "vol": "+0%",
        "gender": "m", "desc": "群杂·无感情转述",
    }),

    # ============ 男声：Yunxia（少年音，专供小个子/机灵型） ============
    ("鲍什", {
        "voice": "zh-CN-YunxiaNeural", "rate": "+18%", "pitch": "+24Hz", "vol": "+0%",
        "gender": "m", "desc": "修补匠——话密机灵，少年音最贴合其形象",
    }),
    ("混沌骑士", {
        "voice": "zh-CN-YunxiaNeural", "rate": "-16%", "pitch": "-30Hz", "vol": "+5%",
        "gender": "m", "desc": "混沌骑士——极致压低的非人感（与鲍什同基底但走向两极）",
    }),
])

# 代词回指兜底：当「他/她说」无法从上下文定位时，用该章视角角色（POV）
# 键为章节标题的模糊匹配片段
CHAPTER_POV = {
    "殇月之坠":     {"m": "泽特",      "f": "丝奎奥克"},
    "荆棘王座":     {"m": "扎贡纳斯",  "f": "仙德尔莎"},
    "未说出口的名字": {"m": "扎贡纳斯", "f": "因佩莉娅"},
    "霜与铁的初遇":  {"m": "斯温",      "f": "莉莱"},
    "雾隐的断章":   {"m": "尤涅弗",    "f": "阿拉克妮雅"},
    "遗忘的晨星":   {"m": "玛尔斯",    "f": "维罗拉"},
    "深海的挽歌":   {"m": "斯拉克",    "f": "司里希丝"},
    "血矛的叛途":   {"m": "哈斯卡",    "f": "司里希丝"},
    "基恩的闹剧":   {"m": "鲍什",      "f": "司里希丝"},
    "光暗的千年之约": {"m": "泽特",     "f": "仙德尔莎"},
    "荆棘巢穴的真相": {"m": "扎贡纳斯", "f": "仙德尔莎"},
    "群星归位":     {"m": "扎贡纳斯",  "f": "仙德尔莎"},
}

# 说话人别名（只收录不会歧义的强指向词）
ALIASES = {
    "先公主": "仙德尔莎",
    "复仇之魂": "仙德尔莎",
    "小女孩": "仙德尔莎",
    "恶魔": "暗影恶魔",
    "大块头": "托夫",
    "巨牙海民": "托夫",
    "那声音": "丝奎奥克",
    "女神": "丝奎奥克",
    "破晓辰星": "维罗拉",
    "老头": "伊扎洛",
    "老者": "伊扎洛",
    "光之守卫": "伊扎洛",
}

# ==========================================================================
# 三、文本解析
# ==========================================================================

NAME_ALT = "|".join(
    re.escape(n) for n in sorted(
        list(CHARACTERS.keys()) + list(ALIASES.keys()),
        key=len, reverse=True
    )
)

SPEAK_VERB = (
    r"(?:说|道|问|答|喊|叫|喝|吼|低语|呢喃|喃喃|轻语|沉声|冷笑|苦笑|微笑|"
    r"转述|开口|出声|叹息|嘟囔|嘀咕|解释|命令|宣布|质问|反问|接话|打断|"
    r"惊呼|自语|嘟哝|嚷|应道|回道|念道|应|念|嚷道|自言自语)"
)

QUOTE_RE = re.compile(r"[“\"]([^“\"\n]{1,400})[”\"]")
# 前置： 「仙德尔莎对他说：」 / 「扎贡纳斯焦急地让她离开：」
PRE_RE = re.compile(
    rf"(?P<name>{NAME_ALT})[^“\"：:.。！？]{{0,30}}?{SPEAK_VERB}\s*[:：]\s*$")
# 后置（具名+动词）： 「仙德尔莎盯着...，认真地说」 / 「鲍什坐在坑边，...，对基恩人说」
POST_NAME_RE = re.compile(
    rf"^\s*[，,、。]?\s*(?P<name>{NAME_ALT})[^“\"：:。！？]{{0,45}}?{SPEAK_VERB}")
POST_PRON_RE = re.compile(
    rf"^\s*[，,、。]?\s*(?P<p>[她他它])[^“\"：:。！？]{{0,30}}?{SPEAK_VERB}")
# 后置（具名，紧贴）： 「姐姐，」因佩莉娅的声音很轻…… / 「你看，」恶魔对因佩莉娅说
POST_LOOSE_RE = re.compile(rf"^\s*[，,、。]?\s*(?P<name>{NAME_ALT})")
# 后置（代词）： 「他最终说」 / 「死人不觉得疼。」他说
POST_PRON_RE = re.compile(rf"^\s*[，,、。]?\s*(?P<p>[她他它])[^“\"：:]{{0,20}}?{SPEAK_VERB}")
# 跨段落引导语：段落以「…问道：」结尾但引号在下一段
PENDING_RE = re.compile(rf"(?P<name>{NAME_ALT}|[她他它])[^“\"：:]{{0,28}}?[:：]\s*$")
# 引导语结尾（未必带冒号，如「她顿了顿」）
LEAD_RE = re.compile(rf"(?P<name>{NAME_ALT})[^“\"：:]{{0,12}}?{SPEAK_VERB}\s*$")


def clean_markdown(text: str) -> str:
    """清理 Markdown 标记，保留可朗读的纯文本。"""
    text = re.sub(r"^---+$", "", text, flags=re.M)          # 分隔线
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.M)      # 标题符号
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)            # 粗体
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", text)  # 斜体
    text = re.sub(r"`([^`]*)`", r"\1", text)                # 行内代码
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)        # 图片
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)    # 链接
    text = re.sub(r"^\s*>\s?", "", text, flags=re.M)        # 引用
    text = re.sub(r"（第[一二三四五六七八九十]+章\s*完）", "", text)
    text = text.replace("……", "，").replace("—", "，")
    text = re.sub(r"[ \t]+", "", text)
    return text.strip()


def split_chapters(md: str):
    """按 ## / ### 标题切章；纯卷标题（无正文）自动并入后续章节归属。"""
    lines = md.split("\n")
    chapters, cur = [], None
    for ln in lines:
        m = re.match(r"^(#{2,3})\s+(.+?)\s*$", ln)
        if m:
            if cur and cur["body"].strip():
                chapters.append(cur)
            cur = {"title": m.group(2).strip(), "level": len(m.group(1)), "body": ""}
        elif cur is not None:
            cur["body"] += ln + "\n"
    if cur and cur["body"].strip():
        chapters.append(cur)
    # 过滤掉没有任何正文的空标题（如「第一卷 折翼与荆棘」）
    return [c for c in chapters if clean_markdown(c["body"])]


def _pov_for(title: str):
    for key, pov in CHAPTER_POV.items():
        if key in title:
            return pov
    return {"m": "扎贡纳斯", "f": "仙德尔莎"}


def _update_recent(paragraph: str, state):
    """扫描段落中「引号之外」的角色名，维护最近提及队列（用于代词回指与对话交替）。"""
    stripped = QUOTE_RE.sub(lambda m: " " * (m.end() - m.start()), paragraph)
    for m in re.finditer(rf"(?P<name>{NAME_ALT})", stripped):
        nm = ALIASES.get(m.group("name"), m.group("name"))
        if nm not in CHARACTERS:
            continue
        if nm in state["recent"]:
            state["recent"].remove(nm)
        state["recent"].append(nm)
        if len(state["recent"]) > 8:
            state["recent"].pop(0)
        g = CHARACTERS[nm]["gender"]
        state["last_f" if g == "f" else "last_m"] = nm


def _is_emphasis(text: str) -> bool:
    """判断是否为「强调性引号」而非对白（如 "危险"、"腾"）。"""
    if len(text) > 6:
        return False
    return not re.search(r"[。！？，、；]", text)


def _resolve_pronoun(p: str, state, pov) -> str:
    if p == "她":
        return state.get("last_f") or pov["f"]
    if p == "他":
        return state.get("last_m") or pov["m"]
    return state.get("last_m") or pov["m"]


# 群杂角色：不会被「对话交替」兜底选中，只有被显式标注时才使用
EXTRAS = {"士兵", "侍女", "长老", "密探"}


def _fallback_speaker(state, pov) -> str:
    """
    兜底说话人：优先在「最近实际说过话的角色」中做对话交替，
    其次在「最近提及角色」中交替，最后用章节视角角色。
    群杂角色不参与兜底，避免旁白里顺带提到的「士兵」被误当说话人。
    """
    prev = state.get("prev_speaker")
    for pool in (state.get("spk", []), state["recent"]):
        if prev:
            for r in reversed(pool):
                if r != prev and r not in EXTRAS:
                    return r
            if prev not in EXTRAS:
                return prev
        else:
            for r in reversed(pool):
                if r not in EXTRAS:
                    return r
    return pov["m"]


def split_segments(paragraph: str, pov, state):
    """
    把一个段落拆成 [(speaker, text), ...]
    speaker 为角色名，或 None 表示旁白。
    """
    _update_recent(paragraph, state)

    quotes = [q for q in QUOTE_RE.finditer(paragraph) if not _is_emphasis(q.group(1))]
    if not quotes:
        # 无对白：可能是对白的引导语（「她问道：」），记录 pending 供下一段使用
        if paragraph.strip():
            pm = PENDING_RE.search(paragraph)
            state["pending"] = pm.group("name") if pm else None
            segs = [(None, paragraph)]
            state["prev_speaker"] = None
            return segs
        return []

    segs, idx = [], 0
    for qi, q in enumerate(quotes):
        pre = paragraph[idx:q.start()]
        if pre.strip():
            segs.append((None, pre))

        # ---------- 判定说话人 ----------
        speaker = None
        before = pre[-28:] if pre else ""

        # 1) 跨段落引导语（「她问道：」在上一段，引号在本段开头）
        if speaker is None and qi == 0 and state.get("pending"):
            pd = state["pending"]
            speaker = _resolve_pronoun(pd, state, pov) if pd in "她他它" and len(pd) == 1 \
                else ALIASES.get(pd, pd)
            state["pending"] = None

        # 2) 引号前置具名： 仙德尔莎…说：
        if speaker is None:
            pm = PRE_RE.search(before)
            if pm:
                speaker = ALIASES.get(pm.group("name"), pm.group("name"))

        # 3) 引号前置代词： 她…问道：
        if speaker is None:
            pm = re.search(rf"(?P<p>[她他它])[^“\"：:]{{0,20}}?{SPEAK_VERB}\s*[:：]\s*$", before)
            if pm:
                speaker = _resolve_pronoun(pm.group("p"), state, pov)

        # 4) 引号后线索
        if speaker is None:
            after = paragraph[q.end(): q.end() + 32]
            nm = POST_NAME_RE.match(after)
            if nm:
                speaker = ALIASES.get(nm.group("name"), nm.group("name"))
            else:
                pr = POST_PRON_RE.match(after)
                if pr:
                    speaker = _resolve_pronoun(pr.group("p"), state, pov)
                else:
                    # 名字须紧贴引号（允许一个标点），如 「…」鲍什坐在坑边…
                    ls = POST_LOOSE_RE.match(after.lstrip("，,、。！？"))
                    if ls:
                        speaker = ALIASES.get(ls.group("name"), ls.group("name"))

        # 5) 兜底：对话交替 / 延续 / 视角角色
        if speaker is None:
            speaker = _fallback_speaker(state, pov)

        speaker = ALIASES.get(speaker, speaker)
        # 维护「最近实际说话人」队列，供对话交替兜底使用
        if speaker in CHARACTERS and speaker not in EXTRAS:
            if speaker in state["spk"]:
                state["spk"].remove(speaker)
            state["spk"].append(speaker)
            if len(state["spk"]) > 6:
                state["spk"].pop(0)

        # ---------- 合并中置插入语 ----------
        # 「A，」XX说，「B」  →  合并为同一说话人的连续对白
        body = q.group(1)
        nxt_q = None
        if qi + 1 < len(quotes):
            nxt = quotes[qi + 1]
            mid = paragraph[q.end(): nxt.start()]
            if (mid and len(mid) < 90
                    and not re.search(r"[。！？；]", mid)
                    and re.search(rf"{NAME_ALT}|[她他它]", mid)
                    and POST_NAME_RE.match(mid.lstrip("，,、。") or "x") is None):
                body = body.rstrip("，, ") + "，" + mid.strip().strip("，,、") + "，" + nxt.group(1)
                nxt_q = nxt

        segs.append((speaker, body))
        state["prev_speaker"] = speaker
        idx = nxt_q.end() if nxt_q else q.end()
        if nxt_q:
            quotes = [x for x in quotes if x is not nxt_q]

    tail = paragraph[idx:]
    if tail.strip():
        # 尾部若是「XX说。」这类提示语，算旁白
        if not re.match(rf"^\s*[，,、。]?\s*(?:{NAME_ALT})?[^“\"]{{0,20}}?{SPEAK_VERB}\s*[。.]?\s*$", tail):
            segs.append((None, tail))
    return segs


def build_chapter_segments(chapter, narrator):
    """产出整章的 [(speaker, text), ...]，含章节标题片头。"""
    title = chapter["title"]
    body = clean_markdown(chapter["body"])
    pov = _pov_for(title)
    state = {"last_m": pov["m"], "last_f": pov["f"], "prev_speaker": None,
             "recent": [], "spk": [], "pending": None}

    # 章节标题片头：用旁白音色读「第一章 荆棘王座」
    segs = [(None, f"{title}。")]
    for para in body.split("\n"):
        para = para.strip()
        if not para:
            continue
        for sp, txt in split_segments(para, pov, state):
            txt = txt.strip()
            if not txt:
                continue
            segs.append((ALIASES.get(sp, sp) if sp else None, txt))
    return segs, pov


# ==========================================================================
# 四、分块（控制单次请求长度）
# ==========================================================================

SENT_END = re.compile(r"(?<=[。！？…；!?])")
SOFT_END = re.compile(r"(?<=[，,、：:])")


def chunk_text(text: str, limit: int = MAX_CHARS):
    """按句末标点把长文本切成 <=limit 的若干块，并补回标点。"""
    if len(text) <= limit:
        return [text] if text.strip() else []

    pieces, buf = [], ""
    for s in SENT_END.split(text):
        if not s:
            continue
        if len(buf) + len(s) <= limit:
            buf += s
        else:
            if buf:
                pieces.append(buf)
            if len(s) <= limit:
                buf = s
            else:
                # 单句仍然过长 → 退而按逗号切
                sub = ""
                for t in SOFT_END.split(s):
                    if len(sub) + len(t) <= limit:
                        sub += t
                    else:
                        if sub:
                            pieces.append(sub)
                        sub = t
                buf = sub
    if buf:
        pieces.append(buf)
    pieces = [p for p in pieces if p.strip()]

    # 兜底：若仍存在超长块（如整段无标点），按字数硬切
    hard = []
    for p in pieces:
        while len(p) > limit * 1.5:
            hard.append(p[:limit])
            p = p[limit:]
        if p:
            hard.append(p)
    return hard


# ==========================================================================
# 五、合成
# ==========================================================================

def spec_for(speaker: str, narrator: dict):
    """取某个说话人的音色参数。"""
    return CHARACTERS.get(speaker, narrator) if speaker else narrator


def sanitize(text: str) -> str:
    text = text.replace("“", "").replace("”", "").replace('"', "")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


async def synth_one(text, spec, out_mp3, sem, retry=RETRY):
    """合成单个分块，带并发控制与重试。"""
    async with sem:
        for attempt in range(retry):
            try:
                comm = edge_tts.Communicate(
                    text, spec["voice"],
                    rate=spec.get("rate", "+0%"),
                    volume=spec.get("vol", "+0%"),
                    pitch=spec.get("pitch", "+0Hz"),
                )
                with open(out_mp3, "wb") as f:
                    async for chunk in comm.stream():
                        if chunk["type"] == "audio":
                            f.write(chunk["data"])
                if os.path.getsize(out_mp3) > 1024:
                    return True
                raise RuntimeError("生成的音频过小")
            except Exception as e:
                if attempt == retry - 1:
                    print(f"    [失败] {out_mp3.name}: {e}", file=sys.stderr)
                    return False
                await asyncio.sleep(1.5 * (attempt + 1) + random.random())
        return False


def probe_duration(path: str) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, timeout=30)
        return float(out.stdout.strip() or 0)
    except Exception:
        return 0.0


def make_silence(path: str, ms: int):
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                    f"anullsrc=r=24000:cl=mono", "-t", f"{ms/1000:.3f}",
                    "-c:a", "libmp3lame", "-q:a", "4", path],
                   capture_output=True)


def fmt_srt_time(sec: float) -> str:
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def concat_parts(part_files, out_mp3):
    """用 ffmpeg 拼接分块为整章音频。"""
    lst = out_mp3 + ".list.txt"
    with open(lst, "w", encoding="utf-8") as f:
        for p in part_files:
            f.write(f"file '{p}'\n")
    r = subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst,
         "-ar", "24000", "-ac", "1", "-c:a", "libmp3lame", "-q:a", "2", out_mp3],
        capture_output=True)
    os.path.exists(lst) and os.remove(lst)
    return r.returncode == 0


# ==========================================================================
# 六、主流程
# ==========================================================================

async def process_chapter(ch, order, args, out_dir: Path):
    title = ch["title"]
    safe = re.sub(r"[^\w\u4e00-\u9fa5]+", "_", title)
    name = f"{order:02d}_{safe}"
    ch_dir = out_dir / "parts" / name
    ch_dir.mkdir(parents=True, exist_ok=True)

    narrator = NARRATOR_EPIC if "序章" in title else NARRATOR
    segs, pov = build_chapter_segments(ch, narrator)

    # 展开为分块任务
    tasks = []
    for i, (sp, txt) in enumerate(segs):
        for j, piece in enumerate(chunk_text(sanitize(txt))):
            if piece:
                tasks.append((sp, piece))

    print(f"\n▶ [{order:02d}] {title}  |  分块 {len(tasks)} 个")

    sem = asyncio.Semaphore(args.concurrency)
    jobs = []
    for k, (sp, piece) in enumerate(tasks):
        mp3 = ch_dir / f"{k:05d}.mp3"
        if mp3.exists() and mp3.stat().st_size > 1024 and not args.force:
            continue
        spec = spec_for(sp, narrator)
        jobs.append(synth_one(piece, spec, mp3, sem))

    if jobs:
        results = await asyncio.gather(*jobs)
        ok = sum(1 for r in results if r)
        print(f"    合成 {ok}/{len(jobs)} 个新分块" + (f"（失败 {len(jobs)-ok}）" if ok < len(jobs) else ""))
    else:
        print("    全部命中缓存，跳过")

    part_files = sorted(ch_dir.glob("*.mp3"))
    if not part_files:
        print("    [跳过] 无音频分块")
        return None

    # 穿插静音后拼接
    silent = out_dir / "parts" / "_silence.mp3"
    if not silent.exists():
        make_silence(str(silent), GAP_MS)
    seq = []
    for i, p in enumerate(part_files):
        seq.append(str(p))
        if i < len(part_files) - 1:
            seq.append(str(silent))

    out_mp3 = out_dir / f"{name}.mp3"
    if concat_parts(seq, str(out_mp3)):
        dur = probe_duration(str(out_mp3))
        print(f"    ✔ {out_mp3.name}  ({dur/60:.1f} 分钟)")
    else:
        print("    [错误] 拼接失败")
        return None

    # 字幕
    if not args.no_srt:
        srt_dir = out_dir / "srt"
        srt_dir.mkdir(exist_ok=True)
        lines, t = [], 0.0
        for i, (sp, piece) in enumerate(tasks):
            d = probe_duration(str(ch_dir / f"{i:05d}.mp3"))
            if d <= 0:
                continue
            lines.append(f"{len(lines)+1}\n{fmt_srt_time(t)} --> {fmt_srt_time(t+d)}\n"
                         f"{'' if sp is None else sp+'：'}{piece}\n")
            t += d + GAP_MS / 1000
        (srt_dir / f"{name}.srt").write_text("\n".join(lines), encoding="utf-8")

    # 角色统计
    stat = Counter()
    for sp, piece in tasks:
        stat[sp or "（旁白）"] += len(piece)
    return {"order": order, "title": title, "file": out_mp3.name,
            "chars": sum(stat.values()), "cast": dict(stat.most_common())}


# 兜底音色池：若微软再次下线音色，从下列候选中按性别自动补位。
# 顺序即优先级，均为目前长期稳定的音色。
FALLBACK_POOL = {
    "f": ["zh-CN-XiaoxiaoNeural", "zh-CN-XiaoyiNeural",
          "zh-CN-liaoning-XiaobeiNeural", "zh-CN-shaanxi-XiaoniNeural",
          "zh-TW-HsiaoChenNeural", "zh-HK-HiuMaanNeural"],
    "m": ["zh-CN-YunxiNeural", "zh-CN-YunyangNeural", "zh-CN-YunjianNeural",
          "zh-CN-YunxiaNeural", "zh-TW-YunJheNeural", "zh-HK-WanLungNeural"],
}


async def repair_voices():
    """
    联网校验音色；把本机不可用的音色自动替换为同性别的可用音色，
    并在同性别内轮转分配，避免所有角色挤到同一个音色上。
    角色的 rate/pitch/vol 微调保持不变，听感差异仍然保留。
    """
    targets = [("<主旁白>", NARRATOR, "f"), ("<序章旁白>", NARRATOR_EPIC, "m")]
    targets += [(n, c, c.get("gender", "m")) for n, c in CHARACTERS.items()]

    try:
        vs = await edge_tts.list_voices()
    except Exception:
        print("  （无法联网校验音色，跳过；若合成报 NoAudioReceived 请检查音色名）")
        return 0

    avail = {v["ShortName"] for v in vs if v["Locale"].startswith("zh")}
    zh = [v for v in vs if v["Locale"].startswith("zh")]

    # 按性别收集真实可用音色：标准普通话优先，方言/其他华语区在后
    pool = {"f": [], "m": []}
    for v in zh:
        g = "f" if v["Gender"] == "Female" else "m"
        pool[g].append(v["ShortName"])

    def sort_key(x):
        std = 0 if re.match(r"^zh-CN-[A-Za-z]+Neural$", x) else 1
        return (std, x)

    for g in pool:
        pool[g] = sorted(set(pool[g]), key=sort_key) or [
            c for c in FALLBACK_POOL[g] if c in avail]

    bad = [(n, c, g) for n, c, g in targets if c["voice"] not in avail]
    if not bad:
        print(f"  ✔ 全部 {len(targets)} 个音色校验通过")
        return 0

    print(f"\n⚠ 检测到 {len(bad)} 个音色在本机不可用，自动替换：")
    cnt = {"f": 0, "m": 0}
    for name, cfg, g in bad:
        pl = pool.get(g) or pool["m"] or pool["f"]
        if not pl:
            continue
        new_v = pl[cnt[g] % len(pl)]
        cnt[g] += 1
        print(f"   {name:<10s} {cfg['voice']:<28s} → {new_v}")
        cfg["voice"] = new_v
    print(f"   （微调参数保持不变，角色间仍可凭 rate/pitch 区分）")
    return len(bad)


async def run(args):
    md = Path(args.input).read_text(encoding="utf-8")
    chapters = split_chapters(md)
    print(f"共识别到 {len(chapters)} 个章节：")
    for i, c in enumerate(chapters, 1):
        print(f"  {i:2d}. {c['title']}")

    # 保留原始章节序号，便于与全书编号对齐
    indexed = list(enumerate(chapters, 1))
    if args.chapters:
        want = {int(x) for x in args.chapters.split(",") if x.strip().isdigit()}
        indexed = [x for x in indexed if x[0] in want]

    out_dir = Path(args.outdir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 无论是否 dry-run 都先校验，保证预览里显示的是修复后的真实音色
    print("\n校验音色可用性 ...")
    await repair_voices()

    # ---------- Dry-run：只解析不合成 ----------
    if args.dry_run:
        print("\n" + "=" * 72)
        print("DRY-RUN 角色分配预览（不联网、不合成）")
        print("=" * 72)
        total = Counter()
        for i, ch in indexed:
            narrator = NARRATOR_EPIC if "序章" in ch["title"] else NARRATOR
            segs, pov = build_chapter_segments(ch, narrator)
            stat, blocks = Counter(), 0
            for sp, txt in segs:
                for piece in chunk_text(sanitize(txt)):
                    if piece:
                        blocks += 1
                        key = sp or "（旁白）"
                        stat[key] += len(piece)
                        total[key] += len(piece)
            print(f"\n[{i:02d}] {ch['title']}   POV:男={pov['m']} 女={pov['f']}   分块 {blocks}")
            for k, v in stat.most_common(8):
                sp = CHARACTERS.get(k, narrator) if k != "（旁白）" else narrator
                print(f"      {k:<10s} {v:>6d} 字  {sp['voice']}  {sp['rate']} {sp['pitch']}")
        print("\n" + "-" * 72)
        print("全书汇总：")
        for k, v in total.most_common():
            print(f"   {k:<10s} {v:>7d} 字")
        print(f"   {'合计':<10s} {sum(total.values()):>7d} 字")
        (out_dir / "casting_preview.json").write_text(
            json.dumps({"per_chapter": {}, "total": dict(total)}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(f"\n预览已保存：{out_dir/'casting_preview.json'}")
        return

    # ---------- 正式合成 ----------
    reports = []
    for i, ch in indexed:
        r = await process_chapter(ch, i, args, out_dir)
        if r:
            reports.append(r)

    if reports:
        rep = out_dir / "casting.json"
        rep.write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n角色分配报告：{rep}")

        if args.merge_all:
            lst = str(out_dir / "_all.list.txt")
            with open(lst, "w", encoding="utf-8") as f:
                for r in sorted(reports, key=lambda x: x["order"]):
                    f.write(f"file '{out_dir/r['file']}'\n")
            all_mp3 = out_dir / "全本.mp3"
            subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst,
                            "-c:a", "libmp3lame", "-q:a", "2", str(all_mp3)],
                           capture_output=True)
            print(f"全本已合并：{all_mp3}")


async def list_voices():
    try:
        vs = await edge_tts.list_voices()
    except Exception as e:
        print(f"无法获取音色列表（需要联网）：{e}")
        return
    print(f"{'音色名':<34s}{'性别':<6s}特点")
    print("-" * 78)
    for v in vs:
        if v["Locale"].startswith("zh-"):
            tag = v.get("VoiceTag", {})
            print(f"{v['ShortName']:<34s}{v['Gender']:<6s}{'/'.join(tag.get('VoicePersonalities', []) or [])}")


def main():
    ap = argparse.ArgumentParser(description="碎月长歌 · edge-tts 有声小说合成")
    ap.add_argument("--input", default=SRC_FILE, help="Markdown 小说路径")
    ap.add_argument("--outdir", default=OUT_DIR, help="输出目录")
    ap.add_argument("--chapters", help="只合成指定章节序号，如 1,2,3")
    ap.add_argument("--concurrency", type=int, default=CONCURRENCY, help="并发数")
    ap.add_argument("--dry-run", action="store_true", help="只解析与分配角色，不合成")
    ap.add_argument("--no-srt", action="store_true", help="不生成字幕")
    ap.add_argument("--merge-all", action="store_true", help="额外合并为全本.mp3")
    ap.add_argument("--force", action="store_true", help="忽略已有缓存，重新合成")
    ap.add_argument("--list-voices", action="store_true", help="列出可用中文音色")
    args = ap.parse_args()

    if args.list_voices:
        asyncio.run(list_voices())
    else:
        asyncio.run(run(args))


if __name__ == "__main__":
    main()


# ==========================================================================
# 附：可选方言音色（扩大女声区分度）
# --------------------------------------------------------------------------
# 女声只有 Xiaoxiao / Xiaoyi 两个基底，若你觉得女性角色区分度不够，
# 先运行 `python novel_tts.py --list-voices` 确认下面两个方言音色是否还在，
# 然后在上面 CHARACTERS 里手动替换（方言味不重，适合有地域色彩的角色）：
#
#   zh-CN-liaoning-XiaobeiNeural  女·辽宁口音  → 建议给 阿拉克妮雅（妖媚）或 托夫的搭档
#   zh-CN-shaanxi-XiaoniNeural    女·陕西口音  → 建议给 瑞莎莉（年长）
#   zh-HK-HiuMaanNeural           女·粤语      → 慎用，口音明显
#
# 例：把阿拉克妮雅改成辽宁口音，只需改一行 voice：
#   ("阿拉克妮雅", {"voice": "zh-CN-liaoning-XiaobeiNeural", "rate": "-10%", ...})
#
# 提示：pitch 超过 ±35Hz 会产生金属音/失真，宁可加大 rate 差距（±25%）也别硬拉 pitch。
# ==========================================================================
