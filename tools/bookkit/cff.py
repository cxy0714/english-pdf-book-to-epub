"""最小 CFF（Type1C）解析器：取出「字符码 → 字形名」映射。

用途：老式 TeX 生成的 PDF 里，数学字体（MTEX/MTSY/RMTMI/MSAM/MSBM…）往往没有
可用的 ToUnicode，PyMuPDF 只能给出控制字符。但这些字体是 CFF，charset 里保留着
原始字形名（`summationdisplay`、`arrowright`、`bracketleftBigg`…），解析出来配上
名字→Unicode 表，就能把控制字符自动还原成真正的数学符号，一次映射、全书通用。

只实现识别需要的部分：INDEX、DICT、charset、encoding 与标准字符串表。
"""

from __future__ import annotations

import struct

# CFF 标准字符串（SID 0–390）。TeX 字体的字形名几乎都在这之外，
# 走 String INDEX；这里列出前 391 项只是为了能解析内置编码。
STANDARD_STRINGS = [
    ".notdef", "space", "exclam", "quotedbl", "numbersign", "dollar", "percent", "ampersand",
    "quoteright", "parenleft", "parenright", "asterisk", "plus", "comma", "hyphen", "period",
    "slash", "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "colon", "semicolon", "less", "equal", "greater", "question", "at", "A", "B", "C", "D", "E",
    "F", "G", "H", "I", "J", "K", "L", "M", "N", "O", "P", "Q", "R", "S", "T", "U", "V", "W",
    "X", "Y", "Z", "bracketleft", "backslash", "bracketright", "asciicircum", "underscore",
    "quoteleft", "a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k", "l", "m", "n", "o", "p",
    "q", "r", "s", "t", "u", "v", "w", "x", "y", "z", "braceleft", "bar", "braceright",
    "asciitilde", "exclamdown", "cent", "sterling", "fraction", "yen", "florin", "section",
    "currency", "quotesingle", "quotedblleft", "guillemotleft", "guilsinglleft", "guilsinglright",
    "fi", "fl", "endash", "dagger", "daggerdbl", "periodcentered", "paragraph", "bullet",
    "quotesinglbase", "quotedblbase", "quotedblright", "guillemotright", "ellipsis",
    "perthousand", "questiondown", "grave", "acute", "circumflex", "tilde", "macron", "breve",
    "dotaccent", "dieresis", "ring", "cedilla", "hungarumlaut", "ogonek", "caron", "emdash",
    "AE", "ordfeminine", "Lslash", "Oslash", "OE", "ordmasculine", "ae", "dotlessi", "lslash",
    "oslash", "oe", "germandbls", "onesuperior", "logicalnot", "mu", "trademark", "Eth",
    "onehalf", "plusminus", "Thorn", "onequarter", "divide", "brokenbar", "degree", "thorn",
    "threequarters", "twosuperior", "registered", "minus", "eth", "multiply", "threesuperior",
    "copyright", "Aacute", "Acircumflex", "Adieresis", "Agrave", "Aring", "Atilde", "Ccedilla",
    "Eacute", "Ecircumflex", "Edieresis", "Egrave", "Iacute", "Icircumflex", "Idieresis",
    "Igrave", "Ntilde", "Oacute", "Ocircumflex", "Odieresis", "Ograve", "Otilde", "Scaron",
    "Uacute", "Ucircumflex", "Udieresis", "Ugrave", "Yacute", "Ydieresis", "Zcaron", "aacute",
    "acircumflex", "adieresis", "agrave", "aring", "atilde", "ccedilla", "eacute", "ecircumflex",
    "edieresis", "egrave", "iacute", "icircumflex", "idieresis", "igrave", "ntilde", "oacute",
    "ocircumflex", "odieresis", "ograve", "otilde", "scaron", "uacute", "ucircumflex",
    "udieresis", "ugrave", "yacute", "ydieresis", "zcaron",
]


class CFFError(RuntimeError):
    pass


def _read_index(buf: bytes, pos: int) -> tuple[list[bytes], int]:
    if pos + 2 > len(buf):
        raise CFFError("INDEX 越界")
    count = struct.unpack(">H", buf[pos:pos + 2])[0]
    pos += 2
    if count == 0:
        return [], pos
    off_size = buf[pos]
    pos += 1
    offsets = []
    for _ in range(count + 1):
        offsets.append(int.from_bytes(buf[pos:pos + off_size], "big"))
        pos += off_size
    base = pos - 1
    return [buf[base + offsets[i]:base + offsets[i + 1]] for i in range(count)], base + offsets[-1]


