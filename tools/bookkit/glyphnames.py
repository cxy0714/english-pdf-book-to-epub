"""TeX / 数学字体字形名 → Unicode。

老式 TeX PDF 的数学字形没有 ToUnicode，但字体里保留着原始名字
（`summationdisplay`、`arrowright`、`element`…）。这张表把它们接到真 Unicode。
表是通用的：以后遇到同样由 Textures / dvips / pdfTeX 生成的书可以直接复用，
新名字继续往下加即可。
"""

from __future__ import annotations

GREEK = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ϵ", "epsilon1": "ε",
    "zeta": "ζ", "eta": "η", "theta": "θ", "theta1": "ϑ", "iota": "ι", "kappa": "κ",
    "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ", "pi": "π", "pi1": "ϖ", "rho": "ρ",
    "sigma": "σ", "tau": "τ", "upsilon": "υ", "phi": "ϕ", "phi1": "φ", "chi": "χ",
    "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Gamma1": "Γ", "Delta": "Δ", "Delta1": "Δ", "Theta": "Θ", "Theta1": "Θ",
    "Lambda": "Λ", "Xi": "Ξ", "Pi": "Π", "Pi1": "Π", "Sigma": "Σ", "Upsilon": "Υ",
    "Phi": "Φ", "Psi": "Ψ", "Psi1": "Ψ", "Omega": "Ω", "Omega1": "Ω",
}

OPERATORS = {
    "summationtext": "∑", "summationdisplay": "∑", "sum": "∑",
    "integraltext": "∫", "integraldisplay": "∫", "integral": "∫",
    "producttext": "∏", "productdisplay": "∏",
    "uniontext": "⋃", "uniondisplay": "⋃", "union": "∪", "unionbig": "⋃",
    "intersectiontext": "⋂", "intersectiondisplay": "⋂", "intersection": "∩",
    "radical": "√", "radicalbig": "√",
    "partialdiff": "∂", "nabla": "∇", "infinity": "∞",
}

RELATIONS = {
    "minus": "−", "plus": "+", "equal": "=", "less": "<", "greater": ">",
    "lessequal": "≤", "greaterequal": "≥", "equivalence": "≡", "similar": "∼",
    "approxequal": "≈", "congruent": "≅", "propersubset": "⊂", "propersuperset": "⊃",
    "reflexsubset": "⊆", "reflexsuperset": "⊇", "element": "∈", "notelement": "∉",
    "negationslash": "≠", "notequal": "≠", "propto": "∝", "perpendicular": "⊥",
    "parallel": "∥", "logicalnot": "¬", "therefore": "∴", "because": "∵",
}

ARROWS = {
    "arrowright": "→", "arrowleft": "←", "arrowup": "↑", "arrowdown": "↓",
    "arrowdblright": "⇒", "arrowdblleft": "⇐", "arrowdblboth": "⇔", "arrowboth": "↔",
    "mapstochar": "↦", "mapsto": "↦", "arrowhookleft": "↩", "arrowhookright": "↪",
}

LETTERLIKE = {
    "universal": "∀", "existential": "∃", "emptyset": "∅", "logicaland": "∧",
    "logicalor": "∨", "vector": "\u20d7", "prime": "′", "angle": "∠",
    "triangle": "△", "squaresolid": "■", "square": "□", "bullet": "•",
    "openbullet": "∘", "asteriskmath": "∗", "circlemultiply": "⊗", "circleplus": "⊕",
    "periodcentered": "·", "multiply": "×", "plusminus": "±", "divide": "÷",
    "star": "⋆", "dagger": "†", "daggersquared": "‡", "ellipsis": "…",
    "aleph": "ℵ", "hbar": "ℏ", "ell": "ℓ", "wp": "℘", "Re": "ℜ", "Im": "ℑ",
    "flat": "♭", "natural": "♮", "sharp": "♯", "clubsuit": "♣", "diamondsuit": "♦",
    "heartsuit": "♥", "spadesuit": "♠", "degree": "°", "minute": "′", "second": "″",
}

DELIMITERS = {
    "parenleft": "(", "parenright": ")", "bracketleft": "[", "bracketright": "]",
    "braceleft": "{", "braceright": "}", "bar": "|", "vextendsingle": "|",
    "vextenddouble": "‖", "slash": "/", "backslash": "\\", "floorleft": "⌊",
    "floorright": "⌋", "ceilingleft": "⌈", "ceilingright": "⌉", "angleleft": "⟨",
    "angleright": "⟩", "less": "<", "greater": ">",
}

