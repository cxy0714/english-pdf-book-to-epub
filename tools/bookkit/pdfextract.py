"""PDF 版面解析引擎。

目标：把一本扫描/排版 PDF 的文字层，还原成「可对齐、可翻译、可重排」的 block 序列，
而不是把视觉行直接丢给翻译。要处理的老式 Distiller 输出缺陷：

1. **缺空格**：两端对齐的整行可能没有空格字符（`designandoperationofthe`）。
   单词间距约 1.1–1.4pt，字间距约 0.0–0.2pt，因此在字符级用间距阈值补空格非常可靠。
2. **断词**：行尾连字符 + 下行词尾，用全书词表判断去留（`strate-/gies`→`strategies`，
   `quasi-/linear`→`quasi-linear`）。
3. **页眉页码**：跨页重复的页眉签名与纯数字页眉一律删除。
4. **脚注**：正文角标（5.2pt 数字）与页脚注释块（7.5pt，以 5.2pt 数字开头）分别识别，
   正文里替换成 `[^n]`，注释文本按章附在文末。
5. **表格**：同一 y 上的多个单元格 → 行；按列位置切分 → Markdown 表。
6. **插图**：矢量图按 drawing 聚类裁切渲染（本 PDF 只有 5 张位图，图形都是矢量绘制）。
7. **公式**：数学字体占比高且无长英文词的行判为公式行，单独成块、不翻译；
   未映射字形由 data/glyphmap.json 还原（见 tools/glyph_probe.py）。
8. **标题**：以 PDF 书签为准定位并给出层级，避免靠字号猜。
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
import re
from collections import Counter, defaultdict

import fitz

from . import textnorm as tn
from .config import Book

# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Char:
    c: str
    x0: float
    y0: float
    x1: float
    y1: float
    size: float
    font: str
    flags: int = 0


@dataclasses.dataclass
class Line:
    page: int
    chars: list[Char]
    text: str
    x0: float
    x1: float
    y0: float
    y1: float
    size: float
    first_size: float
    math_ratio: float
    longest_run: int
    fonts: list[str]
    kind: str = "body"
    marked_text: str = ""
    cells: list[str] = dataclasses.field(default_factory=list)
    number: int = 0
    page_h: float = 0.0
    bullet_chars: int = 0

    @property
    def is_bullet(self) -> bool:
        return self.bullet_chars > 0

    @property
    def text_without_bullet(self) -> str:
        if not self.bullet_chars:
            return self.text
        return self.text[self.bullet_chars:].lstrip()

    @property
    def ykey(self) -> int:
        return int(round(self.y0 / 1.6))


@dataclasses.dataclass
class Block:
    type: str  # part / heading / paragraph / list / caption / source / row / figure / equation / note
    text: str
    page: int
    part: str
    level: int = 0
    image: str = ""
    number: int = 0
    no_translate: bool = False
    meta: dict = dataclasses.field(default_factory=dict)

    def to_dict(self) -> dict:
        data = {
            "type": self.type,
            "text": self.text,
            "page": self.page,
            "part": self.part,
        }
        if self.level:
            data["level"] = self.level
        if self.image:
            data["image"] = self.image
        if self.number:
            data["number"] = self.number
        if self.no_translate:
            data["no_translate"] = True
        if self.meta:
            data["meta"] = self.meta
        return data

    # --- Markdown 表示（一行一个 block，便于逐块对齐）-------------------
    def to_md(self) -> str:
        if self.type in ("heading", "part"):
            level = max(1, min(6, self.level or 2))
            return f"{'#' * level} {self.text}"
        if self.type == "subhead":
            return f"**{self.text}**"
        if self.type == "list":
            return f"- {self.text}"
        if self.type == "caption":
            return f"*{self.text}*"
        if self.type == "source":
            return f"*{self.text}*"
        if self.type == "row":
            return self.text
        if self.type == "figure":
            return f"![{self.meta.get('alt', 'figure')}]({self.image})"
        if self.type == "equation":
            return f"$$ {self.text} $$"
        if self.type == "note":
            return f"[^{self.number}]: {self.text}"
        return self.text

    def aligned_key(self) -> tuple:
        return (self.type, self.level, self.number)


# ---------------------------------------------------------------------------
# 行的构造
# ---------------------------------------------------------------------------
MATH_FALLBACK = ("MTEX", "MTSY", "RMTMI", "MSAM", "MSBM", "EUSM", "EUM", "CMSY", "CMMI", "CMEX")


def is_math_font(font: str, prefixes) -> bool:
    return any(font.startswith(prefix) for prefix in prefixes)


def join_chars(chars: list[Char], glyphmap: GlyphMap, page: int, min_gap: float = 0.45,
               factor: float = 0.085) -> str:
    """按字符间距补空格并还原未映射字形。"""
    out: list[str] = []
    for index, char in enumerate(chars):
        if index:
            prev = chars[index - 1]
            gap = char.x0 - prev.x1
            threshold = max(min_gap, factor * max(char.size, prev.size))
            if (
                gap > threshold
                and prev.c.strip()
                and char.c.strip()
                and prev.c not in "([{"
                and not prev.c.isspace()
                and not char.c.isspace()
            ):
                out.append(" ")
        out.append(glyphmap.render(page, char.font, char.c))
    return "".join(out)


def build_line(page_no: int, raw_line: dict, glyphmap: GlyphMap, math_prefixes, page_h: float,
               bullet_fonts=()) -> Line | None:
    chars: list[Char] = []
    for span in raw_line.get("spans", []):
        font = span.get("font", "")
        size = float(span.get("size", 0.0))
        flags = int(span.get("flags", 0))
        for char in span.get("chars", []):
            bbox = char["bbox"]
            chars.append(Char(char["c"], bbox[0], bbox[1], bbox[2], bbox[3], size, font, flags))
    if not chars:
        return None
    chars.sort(key=lambda c: (round(c.x0, 2), c.y0))

    text = join_chars(chars, glyphmap, page_no)
    text, _missing = tn.strip_control(text, None)
    text = tn.deligature(text)
    text = tn.collapse_spaces(text)

    sizes_all = Counter(round(c.size, 1) for c in chars)
    sizes_text = Counter(round(c.size, 1) for c in chars if not is_math_font(c.font, math_prefixes))
    size = (sizes_text or sizes_all).most_common(1)[0][0]
    nonspace = [c for c in chars if not c.c.isspace()]
    math_count = sum(1 for c in nonspace if is_math_font(c.font, math_prefixes))
    longest = max((len(run) for run in re.findall(r"[A-Za-z]+", text)), default=0)
    fonts = sorted({c.font for c in chars})
    bullet_chars = 0
    for char in chars:
        if char.font in bullet_fonts:
            bullet_chars += 1
        else:
            break

    return Line(
        page=page_no,
        chars=chars,
        text=text,
        x0=min(c.x0 for c in chars),
        x1=max(c.x1 for c in chars),
        y0=min(c.y0 for c in chars),
        y1=max(c.y1 for c in chars),
        size=size,
        first_size=round(chars[0].size, 1),
        math_ratio=math_count / max(1, len(nonspace)),
        longest_run=longest,
        fonts=fonts,
        page_h=page_h,
        bullet_chars=bullet_chars,
    )


def read_pages(doc, book: Book, glyphmap: GlyphMap) -> list[list[Line]]:
    prefixes = book.fonts["math_prefixes"] or MATH_FALLBACK
    bullet_fonts = set(book.layout.get("bullet_fonts") or ())
    pages: list[list[Line]] = []
    for page_no in range(doc.page_count):
        page = doc[page_no]
        raw = page.get_text("rawdict")
        lines: list[Line] = []
        for block in raw.get("blocks", []):
            if block.get("type") != 0:
                continue
            for raw_line in block.get("lines", []):
                line = build_line(page_no + 1, raw_line, glyphmap, prefixes, page.rect.height, bullet_fonts)
                if line and line.text.strip():
                    lines.append(line)
        lines.sort(key=lambda l: (l.ykey, l.x0))
        pages.append(lines)
    return pages


# ---------------------------------------------------------------------------
# 页眉/页码净化
# ---------------------------------------------------------------------------
BAND_TOP = 0.078
BAND_BOTTOM = 0.925
PURE_NUMBER_RE = re.compile(r"^(?:\d{1,4}|[ivxlcdm]{1,7})[.\u2013-]?$", re.I)


def normalize_for_match(text: str) -> str:
    text = re.sub(r"\s+", "", text or "").lower()
    return re.sub(r"[\u2013\u2014\u2010\u2011\-\u2018\u2019\"']", "", text)


def find_furniture(pages: list[list[Line]], toc_titles: list[str] | None = None) -> tuple[set[str], dict[str, int]]:
    """收集页眉/页脚签名：跨页重复的、纯页码的、与书签标题重合的。"""
    signatures: Counter = Counter()
    total = len(pages)
    for lines in pages:
        if not lines:
            continue
        height = lines[0].page_h or 1.0
        for line in lines:
            if not (line.y1 <= height * BAND_TOP or line.y0 >= height * BAND_BOTTOM):
                continue
            signature = re.sub(r"\d+", "#", line.text).strip()
            signatures[signature] += 1
    threshold = max(3, int(total * 0.03))
    drop = {sig for sig, count in signatures.items() if count >= threshold and sig}
    return drop, dict(signatures)


def remove_furniture(pages: list[list[Line]], drop: set[str], toc_titles: list[str] | None = None) -> int:
    """删除页眉/页码。页脚只删纯页码；页眉另删与书签标题重合的行。"""
    titles = [normalize_for_match(t) for t in (toc_titles or [])]
    titles = [t for t in titles if len(t) >= 4]
    removed = 0
    for lines in pages:
        if not lines:
            continue
        height = lines[0].page_h or 1.0
        keep: list[Line] = []
        for line in lines:
            text = line.text.strip()
            signature = re.sub(r"\d+", "#", text).strip()
            in_header = line.y1 <= height * BAND_TOP
            in_footer = line.y0 >= height * BAND_BOTTOM
            if in_footer and not PURE_NUMBER_RE.match(text):
                keep.append(line)
                continue
            if not (in_header or in_footer):
                keep.append(line)
                continue
            if PURE_NUMBER_RE.match(text) or signature in drop:
                removed += 1
                continue
            if in_header and len(text) >= 4:
                normalized = normalize_for_match(text)
                if normalized in titles or any(normalized in title for title in titles):
                    removed += 1
                    continue
            keep.append(line)
        lines[:] = keep
    return removed


# ---------------------------------------------------------------------------
# 脚注
# ---------------------------------------------------------------------------
def detect_footnotes(lines: list[Line], marker_size: float, footnote_size: float, min_y_ratio: float,
                     exclude_ykeys: set[int] | None = None) -> tuple[list[tuple[int, list[Line]]], list[Line], list[Line]]:
    """识别本页脚注块。

    脚注有两种版式：角标与注释文字同行（`11 An instance…`，首字符 5.2pt），
    或角标独占一行、注释文字另起（`1` + 后续 7.5pt 行）。两种都要认。

    返回 (notes, leftover_small_lines, consumed_lines)。
    """
    height = lines[0].page_h if lines else 0.0
    exclude_ykeys = exclude_ykeys or set()
    candidates = [
        line
        for line in lines
        if line.size <= footnote_size + 0.55
        and line.size >= marker_size - 0.3
        and line.y0 >= height * min_y_ratio
        and line.ykey not in exclude_ykeys
    ]
    notes: list[tuple[int, list[Line]]] = []
    leftover: list[Line] = []
    current: list[Line] = []
    current_no = 0

    def flush() -> None:
        nonlocal current, current_no
        if current:
            notes.append((current_no, current))
            current = []
            current_no = 0

    marker_only = re.compile(r"^\d{1,3}$")

    for line in candidates:
        starts_note = False
        number = 0
        if abs(line.first_size - marker_size) < 0.25:
            if marker_only.match(line.text.strip()):
                starts_note = True
                number = int(line.text.strip())
            elif re.match(r"^\d{1,3}\b", line.text) and line.size <= footnote_size + 0.55:
                starts_note = True
                number = int(re.match(r"^(\d{1,3})", line.text).group(1))
        if starts_note:
            flush()
            current_no = number
            current = [line]
        elif current:
            current.append(line)
        else:
            leftover.append(line)
    flush()
    return notes, leftover, candidates


def mark_note_refs(line: Line, known: set[int], marker_sizes, glyphmap: GlyphMap | None = None,
                   math_prefixes=(), body_size: float = 9.7) -> str:
    """把正文里的脚注角标替换成 `[^n]`。

    难点：正文角标与数学上下标同为小字号（本书正文角标 6.8pt，数学下标也是 6.8pt），
    单看字号无法区分。按**垂直位置**区分最可靠（实测以 9.7pt 正文行中心为基准）：

        脚注角标（上标） 相对中心 −0.29 em
        数学下标（如 CO2） 相对中心 +0.23 em

    再叠加「编号必须出现在本章脚注里」与「前面不能是空格或左括号」两条，
    误替换基本可以排除。
    """
    glyphmap = glyphmap or GlyphMap()
    chars = line.chars

    def render(char: Char) -> str:
        return glyphmap.render(line.page, char.font, char.c)

    body = [(c.y0 + c.y1) / 2 for c in chars if abs(c.size - line.size) < 0.4]
    body_center = sorted(body)[len(body) // 2] if body else None

    def is_ref(char: Char) -> bool:
        if body_center is None or not char.c.isdigit():
            return False
        if is_math_font(char.font, math_prefixes):
            return False
        if not any(abs(char.size - size) < 0.35 for size in marker_sizes):
            return False
        return ((char.y0 + char.y1) / 2 - body_center) < -0.12 * body_size

    out: list[str] = []
    index = 0
    while index < len(chars):
        char = chars[index]
        if is_ref(char):
            run = []
            cursor = index
            while cursor < len(chars) and is_ref(chars[cursor]):
                run.append(chars[cursor].c)
                cursor += 1
            number = int("".join(run))
            prev_char = chars[index - 1] if index else None
            attachable = (
                prev_char is not None
                and not prev_char.c.isspace()
                and prev_char.c not in "([{"
            )
            if number in known and attachable:
                if out and out[-1].endswith(" "):
                    out[-1] = out[-1].rstrip()
                out.append(f"[^{number}]")
                index = cursor
                continue
        if index:
            prev = chars[index - 1]
            gap = char.x0 - prev.x1
            threshold = max(0.45, 0.085 * max(char.size, prev.size))
            if (
                gap > threshold
                and prev.c.strip()
                and char.c.strip()
                and prev.c not in "([{"
                and not prev.c.isspace()
                and not char.c.isspace()
            ):
                out.append(" ")
        out.append(render(char))
        index += 1
    text = "".join(out)
    text = tn.deligature(text)
    return tn.collapse_spaces(text)


# ---------------------------------------------------------------------------
# 表格
# ---------------------------------------------------------------------------
def detect_table_rows(lines: list[Line], max_size: float) -> dict[int, list[Line]]:
    groups: dict[int, list[Line]] = defaultdict(list)
    for line in lines:
        if line.size <= max_size + 0.6:
            groups[line.ykey].append(line)
    return {key: sorted(value, key=lambda l: l.x0) for key, value in groups.items() if len(value) >= 2}


def cluster_columns(rows: list[list[Line]], gap: float = 6.0) -> list[tuple[float, float]]:
    """按单元格横向区间聚类出列。

    表头左对齐、数字右对齐（`255,124` 与 `2,371,000` 的 x0 不同），
    所以不能只比 x0，要比区间重叠。返回 [(x0, x1)] 列表。
    """
    intervals = sorted((cell.x0, cell.x1) for row in rows for cell in row)
    columns: list[list[float]] = []
    for x0, x1 in intervals:
        if columns and x0 <= columns[-1][1] + gap:
            columns[-1][1] = max(columns[-1][1], x1)
            columns[-1][0] = min(columns[-1][0], x0)
        else:
            columns.append([x0, x1])
    return [(x0, x1) for x0, x1 in columns]


def collect_table_rows(table_rows: dict[int, list[Line]], start_key: int,
                       consumed: set[int] | None = None) -> tuple[list[list[Line]], set[int]]:
    """从 start_key 起收集连续的表格行（相邻行基线间隔不超过 25pt）。"""
    consumed = consumed or set()
    keys = sorted(table_rows)
    if start_key not in keys:
        return [], set()
    index = keys.index(start_key)
    rows: list[list[Line]] = []
    used: set[int] = set()
    previous_y = None
    for key in keys[index:]:
        baseline = table_rows[key][0].y0
        if previous_y is not None and baseline - previous_y > 25.0:
            break
        cells = [cell for cell in table_rows[key] if id(cell) not in consumed]
        if len(cells) < 2:
            break
        rows.append(cells)
        used.add(key)
        previous_y = baseline
    return rows, used


def rows_to_markdown(rows: list[list[Line]], columns: list[tuple[float, float]]) -> list[str]:
    out: list[str] = []
    width = len(columns)

    def slot(cell: Line) -> int:
        best, best_overlap = 0, -1.0
        for index, (x0, x1) in enumerate(columns):
            overlap = min(cell.x1, x1) - max(cell.x0, x0)
            if overlap > best_overlap:
                best, best_overlap = index, overlap
        return best

    for row_index, row in enumerate(rows):
        cells = [""] * width
        for cell in row:
            position = slot(cell)
            text = cell.text.strip()
            if cells[position]:
                cells[position] = f"{cells[position]} {text}".strip()
            else:
                cells[position] = text
        out.append("| " + " | ".join(cells) + " |")
        if row_index == 0:
            out.append("| " + " | ".join("---" for _ in range(width)) + " |")
    return out


# ---------------------------------------------------------------------------
# 插图（矢量图聚类 + 裁切渲染）
# ---------------------------------------------------------------------------
def page_vector_rects(page: fitz.Page) -> list[fitz.Rect]:
    rects: list[fitz.Rect] = []
    for drawing in page.get_drawings():
        rect = fitz.Rect(drawing["rect"])
        if rect.is_empty or rect.width < 0 or rect.height < 0:
            continue
        inter = rect & page.rect
        if inter.is_empty or inter.get_area() < 12:
            continue
        rects.append(inter)
    return rects


def cluster_rects(rects: list[fitz.Rect], gap: float = 26.0) -> list[fitz.Rect]:
    clusters: list[fitz.Rect] = []
    for rect in sorted(rects, key=lambda r: (r.y0, r.x0)):
        merged = False
        for index, cluster in enumerate(clusters):
            expanded = fitz.Rect(cluster)
            expanded.x0 -= gap
            expanded.y0 -= gap
            expanded.x1 += gap
            expanded.y1 += gap
            if not (expanded & rect).is_empty:
                clusters[index] = cluster | rect
                merged = True
                break
        if not merged:
            clusters.append(fitz.Rect(rect))
    # 合并后再迭代一轮，处理链式相邻
    changed = True
    while changed:
        changed = False
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                a, b = clusters[i], clusters[j]
                expanded = fitz.Rect(a)
                expanded.x0 -= gap
                expanded.y0 -= gap
                expanded.x1 += gap
                expanded.y1 += gap
                if not (expanded & b).is_empty:
                    clusters[i] = a | b
                    clusters.pop(j)
                    changed = True
                    break
            if changed:
                break
    return clusters


def clip_figure(page: fitz.Page, cluster: fitz.Rect, lines: list[Line], captions: set[int],
                body_size: float, pad: float = 12.0) -> fitz.Rect | None:
    """把图形裁切框扩到包含图内文字标签，但排除正文与图注。"""
    rect = fitz.Rect(cluster)
    rect.x0 -= pad
    rect.y0 -= pad
    rect.x1 += pad
    rect.y1 += pad
    for line in lines:
        if id(line) in captions:
            continue
        line_rect = fitz.Rect(line.x0 - 1, line.y0 - 1, line.x1 + 1, line.y1 + 1)
        if (line_rect & rect).is_empty:
            continue
        if line.size >= body_size - 0.6:
            # 正文行：把裁切框缩到它之外
            if line.y0 >= rect.y0 and line.y0 < rect.y1:
                rect.y0 = max(rect.y0, line.y1 + 1)
            elif line.y1 <= rect.y1:
                rect.y1 = min(rect.y1, line.y0 - 1)
    for line in lines:
        if id(line) in captions:
            caption_rect = fitz.Rect(line.x0 - 1, line.y0 - 1, line.x1 + 1, line.y1 + 1)
            if not (caption_rect & rect).is_empty:
                rect.y1 = min(rect.y1, line.y0 - 1)
    rect &= page.rect
    if rect.is_empty or rect.width < 30 or rect.height < 20:
        return None
    return rect


CAPTION_RE = re.compile(r"^\s*(FIGURE|Figure|Table|TABLE|Chart|Exhibit)\s*\d")
SOURCE_RE = re.compile(r"^\s*(Source|Sources|SOURCE|Note|Notes)\s*[:.]", re.I)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
@dataclasses.dataclass
class ExtractResult:
    blocks: dict[str, list[Block]]
    notes: dict[str, dict[int, str]]
    stats: dict
    figures: list[dict]
    qa_lines: list[str]


class Extractor:
    def __init__(self, book: Book, glyphmap: GlyphMap | None = None):
        self.book = book
        self.layout = book.layout
        self.glyphmap = glyphmap or {}
        self.doc = fitz.open(book.pdf_path)
        self.body_size = float(self.layout["body_size"])
        self.marker_size = float(self.layout["marker_size"])
        self.ref_sizes = [float(size) for size in self.layout.get("ref_sizes", [self.marker_size])]
        self.math_prefixes = book.fonts["math_prefixes"] or MATH_FALLBACK
        self.footnote_size = float(self.layout["footnote_size"])
        self.caption_sizes = set(self.layout["caption_sizes"])
        self.table_size = float(self.layout["table_size"])
        self.zoom = float(self.layout.get("figure_render_zoom", 4.0))
        self.pages: list[list[Line]] = []
        self.dehyphenator = tn.Dehyphenator()
        self.paragraph_lead = 13.45
        self.qa: list[str] = []
        self.stats: dict = defaultdict(int)
        self.figures: list[dict] = []

    # -- 准备 -----------------------------------------------------------
    def prepare(self) -> None:
        self.pages = read_pages(self.doc, self.book, self.glyphmap)
        toc_titles = [entry[1] for entry in self.doc.get_toc()]
        drop, signatures = find_furniture(self.pages)
        removed = remove_furniture(self.pages, drop, toc_titles)
        self.stats["furniture_lines_removed"] = removed
        self.qa.append(f"页眉/页码净化：删除 {removed} 行（{len(drop)} 种重复签名 + 纯页码 + 与书签重合的页眉）")
        top = sorted(signatures.items(), key=lambda kv: -kv[1])[:10]
        self.qa.append("  最常见页眉签名：" + ", ".join(f"{sig!r}×{count}" for sig, count in top))

        # 词表：用整页文本（PyMuPDF 的 text 模式大部分空格是好的）
        raw_texts = []
        for page_no in range(self.doc.page_count):
            raw_texts.append(self.doc[page_no].get_text("text"))
        vocab = tn.build_vocab(raw_texts)
        # 补上正文里本来就带连字符的写法
        for word in list(vocab):
            if "-" in word:
                vocab[word] = vocab[word]
        self.vocab = vocab
        self.dehyphenator = tn.Dehyphenator(vocab)

        # 行距中位数
        gaps: list[float] = []
        for lines in self.pages:
            for prev, cur in zip(lines, lines[1:]):
                delta = cur.y0 - prev.y0
                if 0 < delta < 30:
                    gaps.append(delta)
        if gaps:
            gaps.sort()
            self.paragraph_lead = gaps[len(gaps) // 2]
        self.qa.append(f"正文行距中位数：{self.paragraph_lead:.2f}pt")

    # -- 主页 -----------------------------------------------------------
    def run(self) -> ExtractResult:
        self.prepare()
        blocks: dict[str, list[Block]] = {}
        notes: dict[str, dict[int, str]] = {}
        for part in self.book.content_parts():
            part_blocks, part_notes = self.extract_part(part)
            blocks[part["slug"]] = part_blocks
            notes[part["slug"]] = part_notes
        stats = dict(self.stats)
        stats["unresolved_dehyphenation"] = len(self.dehyphenator.unresolved)
        return ExtractResult(blocks, notes, stats, self.figures, self.qa)

    def extract_part(self, part: dict) -> tuple[list[Block], dict[int, str]]:
        slug = part["slug"]
        start, end = part["page_start"], part["page_end"]
        part_lines: list[Line] = []
        for page_no in range(start, min(end, self.doc.page_count) + 1):
            part_lines.extend(self.pages[page_no - 1])

        # 1) 标题：以书签为准定位
        headings, heading_by_line = self.locate_headings(part, part_lines)

        height = part_lines[0].page_h if part_lines else 1.0
        by_page: dict[int, list[Line]] = defaultdict(list)
        for line in part_lines:
            by_page[line.page].append(line)

        # 2) 表格行（先识别，避免表格被当成脚注）
        table_rows_by_page: dict[int, dict[int, list[Line]]] = {}
        for page_no, page_lines in by_page.items():
            table_rows_by_page[page_no] = detect_table_rows(page_lines, self.table_size)

        # 3) 脚注
        notes: dict[int, str] = {}
        consumed: set[int] = set()
        carry: list[Line] = []
        for page_no in sorted(by_page):
            page_lines = by_page[page_no]
            table_ykeys = {key for key, row in table_rows_by_page[page_no].items() if len(row) >= 2}
            found, leftover, candidates = detect_footnotes(
                page_lines, self.marker_size, self.footnote_size,
                float(self.layout["note_area_min_y_ratio"]),
                exclude_ykeys=table_ykeys,
            )
            for line in candidates:
                consumed.add(id(line))
            if not found and leftover and notes:
                last_no = max(notes)
                notes[last_no] = (notes[last_no] + " " + " ".join(l.text for l in leftover)).strip()
                continue
            for number, lines in found:
                body = self.join_lines(lines, first_has_marker=True)
                if number in notes:
                    notes[number] = (notes[number] + " " + body).strip()
                else:
                    notes[number] = body
                carry = lines

        known_numbers = set(notes)
        self.stats[f"{slug}:footnotes"] = len(notes)

        # 4) 正文角标 → [^n]
        marked: dict[int, str] = {}
        for line in part_lines:
            if id(line) in consumed:
                continue
            marked[id(line)] = mark_note_refs(
                line, known_numbers, self.ref_sizes, self.glyphmap, self.math_prefixes, self.body_size
            )

        # 5) 图注
        captions: set[int] = set()
        for line in part_lines:
            if id(line) in consumed:
                continue
            if line.size <= max(self.caption_sizes | {self.table_size}) + 0.6:
                text = marked.get(id(line), line.text)
                if CAPTION_RE.match(text):
                    captions.add(id(line))

        # 6) 图片区域（按页）
        figures_by_page: dict[int, list[dict]] = self.figure_regions_by_page(
            by_page, consumed, captions
        )

        # 7) 组装
        out: list[Block] = []
        out.append(Block("part", part["title_en"], start, slug, level=1))
        heading_queue = {entry["line_id"]: entry for entry in headings}
        no_translate = part.get("kind") in ("library", "index")
        hanging = no_translate  # 参考文献/索引：条目首行在左边距，续行缩进
        reference_section = False
        buffer: list[Line] = []
        list_buffer: list[Line] = []

        def flush_paragraph() -> None:
            nonlocal buffer
            if not buffer:
                return
            text = self.join_lines(buffer, marked)
            if text:
                out.append(
                    Block(
                        "paragraph",
                        text,
                        buffer[0].page,
                        slug,
                        no_translate=no_translate or reference_section,
                    )
                )
            buffer = []

        def flush_list() -> None:
            nonlocal list_buffer
            if not list_buffer:
                return
            text = self.join_lines(list_buffer, marked)
            if list_buffer[0].is_bullet:
                text = text[list_buffer[0].bullet_chars:].lstrip()
            if text:
                out.append(
                    Block(
                        "list",
                        text,
                        list_buffer[0].page,
                        slug,
                        no_translate=no_translate or reference_section,
                    )
                )
            list_buffer = []

        page_order = sorted(by_page)
        for page_no in page_order:
            page_lines = [l for l in by_page[page_no] if id(l) not in consumed]
            table_rows = table_rows_by_page.get(page_no, {})
            emitted_rows: set[int] = set()
            figure_specs = figures_by_page.get(page_no, [])
            figure_done: set[int] = set()

            for line in sorted(page_lines, key=lambda l: (l.ykey, l.x0)):
                key = id(line)
                if key in heading_queue:
                    flush_paragraph()
                    flush_list()
                    entry = heading_queue[key]
                    title = marked.get(key, line.text)
                    reference_section = bool(re.match(r"^(REFERENCES|BIBLIOGRAPHY)\b", title, re.I))
                    hanging = hanging or reference_section
                    level = max(2, min(6, 2 + max(0, entry["level"] - part.get("chapter_level", 1))))
                    out.append(Block("heading", title, page_no, slug, level=level,
                                     no_translate=no_translate or reference_section))
                    continue

                if key in captions:
                    flush_paragraph()
                    flush_list()
                    text = marked.get(key, line.text)
                    # 图片先于图注
                    for figure in figure_specs:
                        if figure["caption_line"] == key and figure["id"] not in figure_done:
                            out.append(self.figure_block(figure, slug, text))
                            figure_done.add(figure["id"])
                    kind = "source" if SOURCE_RE.match(text) else "caption"
                    out.append(Block(kind, text, page_no, slug,
                                     no_translate=no_translate or reference_section))
                    continue

                ykey = line.ykey
                row = table_rows.get(ykey)
                if row and len(row) >= 2 and key in {id(c) for c in row}:
                    if ykey not in emitted_rows:
                        flush_paragraph()
                        rows, keys_used = collect_table_rows(table_rows, ykey, consumed)
                        if len(rows) >= 2:
                            columns = cluster_columns(rows)
                            for markdown in rows_to_markdown(rows, columns):
                                out.append(Block("row", markdown, page_no, slug, no_translate=True))
                        else:
                            # 只有一行，退回普通段落处理
                            for cell in rows[0]:
                                if buffer and self.starts_new_paragraph(buffer[-1], cell):
                                    flush_paragraph()
                                buffer.append(cell)
                            keys_used = set()
                        emitted_rows.update(keys_used)
                    continue

                # 公式行
                if line.math_ratio >= 0.55 and line.longest_run <= 18 and abs(line.size - self.body_size) < 1.2:
                    if list_buffer:
                        list_buffer.append(line)
                        continue
                    flush_paragraph()
                    out.append(Block("equation", tn.tidy_math(marked.get(key, line.text)),
                                     page_no, slug, no_translate=True))
                    continue

                # 列表项（项目符号用 LCIRCLE10 之类的符号字体，续行缩进更深）
                if line.is_bullet:
                    flush_paragraph()
                    flush_list()
                    list_buffer.append(line)
                    continue
                if list_buffer and line.x0 - self.left_margin > 3.0:
                    # 上一行是满行 → 条目还没结束；上一行很短 → 条目已结束
                    if list_buffer[-1].x1 >= self.right_margin - 5.0 * line.size:
                        flush_paragraph()
                        list_buffer.append(line)
                        continue
                flush_list()

                # 正文
                if buffer and self.starts_new_paragraph(buffer[-1], line, hanging=hanging):
                    flush_paragraph()
                buffer.append(line)
        flush_paragraph()
        flush_list()

        # 6) 文末注释定义。脚注/尾注是正文内容，必须翻译（原来一律标了 no_translate，
        #    等于中文版里所有注释都留英文）；只有参考文献、索引这类不翻译的篇章才照抄。
        for number in sorted(notes):
            out.append(Block("note", notes[number], end, slug, number=number, no_translate=no_translate))
        self.stats[f"{slug}:blocks"] = len(out)
        return out, notes

    # -- 段落 -----------------------------------------------------------
    def starts_new_paragraph(self, prev: Line, cur: Line, hanging: bool = False) -> bool:
        """段落切分：缩进变化 + 行距。

        两种缩进惯例都要支持：

        * **正文**（`hanging=False`）：段首行缩进约 12pt，续行回到左边距。
          所以「当前行比上一行更靠右」= 新段落。
        * **参考文献/索引**（`hanging=True`）：条目首行在左边距，续行缩进约 8.4pt。
          此时方向相反：「当前行比上一行更靠左」= 新条目。

        不能用「离左边距的绝对距离」判断，否则其中一种惯例必然被切碎。
        也不用「换页」当断点，否则跨页段落会被切成两段。
        """
        if tn.ends_with_break_hyphen(prev.text):
            return False
        delta = cur.x0 - prev.x0
        if cur.page == prev.page:
            if cur.y0 - prev.y1 > 0.62 * self.paragraph_lead:
                return True
            if hanging:
                if delta < -3.0:
                    return True
            elif delta > 3.0:
                return True
        # 段落末行明显短，下一行必是新段落（缩进未变的悬挂式条目也靠这条兜底）
        if abs(delta) < 3.0 and prev.x1 < self.right_margin - 5.0 * cur.size:
            return True
        return False

    def join_lines(self, lines: list[Line], marked: dict[int, str] | None = None,
                   first_has_marker: bool = False) -> str:
        pieces: list[str] = []
        for index, line in enumerate(lines):
            text = (marked or {}).get(id(line), line.text)
            if first_has_marker and index == 0:
                text = re.sub(r"^\d{1,3}\s*", "", text)
            if not pieces:
                pieces.append(text)
                continue
            prev = pieces[-1]
            if tn.ends_with_break_hyphen(prev):
                # 断词：交给词表判定
                tail_match = re.match(r"^([A-Za-z\u00c0-\u024f]+)", text)
                if tail_match:
                    head_word = re.search(r"([A-Za-z\u00c0-\u024f]+-)$", prev)
                    if head_word:
                        joined = self.dehyphenator.resolve(head_word.group(1), tail_match.group(1))
                        prev = prev[: head_word.start()] + joined + text[tail_match.end():]
                        pieces[-1] = prev
                        continue
            if tn.ends_with_dash(prev):
                pieces[-1] = prev + text
                continue
            pieces.append(text)
        text = " ".join(pieces)
        text = tn.collapse_spaces(text)
        return text

    @property
    def left_margin(self) -> float:
        if not hasattr(self, "_left"):
            xs = []
            for lines in self.pages:
                for line in lines:
                    if abs(line.size - self.body_size) < 0.6:
                        xs.append(round(line.x0, 1))
            self._left = min(xs) if xs else 24.0
        return self._left

    @property
    def right_margin(self) -> float:
        if not hasattr(self, "_right"):
            xs = []
            for lines in self.pages:
                for line in lines:
                    if abs(line.size - self.body_size) < 0.6:
                        xs.append(round(line.x1, 1))
            self._right = max(xs) if xs else 336.0
        return self._right

    # -- 标题 -----------------------------------------------------------
    def locate_headings(self, part: dict, part_lines: list[Line]) -> tuple[list[dict], dict]:
        found: list[dict] = []
        used: set[int] = set()

        def normalize(text: str) -> str:
            text = re.sub(r"\s+", "", text).lower()
            return re.sub(r"[\u2013\u2014\u2010\u2011-]", "", text)

        for entry in part["entries"]:
            target = normalize(entry["title"])
            if not target:
                continue
            for line in part_lines:
                if id(line) in used or line.page != entry["page"]:
                    continue
                candidate = normalize(line.text)
                if not candidate:
                    continue
                if candidate == target or (len(target) > 12 and candidate.startswith(target[:14])):
                    used.add(id(line))
                    found.append({"line_id": id(line), "level": entry["level"], "title": entry["title"]})
                    break
        return found, {f["line_id"]: f for f in found}

    # -- 插图 -----------------------------------------------------------
    def figure_regions_by_page(self, by_page: dict[int, list[Line]], consumed: set[int],
                               captions: set[int]) -> dict[int, list[dict]]:
        result: dict[int, list[dict]] = defaultdict(list)
        for page_no, page_lines in sorted(by_page.items()):
            page = self.doc[page_no - 1]
            rects = page_vector_rects(page)
            if not rects:
                continue
            clusters = [
                rect for rect in cluster_rects(rects)
                if rect.get_area() >= 1500 and rect.width >= 40 and rect.height >= 25
            ]
            if not clusters:
                continue
            caption_lines = [l for l in page_lines if id(l) in captions]
            for cluster in clusters:
                rect = clip_figure(page, cluster, page_lines, captions, self.body_size)
                if rect is None:
                    continue
                caption = None
                for line in sorted(caption_lines, key=lambda l: abs(l.y0 - rect.y1)):
                    if -80 < line.y0 - rect.y1 < 80 or -80 < rect.y0 - line.y1 < 80:
                        caption = line
                        break
                figure_id = f"{page_no}-{int(cluster.y0)}"
                file_name = f"fig-p{page_no:03d}-{int(cluster.y0)}.png"
                spec = {
                    "id": figure_id,
                    "page": page_no,
                    "rect": [round(v, 1) for v in rect],
                    "file": file_name,
                    "caption_line": id(caption) if caption is not None else 0,
                    "caption": caption.text if caption is not None else "",
                }
                result[page_no].append(spec)
                self.figures.append(spec)
                self.stats["figures"] += 1
        return result

    def figure_block(self, figure: dict, slug: str, caption_text: str) -> Block:
        return Block(
            "figure",
            caption_text,
            figure["page"],
            slug,
            image=f"images/{figure['file']}",
            no_translate=True,
            meta={"alt": re.sub(r"[*`]", "", caption_text)[:120], "fig": figure["id"]},
        )

    def render_figures(self, target_dir: pathlib.Path) -> list[str]:
        target_dir.mkdir(parents=True, exist_ok=True)
        written = []
        for figure in self.figures:
            page = self.doc[figure["page"] - 1]
            rect = fitz.Rect(*figure["rect"])
            pixmap = page.get_pixmap(clip=rect, matrix=fitz.Matrix(self.zoom, self.zoom))
            path = target_dir / figure["file"]
            pixmap.save(path)
            figure["rendered"] = str(path.name)
            written.append(str(path))
        return written


class GlyphMap:
    """字形还原表。

    同一基础字体名（如 MTEX）在文档里可能有多个子集，各自的码位含义不同，
    所以查找顺序是「按页 → 全局 → `*` 通配」。
    """

    def __init__(self, payload: dict | None = None):
        payload = payload or {}
        self.fonts: dict[str, dict[str, str]] = payload.get("fonts") or {}
        self.pages: dict[int, dict[str, dict[str, str]]] = {
            int(key): value for key, value in (payload.get("pages") or {}).items()
        }
        self.star: dict[str, str] = self.fonts.get("*") or {}

    def lookup(self, page: int, font: str, char: str) -> str | None:
        page_fonts = self.pages.get(page)
        if page_fonts:
            value = (page_fonts.get(font) or {}).get(char)
            if value:
                return value
        value = (self.fonts.get(font) or {}).get(char) or self.star.get(char)
        return value or None

    def render(self, page: int, font: str, char: str) -> str:
        value = self.lookup(page, font, char)
        return value if value else tn.placeholderize(char)


def load_glyphmap(root: pathlib.Path) -> GlyphMap:
    path = root / "data" / "glyphmap.json"
    if not path.exists():
        return GlyphMap()
    return GlyphMap(json.loads(path.read_text(encoding="utf-8")))