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


# ---------------------------------------------------------------------------
# 公式误判：展示公式被拆成多行后，常常以「段落」的身份进入块序列
# ---------------------------------------------------------------------------
# 数学符号（出现其一才可能是公式；光有数字不算，免得把表格里的数字行也算进去）
MATH_SIGNALS = "=<>≤≥∈∫∂∑√→∞∗±∓×÷≠⊂⊃∪∩⊕⊗·→←↔∀∃"
# 这些"词"是数学记号（sup、max、微分算子…），不算作散文词
MATH_WORDS = {
    "sup", "inf", "max", "min", "lim", "exp", "log", "arg", "det", "sin", "cos", "tan",
    "ds", "dt", "dx", "dy", "dz", "dp", "dq", "dr", "dc", "dv", "dw", "di", "dj", "dk",
    "st", "th", "nd", "rd", "mod", "sgn", "tr", "diag", "vol",
}
# 公式编号行：(3.4) / [3.4] / 3.4，允许后面还挂着一小截（(3.10) 0）。
# 注意**不匹配**孤零零的「4」——那是别的东西。
EQUATION_NUMBER_RE = re.compile(r"^(?:[\(\[]\s*\d+(?:\.\d+)*\s*[\)\]])\.?$|^\d+(?:\.\d+)+\.?$")
EQUATION_NUMBER_HEAD_RE = re.compile(r"^(?:[\(\[]\s*\d+(?:\.\d+)*\s*[\)\]]|\d+(?:\.\d+)+)\s")
# 单字母/双字母变量（xi、Ri、tN、t1…）：长得像词，其实不是散文。大小写都算变量。
VARIABLE_RE = re.compile(r"^[A-Za-z]{1,2}\d*$")
# 带数字的短记号（pn1）
INDEXED_RE = re.compile(r"^[A-Za-z]{2,3}\d+$")
# 微分记号（ds、dt、dvi、dH…）：d 后面跟 1–2 个字母
DIFFERENTIAL_RE = re.compile(r"^d[A-Za-z]{1,2}$")
# 两字母的英文词：它们长得像变量，其实是散文（`if t ≤ tF, β(t) =` 要按段落翻译）
SHORT_ENGLISH = {
    "if", "is", "of", "we", "it", "so", "at", "to", "in", "as", "an", "or", "by", "on",
    "no", "be", "do", "he", "us", "me", "my", "up", "am", "go", "vs", "et", "al", "ie",
    "eg", "cf", "st", "nd", "rd", "th",
}


def is_math_variable(word: str) -> bool:
    """这个词是不是数学记号（xi、Ri、tN、mE、MRi、pn1、dvi）。

    两字母的小写英文词（if/is/we…）不算——它们是散文；大写形式（US）也不算。
    混合大小写（Ri、mE、MRi）一律当变量：英文词不会写成那样。
    """
    if word.lower() in SHORT_ENGLISH:
        return False
    if any(char.isupper() for char in word) and any(char.islower() for char in word) and len(word) <= 4:
        return True
    return bool(VARIABLE_RE.match(word) or INDEXED_RE.match(word) or DIFFERENTIAL_RE.match(word))


def prose_words(text: str) -> list[str]:
    """这段文本里的「散文词」——判定公式时用来区分「真句子」和「数学行」。

    只排除两类「像词但不是词」的记号：数学算子（sup/max/dt…）与变量（xi、Ri、tN、pn1）。
    三字母以上的英文词（含虚词 such/that/for/all）**一律算散文**：它们出现说明这行里有
    真句子，宁可留在段落里交给翻译，也不要当成公式丢掉那半句话。
    """
    words = re.findall(r"[A-Za-z]{2,}", text or "")
    return [word for word in words if word.lower() not in MATH_WORDS and not is_math_variable(word)]


