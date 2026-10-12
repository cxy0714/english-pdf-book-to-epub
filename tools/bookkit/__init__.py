"""bookkit —— 「英文 PDF 书 → 中英双语 Markdown + EPUB」流水线核心库。

模块划分：
  config       读取 config/mt.*.json、books/<slug>/book.json、parts.json、glossary.json
  textnorm     文本规范化：连字、断词重连、中拉空格、非法字形占位
  pdfextract   PDF 版面解析：行/段落/列表/脚注/表/图/公式 → 结构化 block
  planning     目录（书签）→ 篇章切分
  cff          最小 CFF 解析器：从字体里取回字形名
  glyphnames   字形名 → Unicode 表（TeX / MathTime / 符号字体）
  pairing      英文块与中文块配对、对齐校验
  mt           翻译接口：批切分、提示词、占位符保护、缓存、重试
  epub         手写 EPUB3 打包器（双语 / 纯中文）
"""

__all__ = [
    "config",
    "textnorm",
    "pdfextract",
    "planning",
    "cff",
    "glyphnames",
    "pairing",
    "mt",
    "epub",
]