# 大号定界符：big / Big / bigg / Bigg 各自成组，Unicode 只有一档，统一映射
for _name, _char in (
    ("parenleft", "("), ("parenright", ")"), ("bracketleft", "["), ("bracketright", "]"),
    ("braceleft", "{"), ("braceright", "}"), ("angleleft", "⟨"), ("angleright", "⟩"),
    ("bar", "|"), ("vextendsingle", "|"),
):
    for _suffix in ("big", "Big", "bigg", "Bigg", "bigl", "bigr", "Bigl", "Bigr"):
        DELIMITERS[f"{_name}{_suffix}"] = _char

# 大括号的拼接件：单字符渲染只能取近似
DELIMITERS.update(
    {
        "bracelefttp": "⎧", "braceleftbt": "⎩", "braceleftmid": "⎨", "braceex": "⎪",
        "bracerighttp": "⎫", "bracerightbt": "⎭", "bracerightmid": "⎬",
        "bracehtipdownleft": "⎫", "bracehtipdownright": "⎭",
    }
)

BLACKBOARD = {
    "R": "ℝ", "Z": "ℤ", "Q": "ℚ", "N": "ℕ", "C": "ℂ", "P": "ℙ", "H": "ℍ", "E": "𝔼",
}

TEXT = {
    "space": " ", "period": ".", "comma": ",", "colon": ":", "semicolon": ";",
    "hyphen": "-", "endash": "–", "emdash": "—", "quotesingle": "'",
    "quotedblleft": "“", "quotedblright": "”", "quoteleft": "‘", "quoteright": "’",
    "exclam": "!", "question": "?", "at": "@", "ampersand": "&", "numbersign": "#",
    "dollar": "$", "percent": "%", "asterisk": "*", "underscore": "_", "asciicircum": "^",
    "asciitilde": "~", "grave": "`", "asciiacute": "´", "cedilla": "¸", "dieresis": "¨",
    "dotaccent": "˙", "caron": "ˇ", "breve": "˘", "ring": "˚", "macron": "¯",
    "fi": "fi", "fl": "fl", "ff": "ff", "ffi": "ffi", "ffl": "ffl",
    # FontForge 导出的匿名名（LCIRCLE10 的项目符号）
    "a114": "●", "a1": "●", "a2": "○", "a3": "■", "a4": "□",
}

# TeX 的「加宽重音」字形：单个 Unicode 组合符号只是近似，
# 原文里它们跨在下一行的表达式上方，这里给出最接近的可见字符。
ACCENTS = {
    "hat": "ˆ", "hatwide": "ˆ", "hatwider": "ˆ", "widehat": "ˆ", "circumflex": "ˆ",
    "tilde": "˜", "tildewide": "˜", "widetilde": "˜",
    "bar": "¯", "overline": "¯", "underline": "_", "vectortext": "\u20d7",
    "dot": "˙", "ddot": "¨", "acute": "´", "grave": "`", "check": "ˇ",
    "breve": "˘", "macron": "¯", "ring": "˚",
    "arrowaccent": "\u20d7", "vextendsingle": "|",
}

GLYPH_TO_UNICODE: dict[str, str] = {}
for _table in (GREEK, OPERATORS, RELATIONS, ARROWS, LETTERLIKE, DELIMITERS, BLACKBOARD, TEXT, ACCENTS):
    for _key, _value in _table.items():
        GLYPH_TO_UNICODE.setdefault(_key, _value)

# 单字母字形（数学斜体 A、b、x…）直接取字母本身
for _letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz":
    GLYPH_TO_UNICODE.setdefault(_letter, _letter)
for _digit in "0123456789":
    GLYPH_TO_UNICODE.setdefault(_digit, _digit)


def to_unicode(glyph_name: str) -> str | None:
    """字形名 → Unicode；认不出返回 None。"""
    if not glyph_name:
        return None
    if glyph_name in GLYPH_TO_UNICODE:
        return GLYPH_TO_UNICODE[glyph_name]
    # 去掉常见后缀/前缀再试一次（如 `uni2211`、`arrowright.big`）
    cleaned = glyph_name.split(".")[0]
    if cleaned in GLYPH_TO_UNICODE:
        return GLYPH_TO_UNICODE[cleaned]
    if cleaned.startswith("uni") and len(cleaned) >= 7:
        try:
            return chr(int(cleaned[3:7], 16))
        except ValueError:
            return None
    return None