def looks_like_math(text: str, max_chars: int = 90) -> bool:
    """这段文本像不像展示公式（而不是正文段落）。

    判据（宁可不改，漏改只是排版差一点，误改会丢译文）：
      * 长度不超过 ``max_chars``；
      * **一个散文词都没有**（唯一放行条件），且要么有数学符号、要么含非字母字符、
        要么整行的词全是数学算子（``max`` 这种）；
      * ``(3.4)`` 这种公式编号单独认（孤立的 ``4`` 不认）。
    混排行（``Then if β¯i (xi) = 0 we have …``、``… such that … for all …``）有散文词，
    仍按段落走，里面的散文部分照常翻译。
    """
    body = (text or "").strip()
    if not body or len(body) > max_chars:
        return False
    if EQUATION_NUMBER_RE.match(body):
        return True
    if EQUATION_NUMBER_HEAD_RE.match(body) and not prose_words(body):
        # 「(3.10) 0」是编号后面挂着一小截公式；「(2.2) Vickrey–Clarke–Groves Mechanisms」
        # 是书眉，有真文字，不能当公式（那样会把它已有的译文丢掉）
        return True
    if not re.search(r"[A-Za-z]", body):
        # 没有字母：只有出现数学符号才当公式（纯数字行留给表格处理）
        return any(signal in body for signal in MATH_SIGNALS)
    if prose_words(body):
        return False
    # 一个散文词都没有，且所有字母记号都是数学记号（单字母变量、xi/Ri/tN 这类、sup/max）
    tokens = re.findall(r"[A-Za-z]+", body)
    if tokens and all(len(token) == 1 or token.lower() in MATH_WORDS or is_math_variable(token) for token in tokens):
        return True
    return any(signal in body for signal in MATH_SIGNALS) or bool(re.search(r"[^A-Za-z\s]", body))


def retype_math_blocks(blocks: list[dict]) -> list[int]:
    """把「像公式的段落块」改成 ``equation``（会连带标成不译）。返回改动块的下标。

    为什么值得改：这些块以段落身份进入块序列后，一是会被当成待译条目白白发给模型
    （模型只能把数学原样抄回来），二是对照稿里同一行公式会印两遍（正文一次 + 英文
    对照一次）。改成 ``equation`` 后中英共用一份、不翻译。
    """
    changed: list[int] = []
    for index, block in enumerate(blocks):
        if block.get("type") != "paragraph":
            continue
        if not looks_like_math(block.get("text") or ""):
            continue
        block["type"] = "equation"
        block["no_translate"] = True
        changed.append(index)
    return changed


# ---------------------------------------------------------------------------
# 参考文献区 / 标点碎片：也不该送去翻译
# ---------------------------------------------------------------------------
REFERENCE_HEADING_RE = re.compile(r"^(references?|bibliography|literature cited|works cited)$", re.I)
# 「姓, 名」开头（Akerlof, George / Ausubel, Lawrence and Paul Milgrom），配合年份判定
SURNAME_FIRST_RE = re.compile(r"^[A-Z][A-Za-z’'\-]+,\s+[A-Z][A-Za-z.\-’']*")
YEAR_DOT_RE = re.compile(r"\(\d{4}[a-z]?\)\.")
YEAR_RE = re.compile(r"\(\d{4}[a-z]?\)")
# 卷期页，卷号允许罗马数字（Journal of Finance XVI: 8–37）
VOLUME_PAGES_RE = re.compile(r"(?:[IVXLC]+|\d+)(?:\(\d+\))?:\s*\d+[–-]\d+")
JOURNAL_RE = re.compile(
    r"(Journal|Econometrica|Quarterly|Review|Economica|Management Science|Frontiers"
    r"|Studies|Letters|Proceedings|American Economic|Economic)"
)


