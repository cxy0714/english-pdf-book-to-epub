"""读取并校验每本书的配置。

目录约定（以 books/putting-auction-theory-to-work 为例）：

    config/mt.example.json          全局翻译接口模板（入库）
    config/mt.local.json            全局翻译接口本地配置，含 key（不入库）
    config/glossary.json            全局术语表：跨书通用（入库，可选）
    books/<slug>/book.json          书的基本信息与版面参数（入库）
    books/<slug>/parts.json         篇章切分（入库；由 tools/plan_parts.py 生成后人工确认）
    books/<slug>/glossary.json      本书术语表：定译 / 强制保留原文（入库，可选）
    books/<slug>/mt.local.json      本书专属的接口覆盖项，含 key（不入库）
    books/<slug>/source/*.pdf       源 PDF（不入库）
    books/<slug>/sources/en/*.md    抽取出的英文底本（不入库）
    books/<slug>/sources/zh/*.md    译文（不入库）
    books/<slug>/build/             中间产物：blocks.json、图、公式页、MT 缓存（不入库）
    books/<slug>/out/               交付物：双语 MD、EPUB（不入库）
    books/<slug>/qa/                质量报告（不入库）

翻译接口配置的生效顺序（后者覆盖前者）：
    内置默认值 → config/mt.example.json → config/mt.local.json
    → books/<slug>/mt.local.json → 环境变量（仅补 api_key）→ 命令行 --model
"""

from __future__ import annotations

import json
import os
import pathlib
from dataclasses import dataclass, field
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
BOOKS_DIR = REPO_ROOT / "books"
CONFIG_DIR = REPO_ROOT / "config"

DEFAULT_MT: dict[str, Any] = {
    "base_url": "https://moyu.cn/v1",
    "model": "deepseek-v4.1-flash",
    "api_key": "",
    "api_key_env": ["MOYU_API_KEY", "MT_API_KEY"],
    "temperature": 0.15,
    "max_tokens": 8192,
    "timeout": 600,
    "retries": 4,
    "concurrency": 3,
    # 批大小按【译文预算】算，而不是按输入块数
    "target_output_chars": 3200,
    # 单次请求的输入字符上限。这不是模型上下文的天花板，而是「别发出荒唐请求」的
    # 保险丝：真正的瓶颈在**返回**长度（max_tokens），输入给长一些反而更划算——
    # 术语表、大纲、前后文在每个请求里都要重复一遍，请求越少总输入越小、跨批断点越少。
    # 24000 太紧，会把长章切碎，总花费反而上升。
    "max_input_chars": 60000,
    # 每批附带多少已译前文 / 待译后文，用来维持跨块一致性
    "context_prev_blocks": 8,
    "context_next_blocks": 2,
    # 前文语境里带上「已经译好的中文」，让模型照着自己的用词往下译。开启后本篇
    # 各批会按顺序翻译（并发降为 1），否则后一批看不到前一批定下的中文。
    "context_prev_zh": True,
    "glossary_in_prompt": True,
    "chapter_brief": True,
}

DEFAULT_LAYOUT: dict[str, Any] = {
    "body_size": 9.7,
    "footnote_size": 7.5,
    "marker_size": 5.2,
    "caption_sizes": [8.0, 8.5],
    "table_size": 8.0,
    "ref_sizes": [5.2, 6.8],
    "bullet_fonts": ["LCIRCLE10", "LBOX", "LDINGBAT", "LSYMBOL", "LNCIRCLE", "ZapfDingbats", "Wingdings"],
    "note_area_min_y_ratio": 0.4,
    "figure_render_zoom": 4.0,
}
DEFAULT_STYLE_FONTS: dict[str, Any] = {
    "bold": ["Utopia-Semibold", "Utopia-Bold", "Utopia-SemiboldItalic"],
    "italic": ["Utopia-Italic", "Utopia-SemiboldItalic"],
    "math_prefixes": ["MTEX", "MTSY", "RMTMI", "MSAM", "MSBM", "EUSM", "EUM", "CMSY", "CMMI", "CMEX"],
}


class ConfigError(RuntimeError):
    pass