def _parse_dict(data: bytes) -> dict[int, list[float]]:
    result: dict[int, list[float]] = {}
    operands: list[float] = []
    pos = 0
    while pos < len(data):
        b0 = data[pos]
        if b0 <= 21:
            if b0 == 12:
                operator = 1200 + data[pos + 1]
                pos += 2
            else:
                operator = b0
                pos += 1
            result[operator] = operands
            operands = []
        elif b0 == 28:
            operands.append(struct.unpack(">h", data[pos + 1:pos + 3])[0])
            pos += 3
        elif b0 == 29:
            operands.append(struct.unpack(">i", data[pos + 1:pos + 5])[0])
            pos += 5
        elif b0 == 30:  # 实数（BCD），只用于极少数字段，跳过即可
            pos += 1
            while pos < len(data):
                byte = data[pos]
                pos += 1
                if (byte >> 4) == 0xF or (byte & 0xF) == 0xF:
                    break
            operands.append(0.0)
        elif 32 <= b0 <= 246:
            operands.append(b0 - 139)
            pos += 1
        elif 247 <= b0 <= 250:
            operands.append((b0 - 247) * 256 + data[pos + 1] + 108)
            pos += 2
        elif 251 <= b0 <= 254:
            operands.append(-(b0 - 251) * 256 - data[pos + 1] - 108)
            pos += 2
        else:
            pos += 1
    return result


def _parse_charset(buf: bytes, offset: int, n_glyphs: int) -> list[int]:
    """返回 GID → SID。GID 0 固定为 .notdef。"""
    sids = [0]
    if offset in (0, 1, 2):
        return sids + [0] * max(0, n_glyphs - 1)
    pos = offset
    fmt = buf[pos]
    pos += 1
    if fmt == 0:
        while len(sids) < n_glyphs:
            sids.append(struct.unpack(">H", buf[pos:pos + 2])[0])
            pos += 2
    elif fmt in (1, 2):
        while len(sids) < n_glyphs:
            first = struct.unpack(">H", buf[pos:pos + 2])[0]
            pos += 2
            if fmt == 1:
                n_left = buf[pos]
                pos += 1
            else:
                n_left = struct.unpack(">H", buf[pos:pos + 2])[0]
                pos += 2
            sids.extend(first + i for i in range(n_left + 1))
    return sids[:n_glyphs]


def _parse_encoding(buf: bytes, offset: int) -> tuple[dict[int, int], dict[int, int]]:
    """解析 CFF 内置 Encoding，返回 (码 → GID, 码 → SID)。

    补充段（supplements）只给 SID，而 TeX 字体的大号定界符多数就在补充段里，
    所以这一段必须解析。
    """
    if offset in (0, 1):
        return {}, {}
    pos = offset
    raw_format = buf[pos]
    fmt = raw_format & 0x7F
    pos += 1
    code_to_gid: dict[int, int] = {}
    if fmt == 0:
        n_codes = buf[pos]
        pos += 1
        for gid in range(1, n_codes + 1):
            code_to_gid[buf[pos]] = gid
            pos += 1
    elif fmt == 1:
        n_ranges = buf[pos]
        pos += 1
        gid = 1
        for _ in range(n_ranges):
            first = buf[pos]
            n_left = buf[pos + 1]
            pos += 2
            for code in range(first, first + n_left + 1):
                code_to_gid[code] = gid
                gid += 1
    code_to_sid: dict[int, int] = {}
    if raw_format & 0x80:
        n_sups = buf[pos]
        pos += 1
        for _ in range(n_sups):
            code_to_sid[buf[pos]] = struct.unpack(">H", buf[pos + 1:pos + 3])[0]
            pos += 3
    return code_to_gid, code_to_sid


def glyph_names(buf: bytes, strict: bool = False) -> dict[int, str]:
    """解析 CFF 程序，返回 字符码 → 字形名。

    ``strict=True`` 时，如果字体没有内置 Encoding（此时码位与字形名无从对应），
    直接抛错而不是猜测——猜测会把正常字符也改错。
    """
    if len(buf) < 4:
        raise CFFError("CFF 太短")
    hdr_size = buf[2]
    _, pos = _read_index(buf, hdr_size)          # Name INDEX
    top_dicts, pos = _read_index(buf, pos)       # Top DICT INDEX
    strings, pos = _read_index(buf, pos)         # String INDEX
    if not top_dicts:
        raise CFFError("没有 Top DICT")
    top = _parse_dict(top_dicts[0])
    charstrings_offset = int(top.get(17, [0])[0])
    if not charstrings_offset:
        raise CFFError("没有 CharStrings")
    charset_offset = int(top.get(15, [0])[0])
    encoding_offset = int(top.get(16, [0])[0])

    def sid_to_name(sid: int) -> str:
        if 0 <= sid < len(STANDARD_STRINGS):
            return STANDARD_STRINGS[sid]
        index = sid - 391
        if 0 <= index < len(strings):
            return strings[index].decode("latin1")
        return f"sid{sid}"

    count = struct.unpack(">H", buf[charstrings_offset:charstrings_offset + 2])[0]
    sids = _parse_charset(buf, charset_offset, count)
    gid_to_name = {gid: sid_to_name(sid) for gid, sid in enumerate(sids)}
    code_to_gid, code_to_sid = _parse_encoding(buf, encoding_offset)

    result: dict[int, str] = {}
    for code, gid in code_to_gid.items():
        result[code] = gid_to_name.get(gid, f"gid{gid}")
    for code, sid in code_to_sid.items():
        result[code] = sid_to_name(sid)
    if result:
        return result
    if strict:
        raise CFFError("字体没有内置 Encoding，无法把码位对应到字形名")
    # 没有内置 Encoding 时退化为「码位 = GID」
    return {gid: name for gid, name in gid_to_name.items() if gid}