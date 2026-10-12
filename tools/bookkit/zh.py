"""中文译文的后处理与质检。

翻译接口返回的译稿在标点、空格、括号上必然不统一（同一段里可能既出现
`中文,` 又出现 `中文，`）。这里把可机械判定的部分规范化，把不可机械判定的
部分报出来给人看——**绝不改动词句**。

排版约定（与 docs/STYLE.zh.md 一致）：
  - 汉字之间不留空格；
  - 汉字与拉丁字母/数字之间留一个半角空格；
  - 全部用全角中文标点；
  - 脚注角标 `[^n]` 紧贴前面的词，前后不留空格。
"""

from __future__ import annotations

import re

CJK_CLASS = r"\u3400-\u4dbf\u4e00-\u9fff"
CJK_PUNCT_CLASS = r"\u3000-\u303f\uff01-\uff60\uffe0-\uffe6"
CJK_RE = re.compile(rf"[{CJK_CLASS}]")
CJK_OR_PUNCT_RE = re.compile(rf"[{CJK_CLASS}{CJK_PUNCT_CLASS}]")
LATIN_RE = re.compile(r"[A-Za-z]")
FOOTNOTE_REF_RE = re.compile(r"\[\^\d+\]")
PLACEHOLDER_RE = re.compile(r"\[\[(?:fn|m|img)\d+\]\]")

# 紧邻汉字时应当全角化的 ASCII 标点（括号单独配对处理，见 _parens）
ASCII_TO_FULL = {
    ",": "，", ";": "；", ":": "：", "?": "？", "!": "！",
}


def has_cjk(text: str) -> bool:
    return bool(CJK_RE.search(text or ""))


def _parens(text: str) -> str:
    """括号全角化，且只动「中文语境」的那一对。

    `拍卖(auction)机制` → `拍卖（auction）机制`；
    而 `设 y=f(x)中文` 里的 `(x)` 是公式括号，不能变成全角——所以先按「前面是汉字」
    把左括号全角化，再按配对关系决定哪些右括号跟着变。
    """
    out: list[str] = []
    stack: list[int] = []
    for index, char in enumerate(text):
        if char == "(" and index and CJK_RE.match(text[index - 1]):
            stack.append(len(out))
            out.append("（")
        elif char == ")" and stack:
            stack.pop()
            out.append("）")
        else:
            out.append(char)
    return "".join(out)


def _fullwidth_punct(text: str) -> str:
    """把紧邻汉字的 ASCII 标点改成全角。

    只处理「直接贴着汉字」的那些：`图3-1,` → `图3-1，`，而 `f (x), y` 里的
    逗号两侧是空格或拉丁字符，保持原样（公式里的标点不能动）。
    """
    out = text
    for ascii_char, full_char in ASCII_TO_FULL.items():
        escaped = re.escape(ascii_char)
        # 汉字 + ASCII 标点
        out = re.sub(rf"(?<=[{CJK_CLASS}]){escaped}", full_char, out)
        # ASCII 标点 + 汉字
        out = re.sub(rf"{escaped}(?=[{CJK_CLASS}])", full_char, out)
        # 数字/拉丁/右括号 + ASCII 标点 + 汉字（如 `图3-1,中文`、`f(x),其中`）
        out = re.sub(rf"(?<=[0-9A-Za-z%\)\]]]){escaped}(?=[{CJK_CLASS}])", full_char, out)
    out = _parens(out)
    # 句号：只在「汉字 + . + 空白/汉字/结尾」时全角化，避开 3.2、2.5% 这类
    out = re.sub(rf"(?<=[{CJK_CLASS}])\.(?=\s|$|[{CJK_CLASS}])", "。", out)
    # 省略号与破折号
    out = re.sub(rf"(?<=[{CJK_CLASS}])\.\.\.", "……", out)
    out = re.sub(rf"(?<=[{CJK_CLASS}])--(?=[{CJK_CLASS}])", "——", out)
    return out


def _quotes(text: str) -> str:
    """成对的 ASCII 引号改成中文引号（只在有汉字的块里做，且要求左右各偶数个）。"""
    if text.count('"') % 2 == 0 and text.count('"'):
        pieces = text.split('"')
        rebuilt = pieces[0]
        for index, piece in enumerate(pieces[1:]):
            rebuilt += ("“" if index % 2 == 0 else "”") + piece
        text = rebuilt
    if text.count("'") % 2 == 0 and text.count("'"):
        pieces = text.split("'")
        rebuilt = pieces[0]
        for index, piece in enumerate(pieces[1:]):
            rebuilt += ("‘" if index % 2 == 0 else "’") + piece
        text = rebuilt
    return text