def looks_like_reference(text: str) -> bool:
    """这一块像不像参考文献条目（期刊/书名 + 卷期页 + 年份）。

    **不要**用「正文里出现两个年份」当判据：``Laffont, Ossard, and Vuong (1995) used
    the inversion technique`` 这类正文一句话就能引用两份文献，那样会把定理证明整段
    误判成参考文献。
    """
    body = (text or "").strip()
    if not body or len(body) > 400:
        return False
    if "http://" in body or "https://" in body or "www." in body:
        return True
    # 一整行都是「姓, 名」形式的姓名列表（专利/报告的发明人名单）：没有动词、没有小写词，
    # 也没有数学符号/括号/数字（否则 `i, bi) + u(x˜i, ai) ≤ …` 这种公式行会被误判）
    stripped = re.sub(r"^\[\^\d+\]\s*", "", body)
    if (
        stripped.count(",") >= 4
        and not re.search(r"[0-9=≤≥+−∫∂(){}\[\]|∗·]", stripped)
        and not re.search(r"\b[a-z]{3,}\b", stripped)
    ):
        return True
    head = body[:120]
    # 「姓, 名 (年份)」开头**而且**紧接着是引号/句点/期刊卷页才算条目：
    # `Laffont, Ossard, and Vuong (1995) used the inversion technique` 是正文句，不是条目
    if SURNAME_FIRST_RE.search(body) and YEAR_RE.search(head):
        return bool(
            re.search(r"\)\s*[.“”]", body)
            or JOURNAL_RE.search(body)
            or VOLUME_PAGES_RE.search(body)
        )
    if YEAR_DOT_RE.search(body[:60]) and (JOURNAL_RE.search(body) or VOLUME_PAGES_RE.search(body)):
        return True
    if VOLUME_PAGES_RE.search(body) and (YEAR_RE.search(body) or "”" in body or "“" in body):
        return True
    # 期刊名 + 年份 + 引号里的篇名（卷期页被拆到别的块里时靠这条兜住）
    if JOURNAL_RE.search(body) and YEAR_RE.search(body) and ("”" in body or "“" in body):
        return True
    # 引号收尾 + 期刊/出版机构名（`Auction with Package Bidding.” Patent Cooperation`）：
    # 典型的是文献条目被拆行后的续行
    return bool(re.search(r"\.\s*”", body[:60]) and JOURNAL_RE.search(body))


def starts_reference_entry(text: str) -> bool:
    """这一块是不是一条参考文献的**开头**（够强的特征，可以用来开启整个文献区段）。

    只有 ``姓, 名 (年份). `` 后面跟引号/期刊这种形态才算；正文里引用年份的句子不算。
    """
    body = (text or "").strip()
    if not body or len(body) > 200:
        return False
    return bool(SURNAME_FIRST_RE.search(body) and YEAR_RE.search(body[:120]) and re.search(r"\)\s*[.“”]", body))


def looks_like_debris(text: str) -> bool:
    """零碎行：``(``、``]``、``4``、``a``、``i`` 这类被拆出来的小碎片。

    ≤2 个字符的块没有可译内容（单字母既可能是变量也可能是冠词，两种都不该单独成句），
    一律标成不译，免得模型把它当句子译出一个孤零零的词。
    """
    body = (text or "").strip()
    if not body:
        return False
    if len(body) <= 2:
        # 一两个字符不可能是完整句子（`a`、`i`、`ui`、`4`、`(`）；
        # 但两字母的英文词（`an`、`is`、`If`）例外——它们被拆出来也是有意义的词
        return not (body.isalpha() and body.lower() in SHORT_ENGLISH)
    return len(body) <= 3 and not any(char.isalpha() for char in body)


def _get(block, name: str, default=None):
    """块可能是 dict（blocks.json）也可能是抽取阶段的 Block 对象，两种都支持。"""
    if isinstance(block, dict):
        return block.get(name, default)
    return getattr(block, name, default)


def _set(block, name: str, value) -> None:
    if isinstance(block, dict):
        block[name] = value
    else:
        setattr(block, name, value)


