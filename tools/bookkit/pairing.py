"""把英文块与中文块配对，供 Markdown / EPUB 排版使用。

抽取阶段写出 ``build/blocks.json``（英文结构），翻译阶段写出 ``build/zh-blocks.json``
（逐块译文，下标一一对应）。排版阶段只做下标配对，不反解 Markdown，所以
块数不一致时能立刻发现而不是悄悄错位。
"""

from __future__ import annotations

import html
import json
import pathlib
import re
from dataclasses import dataclass

from .config import Book, ConfigError

# 非翻译块：中英共用同一行
SHARED_TYPES = {"equation", "row", "figure"}

FOOTNOTE_REF_RE = re.compile(r"\[\^(\d+)\]")


@dataclass
class Pair:
    """一对中英块。zh 为空表示还没有译文。"""

    en: dict
    zh: dict | None

    @property
    def type(self) -> str:
        return self.en["type"]

    @property
    def shared(self) -> bool:
        return self.type in SHARED_TYPES or bool(self.en.get("no_translate"))

    @property
    def en_text(self) -> str:
        return self.en.get("text") or ""

    @property
    def zh_text(self) -> str:
        if self.zh and (self.zh.get("text") or "").strip():
            return self.zh["text"]
        return self.en_text if self.shared else ""

    @property
    def page(self) -> int:
        return int(self.en.get("page") or 0)

    @property
    def level(self) -> int:
        return int(self.en.get("level") or 2)

    @property
    def number(self) -> int:
        return int(self.en.get("number") or 0)

    @property
    def image(self) -> str:
        return self.en.get("image") or ""


@dataclass
class Part:
    slug: str
    title_en: str
    title_zh: str
    kind: str
    page_start: int
    page_end: int
    pairs: list[Pair]

    @property
    def body(self) -> list[Pair]:
        """去掉首块篇章标题后的正文块。"""
        return [pair for pair in self.pairs if pair.type not in ("part",)]

    @property
    def notes(self) -> list[Pair]:
        return [pair for pair in self.pairs if pair.type == "note"]

    def headings(self, max_level: int = 2) -> list[Pair]:
        return [pair for pair in self.pairs if pair.type == "heading" and pair.level <= max_level]


def _load(path: pathlib.Path, book: Book, what: str) -> dict:
    if not path.exists():
        raise ConfigError(f"缺少 {path}，请先运行：{what}")
    return json.loads(path.read_text(encoding="utf-8"))


def load_parts(book: Book) -> list[Part]:
    en = _load(book.build_dir / "blocks.json", book, "python tools/extract_pdf.py")
    zh_path = book.build_dir / "zh-blocks.json"
    zh: dict[str, list[dict]] = {}
    if zh_path.exists():
        payload = json.loads(zh_path.read_text(encoding="utf-8"))
        zh = {part["slug"]: part["blocks"] for part in payload.get("parts", [])}

    parts: list[Part] = []
    for entry in en["parts"]:
        slug = entry["slug"]
        en_blocks = entry["blocks"]
        zh_blocks = zh.get(slug) or []
        pairs: list[Pair] = []
        for index, block in enumerate(en_blocks):
            counterpart = zh_blocks[index] if index < len(zh_blocks) else None
            pairs.append(Pair(en=block, zh=counterpart))
        meta = book.part(slug) if book.parts else {}
        parts.append(
            Part(
                slug=slug,
                title_en=meta.get("title_en") or slug,
                title_zh=(pairs[0].zh_text if pairs and pairs[0].type == "part" else "")
                or meta.get("title_zh")
                or meta.get("title_en")
                or slug,
                kind=meta.get("kind") or "chapter",
                page_start=int(meta.get("page_start") or 0),
                page_end=int(meta.get("page_end") or 0),
                pairs=pairs,
            )
        )
    return parts


def alignment_report(parts: list[Part]) -> list[str]:
    """检查中英块数是否一致，返回问题列表。"""
    problems: list[str] = []
    for part in parts:
        missing_zh = sum(1 for pair in part.pairs if pair.zh is None)
        untranslated = [
            pair for pair in part.pairs if not pair.shared and pair.zh is not None and not pair.zh_text.strip()
        ]
        if missing_zh:
            problems.append(f"{part.slug}: {missing_zh} 块没有对应译文（zh-blocks.json 块数不足）")
        if untranslated:
            problems.append(f"{part.slug}: {len(untranslated)} 块译文为空")
    return problems


def footnote_refs(text: str) -> set[str]:
    return set(FOOTNOTE_REF_RE.findall(text))


def escape(text: str) -> str:
    return html.escape(text, quote=False)