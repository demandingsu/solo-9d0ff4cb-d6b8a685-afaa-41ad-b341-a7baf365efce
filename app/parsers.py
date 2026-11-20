"""用字表 / 字库清单 CSV 解析。"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass

from .errors import DomainError

# 允许的列名（含常见别名）
LIBRARY_ALIASES = {
    "library": ("字库", "字库名", "字库名称", "library", "library_name"),
    "edition": ("版次", "edition", "version"),
    "matrix_no": ("字模号", "字模编号", "matrix_no", "matrix", "code"),
    "glyph_char": ("字", "用字", "字符", "glyph", "char", "character"),
}
VOLUME_ALIASES = {
    "glyph_char": ("字", "用字", "字符", "glyph", "char", "character"),
    "page_no": ("页码", "页", "页号", "page", "page_no", "pageno"),
}


def decode_bytes(data: bytes) -> str:
    """优先 UTF-8（含 BOM），回退 GBK；全空按空文件处理。"""
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise DomainError("文件编码无法识别，请使用 UTF-8 或 GBK 编码的 CSV")


def _normalize(header: str) -> str:
    return header.strip().lstrip("﻿").lower()


def _map_headers(fieldnames, aliases: dict[str, tuple[str, ...]]) -> dict[str, int]:
    normalized = [_normalize(h or "") for h in fieldnames]
    mapping: dict[str, int] = {}
    for key, names in aliases.items():
        for name in names:
            needle = name.lower()
            if needle in normalized:
                mapping[key] = normalized.index(needle)
                break
    return mapping


@dataclass
class LibraryRow:
    library: str
    edition: str
    matrix_no: str
    glyph_char: str


@dataclass
class VolumeRow:
    glyph_char: str


@dataclass
class VolumeTable:
    """分册用字表解析结果。

    chars     按文件顺序去重后的用字清单（交印时逐字解析字模）。
    page_rows (页码, 字) 逐行记录（文件顺序、已去重）；为空表示无页码用字表。
    """

    chars: list[str]
    page_rows: list[tuple[str, str]]

    @property
    def paged(self) -> bool:
        return bool(self.page_rows)

    def page_map(self) -> dict[str, list[str]]:
        """字 -> 该字出现的页码（按文件顺序去重）。"""
        pages: dict[str, list[str]] = {}
        for page_no, ch in self.page_rows:
            bucket = pages.setdefault(ch, [])
            if page_no not in bucket:
                bucket.append(page_no)
        return pages


def parse_library_csv(data: bytes) -> list[LibraryRow]:
    text = decode_bytes(data)
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise DomainError("字库清单为空")
    mapping = _map_headers(header, LIBRARY_ALIASES)
    missing = [k for k in ("library", "edition", "matrix_no", "glyph_char") if k not in mapping]
    if missing:
        raise DomainError(
            "字库清单缺少必需列: " + ", ".join(missing)
            + "；表头需含 字库/版次/字模号/字"
        )
    rows: list[LibraryRow] = []
    seen: set[tuple[str, str, str]] = set()
    for line_no, raw in enumerate(reader, start=2):
        if not raw or all(not c.strip() for c in raw):
            continue
        try:
            row = LibraryRow(
                library=raw[mapping["library"]].strip(),
                edition=raw[mapping["edition"]].strip(),
                matrix_no=raw[mapping["matrix_no"]].strip(),
                glyph_char=raw[mapping["glyph_char"]].strip(),
            )
        except IndexError:
            raise DomainError(f"字库清单第 {line_no} 行列数不足")
        if not (row.library and row.edition and row.matrix_no and row.glyph_char):
            raise DomainError(f"字库清单第 {line_no} 行存在空值")
        if len(row.glyph_char) != 1:
            raise DomainError(f"字库清单第 {line_no} 行的“字”必须是单个字符: {row.glyph_char!r}")
        key = (row.library, row.edition, row.matrix_no)
        if key in seen:
            raise DomainError(
                f"字库清单第 {line_no} 行字模号重复: {row.library}/{row.edition}/{row.matrix_no}"
            )
        seen.add(key)
        rows.append(row)
    if not rows:
        raise DomainError("字库清单没有数据行")
    return rows


def parse_volume_csv(data: bytes) -> VolumeTable:
    """解析分册用字表。

    支持两种格式：
    - 无页码：只有“字”列（或干脆无表头、每行一个字）；
    - 带页码：表头含「页码、字」两列，逐行记录每个字所在页；
      字格内可连写多字（同页），同一 (页码, 字) 重复出现只记一次。
    """
    text = decode_bytes(data)
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise DomainError("用字表为空")
    mapping = _map_headers(header, VOLUME_ALIASES)
    paged = "page_no" in mapping
    if paged and "glyph_char" not in mapping:
        raise DomainError("用字表带“页码”列时还必须包含“字”列")
    if "glyph_char" not in mapping:
        # 兼容无表头、每行一个字的写法
        chars: list[str] = []
        for raw in [header] + list(reader):
            token = (raw[0] if raw else "").strip()
            if token:
                chars.extend(list(token.replace(" ", "")))
        if not chars:
            raise DomainError("用字表为空，或缺少“字/字符”列")
        page_rows: list[tuple[str, str]] = []
    else:
        idx = mapping["glyph_char"]
        page_idx = mapping.get("page_no")
        chars = []
        page_rows = []
        seen_page_row: set[tuple[str, str]] = set()
        for line_no, raw in enumerate(reader, start=2):
            if not raw or all(not c.strip() for c in raw):
                continue
            if idx >= len(raw):
                raise DomainError(f"用字表第 {line_no} 行列数不足")
            token = raw[idx].strip()
            if not token:
                continue
            row_chars = list(token.replace(" ", ""))
            chars.extend(row_chars)
            if paged:
                page_no = (raw[page_idx].strip() if page_idx is not None
                           and page_idx < len(raw) else "")
                if not page_no:
                    raise DomainError(f"用字表第 {line_no} 行页码为空")
                for ch in row_chars:
                    key = (page_no, ch)
                    if key not in seen_page_row:
                        seen_page_row.add(key)
                        page_rows.append((page_no, ch))
    ordered: list[str] = []
    seen: set[str] = set()
    for ch in chars:
        if ch not in seen:
            seen.add(ch)
            ordered.append(ch)
    if not ordered:
        raise DomainError("用字表没有任何字")
    return VolumeTable(chars=ordered, page_rows=page_rows)