def retype_nonprose_blocks(blocks: list) -> dict[str, list[int]]:
    """按内容把「不该翻译的段落块」标成不译：公式、参考文献、标点碎片。

    参考文献分两条路：一是显式标题（``REFERENCES`` / ``Bibliography``）之后整段都算——
    标题可靠，之后直到下个标题或注释区为止；二是逐块识别条目本身（``姓, 名 (年份)``、
    卷期页）。**逐块命中不传播**：正文里引用年份的句子（``...first proved by Holmstrom
    (1979), who...``）很容易误判，一旦传播就会把后面的定理证明整段吞掉。
    返回 ``{类别: [块下标]}``。
    """
    changed: dict[str, list[int]] = {"equation": [], "reference": [], "debris": [], "running-head": []}
    in_reference_section = False
    # 一篇里出现多条文献条目，就说明后面那段是文献区（这本书 ch08 这类篇章没有
    # REFERENCES 标题，只有零散条目），从**第一条**开始整段标掉。
    reference_like = [i for i, b in enumerate(blocks) if _get(b, "type") == "paragraph" and looks_like_reference(_get(b, "text") or "")]
    bulk_start = None
    if len(reference_like) >= 5:
        # 只有「文献条目一直排到篇末」才算文献区（最后一屏里还有条目）；否则不猜，
        # 免得一条早期误判把整篇正文都标成不译（这条规则踩过坑：级联标掉 274 块）。
        tail = [i for i in reference_like if i >= len(blocks) - max(15, len(blocks) // 5)]
        if len(tail) >= 3:
            bulk_start = reference_like[0]
    # 页眉/页脚：同一短段落在不同页上逐字重复出现（如书名/章名的书眉）。
    # 判据要**像标题**（≥3 个词、首字母大写、不以句点结尾），否则会把被拆碎的常用词
    # （满页都是的 `the`）当成书眉，把已经译好的碎片又打回英文。
    repeated: dict[str, set] = {}
    for block in blocks:
        if _get(block, "type") != "paragraph":
            continue
        text = (_get(block, "text") or "").strip()
        words = text.split()
        if 2 <= len(words) <= 8 and len(text) >= 12 and text[:1].isupper() and not text.endswith("."):
            repeated.setdefault(text, set()).add(_get(block, "page"))
    heads = {text for text, pages in repeated.items() if len(pages) >= 2}

    for index, block in enumerate(blocks):
        kind = _get(block, "type")
        if kind in ("part", "heading"):
            in_reference_section = bool(REFERENCE_HEADING_RE.match((_get(block, "text") or "").strip()))
            continue
        if kind == "note":
            # 注释区终止正文里的文献区段。脚注里也有纯文献（`Hohner, Rich, Ng, …`），
            # 但**不在这里判定**：同一个模型的同类脚注有时会认真译成人名（「爱泼斯坦…」），
            # 标成不译反而会把它已译好的内容丢掉。
            in_reference_section = False
            continue
        if kind != "paragraph":
            continue
        text = (_get(block, "text") or "").strip()
        if looks_like_debris(text):
            _set(block, "no_translate", True)
            changed["debris"].append(index)
            continue
        if looks_like_math(text):
            _set(block, "type", "equation")
            _set(block, "no_translate", True)
            changed["equation"].append(index)
            continue
        if text in heads:
            _set(block, "no_translate", True)
            changed["running-head"].append(index)
            continue
        if in_reference_section or looks_like_reference(text) or (bulk_start is not None and index >= bulk_start):
            _set(block, "no_translate", True)
            changed["reference"].append(index)
            continue
        if starts_reference_entry(text):
            # 章末文献区未必有 REFERENCES 标题（这本书 part1/ch02/ch08/part2 就没有）：
            # 由一条**强特征**的条目开头开启区段，直到注释区、下个标题，或一段明显是
            # 正文的长段落（≥15 个散文词且不含年份）为止。
            in_reference_section = True
            _set(block, "no_translate", True)
            changed["reference"].append(index)
            continue
        if in_reference_section and not (len(prose_words(text)) >= 15 and not YEAR_RE.search(text)):
            _set(block, "no_translate", True)
            changed["reference"].append(index)
            continue
        in_reference_section = False
    return changed