def normalize(text: str) -> str:
    """规范化一段译文。没有汉字时只做空白清理，不做标点改写。"""
    if not text:
        return text
    if not has_cjk(text):
        return re.sub(r"[ \t]{2,}", " ", text).strip()

    out = text.replace("\u3000", " ")
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = _fullwidth_punct(out)
    out = _quotes(out)

    # 汉字之间的空格去掉
    out = re.sub(rf"(?<=[{CJK_CLASS}]) +(?=[{CJK_CLASS}])", "", out)
    # 全角标点前后的空格去掉
    out = re.sub(rf" +(?=[{CJK_PUNCT_CLASS}])", "", out)
    out = re.sub(rf"(?<=[{CJK_PUNCT_CLASS}]) +", "", out)
    # 汉字与拉丁字母/数学之间补一个空格；数字不加（第3.2节、2.5%、图3-1 保持连写）
    out = re.sub(rf"(?<=[{CJK_CLASS}])(?=[A-Za-z$\\])", " ", out)
    out = re.sub(rf"(?<=[A-Za-z%\)])(?=[{CJK_CLASS}])", " ", out)
    # 数字与汉字之间的空格一律去掉，避免「第 3.2 节」这种写法
    out = re.sub(rf"(?<=[{CJK_CLASS}]) +(?=[0-9])", "", out)
    out = re.sub(rf"(?<=[0-9%]) +(?=[{CJK_CLASS}])", "", out)
    # 脚注角标紧贴前面的词
    out = re.sub(r"[ \t]*(\[\^\d+\])[ \t]*", r"\1", out)
    out = re.sub(r"[ \t]*(\[\[fn\d+\]\])[ \t]*", r"\1", out)
    # 标点后紧跟全角标点这类小瑕疵
    out = re.sub(r"([，。；：、？！])\1+", r"\1", out)
    out = re.sub(r"，\s*。", "。", out)
    return out.strip()


# ---------------------------------------------------------------------------
# 质检
# ---------------------------------------------------------------------------
# `glossary.json` 的 avoid 段形如 {"mechanism": "机制（不要译成“机理”）"}：
# 键是英文词，值是「正确译法 + 不要用的译法」。真正要抓的是引号里那个坏译法。
AVOID_VARIANT_RE = re.compile(r"不要(?:一律)?译成[“\"「]([^”\"」]+)[”\"」]")


def avoid_variants(glossary: dict | None) -> list[tuple[str, str]]:
    """从 avoid 段解析出 (英文词, 不要用的译法) 列表。"""
    pairs: list[tuple[str, str]] = []
    for english, note in (glossary or {}).get("avoid", {}).items():
        if isinstance(note, str):
            pairs.extend((english, variant) for variant in AVOID_VARIANT_RE.findall(note))
        elif isinstance(note, (list, tuple)):
            pairs.extend((english, str(item)) for item in note)
    return pairs


def lint(text: str, source: str = "", glossary: dict | None = None) -> list[str]:
    """检查一段译文，返回问题描述列表（可读中文，供报告直接使用）。"""
    issues: list[str] = []
    if not text or not text.strip():
        return ["译文为空"]

    if not has_cjk(text):
        if len(re.findall(r"[A-Za-z]{2,}", text)) >= 5:
            issues.append("整块未见汉字（可能没翻译）")
        return issues

    if PLACEHOLDER_RE.search(text):
        issues.append("残留占位符")
    if re.search(rf"[{CJK_CLASS}][,;:?!]", text) or re.search(rf"[,;:?!][{CJK_CLASS}]", text):
        issues.append("ASCII 标点紧贴汉字（应全角）")
    if text.count("(") != text.count(")") and text.count("（") == text.count("）"):
        issues.append("全角括号不配对")
    if text.count("（") != text.count("）"):
        issues.append("全角括号数量不等")
    if text.count("“") != text.count("”"):
        issues.append("中文引号不配对")
    if re.search(r"[\u4e00-\u9fff]{40,}", text):
        issues.append("超长汉字串（可能漏了空格或标点）")

    source_words = len(re.findall(r"[A-Za-z]{2,}", source or ""))
    cjk_chars = len(CJK_RE.findall(text))
    if source_words >= 40 and cjk_chars < source_words * 0.15:
        issues.append(f"译文过短（原文 {source_words} 词，译文 {cjk_chars} 汉字）")

    if glossary:
        for english, variant in avoid_variants(glossary):
            if variant and variant in text:
                issues.append(f"用了避免译法「{variant}」（{english}）")
        keep_english = {str(item).lower() for item in (glossary.get("keep_english") or [])}
        for english, chinese in (glossary.get("terms") or {}).items():
            if not english or not chinese or english.lower() in keep_english:
                continue
            if re.search(rf"(?<![A-Za-z]){re.escape(english)}(?![A-Za-z])", text, re.IGNORECASE):
                issues.append(f"术语未译：「{english}」应作「{chinese}」")
    return issues