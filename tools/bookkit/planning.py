"""由 PDF 书签（TOC）推导篇章切分，生成 books/<slug>/parts.json。

parts.json 是人工可编辑的配置：自动生成后应人工确认一遍，之后的抽取决不再猜结构。

字段：
    slug        篇章标识，用于文件名（ch01、front、part1、bibliography …）
    kind        front / chapter / part / library / index / skip
    level       PDF 书签层级，用于换算标题层级
    title_en    篇章标题
    page_start  起始物理页（1 起）
    page_end    结束物理页（含）
    entries     该篇章内部的标题条目 [{level, title, page}]
"""

from __future__ import annotations

import html
import re

FRONT_TITLES = {
    "preface",
    "foreword",
    "acknowledgments",
    "acknowledgements",
    "a note to readers",
    "about the author",
    "editor's preface",
    "prologue",
}
SKIP_TITLES = {
    "half-title",
    "halftitle",
    "title",
    "title page",
    "copyright",
    "table of contents",
    "contents",
    "dedication",
    "cover",
    "frontmatter",
    "front matter",
    "list of figures",
    "list of tables",
}
LIBRARY_TITLES = {"bibliography", "references", "works cited", "further reading"}
INDEX_TITLES = {"author index", "subject index", "index", "name index", "index of names"}

NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7,
    "eighth": 8, "ninth": 9, "tenth": 10,
}

ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10,
         "xi": 11, "xii": 12, "xiii": 13, "xiv": 14, "xv": 15, "xvi": 16, "xvii": 17, "xviii": 18,
         "xix": 19, "xx": 20, "xxi": 21, "xxii": 22, "xxiii": 23, "xxiv": 24}


def clean_title(title: str) -> str:
    """书签标题里的 HTML 实体与异常空白。"""
    text = html.unescape(title or "")
    text = text.replace("\u2010", "-").replace("\u2011", "-")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def roman_to_int(token: str) -> int | None:
    token = token.strip().lower().rstrip(".")
    if token.isdigit():
        return int(token)
    if token in ROMAN:
        return ROMAN[token]
    if token in NUMBER_WORDS:
        return NUMBER_WORDS[token]
    return None


def normalize_chapter_title(title: str) -> str:
    """`CHAPTER TWO: Vickrey–Clarke–Groves Mechanisms` → `Chapter 2: Vickrey–Clarke–Groves Mechanisms`"""
    match = re.match(
        r"^(chapter|part)\s+([ivxlcdm]+|\d+|[a-z]+)\s*[:.\u2013\u2014-]?\s*(.*)$", title, re.I
    )
    if not match:
        return title
    word, numeral, rest = match.group(1), match.group(2), match.group(3)
    number = roman_to_int(numeral)
    label = word.capitalize()
    if number is not None:
        label = f"{label} {number}"
    return f"{label}: {rest}".strip().rstrip(":") if rest else label


def classify(title: str) -> str:
    key = title.strip().lower().rstrip(".")
    if key in SKIP_TITLES:
        return "skip"
    if key in FRONT_TITLES:
        return "front"
    if key in LIBRARY_TITLES:
        return "library"
    if key in INDEX_TITLES:
        return "index"
    if re.match(r"^(chapter|part)\b", key):
        return "chapter"
    return "chapter"


def read_toc(doc) -> list[dict]:
    entries = []
    for level, title, page in doc.get_toc():
        entries.append({"level": int(level), "title": clean_title(title), "page": int(page)})
    return entries


def is_boundary(entry: dict) -> bool:
    """篇章边界：一级书签，以及各级 `CHAPTER …` 书签（本书 PART 内的章是二级）。"""
    if entry["level"] == 1:
        return True
    return bool(re.match(r"^(chapter|part)\b", entry["title"], re.I))


def build_parts(doc, total_pages: int) -> list[dict]:
    toc = [e for e in read_toc(doc) if e["title"]]
    boundaries = [i for i, entry in enumerate(toc) if is_boundary(entry)]
    groups: list[list[dict]] = []
    for position, start in enumerate(boundaries):
        end = boundaries[position + 1] if position + 1 < len(boundaries) else len(toc)
        groups.append(toc[start:end])

    parts: list[dict] = []
    front_buffer: list[dict] = []
    counters: dict[str, int] = {}

    def flush_front() -> None:
        if not front_buffer:
            return
        first, last = front_buffer[0], front_buffer[-1]
        parts.append(
            {
                "slug": "front",
                "kind": "front",
                "level": 1,
                "title_en": "Front Matter",
                "page_start": first["page"],
                "page_end": _group_end(front_buffer, groups),
                "entries": [dict(e) for e in front_buffer],
            }
        )
        front_buffer.clear()

    for group in groups:
        head = group[0]
        kind = classify(head["title"])
        if kind == "skip":
            continue
        if kind == "front":
            front_buffer.extend(group)
            continue
        flush_front()
        if kind == "library":
            slug = "bibliography" if "biblio" in head["title"].lower() else "references-back"
            parts.append(_make(parts, slug, "library", head, group, total_pages))
        elif kind == "index":
            slug = re.sub(r"[^a-z]+", "-", head["title"].lower()).strip("-") or "index"
            parts.append(_make(parts, slug, "index", head, group, total_pages))
        elif re.match(r"^part\b", head["title"], re.I):
            counters["part"] = counters.get("part", 0) + 1
            parts.append(_make(parts, f"part{counters['part']}", "part", head, group, total_pages))
        else:
            counters["chapter"] = counters.get("chapter", 0) + 1
            parts.append(_make(parts, f"ch{counters['chapter']:02d}", "chapter", head, group, total_pages))
    flush_front()
    return parts


def _group_end(group: list[dict], groups: list[list[dict]]) -> int:
    """篇章结束页：下一个篇章的起始页减一（由调用方统一修正）。"""
    return group[-1]["page"]


def _make(parts: list[dict], slug: str, kind: str, head: dict, group: list[dict], total_pages: int) -> dict:
    return {
        "slug": slug,
        "kind": kind,
        "level": head["level"],
        "title_en": normalize_chapter_title(head["title"]),
        "page_start": head["page"],
        "page_end": total_pages,
        "chapter_level": head["level"],
        "entries": [dict(e) for e in group],
    }


def finish_parts(parts: list[dict], total_pages: int) -> list[dict]:
    """按顺序回填 page_end（下一个篇章的起始页 - 1）。"""
    ordered = sorted(parts, key=lambda p: p["page_start"])
    for index, part in enumerate(ordered):
        if index + 1 < len(ordered):
            part["page_end"] = max(part["page_start"], ordered[index + 1]["page_start"] - 1)
        else:
            part["page_end"] = total_pages
    return ordered