@dataclass
class Book:
    """一本待处理的书。"""

    dir: pathlib.Path
    data: dict[str, Any]
    parts: list[dict[str, Any]] = field(default_factory=list)

    # --- 基本信息 -------------------------------------------------------
    @property
    def slug(self) -> str:
        return self.data["slug"]

    @property
    def title_en(self) -> str:
        return self.data.get("title_en", self.slug)

    @property
    def title_zh(self) -> str:
        return self.data.get("title_zh", self.title_en)

    @property
    def author(self) -> str:
        return self.data.get("author", "")

    # --- 版面参数 -------------------------------------------------------
    @property
    def layout(self) -> dict[str, Any]:
        merged = dict(DEFAULT_LAYOUT)
        merged.update(self.data.get("layout") or {})
        return merged

    @property
    def fonts(self) -> dict[str, Any]:
        merged = dict(DEFAULT_STYLE_FONTS)
        for key, value in (self.data.get("style_fonts") or {}).items():
            merged[key] = value
        return merged

    # --- 路径 -----------------------------------------------------------
    @property
    def pdf_path(self) -> pathlib.Path:
        rel = self.data.get("source_pdf")
        if not rel:
            raise ConfigError(f"{self.slug}: book.json 缺少 source_pdf")
        path = (self.dir / rel).resolve()
        return path

    def sub(self, name: str) -> pathlib.Path:
        path = self.dir / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def en_dir(self) -> pathlib.Path:
        return self.sub("sources/en")

    @property
    def zh_dir(self) -> pathlib.Path:
        return self.sub("sources/zh")

    @property
    def build_dir(self) -> pathlib.Path:
        return self.sub("build")

    @property
    def out_dir(self) -> pathlib.Path:
        return self.sub("out")

    @property
    def qa_dir(self) -> pathlib.Path:
        return self.sub("qa")

    @property
    def images_dir(self) -> pathlib.Path:
        return self.sub("out/images")

    def en_md(self, slug: str) -> pathlib.Path:
        return self.en_dir / f"{slug}.en.md"

    def zh_md(self, slug: str) -> pathlib.Path:
        return self.zh_dir / f"{slug}.zh.md"

    # --- 篇章 -----------------------------------------------------------
    def part(self, slug: str) -> dict[str, Any]:
        for part in self.parts:
            if part["slug"] == slug:
                return part
        raise ConfigError(f"{self.slug}: parts.json 中没有 {slug}")

    def content_parts(self) -> list[dict[str, Any]]:
        return [p for p in self.parts if p.get("kind") != "skip"]

    def translated_parts(self) -> list[dict[str, Any]]:
        return [p for p in self.content_parts() if self.zh_md(p["slug"]).exists()]

    # --- 翻译接口 -------------------------------------------------------
    def glossary(self) -> dict[str, Any]:
        """合并全局术语表与本书术语表。"""
        merged: dict[str, Any] = {}
        for path in (CONFIG_DIR / "glossary.json", self.dir / "glossary.json"):
            if path.exists():
                data = _strip_comments(json.loads(path.read_text(encoding="utf-8")))
                for key, value in data.items():
                    if isinstance(value, dict) and isinstance(merged.get(key), dict):
                        merged[key].update(value)
                    else:
                        merged[key] = value
        return merged

    def style_guide(self) -> str:
        """译文风格约定，注入系统提示词。"""
        path = REPO_ROOT / "docs" / "STYLE.zh.md"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def mt_config(self, model: str | None = None) -> dict[str, Any]:
        cfg = dict(DEFAULT_MT)
        for path in (
            CONFIG_DIR / "mt.example.json",
            CONFIG_DIR / "mt.local.json",
            self.dir / "mt.local.json",
        ):
            if path.exists():
                cfg.update(_strip_comments(json.loads(path.read_text(encoding="utf-8"))))
        if not cfg.get("api_key"):
            for env_name in cfg.get("api_key_env") or []:
                if os.environ.get(env_name):
                    cfg["api_key"] = os.environ[env_name]
                    break
        if model:
            cfg["model"] = model
        return cfg


def _strip_comments(data: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in data.items() if not k.startswith("_")}


def load_book(slug_or_path: str | pathlib.Path) -> Book:
    """按 slug 或目录路径载入一本书。"""
    candidate = pathlib.Path(slug_or_path)
    if candidate.is_dir():
        book_dir = candidate.resolve()
    else:
        book_dir = (BOOKS_DIR / str(slug_or_path)).resolve()
    config_path = book_dir / "book.json"
    if not config_path.exists():
        raise ConfigError(f"找不到 {config_path}")
    data = json.loads(config_path.read_text(encoding="utf-8"))
    book = Book(dir=book_dir, data=data)
    parts_path = book_dir / str(data.get("parts_source", "parts.json"))
    if parts_path.exists():
        parts_data = json.loads(parts_path.read_text(encoding="utf-8"))
        book.parts = parts_data["parts"] if isinstance(parts_data, dict) else parts_data
    return book


def list_books() -> list[str]:
    if not BOOKS_DIR.exists():
        return []
    return sorted(p.name for p in BOOKS_DIR.iterdir() if (p / "book.json").exists())


def resolve_book(argument: str | None) -> Book:
    """命令行入口：给定 slug 就用它，否则在只有一本书时自动选中。"""
    if argument:
        return load_book(argument)
    books = list_books()
    if not books:
        raise ConfigError("books/ 下没有找到任何书（缺少 book.json）")
    if len(books) > 1:
        raise ConfigError(f"books/ 下有多本书，请显式指定：{', '.join(books)}")
    return load_book(books[0])