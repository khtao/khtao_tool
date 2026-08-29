#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
universal_tracked_revision_v4.py

One-file tracked-change generator for LaTeX manuscripts.

Goals
-----
1. Compare an OLD .tex manuscript with a REVISED .tex manuscript.
2. Use semantic block alignment for prose / headings / equations / floats.
3. Tables are processed with a domain-independent structural cell/row/column engine:
   - balanced TeX parsing
   - structural multicolumn/multirow preservation
   - generic Unicode row/column similarity alignment
   - cell-level old-delete + new-add marking
   - preservation of old-only rows
   - new tables colored blue without wrapping control syntax
4. Figures:
   - NEVER emit \\deleted{\\includegraphics...}
   - an entirely deleted figure is omitted completely
   - if a matched figure changes its graphic file, keep only the revised graphic
     while tracking caption changes
5. Apply ulem/footnote/display-math compile-safety repairs.
6. Can run from command line or, with no paths supplied, via file-selection dialogs.

Examples
--------
python universal_tracked_revision_v4.py old.tex revised.tex -o tracked.tex
python universal_tracked_revision_v4.py

The no-argument form opens file pickers and writes
<revised_stem>_tracked.tex next to the revised file.
"""
from __future__ import annotations

from pathlib import Path
import argparse
import difflib
import math
import re
import sys
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import List, Optional, Tuple, Dict


# ---------------------------------------------------------------------------
# Generic TeX helpers
# ---------------------------------------------------------------------------

def find_balanced(s: str, start: int, open_ch: str = "{", close_ch: str = "}") -> int:
    if start >= len(s) or s[start] != open_ch:
        raise ValueError(f"position {start} does not point to {open_ch!r}")
    depth = 0
    i = start
    while i < len(s):
        if s[i] == "\\":
            i += 2
            continue
        if s[i] == open_ch:
            depth += 1
        elif s[i] == close_ch:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError("Unbalanced delimiters")


def balanced_arg(s: str, start_after_open: int) -> Tuple[str, int]:
    """start_after_open points immediately after an opening {."""
    end = find_balanced(s, start_after_open - 1)
    return s[start_after_open:end], end + 1


def command_arg_span(s: str, command: str, start: int = 0):
    m = re.search(r"\\" + re.escape(command) + r"(?:\[[^\]]*\])?\s*\{", s[start:])
    if not m:
        return None
    abs_start = start + m.start()
    brace = start + m.end() - 1
    end = find_balanced(s, brace)
    return abs_start, brace + 1, end, end + 1


def get_command_arg(s: str, command: str, occurrence: int = 0) -> Optional[str]:
    pos = 0
    found = None
    for _ in range(occurrence + 1):
        found = command_arg_span(s, command, pos)
        if not found:
            return None
        pos = found[3]
    return s[found[1]:found[2]]


def replace_command_arg(s: str, command: str, new_content: str, occurrence: int = 0) -> str:
    pos = 0
    found = None
    for _ in range(occurrence + 1):
        found = command_arg_span(s, command, pos)
        if not found:
            return s
        pos = found[3]
    _, b, c, _ = found
    return s[:b] + new_content + s[c:]


def strip_comments(s: str) -> str:
    out = []
    for line in s.splitlines():
        cut = None
        for i, ch in enumerate(line):
            if ch == "%" and (i == 0 or line[i - 1] != "\\"):
                cut = i
                break
        out.append(line if cut is None else line[:cut])
    return "\n".join(out)


def extract_env_blocks(txt: str, env: str):
    pat = rf"\\begin\{{{env}\*?\}}.*?\\end\{{{env}\*?\}}"
    return [(m.start(), m.end(), m.group(0)) for m in re.finditer(pat, txt, re.S)]


def word_diff(old_s: str, new_s: str) -> str:
    """Word/whitespace diff while keeping LaTeX control syntax outside markers."""
    if old_s == new_s:
        return new_s
    tok = lambda s: re.findall(r"\s+|\S+", s)
    a, b = tok(old_s), tok(new_s)
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    out = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        oa = "".join(a[i1:i2])
        nb = "".join(b[j1:j2])
        if tag == "equal":
            out.append(nb)
        elif tag == "delete":
            if oa:
                out.append(mark_latex_fragment(oa, "deleted"))
        elif tag == "insert":
            if nb:
                out.append(mark_latex_fragment(nb, "added"))
        else:
            if oa:
                out.append(mark_latex_fragment(oa, "deleted"))
            if nb:
                out.append(mark_latex_fragment(nb, "added"))
    return "".join(out)


# ---------------------------------------------------------------------------
# Generic table logic
# ---------------------------------------------------------------------------
# The table engine is domain-agnostic. Matching uses only LaTeX structure,
# Unicode-visible text,
# cell similarity, column similarity, row order, and one-to-one constraints.

TABLE_LIKE_ENVS = {
    "table", "table*", "tabular", "tabular*", "tabularx", "tabulary",
    "longtable", "tabu", "longtabu", "supertabular", "xtabular",
    "mpxtabular", "tblr", "longtblr", "NiceTabular", "NiceTabularX",
}

# Number of mandatory arguments belonging to the tabular-like environment
# before row content starts.  These are LaTeX/package syntax rules, not
# document-specific semantics.
TABULAR_MANDATORY_ARGS = {
    "tabular": 1,
    "tabular*": 2,
    "tabularx": 2,
    "tabulary": 2,
    "longtable": 1,
    "tabu": 1,
    "longtabu": 1,
    "supertabular": 1,
    "xtabular": 1,
    "mpxtabular": 1,
    "tblr": 1,
    "longtblr": 1,
    "NiceTabular": 1,
    "NiceTabularX": 2,
}

RULE_RE = re.compile(
    r"^(?P<prefix>(?:\s|%[^\n]*\n|"
    r"\\(?:toprule|midrule|bottomrule|hline|hdashline)\s*|"
    r"\\(?:cline|cmidrule|cdashline|Xcline)\s*(?:\([^)]*\))?\{[^}]*\}\s*|"
    r"\\(?:addlinespace|noalign)(?:\[[^\]]*\])?\s*|"
    r"\\specialrule\s*\{[^}]*\}\s*\{[^}]*\}\s*\{[^}]*\}\s*)*)",
    re.S,
)


def split_leading_rules(cell: str):
    m = RULE_RE.match(cell)
    prefix = m.group("prefix") if m else ""
    return prefix, cell[len(prefix):].strip()


def strip_rules(cell: str) -> str:
    return split_leading_rules(cell)[1]


def unwrap_structural(cell: str) -> str:
    """Return only the visible payload of multicolumn/multirow cells."""
    s = strip_rules(cell).strip()
    if s.startswith(r"\multicolumn"):
        spans = []
        i = s.find("{")
        while i >= 0 and len(spans) < 3:
            e = find_balanced(s, i)
            spans.append((i, e))
            i = s.find("{", e + 1)
        if len(spans) >= 3:
            a, b = spans[2]
            return s[a + 1:b]
    if s.startswith(r"\multirow"):
        spans = []
        i = s.find("{")
        while i >= 0:
            e = find_balanced(s, i)
            spans.append((i, e))
            i = s.find("{", e + 1)
        if spans:
            a, b = spans[-1]
            return s[a + 1:b]
    return s


def replace_structural_content(cell: str, content: str) -> str:
    """Replace only the visible payload while retaining structural arguments."""
    prefix, s = split_leading_rules(cell)
    st = s.strip()
    if st.startswith(r"\multicolumn"):
        spans = []
        i = st.find("{")
        while i >= 0 and len(spans) < 3:
            e = find_balanced(st, i)
            spans.append((i, e))
            i = st.find("{", e + 1)
        if len(spans) >= 3:
            a, b = spans[2]
            return prefix + st[:a + 1] + content + st[b:]
    if st.startswith(r"\multirow"):
        spans = []
        i = st.find("{")
        while i >= 0:
            e = find_balanced(st, i)
            spans.append((i, e))
            i = st.find("{", e + 1)
        if spans:
            a, b = spans[-1]
            return prefix + st[:a + 1] + content + st[b:]
    return prefix + content


def wrap_cell(cell: str, macro: str) -> str:
    prefix, core = split_leading_rules(cell)
    core = core.strip()
    if not core:
        return prefix + core
    if core.startswith(r"\multicolumn") or core.startswith(r"\multirow"):
        return replace_structural_content(
            prefix + core, mark_latex_fragment(unwrap_structural(core), macro)
        )
    return prefix + mark_latex_fragment(core, macro)


def _unicode_normalize_visible(text: str) -> str:
    """Language-independent normalization used only for matching."""
    text = unicodedata.normalize("NFKC", text).casefold()
    out = []
    for ch in text:
        cat = unicodedata.category(ch)
        if cat and cat[0] in ("L", "N"):
            out.append(ch)
        elif ch in "%+-−±×÷=<>≤≥./:_":
            out.append(ch)
        else:
            out.append(" ")
    return re.sub(r"\s+", " ", "".join(out)).strip()


def visible_cell_text(s: str) -> str:
    """Best-effort extraction of visible table text without domain assumptions."""
    s = strip_rules(s).strip()
    s = unwrap_structural(s)

    # Unwrap common text-formatting commands.  These are presentation syntax;
    # their argument is the visible content.
    prev = None
    while prev != s:
        prev = s
        s = re.sub(
            r"\\(?:textbf|textit|emph|textrm|textsf|texttt|textsc|textnormal|"
            r"textup|textsl|mathrm|mathbf|mathit|mathsf|mathtt|operatorname)"
            r"\{([^{}]*)\}",
            r"\1",
            s,
        )

    # Remove identifier-only commands and common table styling commands from
    # the matching representation.  The original source itself is untouched.
    s = re.sub(
        r"\\(?:label|ref|eqref|pageref|autoref|cref|Cref|cite\w*|tnote|footnotemark)"
        r"(?:\[[^\]]*\])?\{[^{}]*\}",
        " ",
        s,
    )
    s = re.sub(
        r"\\(?:rowcolor|cellcolor|arrayrulecolor)\s*(?:\[[^\]]*\])?\{[^{}]*\}",
        " ",
        s,
    )
    s = s.replace("~", " ").replace(r"\%", "%").replace(r"\&", "&")
    s = re.sub(r"\$([^$]*)\$", r"\1", s)
    s = (
        s.replace(r"\geq", "≥")
        .replace(r"\leq", "≤")
        .replace(r"\times", "×")
        .replace(r"\pm", "±")
    )
    # Drop remaining command names/options while leaving potential visible
    # brace payloads for matching.
    s = re.sub(r"\\[A-Za-z@]+\*?(?:\[[^\]]*\])?", " ", s)
    s = s.replace("{", " ").replace("}", " ")
    return re.sub(r"\s+", " ", s).strip()


def normalized_cell_text(s: str) -> str:
    return _unicode_normalize_visible(visible_cell_text(s))


def _text_tokens(text: str):
    text = _unicode_normalize_visible(text)
    return re.findall(r"[^\W_]+(?:[-'’][^\W_]+)*", text, flags=re.UNICODE)


def _char_ngrams(text: str, n: int = 2):
    compact = re.sub(r"\s+", "", _unicode_normalize_visible(text))
    if not compact:
        return set()
    if len(compact) <= n:
        return {compact}
    return {compact[i:i+n] for i in range(len(compact) - n + 1)}


def generic_text_similarity(a: str, b: str) -> float:
    """Generic Unicode text similarity; no aliases or domain vocabulary."""
    a = _unicode_normalize_visible(a)
    b = _unicode_normalize_visible(b)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0

    seq = difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()
    ta, tb = set(_text_tokens(a)), set(_text_tokens(b))
    tok = len(ta & tb) / len(ta | tb) if ta or tb else 0.0
    ga, gb = _char_ngrams(a), _char_ngrams(b)
    gram = (2.0 * len(ga & gb) / (len(ga) + len(gb))) if ga and gb else 0.0
    return 0.50 * seq + 0.25 * tok + 0.25 * gram


def mark_cell(new_cell: str, old_cell: Optional[str] = None,
              force_added: bool = False, force_deleted: bool = False) -> str:
    if force_added:
        return wrap_cell(new_cell, "added")
    if force_deleted:
        core = unwrap_structural(new_cell)
        return mark_latex_fragment(core, "deleted") if core.strip() else ""

    new_core = strip_rules(new_cell).strip()
    old_core = strip_rules(old_cell or "").strip()
    nv, ov = visible_cell_text(new_core), visible_cell_text(old_core)

    if normalized_cell_text(new_core) == normalized_cell_text(old_core):
        return new_cell
    if not ov and nv:
        return wrap_cell(new_cell, "added")
    if ov and not nv:
        prefix, _ = split_leading_rules(new_cell)
        return prefix + mark_latex_fragment(unwrap_structural(old_core), "deleted")

    combined = (
        mark_latex_fragment(unwrap_structural(old_core), "deleted") + r"\,"
        + mark_latex_fragment(unwrap_structural(new_core), "added")
    )
    if new_core.startswith(r"\multicolumn") or new_core.startswith(r"\multirow"):
        return replace_structural_content(new_cell, combined)
    prefix, _ = split_leading_rules(new_cell)
    return prefix + combined


def _consume_square_local(s: str, start: int) -> int:
    if start >= len(s) or s[start] != "[":
        return start
    depth = 1
    i = start + 1
    while i < len(s) and depth:
        if s[i] == "\\":
            i += 2
            continue
        if s[i] == "[":
            depth += 1
        elif s[i] == "]":
            depth -= 1
        i += 1
    return i


def _matching_env_end(block: str, env: str, begin_end: int) -> Optional[int]:
    pat = re.compile(r"\\(begin|end)\{" + re.escape(env) + r"\}")
    depth = 1
    for m in pat.finditer(block, begin_end):
        if m.group(1) == "begin":
            depth += 1
        else:
            depth -= 1
            if depth == 0:
                return m.start()
    return None


def extract_tabular_span(block: str):
    """Return row-content span for a supported tabular-like environment."""
    names = sorted((e for e in TABLE_LIKE_ENVS if not e.startswith("table")),
                   key=len, reverse=True)
    # `table` / `table*` are float shells, not row containers.
    names = [e for e in names if e not in {"table", "table*"}]
    pat = re.compile(r"\\begin\{(" + "|".join(re.escape(x) for x in names) + r")\}")
    m = pat.search(block)
    if not m:
        return None
    env = m.group(1)
    required = TABULAR_MANDATORY_ARGS.get(env, 1)
    pos = m.end()
    mand_seen = 0

    while pos < len(block) and mand_seen < required:
        while pos < len(block) and block[pos].isspace():
            pos += 1
        if pos < len(block) and block[pos] == "[":
            pos = _consume_square_local(block, pos)
            continue
        if pos < len(block) and block[pos] == "{":
            try:
                pos = find_balanced(block, pos) + 1
            except ValueError:
                return None
            mand_seen += 1
            continue
        return None

    end_marker = _matching_env_end(block, env, m.end())
    if end_marker is None or end_marker < pos:
        return None
    return pos, end_marker


def split_rows(content: str):
    """Split at top-level row terminators and retain each terminator verbatim."""
    rows = []
    cur = []
    i = 0
    depth = 0
    while i < len(content):
        ch = content[i]
        if ch == "\\":
            if depth == 0 and content.startswith(r"\tabularnewline", i):
                term = r"\tabularnewline"
                rows.append(("".join(cur), term))
                cur = []
                i += len(term)
                continue
            if i + 1 < len(content) and content[i + 1] == "\\" and depth == 0:
                j = i + 2
                if j < len(content) and content[j] == "*":
                    j += 1
                while j < len(content) and content[j] in " \t":
                    j += 1
                if j < len(content) and content[j] == "[":
                    j = _consume_square_local(content, j)
                term = content[i:j]
                rows.append(("".join(cur), term))
                cur = []
                i = j
                continue
            cur.append(ch)
            i += 1
            if i < len(content):
                cur.append(content[i])
                i += 1
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
        cur.append(ch)
        i += 1
    if "".join(cur).strip():
        rows.append(("".join(cur), ""))
    return rows


def split_cells(row: str):
    cells, cur = [], []
    depth, i = 0, 0
    while i < len(row):
        ch = row[i]
        if ch == "\\":
            cur.append(ch)
            i += 1
            if i < len(row):
                cur.append(row[i])
                i += 1
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
        if ch == "&" and depth == 0:
            cells.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    cells.append("".join(cur))
    return cells


def _control_signature(raw: str) -> str:
    names = re.findall(r"\\([A-Za-z@]+)\*?", strip_comments(raw))
    return " ".join(names)


def parsed_rows(block: str):
    sp = extract_tabular_span(block)
    if not sp:
        return []
    content = block[sp[0]:sp[1]]
    out = []
    for raw, term in split_rows(content):
        if not raw.strip() and not term:
            continue
        cells = split_cells(raw)
        visible = [visible_cell_text(c) for c in cells]
        # Captions/labels inside longtable-like environments are metadata, not
        # data cells. Caption text is diffed separately before row alignment.
        metadata_row = bool(re.match(r"^\s*\\(?:caption|label)\b", raw))
        out.append({
            "raw": raw,
            "cells": cells,
            "term": term,
            "structural_only": metadata_row or not any(x.strip() for x in visible),
            "control_signature": _control_signature(raw),
        })
    return out


def replace_tabular_content(block: str, new_content: str) -> str:
    sp = extract_tabular_span(block)
    if not sp:
        return block
    return block[:sp[0]] + new_content + block[sp[1]:]


def _column_values(rows, col: int):
    vals = []
    for r in rows:
        if r["structural_only"] or col >= len(r["cells"]):
            continue
        v = normalized_cell_text(r["cells"][col])
        if v:
            vals.append(v)
    return vals


def _best_cross_similarity(a_vals, b_vals, cap: int = 12) -> float:
    if not a_vals or not b_vals:
        return 0.0
    a_vals = a_vals[:cap]
    b_vals = b_vals[:cap]
    left = sum(max(generic_text_similarity(a, b) for b in b_vals) for a in a_vals) / len(a_vals)
    right = sum(max(generic_text_similarity(b, a) for a in a_vals) for b in b_vals) / len(b_vals)
    return 0.5 * (left + right)


def _column_similarity(old_rows, new_rows, oi: int, nj: int, old_n: int, new_n: int) -> float:
    ov = _column_values(old_rows, oi)
    nv = _column_values(new_rows, nj)
    if not ov and not nv:
        text_score = 0.5
        exact = 0.0
        first = 0.0
    elif not ov or not nv:
        text_score = exact = first = 0.0
    else:
        text_score = _best_cross_similarity(ov, nv)
        so, sn = set(ov), set(nv)
        exact = len(so & sn) / max(1, len(so | sn))
        first = generic_text_similarity(ov[0], nv[0])
    op = oi / max(1, old_n - 1)
    np = nj / max(1, new_n - 1)
    position = max(0.0, 1.0 - abs(op - np))
    return 0.45 * text_score + 0.20 * exact + 0.20 * first + 0.15 * position


def align_columns_generic(old_rows, new_rows):
    old_n = max((len(r["cells"]) for r in old_rows if not r["structural_only"]), default=0)
    new_n = max((len(r["cells"]) for r in new_rows if not r["structural_only"]), default=0)
    if not old_n or not new_n:
        return {}

    score = {
        (i, j): _column_similarity(old_rows, new_rows, i, j, old_n, new_n)
        for i in range(old_n) for j in range(new_n)
    }

    # Same-width tables are position-preserving by default.  Reordering is
    # accepted only when text evidence clearly beats the diagonal.
    if old_n == new_n:
        identity = {j: j for j in range(new_n)}
        diag_avg = sum(score[(j, j)] for j in range(new_n)) / new_n
        cands = sorted((sc, i, j) for (i, j), sc in score.items())
        used_o, used_n, greedy = set(), set(), {}
        for sc, i, j in reversed(cands):
            if i in used_o or j in used_n:
                continue
            greedy[j] = i
            used_o.add(i)
            used_n.add(j)
        greedy_avg = sum(score[(i, j)] for j, i in greedy.items()) / max(1, len(greedy))
        if len(greedy) == new_n and greedy_avg > diag_avg + 0.12:
            return greedy
        return identity

    # Different-width tables: assign strongest semantic columns first.
    cands = sorted((sc, i, j) for (i, j), sc in score.items())
    used_o, used_n, mapping = set(), set(), {}
    for sc, i, j in reversed(cands):
        if sc < 0.34 or i in used_o or j in used_n:
            continue
        mapping[j] = i
        used_o.add(i)
        used_n.add(j)

    # Weak textual evidence can still be resolved by relative position, but
    # only for remaining one-to-one candidates.
    for j in range(new_n):
        if j in used_n:
            continue
        candidates = [i for i in range(old_n) if i not in used_o]
        if not candidates:
            break
        target = j / max(1, new_n - 1)
        i = min(candidates, key=lambda x: abs(x / max(1, old_n - 1) - target))
        if score[(i, j)] >= 0.18:
            mapping[j] = i
            used_o.add(i)
            used_n.add(j)
    return mapping


def _row_aggregate(row) -> str:
    return " | ".join(normalized_cell_text(c) for c in row["cells"] if normalized_cell_text(c))


def _first_visible_cell(row) -> str:
    for c in row["cells"]:
        v = normalized_cell_text(c)
        if v:
            return v
    return ""


def _row_similarity(old_row, new_row, colmap, oi, ni, old_count, new_count) -> float:
    if old_row["structural_only"] or new_row["structural_only"]:
        if old_row["structural_only"] and new_row["structural_only"]:
            a, b = old_row["control_signature"], new_row["control_signature"]
            return 1.0 if a and a == b else generic_text_similarity(a, b)
        return 0.0

    sims = []
    exact = 0
    comparable = 0
    for nj, oldj in colmap.items():
        nc = new_row["cells"][nj] if nj < len(new_row["cells"]) else ""
        oc = old_row["cells"][oldj] if oldj < len(old_row["cells"]) else ""
        nv, ov = normalized_cell_text(nc), normalized_cell_text(oc)
        if not nv and not ov:
            continue
        comparable += 1
        sims.append(generic_text_similarity(ov, nv))
        if ov and ov == nv:
            exact += 1
    cell_score = sum(sims) / len(sims) if sims else 0.0
    exact_score = exact / comparable if comparable else 0.0
    aggregate = generic_text_similarity(_row_aggregate(old_row), _row_aggregate(new_row))
    anchor = generic_text_similarity(_first_visible_cell(old_row), _first_visible_cell(new_row))
    op = oi / max(1, old_count - 1)
    np = ni / max(1, new_count - 1)
    position = max(0.0, 1.0 - abs(op - np))
    width = min(len(old_row["cells"]), len(new_row["cells"])) / max(1, max(len(old_row["cells"]), len(new_row["cells"])))
    return 0.40 * cell_score + 0.20 * exact_score + 0.20 * aggregate + 0.10 * anchor + 0.05 * position + 0.05 * width


def align_rows_generic(old_rows, new_rows, colmap):
    n, m = len(old_rows), len(new_rows)
    if not n or not m:
        return {}

    scores = [[_row_similarity(old_rows[i], new_rows[j], colmap, i, j, n, m)
               for j in range(m)] for i in range(n)]
    dp = [[0.0] * (m + 1) for _ in range(n + 1)]
    act = [[0] * (m + 1) for _ in range(n + 1)]

    for i in range(1, n + 1):
        for j in range(1, m + 1):
            best, action = dp[i - 1][j], 1
            if dp[i][j - 1] > best + 1e-12:
                best, action = dp[i][j - 1], 2
            sc = scores[i - 1][j - 1]
            threshold = 0.58 if (old_rows[i-1]["structural_only"] or new_rows[j-1]["structural_only"]) else 0.30
            if sc >= threshold:
                val = dp[i - 1][j - 1] + (sc - threshold) + 0.03
                if val > best + 1e-12:
                    best, action = val, 3
            dp[i][j], act[i][j] = best, action

    pairs = []
    i, j = n, m
    while i > 0 and j > 0:
        a = act[i][j]
        if a == 3:
            pairs.append((i - 1, j - 1))
            i -= 1
            j -= 1
        elif a == 1:
            i -= 1
        else:
            j -= 1
    pairs.reverse()

    mapping = {nj: oi for oi, nj in pairs}

    # If two tables have the same number of visible rows but almost no lexical
    # overlap (e.g. every value was revised), positional pairing is less noisy
    # and still semantically represents row-by-row replacement.
    old_vis = [i for i, r in enumerate(old_rows) if not r["structural_only"]]
    new_vis = [j for j, r in enumerate(new_rows) if not r["structural_only"]]
    matched_vis = sum(1 for j, i in mapping.items()
                      if not old_rows[i]["structural_only"] and not new_rows[j]["structural_only"])
    if old_vis and len(old_vis) == len(new_vis) and matched_vis < len(new_vis) / 2:
        for oi, nj in zip(old_vis, new_vis):
            mapping[nj] = oi

    # Exact structural control rows in the revised source are paired only when
    # an identical control row exists; otherwise the revised structure wins.
    used_old = set(mapping.values())
    for nj, nr in enumerate(new_rows):
        if not nr["structural_only"] or nj in mapping:
            continue
        for oi, orow in enumerate(old_rows):
            if oi in used_old or not orow["structural_only"]:
                continue
            if nr["control_signature"] and nr["control_signature"] == orow["control_signature"]:
                mapping[nj] = oi
                used_old.add(oi)
                break
    return mapping


def _render_rows(rows_with_text) -> str:
    parts = ["\n"]
    for item in rows_with_text:
        text = item["text"].strip()
        if not text and not item.get("term"):
            continue
        parts.append("\t\t\t\t" + text)
        term = item.get("term", "")
        if term:
            parts.append(term)
        parts.append("\n")
    parts.append("\t\t\t")
    return "".join(parts)


def annotate_table_pair(new_block: str, old_block: str) -> str:
    """Generic structural table diff with no document-specific rules."""
    oc = get_command_arg(old_block, "caption") or ""
    nc = get_command_arg(new_block, "caption") or ""
    if nc:
        new_block = replace_command_arg(new_block, "caption", word_diff(oc, nc))

    nr = parsed_rows(new_block)
    orows = parsed_rows(old_block)
    if not nr or not orows:
        # Revised table structure is authoritative.  If a row container cannot
        # be parsed, keep it intact rather than applying unsafe source-level diff.
        return new_block

    colmap = align_columns_generic(orows, nr)
    rowmap = align_rows_generic(orows, nr, colmap)

    rendered = []
    for ni, r in enumerate(nr):
        if r["structural_only"]:
            rendered.append({"text": r["raw"], "term": r["term"]})
            continue

        oi = rowmap.get(ni)
        ocells = orows[oi]["cells"] if oi is not None and not orows[oi]["structural_only"] else []
        outcells = []
        for j, ncell in enumerate(r["cells"]):
            oldj = colmap.get(j)
            ocell = ocells[oldj] if oldj is not None and oldj < len(ocells) else None
            outcells.append(mark_cell(ncell, ocell))
        rendered.append({"text": " & ".join(outcells), "term": r["term"] or r"\\"})

    # Preserve old-only VISIBLE rows.  Old-only structural control rows are not
    # inserted because the revised table structure must remain authoritative.
    used_old = set(rowmap.values())
    old_only = [i for i, r in enumerate(orows) if i not in used_old and not r["structural_only"]]
    if old_only:
        insertions = {}
        mapped_pairs = sorted((oi, ni) for ni, oi in rowmap.items()
                              if not orows[oi]["structural_only"] and not nr[ni]["structural_only"])
        new_width = max((len(r["cells"]) for r in nr if not r["structural_only"]), default=0)
        for oi in old_only:
            next_ni = None
            for moi, mni in mapped_pairs:
                if moi > oi:
                    next_ni = mni
                    break
            if next_ni is None:
                # Insert before trailing revised structural rows such as
                # \bottomrule / longtable footer controls.
                next_ni = len(rendered)
                while next_ni > 0 and nr[next_ni - 1]["structural_only"]:
                    next_ni -= 1
            vals = []
            ocells = orows[oi]["cells"]
            for j in range(new_width):
                oldj = colmap.get(j)
                if oldj is not None and oldj < len(ocells):
                    vals.append(mark_cell(ocells[oldj], force_deleted=True))
                else:
                    vals.append("")
            insertions.setdefault(next_ni, []).append({
                "text": " & ".join(vals),
                "term": r"\\",
            })

        rebuilt = []
        for ni, item in enumerate(rendered):
            rebuilt.extend(insertions.get(ni, []))
            rebuilt.append(item)
        rebuilt.extend(insertions.get(len(rendered), []))
        rendered = rebuilt

    new_block = replace_tabular_content(new_block, _render_rows(rendered))

    # Generic tablenotes handling: diff the complete inner content.  The
    # inline engine keeps \item and all control syntax outside revision macros.
    old_notes = re.search(
        r"(\\begin\{tablenotes\}(?:\[[^\]]*\])?)(.*?)(\\end\{tablenotes\})",
        old_block, re.S,
    )
    new_notes = re.search(
        r"(\\begin\{tablenotes\}(?:\[[^\]]*\])?)(.*?)(\\end\{tablenotes\})",
        new_block, re.S,
    )
    if new_notes:
        old_inner = old_notes.group(2) if old_notes else ""
        new_inner = new_notes.group(2)
        marked_inner = inline_diff(old_inner, new_inner) if old_notes else mark_latex_fragment(new_inner, "added")
        new_block = (
            new_block[:new_notes.start(2)] + marked_inner + new_block[new_notes.end(2):]
        )

    return new_block


def make_table_blue(block: str) -> str:
    """Color a completely new table blue without altering its control syntax."""
    outer = re.search(r"\\begin\{table\*?\}(?:\[[^\]]*\])?", block)
    if outer:
        return block[:outer.end()] + "\n\t\t\t" + r"\color{blue}% Entirely new table" + block[outer.end():]
    sp = extract_tabular_span(block)
    if sp:
        return block[:sp[0]] + "\n\t\t\t" + r"\color{blue}% Entirely new table" + block[sp[0]:]
    return block


def mark_deleted_table(block: str) -> str:
    """Keep a deleted table structurally valid and mark only visible content."""
    cap = get_command_arg(block, "caption")
    if cap is not None:
        block = replace_command_arg(block, "caption", mark_latex_fragment(cap, "deleted"))

    rows = parsed_rows(block)
    if not rows:
        return block
    rendered = []
    for r in rows:
        if r["structural_only"]:
            rendered.append({"text": r["raw"], "term": r["term"]})
            continue
        text = " & ".join(mark_cell(c, force_deleted=True) for c in r["cells"])
        rendered.append({"text": text, "term": r["term"] or r"\\"})
    return replace_tabular_content(block, _render_rows(rendered))

# ---------------------------------------------------------------------------
# Block parsing and semantic alignment
# ---------------------------------------------------------------------------

@dataclass
class Block:
    raw: str
    kind: str
    key: str = ""
    idx: int = -1


FIGURE_ENVS = {"figure", "figure*"}
MATH_ENVS = {
    "equation", "equation*", "align", "align*", "alignat", "alignat*",
    "flalign", "flalign*", "gather", "gather*", "multline", "multline*",
    "displaymath", "math",
}
ABSTRACT_ENVS = {"abstract"}
# Protect complete floats/math/tabular containers as blocks.  Other arbitrary
# environments are still safe because their \begin/\end lines are isolated as
# structural commands and mark_latex_fragment never wraps control syntax.
PROTECTED_ENVS = FIGURE_ENVS | TABLE_LIKE_ENVS | MATH_ENVS | ABSTRACT_ENVS
STRUCT_CMDS = [
    "title", "author", "affil", "part", "chapter", "section", "subsection",
    "subsubsection", "paragraph", "subparagraph", "item", "bibitem",
    "begin", "end", "flushbottom", "maketitle", "thispagestyle",
    "bibliographystyle", "bibliography",
]
struct_re = re.compile(
    r"^\s*\\(" + "|".join(map(re.escape, STRUCT_CMDS)) + r")\*?(?:\[[^\]]*\])?\b"
)


def split_technical(text: str):
    m = re.search(r"(?m)^\s*\\title\s*\{", text)
    if not m:
        # fallback: split immediately before \begin{document}
        m = re.search(r"(?m)^\s*\\begin\{document\}", text)
    if not m:
        return "", text
    return text[:m.start()], text[m.start():]


def block_kind(raw: str):
    s = raw.lstrip()
    m = re.match(r"\\begin\{([^}]+)\}", s)
    if m and m.group(1) in PROTECTED_ENVS:
        env = m.group(1)
        if env in FIGURE_ENVS:
            return "figure", env
        if env in TABLE_LIKE_ENVS:
            return "table", env
        if env in ABSTRACT_ENVS:
            return "abstract", env
        if env in MATH_ENVS:
            return "equation", env
        return "environment", env
    m = re.match(r"\\(section|subsection|subsubsection)\*?", s)
    if m:
        return m.group(1), m.group(1)
    m = re.match(r"\\([A-Za-z@]+)\*?", s)
    if m:
        return "command", m.group(1)
    if s.startswith("%"):
        return "comment", "%"
    return "paragraph", ""


def parse_blocks(text: str) -> List[Block]:
    lines = text.splitlines(keepends=True)
    blocks, buf = [], []
    protected_env = None
    env_depth = 0

    def flush():
        nonlocal buf
        if buf:
            raw = "".join(buf)
            if raw.strip():
                kind, key = block_kind(raw)
                blocks.append(Block(raw, kind, key, len(blocks)))
            buf = []

    for line in lines:
        if protected_env:
            buf.append(line)
            env_depth += len(re.findall(r"\\begin\{" + re.escape(protected_env) + r"\}", line))
            env_depth -= len(re.findall(r"\\end\{" + re.escape(protected_env) + r"\}", line))
            if env_depth <= 0:
                flush()
                protected_env = None
                env_depth = 0
            continue

        m = re.match(r"^\s*\\begin\{([^}]+)\}", line)
        if m and m.group(1) in PROTECTED_ENVS:
            flush()
            protected_env = m.group(1)
            env_depth = 0
            buf = [line]
            env_depth += len(re.findall(r"\\begin\{" + re.escape(protected_env) + r"\}", line))
            env_depth -= len(re.findall(r"\\end\{" + re.escape(protected_env) + r"\}", line))
            if env_depth <= 0:
                flush()
                protected_env = None
                env_depth = 0
            continue

        if line.strip() == "":
            flush()
            continue
        if struct_re.match(line):
            flush()
            kind, key = block_kind(line)
            blocks.append(Block(line, kind, key, len(blocks)))
            continue
        if line.lstrip().startswith("%"):
            flush()
            blocks.append(Block(line, "comment", "%", len(blocks)))
            continue
        buf.append(line)

    flush()
    for i, b in enumerate(blocks):
        b.idx = i
    return blocks


def extract_caption(s: str) -> str:
    return get_command_arg(s, "caption") or ""


def extract_image_stem(s: str) -> str:
    m = re.search(r"\\includegraphics(?:\[[^\]]*\])?\{([^}]*)\}", s)
    if not m:
        return ""
    p = m.group(1).replace("\\", "/").split("/")[-1]
    return re.sub(r"\.[A-Za-z0-9]+$", "", p).lower()


def extract_label_key(s: str) -> str:
    """Return a plain label key for structural matching only."""
    return _strip_revision_wrappers_from_key(get_command_arg(s, "label") or "").strip()


def visible_text(s: str, kind: str) -> str:
    """Language-independent visible-text representation for block alignment."""
    s = strip_comments(s)
    if kind in ("figure", "table"):
        cap = extract_caption(s)
        s = cap + " " + s if kind == "table" else cap
    s = re.sub(r"\\(?:cite\w*|ref|eqref|pageref|autoref|cref|Cref|label)\s*\{[^{}]*\}", " ", s)
    s = re.sub(r"\\[A-Za-z@]+\*?(?:\[[^\]]*\])?", " ", s)
    s = s.replace(r"\&", " & ").replace(r"\%", " % ")
    s = re.sub(r"[{}$&_#~^\\]+", " ", s)
    return _unicode_normalize_visible(s)


def toks(s: str):
    """Unicode features for Latin, CJK and other scripts without stopword lists."""
    base = _text_tokens(s)
    compact = re.sub(r"\s+", "", _unicode_normalize_visible(s))
    # Character bigrams improve matching for scripts that are not whitespace-
    # segmented, while ordinary word tokens remain useful for spaced scripts.
    grams = ["§" + compact[i:i+2] for i in range(max(0, len(compact) - 1))]
    return [x for x in base if x] + grams

def heading_contexts(blocks):
    sec, sub, out = "", "", []
    for b in blocks:
        if b.kind == "section":
            sec, sub = visible_text(b.raw, b.kind), ""
        elif b.kind in ("subsection", "subsubsection"):
            sub = visible_text(b.raw, b.kind)
        out.append((sec, sub))
    return out


def build_alignment(old_blocks, new_blocks):
    old_vis = [visible_text(b.raw, b.kind) for b in old_blocks]
    new_vis = [visible_text(b.raw, b.kind) for b in new_blocks]
    old_cnt = [Counter(toks(s)) for s in old_vis]
    new_cnt = [Counter(toks(s)) for s in new_vis]
    old_ctx, new_ctx = heading_contexts(old_blocks), heading_contexts(new_blocks)
    old_subcnt = [Counter(toks(x[1])) for x in old_ctx]
    new_subcnt = [Counter(toks(x[1])) for x in new_ctx]

    df = Counter()
    for c in old_cnt + new_cnt:
        for w in c:
            df[w] += 1
    N = len(old_cnt) + len(new_cnt)
    idf = {w: math.log((N + 1) / (d + 1)) + 1 for w, d in df.items()}

    def cosine(c1, c2):
        if not c1 or not c2:
            return 0.0
        dot = sum(c1[w] * c2.get(w, 0) * idf.get(w, 1) ** 2 for w in c1)
        n1 = math.sqrt(sum((v * idf.get(w, 1)) ** 2 for w, v in c1.items()))
        n2 = math.sqrt(sum((v * idf.get(w, 1)) ** 2 for w, v in c2.items()))
        return dot / (n1 * n2) if n1 * n2 else 0.0

    def prefix_overlap(a, b, k=14):
        A, B = toks(a)[:k], toks(b)[:k]
        if not A or not B:
            return 0.0
        sa, sb = set(A), set(B)
        return len(sa & sb) / max(1, len(sa | sb))

    def compatible(a, b):
        if a.kind == b.kind:
            if a.kind == "command":
                return a.key == b.key
            return True
        return False

    def score(i, j):
        a, b = old_blocks[i], new_blocks[j]
        if not compatible(a, b):
            return -1.0
        if a.kind == "command" and a.key in (
            "begin", "end", "flushbottom", "maketitle", "thispagestyle",
            "bibliography", "bibliographystyle",
        ):
            return 1.0 if re.sub(r"\s+", "", a.raw) == re.sub(r"\s+", "", b.raw) else 0.45

        c = cosine(old_cnt[i], new_cnt[j])
        p = prefix_overlap(old_vis[i], new_vis[j])
        sc = 0.82 * c + 0.18 * p

        if a.kind == "paragraph":
            osub, nsub = old_ctx[i][1], new_ctx[j][1]
            if osub and nsub:
                cs = cosine(old_subcnt[i], new_subcnt[j])
                if cs < 0.30:
                    sc *= 0.55
                elif cs > 0.55:
                    sc = min(1.0, sc + 0.07)

        if old_vis[i] and old_vis[i] == new_vis[j]:
            sc = max(sc, 0.99)

        if a.kind == "figure":
            sa, sb = extract_image_stem(a.raw), extract_image_stem(b.raw)
            if sa and sa == sb and c >= 0.12:
                sc += 0.13
            sc += 0.12 * p

        if a.kind == "table":
            sc += 0.10 * p

        # A shared LaTeX label is a universal structural identity anchor.
        la, lb = extract_label_key(a.raw), extract_label_key(b.raw)
        if la and la == lb:
            sc = max(sc, 0.995)

        if a.kind in ("section", "subsection", "subsubsection"):
            pos_a = i / max(1, len(old_blocks) - 1)
            pos_b = j / max(1, len(new_blocks) - 1)
            pos_sim = max(0.0, 1.0 - abs(pos_a - pos_b))
            text_sim = generic_text_similarity(old_vis[i], new_vis[j])
            sc = max(sc, 0.65 * text_sim + 0.35 * pos_sim)
        return min(sc, 1.25)

    def threshold(kind):
        return {
            "figure": 0.16, "table": 0.16, "abstract": 0.06, "equation": 0.10,
            "section": 0.18, "subsection": 0.30, "subsubsection": 0.30,
            "command": 0.10, "paragraph": 0.25, "comment": 0.95,
        }.get(kind, 0.15)

    n, m = len(old_blocks), len(new_blocks)
    dp = [[0.0] * (m + 1) for _ in range(n + 1)]
    act = [[0] * (m + 1) for _ in range(n + 1)]

    for i in range(1, n + 1):
        for j in range(1, m + 1):
            best, a = dp[i - 1][j], 1
            if dp[i][j - 1] > best + 1e-12:
                best, a = dp[i][j - 1], 2
            sc = score(i - 1, j - 1)
            if sc >= threshold(old_blocks[i - 1].kind):
                reward = (sc - threshold(old_blocks[i - 1].kind)) + 0.025
                if old_blocks[i - 1].kind in ("section", "subsection", "subsubsection"):
                    reward += 2.0 if sc >= 0.90 else 0.45
                val = dp[i - 1][j - 1] + reward
                if val > best + 1e-12:
                    best, a = val, 3
            dp[i][j], act[i][j] = best, a

    matches = []
    i, j = n, m
    while i > 0 and j > 0:
        a = act[i][j]
        if a == 3:
            matches.append((i - 1, j - 1, score(i - 1, j - 1)))
            i -= 1
            j -= 1
        elif a == 1:
            i -= 1
        else:
            j -= 1
    matches.reverse()

    slots = []
    pi = pj = 0
    for i, j, sc in matches:
        for oi in range(pi, i):
            slots.append((oi, None, None))
        for nj in range(pj, j):
            slots.append((None, nj, None))
        slots.append((i, j, sc))
        pi, pj = i + 1, j + 1
    for oi in range(pi, n):
        slots.append((oi, None, None))
    for nj in range(pj, m):
        slots.append((None, nj, None))

    # Relocate unmatched floats by semantics, as in the main generator.
    for float_kind in ("figure", "table"):
        matched_old = {i for i, j, _ in matches}
        matched_new = {j for i, j, _ in matches}
        un_old = [i for i, b in enumerate(old_blocks) if b.kind == float_kind and i not in matched_old]
        un_new = [j for j, b in enumerate(new_blocks) if b.kind == float_kind and j not in matched_new]
        cands = []
        for oi in un_old:
            for nj in un_new:
                sc = score(oi, nj)
                min_sc = 0.18 if float_kind == "figure" else 0.20
                if sc >= min_sc:
                    cands.append((sc, oi, nj))
        used_o, used_n = set(), set()
        for sc, oi, nj in sorted(cands, reverse=True):
            if oi in used_o or nj in used_n:
                continue
            old_slot = next((k for k, x in enumerate(slots) if x[0] == oi and x[1] is None), None)
            new_slot = next((k for k, x in enumerate(slots) if x[0] is None and x[1] == nj), None)
            if old_slot is None or new_slot is None:
                continue
            slots[new_slot] = (oi, nj, sc)
            if old_slot < new_slot:
                slots.pop(old_slot)
            else:
                slots.pop(old_slot)
            used_o.add(oi)
            used_n.add(nj)
            matches.append((oi, nj, sc))

    return slots, matches


# ---------------------------------------------------------------------------
# Non-table rendering
# ---------------------------------------------------------------------------

def latex_tokens(s: str):
    out, i, L = [], 0, len(s)
    while i < L:
        ch = s[i]
        if ch == "\\":
            j = i + 1
            if j < L and s[j].isalpha():
                while j < L and (s[j].isalpha() or s[j] == "@"):
                    j += 1
                if j < L and s[j] == "*":
                    j += 1
                while True:
                    k = j
                    while k < L and s[k] in " \t":
                        k += 1
                    if k < L and s[k] == "[":
                        d, q = 1, k + 1
                        while q < L and d:
                            if s[q] == "[" and s[q - 1] != "\\":
                                d += 1
                            elif s[q] == "]" and s[q - 1] != "\\":
                                d -= 1
                            q += 1
                        j = q
                        continue
                    if k < L and s[k] == "{":
                        try:
                            q = find_balanced(s, k) + 1
                        except ValueError:
                            break
                        j = q
                        continue
                    break
                out.append(s[i:j])
                i = j
                continue
            out.append(s[i:min(L, i + 2)])
            i += 2
            continue
        if ch == "$":
            dbl = i + 1 < L and s[i + 1] == "$"
            delim = "$$" if dbl else "$"
            j = i + len(delim)
            q = s.find(delim, j)
            if q != -1:
                out.append(s[i:q + len(delim)])
                i = q + len(delim)
                continue
        m = re.match(r"[A-Za-z0-9]+(?:[-'’][A-Za-z0-9]+)*", s[i:])
        if m:
            tok = m.group(0)
            out.append(tok)
            i += len(tok)
            continue
        if ch.isspace():
            j = i + 1
            while j < L and s[j].isspace():
                j += 1
            out.append(s[i:j])
            i = j
            continue
        out.append(ch)
        i += 1
    return out


def protect_vulnerable(s: str) -> str:
    return re.sub(
        r"(\\(?:cite\w*|ref|eqref|pageref)\s*\{[^{}]*\})",
        lambda m: r"\mbox{" + m.group(1) + "}",
        s,
    )


def _consume_square(s: str, i: int) -> int:
    if i >= len(s) or s[i] != "[": return i
    depth=1; j=i+1
    while j < len(s) and depth:
        if s[j] == "\\": j += 2; continue
        if s[j] == "[": depth += 1
        elif s[j] == "]": depth -= 1
        j += 1
    return j

VISIBLE_ARG_COMMANDS = {
    "textbf":0,"textit":0,"emph":0,"textrm":0,"textsf":0,"texttt":0,
    "textsc":0,"textnormal":0,"textup":0,"textsl":0,"mbox":0,"fbox":0,
    "underline":0,"uline":0,"sout":0,"footnote":0,"caption":0,
    "part":0,"chapter":0,"section":0,"subsection":0,"subsubsection":0,
    "paragraph":0,"subparagraph":0,"title":0,"author":0,"affil":0,
    "href":1,"hyperref":1,
}
BEGIN_ENV_MANDATORY_ARGS = {
    "tabular":1,"tabular*":2,"tabularx":2,"array":1,"minipage":1,
    "picture":1,"list":2,"thebibliography":1,"lrbox":1,"savebox":1,
}

def _consume_command_name(s: str, i: int):
    assert s[i] == "\\"
    if i+1 >= len(s): return "", i+1
    if not s[i+1].isalpha() and s[i+1] != "@": return s[i+1], i+2
    j=i+1
    while j < len(s) and (s[j].isalpha() or s[j] == "@"): j += 1
    name=s[i+1:j]
    if j < len(s) and s[j] == "*": name += "*"; j += 1
    return name,j

def _skip_ws(s: str, i: int) -> int:
    while i < len(s) and s[i] in " \t": i += 1
    return i

def _parse_immediate_args(s: str, i: int):
    args=[]; pos=i
    while True:
        ws=_skip_ws(s,pos)
        if ws >= len(s): break
        if s[ws] == "[":
            e=_consume_square(s,ws)
            if e <= ws: break
            args.append(("opt",ws,e,s[ws+1:e-1])); pos=e; continue
        if s[ws] == "{":
            try: e0=find_balanced(s,ws)
            except ValueError: break
            e=e0+1; args.append(("mand",ws,e,s[ws+1:e0])); pos=e; continue
        break
    return args,pos

def _math_color(fragment: str, macro: str) -> str:
    color="blue" if macro=="added" else "red"
    return "{\\color{"+color+"}"+fragment+"}"

def mark_latex_fragment(s: str, macro: str) -> str:
    r"""Mark visible content only; TeX control/environment syntax always stays outside revision macros."""
    if not s: return ""
    out=[]; plain=[]
    def flush_plain():
        if not plain:
            return
        t = "".join(plain)
        plain.clear()
        if not t:
            return
        # Pure layout whitespace is not visible revision content.  Wrapping
        # indentation/newlines in \added/\deleted creates real material before
        # the first \item in list-like environments (itemize, enumerate,
        # description, tablenotes, bibliography lists, etc.) and can trigger
        # "Something's wrong--perhaps a missing \item".
        if t.strip() == "":
            out.append(t)
            return
        out.append(f"\\{macro}{{{protect_vulnerable(t)}}}")
    i=0; L=len(s)
    while i < L:
        if s.startswith(r"\(",i):
            q=s.find(r"\)",i+2)
            if q>=0: flush_plain(); out.append(_math_color(s[i:q+2],macro)); i=q+2; continue
        if s.startswith(r"\[",i):
            q=s.find(r"\]",i+2)
            if q>=0: flush_plain(); out.append(_math_color(s[i:q+2],macro)); i=q+2; continue
        if s[i] == "$":
            delim="$$" if i+1<L and s[i+1]=="$" else "$"; q=s.find(delim,i+len(delim))
            if q>=0: flush_plain(); out.append(_math_color(s[i:q+len(delim)],macro)); i=q+len(delim); continue
        if s[i] == "{":
            flush_plain()
            try: e0=find_balanced(s,i)
            except ValueError: out.append("{"); i+=1; continue
            out.append("{"+mark_latex_fragment(s[i+1:e0],macro)+"}"); i=e0+1; continue
        if s[i] == "}": flush_plain(); out.append("}"); i+=1; continue
        if s[i] == "%" and (i==0 or s[i-1] != "\\"):
            flush_plain(); q=s.find("\n",i)
            if q<0: out.append(s[i:]); i=L
            else: out.append(s[i:q+1]); i=q+1
            continue
        if s[i] == "&": flush_plain(); out.append("&"); i+=1; continue
        if s[i] != "\\": plain.append(s[i]); i+=1; continue

        flush_plain(); name,name_end=_consume_command_name(s,i)
        base=name[:-1] if name.endswith("*") else name
        if not base or (len(base)==1 and not base.isalpha()): out.append(s[i:name_end]); i=name_end; continue
        args,arg_end=_parse_immediate_args(s,name_end)

        if base in ("begin","end"):
            if not args or args[0][0] != "mand": out.append(s[i:name_end]); i=name_end; continue
            env=args[0][3]; consume=1
            if base=="begin":
                req=BEGIN_ENV_MANDATORY_ARGS.get(env,0)
                if req==0:
                    if len(args)>1 and args[1][0]=="opt": consume=2
                else:
                    seen=0
                    for k,a in enumerate(args[1:],1):
                        consume=k+1
                        if a[0]=="mand":
                            seen+=1
                            if seen>=req: break
            pe=args[consume-1][2]; out.append(s[i:pe]); i=pe; continue

        if base == "includegraphics":
            if macro == "added": out.append(s[i:arg_end])
            i=arg_end; continue

        if base in VISIBLE_ARG_COMMANDS:
            target_idx=VISIBLE_ARG_COMMANDS[base]; mand=[a for a in args if a[0]=="mand"]
            if len(mand)>target_idx:
                a,b,content=mand[target_idx][1],mand[target_idx][2],mand[target_idx][3]
                out.append(s[i:a+1]); out.append(mark_latex_fragment(content,macro)); out.append("}")
                out.append(s[b:arg_end]); i=arg_end; continue

        # Conservative universal fallback: unknown control sequence + immediate args are syntax.
        out.append(s[i:arg_end]); i=arg_end
    flush_plain(); return "".join(out)

def marked(s: str, macro: str) -> str:
    if not s: return ""
    m1=re.match(r"\s*",s); m2=re.search(r"\s*$",s)
    lead=m1.group(0); trail=m2.group(0)
    core=s[len(lead):len(s)-len(trail) if trail else len(s)]
    if not core: return s
    return lead+mark_latex_fragment(core,macro)+trail

def inline_diff(old: str, new: str) -> str:
    if old == new:
        return new
    A, B = latex_tokens(old), latex_tokens(new)
    sm = difflib.SequenceMatcher(a=A, b=B, autojunk=False)
    out = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        oa, nb = "".join(A[i1:i2]), "".join(B[j1:j2])
        if tag == "equal":
            out.append(nb)
        elif tag == "delete":
            out.append(marked(oa, "deleted"))
        elif tag == "insert":
            out.append(marked(nb, "added"))
        else:
            out.append(marked(oa, "deleted"))
            out.append(marked(nb, "added"))
    return "".join(out)


def top_level_brace_spans(s: str):
    spans, d, start = [], 0, None
    for i, ch in enumerate(s):
        if ch == "{" and (i == 0 or s[i - 1] != "\\"):
            if d == 0:
                start = i
            d += 1
        elif ch == "}" and (i == 0 or s[i - 1] != "\\") and d:
            d -= 1
            if d == 0:
                spans.append((start, i + 1))
    return spans



# Arguments of these commands are control identifiers / filenames / package
# configuration, not visible revision text. Never place \added/\deleted inside.
NONTRACK_ARGUMENT_COMMANDS = {
    "begin", "end", "label", "ref", "eqref", "pageref", "autoref", "cref", "Cref",
    "cite", "citep", "citet", "citealp", "citeauthor", "citeyear", "nocite",
    "includegraphics", "input", "include", "bibliography", "bibliographystyle",
    "documentclass", "usepackage", "RequirePackage",
    "setlength", "addtolength", "setcounter", "addtocounter",
}

STRUCTURE_ONLY_COMMANDS = {
    "begin", "end", "label", "centering", "raggedright", "raggedleft",
    "hline", "toprule", "midrule", "bottomrule", "cline", "cmidrule",
    "addlinespace", "newpage", "clearpage", "pagebreak", "nopagebreak",
    "noindent", "indent", "vfill", "hfill", "smallskip", "medskip", "bigskip",
    "maketitle", "linenumbers", "nolinenumbers", "flushbottom", "raggedbottom",
    "thispagestyle", "pagestyle",
}

def leading_command_name(raw: str) -> str:
    m = re.match(r"^\s*\\([A-Za-z@]+)\*?", raw)
    return m.group(1) if m else ""

def first_mandatory_arg_span(raw: str):
    r"""Visible argument span of the leading command only.

    Critical case:
      \subsection*{Title}\label{sec:key}
    returns the Title span, never the label key.
    """
    m = re.match(r"^\s*\\[A-Za-z@]+\*?", raw)
    if not m:
        return None
    i = m.end()
    L = len(raw)
    while True:
        while i < L and raw[i].isspace():
            i += 1
        if i < L and raw[i] == "[":
            e = _consume_square(raw, i)
            if e <= i:
                return None
            i = e
            continue
        break
    if i >= L or raw[i] != "{":
        return None
    try:
        e = find_balanced(raw, i)
    except ValueError:
        return None
    return i + 1, e

def _strip_revision_wrappers_from_key(arg: str) -> str:
    # Prefer revised material in a replace pair.
    pair = re.compile(r"\\deleted\{([^{}]*)\}\s*\\added\{([^{}]*)\}")
    while pair.search(arg):
        arg = pair.sub(lambda m: m.group(2), arg)
    arg = re.sub(r"\\added\{([^{}]*)\}", r"\1", arg)
    arg = re.sub(r"\\deleted\{([^{}]*)\}", r"\1", arg)
    return arg

def sanitize_identifier_arguments(text: str) -> str:
    """Remove revision wrappers from label/ref/cite/environment/file keys.

    These strings are written to .aux/.toc/.lof/.lot/.out and must remain
    plain control identifiers.
    """
    names = sorted(NONTRACK_ARGUMENT_COMMANDS, key=len, reverse=True)
    cre = re.compile(
        r"\\(" + "|".join(re.escape(x) for x in names) +
        r")\*?(?:\[[^\]]*\])?\s*\{"
    )
    out = []
    pos = 0
    while True:
        m = cre.search(text, pos)
        if not m:
            out.append(text[pos:])
            break
        out.append(text[pos:m.end()])
        brace = m.end() - 1
        try:
            e = find_balanced(text, brace)
        except ValueError:
            out.append(text[m.end():])
            break
        arg = text[brace + 1:e]
        out.append(_strip_revision_wrappers_from_key(arg))
        out.append("}")
        pos = e + 1
    return "".join(out)

def _leading_command_tail(raw: str):
    """Return (prefix, trailing_text) after immediate command arguments."""
    m = re.match(r"^\s*\\[A-Za-z@]+\*?", raw)
    if not m:
        return raw, ""
    _, end = _parse_immediate_args(raw, m.end())
    return raw[:end], raw[end:]


def _has_visible_tail(text: str) -> bool:
    # Metadata/control-only tails (e.g. a trailing \label) normalize to empty.
    return bool(visible_text(text, "paragraph").strip())


def diff_text_command(old: str, new: str) -> str:
    r"""Track visible command text, never command/control syntax."""
    oc = leading_command_name(old)
    nc = leading_command_name(new)
    if not (oc and nc and oc == nc):
        return new

    # Explicitly visible brace argument (headings, captions, formatting, etc.).
    if nc in VISIBLE_ARG_COMMANDS:
        so = first_mandatory_arg_span(old)
        sn = first_mandatory_arg_span(new)
        if so and sn:
            ao, bo = so
            an, bn = sn
            revised = new[:an] + inline_diff(old[ao:bo], new[an:bn]) + new[bn:]
            return revised

    # Commands whose immediate arguments are identifiers/configuration stay
    # untouched, but any ordinary trailing prose is still trackable.  This
    # covers constructs such as \bibitem{key} text without modifying `key`.
    oprefix, otail = _leading_command_tail(old)
    nprefix, ntail = _leading_command_tail(new)
    if _has_visible_tail(otail) or _has_visible_tail(ntail):
        return nprefix + inline_diff(otail, ntail)

    # Pure structural/unknown control syntax: revised source wins unchanged.
    return new


def first_graphics(raw: str):
    m = re.search(r"\\includegraphics(?:\[[^\]]*\])?\{[^}]*\}", raw)
    return m.group(0) if m else ""


def diff_figure(old: str, new: str) -> str:
    """
    Keep only the revised image command. Never create a deleted includegraphics.
    Caption still receives word-level revision marking.
    """
    out = new
    oc, nc = extract_caption(old), extract_caption(new)
    if r"\caption" in out:
        out = replace_command_arg(out, "caption", word_diff(oc, nc))
    return out


def mark_new_figure(raw: str) -> str:
    out = raw
    cap = extract_caption(out)
    if cap:
        out = replace_command_arg(out, "caption", r"\added{" + cap + "}")
    # The image itself is intentionally left unwrapped.
    return out


def equation_contents(raw: str):
    m = re.search(
        r"\\begin\{(?:equation|align|gather|multline)\*?\}(.*?)"
        r"\\end\{(?:equation|align|gather|multline)\*?\}",
        raw, re.S,
    )
    return m.group(1).strip() if m else raw.strip()


def diff_equation(old: str, new: str) -> str:
    oo, nn = equation_contents(old), equation_contents(new)
    return (
        "\\begin{equation*}\n\\text{" + marked(r"\(\displaystyle " + oo + r"\)", "deleted")
        + "}\n\\end{equation*}\n"
        + "\\begin{equation*}\n\\text{" + marked(r"\(\displaystyle " + nn + r"\)", "added")
        + "}\n\\end{equation*}\n"
    )


def diff_abstract(old: str, new: str) -> str:
    mo = re.search(r"\\begin\{abstract\}(.*?)\\end\{abstract\}", old, re.S)
    mn = re.search(r"\\begin\{abstract\}(.*?)\\end\{abstract\}", new, re.S)
    if not (mo and mn):
        return inline_diff(old, new)
    oi = re.sub(r"\s+", " ", mo.group(1)).strip()
    ni = re.sub(r"\s+", " ", mn.group(1)).strip()
    return "\\begin{abstract}\n\t" + inline_diff(oi, ni) + "\n\\end{abstract}\n"


def whole_block(raw: str, kind: str, macro: str) -> str:
    if kind == "figure":
        # Entirely deleted figures are omitted.
        return "" if macro == "deleted" else mark_new_figure(raw)
    if kind == "table":
        return mark_deleted_table(raw) if macro == "deleted" else make_table_blue(raw)
    if kind == "equation":
        c = equation_contents(raw)
        return (
            "\\begin{equation*}\n\\text{"
            + marked(r"\(\displaystyle " + c + r"\)", macro)
            + "}\n\\end{equation*}\n"
        )
    if kind in ("section", "subsection", "subsubsection", "command"):
        cmd = leading_command_name(raw)
        if cmd in VISIBLE_ARG_COMMANDS:
            sp = first_mandatory_arg_span(raw)
            if sp:
                a, b = sp
                return raw[:a] + marked(raw[a:b], macro) + raw[b:]
        prefix, tail = _leading_command_tail(raw)
        if _has_visible_tail(tail):
            return prefix + marked(tail, macro)
        # Pure structural / identifier-bearing / unknown syntax stays intact.
        return raw
    if kind == "comment":
        return raw if macro == "added" else "% deleted source comment: " + raw.lstrip("% ")
    return marked(raw.strip(), macro) + "\n"


def matched_block(ob: Block, nb: Block) -> str:
    if ob.raw == nb.raw:
        return nb.raw
    if ob.kind == "figure":
        return diff_figure(ob.raw, nb.raw)
    if ob.kind == "table":
        # Table path: generic structural cell/row/column logic; no document-specific fallback.
        return annotate_table_pair(nb.raw, ob.raw)
    if ob.kind == "equation":
        return diff_equation(ob.raw, nb.raw)
    if ob.kind == "abstract":
        return diff_abstract(ob.raw, nb.raw)
    if ob.kind == "command" and ob.key in ("begin", "end") and nb.key == ob.key:
        return nb.raw
    if ob.kind in ("section", "subsection", "subsubsection", "command"):
        return diff_text_command(ob.raw, nb.raw)
    if ob.kind == "comment":
        return nb.raw
    return inline_diff(ob.raw, nb.raw)


# ---------------------------------------------------------------------------
# Repair / cleanup
# ---------------------------------------------------------------------------

def normalize_preamble(text: str) -> str:
    text = re.sub(
        r"(?m)^\\usepackage\{xcolor,\s*ulem\}\s*$",
        r"\\usepackage{xcolor}\n\\usepackage[normalem]{ulem}",
        text,
        count=1,
    )
    if r"\usepackage[normalem]{ulem}" not in text:
        if re.search(r"(?m)^\\usepackage\{xcolor\}\s*$", text):
            text = re.sub(
                r"(?m)^(\\usepackage\{xcolor\}\s*)$",
                r"\1\n\\usepackage[normalem]{ulem}",
                text,
                count=1,
            )
        else:
            m = re.search(r"(?m)^\\usepackage\{hyperref\}\s*$", text)
            ins = "\\usepackage{xcolor}\n\\usepackage[normalem]{ulem}\n"
            text = text[:m.end()] + "\n" + ins + text[m.end():] if m else ins + text

    # Remove definitions produced by older versions.
    text = re.sub(
        r"(?m)^\s*\\(?:newcommand|providecommand|DeclareRobustCommand)"
        r"\{\\(?:added|deleted|addedmath)\}(?:\[[^\]]*\])?\{.*\}\s*$",
        "",
        text,
    )

    robust = r"""
% ---- Robust revision commands ----
\DeclareRobustCommand{\added}[1]{\textcolor{blue}{\uline{#1}}}
\DeclareRobustCommand{\deleted}[1]{\textcolor{red}{\sout{#1}}}
\DeclareRobustCommand{\addedmath}[1]{{\color{blue}\underline{\displaystyle #1}}}
\makeatletter
\@ifpackageloaded{hyperref}{%
  \pdfstringdefDisableCommands{%
    \def\added#1{#1}%
    \def\deleted#1{}%
    \def\addedmath#1{#1}%
  }%
}{}
\makeatother
"""
    m = re.search(r"(?m)^\\title\s*\{", text)
    if not m:
        m = re.search(r"(?m)^\\begin\{document\}", text)
    where = m.start() if m else len(text)
    return text[:where] + robust.strip() + "\n" + text[where:]


def split_footnotes_from_ulem(text: str) -> str:
    out, i = [], 0
    while i < len(text):
        m = re.search(r"\\(added|deleted)\{", text[i:])
        if not m:
            out.append(text[i:])
            break
        start = i + m.start()
        macro = m.group(1)
        open_pos = i + m.end() - 1
        try:
            endbrace = find_balanced(text, open_pos)
        except ValueError:
            out.append(text[i:])
            break
        content = text[open_pos + 1:endbrace]
        end = endbrace + 1
        if r"\footnote{" not in content:
            out.append(text[i:end])
            i = end
            continue

        out.append(text[i:start])
        pieces, p = [], 0
        while True:
            fm = re.search(r"\\footnote\{", content[p:])
            if not fm:
                tail = content[p:]
                if tail:
                    pieces.append(f"\\{macro}{{{tail}}}")
                break
            fs = p + fm.start()
            fo = p + fm.end() - 1
            before = content[p:fs]
            if before:
                pieces.append(f"\\{macro}{{{before}}}")
            try:
                fe = find_balanced(content, fo)
            except ValueError:
                pieces.append(f"\\{macro}{{{content[p:]}}}")
                break
            farg = content[fo + 1:fe]
            pieces.append(f"\\footnote{{\\{macro}{{{farg}}}}}")
            p = fe + 1
        out.append("".join(pieces))
        i = end
    return "".join(out)


def split_display_math_from_added(text: str) -> str:
    out, i = [], 0
    while i < len(text):
        m = re.search(r"\\added\{", text[i:])
        if not m:
            out.append(text[i:])
            break
        start = i + m.start()
        open_pos = i + m.end() - 1
        try:
            endbrace = find_balanced(text, open_pos)
        except ValueError:
            out.append(text[i:])
            break
        content = text[open_pos + 1:endbrace]
        end = endbrace + 1
        if r"\[" not in content or r"\]" not in content:
            out.append(text[i:end])
            i = end
            continue

        out.append(text[i:start])
        p, parts = 0, []
        while p < len(content):
            a = content.find(r"\[", p)
            if a < 0:
                tail = content[p:]
                if tail.strip():
                    parts.append(r"\added{" + tail.strip() + "}")
                break
            b = content.find(r"\]", a + 2)
            if b < 0:
                parts.append(r"\added{" + content[p:] + "}")
                break
            prose = content[p:a]
            if prose.strip():
                parts.append(r"\added{" + prose.strip() + "}")
            formula = re.sub(r"\s+", " ", content[a + 2:b]).strip()
            parts.append("\t\\[\n\t\\addedmath{" + formula + "}\n\t\\]")
            p = b + 2
        out.append("\n".join(parts))
        i = end
    return "".join(out)


def sanitize_revision_spans(text: str) -> str:
    out=[]; i=0
    while i < len(text):
        m=re.search(r"\\(added|deleted)\{",text[i:])
        if not m: out.append(text[i:]); break
        start=i+m.start(); macro=m.group(1); op=i+m.end()-1; out.append(text[i:start])
        try: e=find_balanced(text,op)
        except ValueError: out.append(text[start:]); break
        out.append(mark_latex_fragment(text[op+1:e],macro)); i=e+1
    return "".join(out)


def remove_deleted_graphics(text: str) -> str:
    """
    Remove every \\deleted{...} whose payload contains an \\includegraphics command.
    Balanced-brace aware, so options and nested command arguments are handled safely.
    """
    out, i = [], 0
    needle = r"\deleted{"
    while True:
        p = text.find(needle, i)
        if p < 0:
            out.append(text[i:])
            break
        out.append(text[i:p])
        open_pos = p + len(r"\deleted")
        try:
            endbrace = find_balanced(text, open_pos)
        except ValueError:
            out.append(text[p:])
            break
        payload = text[open_pos + 1:endbrace]
        if re.search(r"\\includegraphics(?:\[[^\]]*\])?\{", payload):
            # completely drop the deleted graphic marker and payload
            i = endbrace + 1
        else:
            out.append(text[p:endbrace + 1])
            i = endbrace + 1
    return "".join(out)


def remove_empty_figures(text: str) -> str:
    """
    Drop figure environments that have no revised graphic/content after cleanup.
    A figure with a caption but no graphic is retained only if it contains other
    substantive revised TeX. Entirely deleted old figures never reach this stage.
    """
    pat = re.compile(r"\\begin\{figure(\*?)\}.*?\\end\{figure\1\}", re.S)

    def keep_or_drop(m):
        block = m.group(0)
        if r"\includegraphics" in block:
            return block
        # if the environment is effectively only shell/labels/caption with deletion marks, drop it
        tmp = re.sub(r"\\caption(?:\[[^\]]*\])?\{.*?\}", "", block, flags=re.S)
        tmp = re.sub(r"\\label\{[^}]*\}", "", tmp)
        tmp = re.sub(r"\\(?:begin|end)\{figure\*?\}", "", tmp)
        tmp = re.sub(r"\\centering\b", "", tmp)
        tmp = re.sub(r"\\deleted\{.*?\}", "", tmp, flags=re.S)
        tmp = strip_comments(tmp).strip()
        return block if tmp else ""

    return pat.sub(keep_or_drop, text)


def repair(text: str) -> str:
    text = normalize_preamble(text)
    text = sanitize_identifier_arguments(text)
    text = split_footnotes_from_ulem(text)
    text = split_display_math_from_added(text)
    text = sanitize_revision_spans(text)
    text = sanitize_identifier_arguments(text)
    text = remove_deleted_graphics(text)
    text = remove_empty_figures(text)
    text = sanitize_identifier_arguments(text)
    return text


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def brace_balance(s: str) -> int:
    d = 0
    for i, ch in enumerate(s):
        if ch not in "{}":
            continue
        bs, j = 0, i - 1
        while j >= 0 and s[j] == "\\":
            bs += 1
            j -= 1
        if bs % 2:
            continue
        d += 1 if ch == "{" else -1
        if d < 0:
            return d
    return d


def identifier_argument_violations(text: str):
    names = sorted(NONTRACK_ARGUMENT_COMMANDS, key=len, reverse=True)
    cre = re.compile(
        r"\\(" + "|".join(re.escape(x) for x in names) +
        r")\*?(?:\[[^\]]*\])?\s*\{"
    )
    bad = []
    for m in cre.finditer(text):
        brace = m.end() - 1
        try:
            e = find_balanced(text, brace)
        except ValueError:
            bad.append((m.group(1), "unbalanced"))
            continue
        arg = text[brace + 1:e]
        if r"\added{" in arg or r"\deleted{" in arg:
            bad.append((m.group(1), arg[:160]))
    return bad

def revision_span_control_violations(text: str):
    """Return control sequences still nested inside added/deleted payloads.

    The generator's invariant is stronger than merely protecting begin/end:
    revision macros should contain visible text only.  LaTeX commands stay
    outside and may contain revision markers only in explicitly visible text
    arguments.
    """
    bad = []
    pos = 0
    while True:
        m = re.search(r"\\(added|deleted)\{", text[pos:])
        if not m:
            break
        start = pos + m.start()
        macro = m.group(1)
        brace = pos + m.end() - 1
        try:
            e = find_balanced(text, brace)
        except ValueError:
            bad.append((macro, "unbalanced"))
            break
        payload = text[brace + 1:e]
        commands = re.findall(r"\\([A-Za-z@]+)\*?", payload)
        if commands:
            bad.append((macro, ",".join(commands[:8])))
        pos = e + 1
    return bad


def validate_output(text: str):
    problems = []
    bal = brace_balance(text)
    if bal != 0:
        problems.append(f"brace balance = {bal}")
    if r"\usepackage[normalem]{ulem}" not in text:
        problems.append("ulem[normalem] missing")
    bad = identifier_argument_violations(text)
    if bad:
        preview = ", ".join(f"{c}:{{{a}}}" for c, a in bad[:5])
        problems.append(
            f"revision macro inside control identifier ({len(bad)}): {preview}"
        )
    span_bad = revision_span_control_violations(text)
    if span_bad:
        preview = ", ".join(f"{m}:{c}" for m, c in span_bad[:5])
        problems.append(
            f"LaTeX control sequence nested inside revision span ({len(span_bad)}): {preview}"
        )
    # Whitespace-only revision spans are semantically useless and structurally
    # dangerous before \item in list-like environments.
    if re.search(r"\\(?:added|deleted)\{\s*\}", text):
        problems.append("whitespace-only revision span")

    hard = [
        (r"\\label\s*\{[^}]*\\(?:added|deleted)\{", "tracked label key"),
        (r"\\(?:ref|eqref|pageref|autoref|cref|Cref)\s*\{[^}]*\\(?:added|deleted)\{", "tracked reference key"),
        (r"\\cite\w*\s*\{[^}]*\\(?:added|deleted)\{", "tracked citation key"),
        (r"\\(?:begin|end)\s*\{[^}]*\\(?:added|deleted)\{", "tracked environment name"),
        (r"\\deleted\{[^{}]*\\includegraphics", "deleted includegraphics"),
    ]
    for pat, desc in hard:
        if re.search(pat, text):
            problems.append(desc)
    return problems


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

def generate(old_path: Path, revised_path: Path, out_path: Path, report_path: Optional[Path] = None):
    old_full = old_path.read_text(encoding="utf-8", errors="replace")
    new_full = revised_path.read_text(encoding="utf-8", errors="replace")

    _, old_content = split_technical(old_full)
    new_technical, new_content = split_technical(new_full)

    old_blocks = parse_blocks(old_content)
    new_blocks = parse_blocks(new_content)
    slots, matches = build_alignment(old_blocks, new_blocks)

    pieces = []
    ignored_deleted_figures = 0
    matched_tables = 0

    for oi, nj, _ in slots:
        if oi is not None and nj is not None:
            if old_blocks[oi].kind == "table":
                matched_tables += 1
            piece = matched_block(old_blocks[oi], new_blocks[nj]).rstrip()
            if piece.strip():
                pieces.append(piece)
        elif oi is not None:
            if old_blocks[oi].kind == "figure":
                ignored_deleted_figures += 1
                continue
            piece = whole_block(old_blocks[oi].raw, old_blocks[oi].kind, "deleted").rstrip()
            if piece.strip():
                pieces.append(piece)
        elif nj is not None:
            piece = whole_block(new_blocks[nj].raw, new_blocks[nj].kind, "added").rstrip()
            if piece.strip():
                pieces.append(piece)

    body = "\n\n".join(pieces) + "\n"

    # Revised technical preamble is authoritative.
    out = new_technical.rstrip() + "\n" + body.lstrip("\n")
    out = repair(out)
    out_path.write_text(out, encoding="utf-8")

    problems = validate_output(out)
    lines = [
        f"OLD: {old_path}",
        f"REVISED: {revised_path}",
        f"OUTPUT: {out_path}",
        f"old blocks={len(old_blocks)}",
        f"revised blocks={len(new_blocks)}",
        f"semantic matches={len(matches)}",
        f"matched tables processed by generic structural table logic={matched_tables}",
        f"entirely deleted figures ignored={ignored_deleted_figures}",
        f"added occurrences={out.count(chr(92) + 'added{')}",
        f"deleted occurrences={out.count(chr(92) + 'deleted{')}",
        f"addedmath occurrences={out.count(chr(92) + 'addedmath{')}",
        f"brace balance={brace_balance(out)}",
        "validation=" + ("OK" if not problems else "FAIL: " + "; ".join(problems)),
    ]
    report = "\n".join(lines) + "\n"
    if report_path:
        report_path.write_text(report, encoding="utf-8")
    print(report)
    return not problems


# ---------------------------------------------------------------------------
# One-click UI / CLI
# ---------------------------------------------------------------------------

def choose_files_gui():
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox
    except Exception as e:
        raise SystemExit(
            "No command-line paths were supplied and tkinter is unavailable.\n"
            "Run: python universal_tracked_revision_v4.py OLD.tex REVISED.tex -o OUTPUT.tex"
        ) from e

    root = tk.Tk()
    root.withdraw()

    old = filedialog.askopenfilename(
        title="Select ORIGINAL / OLD LaTeX file",
        filetypes=[("LaTeX files", "*.tex"), ("All files", "*.*")],
    )
    if not old:
        raise SystemExit("Cancelled: no original file selected.")

    revised = filedialog.askopenfilename(
        title="Select REVISED LaTeX file",
        filetypes=[("LaTeX files", "*.tex"), ("All files", "*.*")],
    )
    if not revised:
        raise SystemExit("Cancelled: no revised file selected.")

    rp = Path(revised)
    out = rp.with_name(rp.stem + "_tracked.tex")
    report = rp.with_name(rp.stem + "_tracked_report.txt")

    ok = generate(Path(old), rp, out, report)
    msg = f"Created:\n{out}\n\nReport:\n{report}"
    if ok:
        messagebox.showinfo("Tracked revision complete", msg)
    else:
        messagebox.showwarning("Tracked revision completed with validation warnings", msg)
    root.destroy()
    return 0 if ok else 2


def main():
    ap = argparse.ArgumentParser(
        description="Generate a tracked-change LaTeX file from original and revised manuscripts."
    )
    ap.add_argument("old", nargs="?", help="original/old .tex")
    ap.add_argument("revised", nargs="?", help="revised/new .tex")
    ap.add_argument("-o", "--output", help="output tracked .tex")
    ap.add_argument("--report", help="diagnostic report path")
    args = ap.parse_args()

    if not args.old and not args.revised:
        return choose_files_gui()
    if not args.old or not args.revised:
        ap.error("provide both OLD.tex and REVISED.tex, or provide neither to use the GUI")

    old = Path(args.old)
    revised = Path(args.revised)
    if not old.is_file():
        raise SystemExit(f"Original file not found: {old}")
    if not revised.is_file():
        raise SystemExit(f"Revised file not found: {revised}")

    out = Path(args.output) if args.output else revised.with_name(revised.stem + "_tracked.tex")
    report = Path(args.report) if args.report else out.with_name(out.stem + "_report.txt")
    ok = generate(old, revised, out, report)
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
