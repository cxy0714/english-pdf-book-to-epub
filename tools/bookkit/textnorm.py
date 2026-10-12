"""文本规范化工具。

处理 PDF 文本层常见的四类脏数据：

1. **连字**：`sufﬁcient` → `sufficient`（Adobe 标准连字 U+FB00–U+FB06）。
2. **断词**：行尾 `strate-` + 下行 `gies` → `strategies`；`quasi-` + `linear`
   → `quasi-linear`。用全书词表判定要不要保留连字符。
3. **缺空格**：老式 Distiller 输出里，两端对齐的整行可能没有空格字符，
   需要在字符级用间距阈值补回来（见 pdfextract.line_text）。
4. **中拉混排**：汉字与拉丁字母/数字之间补一个空格（`tools/normalize_zh.py` 使用）。
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter

# ---------------------------------------------------------------------------
# 连字
# ---------------------------------------------------------------------------
LIGATURES = {
    "\ufb00": "ff",
    "\ufb01": "fi",
    "\ufb02": "fl",
    "\ufb03": "ffi",
    "\ufb04": "ffl",
    "\ufb05": "ft",
    "\ufb06": "st",
}

# 排版用连字符/破折号
DASHES = "\u2010\u2011\u2012\u2013\u2014\u2015"

# 未映射字形（PDF 字体缺 ToUnicode 时 PyMuPDF 会给出控制字符）。
# 由 data/glyphmap.json 负责翻译成真正的符号；这里给出兜底占位符。
# 用「白色竖矩形」而不是别的符号：可见、单宽、是合法 XML 字符
# （控制字符会直接让 XHTML 解析失败）。
UNKNOWN_GLYPH = "\u25af"  # ▯
CONTROL_CHARS = {chr(code) for code in range(0x00, 0x20)} | {chr(0x7F)}

# XML 1.0 不允许的字符：C0 控制符（除 \t \n \r）、DEL、私用区（TeX 字体常用）
ILLEGAL_XML_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ue000-\uf8ff]")


def placeholderize(text: str) -> str:
    """把无法映射、且会让 XHTML 解析失败的字形换成 ▯。"""
    return ILLEGAL_XML_RE.sub(UNKNOWN_GLYPH, text)

# 汉字/全角标点
CJK_RANGES = (
    "\u3000-\u303f"  # CJK 标点
    "\u3400-\u4dbf"  # 扩展 A
    "\u4e00-\u9fff"  # 基本区
    "\uf900-\ufaff"  # 兼容表意
    "\uff00-\uffef"  # 全角
    "\u2e80-\u2eff"  # 部首
)
CJK_CHAR = f"[{CJK_RANGES}]"
LATIN_OR_DIGIT = r"[0-9A-Za-z\u00c0-\u024f]"


def deligature(text: str) -> str:
    """展开 Adobe 连字，并把特殊破折号统一成常见的 `–`/`—` 或 `-`。"""
    if not text:
        return text
    for ligature, replacement in LIGATURES.items():
        if ligature in text:
            text = text.replace(ligature, replacement)
    text = text.replace("\u2011", "-").replace("\u2012", "\u2013")
    text = text.replace("\u2015", "\u2014").replace("\u2010", "-")
    return text


def strip_control(text: str, glyphmap: dict[str, str] | None = None) -> tuple[str, list[str]]:
    """替换未映射字形。返回 (新文本, 用到的未映射键列表)。"""
    if not text:
        return text, []
    missing: list[str] = []
    out: list[str] = []
    for char in text:
        if ord(char) < 0x20 or ord(char) == 0x7F:
            mapped = (glyphmap or {}).get(char)
            if mapped:
                out.append(mapped)
            else:
                out.append(UNKNOWN_GLYPH)
                missing.append(char)
        elif 0xE000 <= ord(char) <= 0xF8FF:  # 私有区
            out.append(UNKNOWN_GLYPH)
            missing.append(char)
        else:
            out.append(char)
    return "".join(out), missing


def collapse_spaces(text: str) -> str:
    """折叠空白、修掉标点前的多余空格。"""
    text = re.sub(r"[ \t\u00a0\u2000-\u200a\u202f\u205f]+", " ", text)
    text = re.sub(r"\s+([,.;:!?%)\]])", r"\1", text)
    text = re.sub(r"([(\[])\s+", r"\1", text)
    return text.strip()


def tidy_math(text: str) -> str:
    """公式文本的轻量清理：不动符号，只收拾空白。"""
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s+([,.;:])", r"\1", text)
    text = re.sub(r"([(\[])\s+", r"\1", text)
    text = re.sub(r"\s+([)\]])", r"\1", text)
    return text.strip()


# ---------------------------------------------------------------------------
# 断词重连
# ---------------------------------------------------------------------------
WORD_RE = re.compile(r"[A-Za-z\u00c0-\u024f][A-Za-z\u00c0-\u024f'\-]*")

# 常见「构词前缀」：这类词单独看是普通单词，但出现在行尾连字符后通常是复合词的一部分，
# 词表两边都查不到时应当保留连字符（expected-profit、quasi-linear、non-stationary…）。
# 只收多字母、构词力强的前缀；`post`（postponed）、`in`（intensive）这类会误伤，不收。
COMPOUND_PREFIXES = {
    "expected", "quasi", "non", "semi", "single", "cross", "multi", "self", "anti",
}


def build_vocab(texts) -> Counter:
    """从全书文本建立词表（用于判断行尾连字符是断词还是真连字符）。"""
    vocab: Counter = Counter()
    for text in texts:
        for word in WORD_RE.findall(text or ""):
            vocab[word.lower()] += 1
    return vocab


class Dehyphenator:
    """决定行尾 `-` 该去掉还是保留。

    规则（按优先级）：
      1. 去连字符的形式在词表里、保留的形式不在 → 去掉（行尾断词）。
      2. 保留的形式在词表里、去掉的形式不在 → 保留（如 `quasi-linear`）。
      3. 两者都在 → 保留（本书正文里本来就带连字符的写法更可信）。
      4. 两者都不在 → 再看两条兜底规则：
         a. 下一行开头是大写 → 保留（专名、`Non-Combinatorial`、`Single-Object`）；
         b. 连字符前是构词前缀 → 保留（`expected-profit`、`non-stationary`）。
         否则去掉，并计入 `unresolved` 供人工复核。
    """

    def __init__(self, vocab: Counter | None = None):
        self.vocab = vocab or Counter()
        self.unresolved: list[tuple[str, str, str]] = []

    def resolve(self, head: str, tail: str) -> str:
        """head 以 `-` 结尾，tail 是下一行的开头。返回拼接后的词。"""
        stem = head[:-1]
        joined = stem + tail
        hyphenated = stem + "-" + tail
        has_joined = self.vocab.get(joined.lower(), 0)
        has_hyphen = self.vocab.get(hyphenated.lower(), 0)
        if has_joined and not has_hyphen:
            return joined
        if has_hyphen and not has_joined:
            return hyphenated
        if has_hyphen and has_joined:
            return hyphenated
        if tail[:1].isupper():
            return hyphenated
        if stem.lower() in COMPOUND_PREFIXES and len(tail) >= 3:
            return hyphenated
        self.unresolved.append((head, tail, joined))
        return joined


def ends_with_break_hyphen(line: str) -> bool:
    """判断一行是否以「断词连字符」结尾（排除 `—`/`–` 和双连字符）。"""
    stripped = line.rstrip()
    if not stripped.endswith("-"):
        return False
    if stripped.endswith("--"):
        return False
    before = stripped[-2:-1]
    return bool(before) and (before.isalpha() or before in "\u00c0-\u024f")


def ends_with_dash(line: str) -> str | None:
    """行尾是 `–`/`—` 时返回该字符（多词术语被拆行的情况，直接接上不补空格）。"""
    stripped = line.rstrip()
    if stripped and stripped[-1] in "\u2013\u2014":
        return stripped[-1]
    return None


# ---------------------------------------------------------------------------
# 中拉混排
# ---------------------------------------------------------------------------
def cjk_latin_space(text: str) -> str:
    """汉字与拉丁字母/数字之间补一个空格（幂等）。"""
    if not text:
        return text
    text = re.sub(f"(?<={CJK_CHAR})(?={LATIN_OR_DIGIT})", " ", text)
    text = re.sub(f"(?<={LATIN_OR_DIGIT})(?={CJK_CHAR})", " ", text)
    text = re.sub(f"({CJK_CHAR}) +([,.;:!?])", r"\1\2", text)
    text = re.sub(r"  +", " ", text)
    return text


def display_width(text: str) -> int:
    """粗略显示宽度：汉字算 2。"""
    total = 0
    for char in text:
        total += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return total


def looks_chinese(text: str, threshold: float = 0.15) -> bool:
    if not text:
        return False
    cjk = sum(1 for char in text if re.match(CJK_CHAR, char))
    return cjk / max(1, len(text.strip())) >= threshold


def has_untranslated_latin(text: str, min_run: int = 25) -> bool:
    """中文段落里是否残留成串英文（漏译的典型信号）。"""
    for run in re.findall(r"[A-Za-z][A-Za-z ,;'\-]{20,}", text or ""):
        if len(run) >= min_run:
            return True
    return False