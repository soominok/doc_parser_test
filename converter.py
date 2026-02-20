#!/usr/bin/env python3
"""
Document → Markdown Converter
==============================
Hybrid parser combining docling + pymupdf4llm + pdfplumber for PDF,
python-docx for DOCX, and pyhwp for HWP.

Hybrid strategy (PDF):
  ┌─────────────────────────────────────────────────────────────────┐
  │ 1. docling      → document structure, header detection, tables  │
  │ 2. pymupdf4llm  → header hierarchy (#/##/###), table markdown   │
  │ 3. pdfplumber   → accurate text with correct line breaks        │
  │                                                                 │
  │ Merge:                                                          │
  │  · docling element list  as structural backbone                 │
  │  · pymupdf4llm header map to resolve H1/H2/H3 levels           │
  │  · docling table export  (+ duplicate-column cleanup)           │
  │  · pdfplumber text as quality fallback                          │
  └─────────────────────────────────────────────────────────────────┘

Usage:
    python converter.py                      # launch GUI
    python converter.py <file> [output_dir]  # CLI mode
"""

import os
import re
import sys
import logging
import threading
import subprocess
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ── tkinter (optional: GUI only) ────────────────────────────────────────────
try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, scrolledtext
    HAS_TK = True
except ImportError:
    HAS_TK = False

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# UTILITY FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def _norm(s: str) -> str:
    """Normalize text for fuzzy matching."""
    return re.sub(r"\s+", " ", s.strip().lower())


def _sim(a: str, b: str) -> float:
    """Character-level similarity ratio."""
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def clean_md(text: str) -> str:
    """Trim excessive blank lines and trailing whitespace."""
    text = re.sub(r"\n{3,}", "\n\n", text)
    lines = [ln.rstrip() for ln in text.split("\n")]
    return "\n".join(lines).strip() + "\n"


def fix_table_cols(table_md: str) -> str:
    """
    Remove all-empty border columns that pymupdf4llm sometimes adds.

    Problem: pymupdf4llm occasionally produces tables like:
        |  | col1 | col2 |  |
        |  | ---- | ---- |  |
    where the first and last columns are always blank.
    """
    lines = [ln for ln in table_md.strip().split("\n") if ln.strip()]
    if not lines:
        return table_md

    sep_re = re.compile(r"^[-:]+$")

    # Parse rows into cell lists
    parsed: List[Tuple[str, List[str]]] = []
    for line in lines:
        if "|" in line:
            parts = line.split("|")
            cells = [c.strip() for c in parts[1:-1]]
            parsed.append((line, cells))
        else:
            parsed.append((line, []))

    if not parsed:
        return table_md

    max_cols = max(len(cells) for _, cells in parsed)
    if max_cols == 0:
        return table_md

    # Identify columns that are always empty in non-separator rows
    empty_cols: set = set()
    for col_i in range(max_cols):
        data_vals = []
        for _, cells in parsed:
            if not cells:
                continue
            v = cells[col_i] if col_i < len(cells) else ""
            if not sep_re.match(v):
                data_vals.append(v)
        if data_vals and all(v == "" for v in data_vals):
            empty_cols.add(col_i)

    if not empty_cols:
        return table_md

    result: List[str] = []
    for orig, cells in parsed:
        if not cells:
            result.append(orig)
            continue
        kept = [c for i, c in enumerate(cells) if i not in empty_cols]
        if kept:
            result.append("| " + " | ".join(kept) + " |")
    return "\n".join(result)


def list_table_to_md(table: List[List]) -> str:
    """Convert a list-of-lists table (pdfplumber format) to a Markdown table."""
    if not table or not table[0]:
        return ""
    rows: List[str] = []
    for row in table:
        cells = [str(c or "").replace("\n", " ").strip() for c in row]
        rows.append("| " + " | ".join(cells) + " |")
    sep = "| " + " | ".join(["---"] * len(table[0])) + " |"
    rows.insert(1, sep)
    return "\n".join(rows)


def extract_mupdf_headers(md_text: str) -> List[Tuple[str, int]]:
    """Extract [(normalized_text, level)] from pymupdf4llm markdown output."""
    headers: List[Tuple[str, int]] = []
    for line in md_text.split("\n"):
        m = re.match(r"^(#{1,6})\s+(.+)$", line.strip())
        if m:
            headers.append((_norm(m.group(2)), len(m.group(1))))
    return headers


# ═══════════════════════════════════════════════════════════════════════════════
# PDF PARSERS
# ═══════════════════════════════════════════════════════════════════════════════

class DoclingParser:
    """
    docling parser.

    Strengths : header detection, overall document structure, table extraction.
    Weaknesses: no header-level hierarchy, auto line-wrapping in text.
    """

    def parse(self, path: str) -> Optional[Dict[str, Any]]:
        try:
            from docling.document_converter import DocumentConverter  # type: ignore

            conv = DocumentConverter()
            result = conv.convert(path)
            doc = result.document

            elems: List[Dict[str, Any]] = []
            try:
                for item, level in doc.iterate_items():
                    cls = type(item).__name__
                    elem: Dict[str, Any] = {
                        "cls": cls,
                        "level": level,
                        "is_header": any(
                            k in cls for k in ("SectionHeader", "Title")
                        ),
                        "is_table": "Table" in cls,
                        "is_list": "List" in cls,
                        "text": (getattr(item, "text", "") or "").strip(),
                    }
                    if elem["is_table"]:
                        try:
                            elem["table_md"] = item.export_to_markdown()
                        except Exception:
                            elem["table_md"] = ""
                    elems.append(elem)
            except Exception as exc:
                logger.debug("docling iterate_items: %s", exc)

            return {"elements": elems, "markdown": doc.export_to_markdown()}

        except ImportError:
            logger.warning("docling not installed — skipping")
            return None
        except Exception as exc:
            logger.error("docling: %s", exc)
            return None


class MuPDFParser:
    """
    pymupdf4llm parser.

    Strengths : header hierarchy detection (#/##/###), table markdown.
    Weaknesses: may add extra empty border columns in tables.
    """

    def parse(self, path: str) -> Optional[Dict[str, Any]]:
        try:
            import pymupdf4llm  # type: ignore

            chunks = pymupdf4llm.to_markdown(path, page_chunks=True)
            pages = [c.get("text", "") for c in chunks]
            full = "\n\n---\n\n".join(pages)

            # Clean up tables produced by pymupdf4llm
            full = re.sub(
                r"(\|[^\n]+\|\n)+",
                lambda m: fix_table_cols(m.group(0)),
                full,
            )

            return {
                "markdown": full,
                "pages": pages,
                "headers": extract_mupdf_headers(full),
            }

        except ImportError:
            logger.warning("pymupdf4llm not installed — skipping")
            return None
        except Exception as exc:
            logger.error("pymupdf4llm: %s", exc)
            return None


class PlumberParser:
    """
    pdfplumber parser.

    Strengths : accurate text with correct line-breaks, image-embedded text.
    Weaknesses: no header hierarchy whatsoever.
    """

    def parse(self, path: str) -> Optional[Dict[str, Any]]:
        try:
            import pdfplumber  # type: ignore

            pages: List[Dict[str, Any]] = []
            with pdfplumber.open(path) as pdf:
                for i, page in enumerate(pdf.pages):
                    pages.append(
                        {
                            "num": i + 1,
                            "text": page.extract_text() or "",
                            "tables": page.extract_tables() or [],
                        }
                    )
            return {"pages": pages}

        except ImportError:
            logger.warning("pdfplumber not installed — skipping")
            return None
        except Exception as exc:
            logger.error("pdfplumber: %s", exc)
            return None


# ═══════════════════════════════════════════════════════════════════════════════
# HYBRID PDF CONVERTER
# ═══════════════════════════════════════════════════════════════════════════════

class HybridPDFConverter:
    """
    Combines docling + pymupdf4llm + pdfplumber.

    Merge logic
    -----------
    1. Run all enabled parsers.
    2. If docling produced structured elements, use them as the backbone:
         • SectionHeader  → resolve level via pymupdf4llm fuzzy-match,
                            fall back to docling nesting depth.
         • TableItem      → docling export_to_markdown() + col cleanup.
         • TextItem/List  → docling text (pdfplumber alignment is
                            non-trivial without layout coordinates).
    3. If docling has no elements, use pymupdf4llm markdown directly.
    4. If pymupdf4llm also failed, fall back to pdfplumber plain text.
    """

    def __init__(
        self,
        use_docling: bool = True,
        use_mupdf: bool = True,
        use_plumber: bool = True,
    ) -> None:
        self.use_docling = use_docling
        self.use_mupdf = use_mupdf
        self.use_plumber = use_plumber

    # ── public ──────────────────────────────────────────────────────────────

    def convert(self, path: str, cb=None) -> str:
        def log(msg: str, pct: float) -> None:
            logger.info("[%3.0f%%] %s", pct, msg)
            if cb:
                cb(msg, pct)

        docling = mupdf = plumber = None

        if self.use_mupdf:
            log("pymupdf4llm 파싱 중…", 10)
            mupdf = MuPDFParser().parse(path)

        if self.use_docling:
            log("docling 파싱 중…", 30)
            docling = DoclingParser().parse(path)

        if self.use_plumber:
            log("pdfplumber 파싱 중…", 60)
            plumber = PlumberParser().parse(path)

        log("결과 병합 중…", 80)

        if docling and docling["elements"]:
            md = self._merge(docling, mupdf, plumber)
        elif mupdf:
            md = mupdf["markdown"]
        elif docling:
            md = docling["markdown"]
        elif plumber:
            md = self._plumber_to_md(plumber)
        else:
            md = "# 변환 실패\n\n사용 가능한 파서가 없거나 모두 실패하였습니다."

        log("마크다운 정리 중…", 95)
        md = clean_md(md)
        log("완료!", 100)
        return md

    # ── private helpers ──────────────────────────────────────────────────────

    def _merge(
        self,
        docling: Dict,
        mupdf: Optional[Dict],
        plumber: Optional[Dict],
    ) -> str:
        mupdf_headers: List[Tuple[str, int]] = (
            mupdf["headers"] if mupdf else []
        )
        parts: List[str] = []

        for elem in docling["elements"]:
            text: str = elem["text"].strip()

            if elem["is_header"] and text:
                level = self._resolve_level(
                    text, mupdf_headers, elem["level"]
                )
                parts.append(f"{'#' * level} {text}")

            elif elem["is_table"]:
                tmd = elem.get("table_md", "")
                if tmd:
                    tmd = fix_table_cols(tmd)
                    parts.append(tmd)

            elif elem["is_list"] and text:
                depth = max(0, elem["level"] - 1)
                parts.append("  " * depth + f"- {text}")

            elif text:
                parts.append(text)

        if parts:
            return "\n\n".join(parts)

        # Fallback: if docling had elements but none produced text
        if mupdf:
            return mupdf["markdown"]
        return docling.get("markdown", "")

    def _resolve_level(
        self,
        header_text: str,
        mupdf_headers: List[Tuple[str, int]],
        docling_level: int,
    ) -> int:
        """
        Determine heading level:
          1. Exact match in pymupdf4llm header list.
          2. Fuzzy match (≥ 0.80 similarity).
          3. Fall back to docling nesting depth + 1.
        """
        hn = _norm(header_text)

        # Exact
        for mh, lvl in mupdf_headers:
            if mh == hn:
                return lvl

        # Fuzzy
        best_lvl, best_sim = None, 0.0
        for mh, lvl in mupdf_headers:
            s = _sim(hn, mh)
            if s > best_sim:
                best_sim = s
                best_lvl = lvl
        if best_lvl is not None and best_sim >= 0.80:
            return best_lvl

        # Docling depth
        return min(6, max(1, docling_level + 1))

    def _plumber_to_md(self, plumber: Dict) -> str:
        parts: List[str] = []
        for page in plumber["pages"]:
            if page["text"]:
                parts.append(page["text"])
            for tbl in page["tables"]:
                tmd = list_table_to_md(tbl)
                if tmd:
                    parts.append(tmd)
        return "\n\n".join(parts)


# ═══════════════════════════════════════════════════════════════════════════════
# DOCX CONVERTER
# ═══════════════════════════════════════════════════════════════════════════════

class DOCXConverter:
    """
    DOCX → Markdown using python-docx.

    Preserves heading levels (H1–H6), bulleted/numbered lists,
    tables, and detects all-bold paragraphs as informal headings.
    Processes paragraphs and tables in document order.
    """

    def convert(self, path: str, cb=None) -> str:
        def log(msg: str, pct: float) -> None:
            if cb:
                cb(msg, pct)

        log("python-docx 로딩 중…", 10)

        try:
            from docx import Document  # type: ignore
        except ImportError:
            return (
                "# 오류\n\n"
                "python-docx 가 설치되지 않았습니다.\n\n"
                "```\npip install python-docx\n```"
            )

        try:
            doc = Document(path)
        except Exception as exc:
            return f"# 오류\n\nDOCX 파일을 열 수 없습니다: {exc}"

        log("단락·표 변환 중…", 40)

        # Build lookup maps for document-order iteration
        para_map = {p._element: p for p in doc.paragraphs}
        table_map = {t._element: t for t in doc.tables}

        parts: List[str] = []

        for child in doc.element.body:
            tag = (
                child.tag.split("}")[-1]
                if "}" in child.tag
                else child.tag
            )

            if tag == "p":
                para = para_map.get(child)
                if para is None:
                    continue
                text = para.text.strip()
                if not text:
                    continue

                style = para.style.name if para.style else ""

                if style.startswith("Heading"):
                    try:
                        lvl = int(re.search(r"\d+", style).group())  # type: ignore
                    except Exception:
                        lvl = 1
                    parts.append(f"{'#' * min(lvl, 6)} {text}")

                elif style in ("Title", "Subtitle"):
                    parts.append(f"# {text}")

                elif "List" in style:
                    li = para.paragraph_format.left_indent
                    depth = 0
                    if li:
                        try:
                            depth = min(4, int(li.pt / 18))
                        except Exception:
                            depth = 0
                    parts.append("  " * depth + f"- {text}")

                else:
                    # Heuristic: all-bold → informal heading
                    runs = [r for r in para.runs if r.text.strip()]
                    if runs and all(r.bold for r in runs):
                        parts.append(f"**{text}**")
                    else:
                        parts.append(text)

            elif tag == "tbl":
                table = table_map.get(child)
                if table is not None:
                    tmd = self._table_to_md(table)
                    if tmd:
                        parts.append(tmd)

        log("완료!", 100)
        return clean_md("\n\n".join(parts))

    def _table_to_md(self, table) -> str:
        seen: set = set()
        rows: List[List[str]] = []
        for row in table.rows:
            cells = [c.text.replace("\n", " ").strip() for c in row.cells]
            key = tuple(cells)
            if key not in seen:
                seen.add(key)
                rows.append(cells)

        if not rows:
            return ""

        md_rows = ["| " + " | ".join(r) + " |" for r in rows]
        sep = "| " + " | ".join(["---"] * len(rows[0])) + " |"
        md_rows.insert(1, sep)
        return "\n".join(md_rows)


# ═══════════════════════════════════════════════════════════════════════════════
# HWP CONVERTER
# ═══════════════════════════════════════════════════════════════════════════════

class HWPConverter:
    """
    HWP → Markdown using pyhwp (hwp5).

    Tries in order:
      1. hwp5txt CLI (ships with pyhwp package)
      2. pyhwp Python API
    Then applies heuristic heading detection on the extracted plain text.
    """

    def convert(self, path: str, cb=None) -> str:
        def log(msg: str, pct: float) -> None:
            if cb:
                cb(msg, pct)

        log("HWP 파일 변환 중…", 10)

        text = self._try_hwp5txt(path)
        if not text:
            log("pyhwp Python API 시도 중…", 30)
            text = self._try_pyhwp_api(path)

        if not text:
            return (
                "# 변환 실패\n\n"
                "HWP 변환에 실패했습니다.\n\n"
                "다음을 확인하세요:\n"
                "- `pip install pyhwp` 설치 여부\n"
                "- `hwp5txt` 명령어 사용 가능 여부\n"
            )

        log("텍스트 → 마크다운 변환 중…", 70)
        md = self._text_to_md(text)
        log("완료!", 100)
        return clean_md(md)

    # ── parsers ─────────────────────────────────────────────────────────────

    def _try_hwp5txt(self, path: str) -> Optional[str]:
        try:
            r = subprocess.run(
                ["hwp5txt", path],
                capture_output=True,
                text=True,
                timeout=120,
            )
            return r.stdout if r.returncode == 0 and r.stdout else None
        except Exception:
            return None

    def _try_pyhwp_api(self, path: str) -> Optional[str]:
        try:
            # pyhwp exposes a low-level API; use OleFileIO approach
            import olefile  # type: ignore

            if not olefile.isOleFile(path):
                return None

            ole = olefile.OleFileIO(path)
            streams = ole.listdir()

            texts: List[str] = []
            for stream in streams:
                name = "/".join(stream)
                if "BodyText" in name or "Section" in name:
                    try:
                        data = ole.openstream(stream).read()
                        # Very rough extraction: grab printable bytes
                        decoded = data.decode("utf-16-le", errors="ignore")
                        cleaned = re.sub(r"[\x00-\x1f\x7f-\x9f]+", " ", decoded)
                        cleaned = re.sub(r"\s+", " ", cleaned).strip()
                        if cleaned:
                            texts.append(cleaned)
                    except Exception:
                        continue
            ole.close()

            return "\n\n".join(texts) if texts else None
        except ImportError:
            return None
        except Exception as exc:
            logger.error("pyhwp API: %s", exc)
            return None

    # ── post-processing ──────────────────────────────────────────────────────

    def _text_to_md(self, text: str) -> str:
        """
        Heuristic conversion of plain HWP text to Markdown.

        A line is treated as a heading candidate when:
          · it is ≤ 60 characters, AND
          · it does not end with sentence-terminating punctuation, AND
          · it does not look like a date string.
        """
        parts: List[str] = []
        for line in text.split("\n"):
            line = line.rstrip()
            if not line:
                continue
            is_heading = (
                len(line) <= 60
                and line[-1] not in ".,:;)】）"
                and not re.search(r"\d{4}년|\d+월|\d+일", line)
            )
            parts.append(f"## {line}" if is_heading else line)
        return "\n\n".join(parts)


# ═══════════════════════════════════════════════════════════════════════════════
# TKINTER GUI
# ═══════════════════════════════════════════════════════════════════════════════

if HAS_TK:

    class App(tk.Tk):
        """
        Main GUI application built with tkinter (Python standard library).

        Layout
        ------
        ┌───────────────────────────────────────────────┐
        │         문서 → 마크다운 변환기 (title)         │
        ├───────────────────────────────────────────────┤
        │ 입력 파일   [___________________________] [찾기]│
        │ 출력 폴더   [___________________________] [찾기]│
        ├─ 파서 옵션 ────────────────────────────────────┤
        │ [✓] docling   [✓] pymupdf4llm  [✓] pdfplumber │
        │              [ 변환 시작 ]                     │
        │ ══════════════════════════════════  0 %        │
        ├─ 로그 ────────────────────────────────────────┤
        │  scrolled text (log)                          │
        ├─ 미리보기 ─────────────────────────────────────┤
        │  scrolled text (markdown preview)             │
        ├───────────────────────────────────────────────┤
        │ [클립보드 복사]  [파일 열기]  <출력 경로>       │
        └───────────────────────────────────────────────┘
        """

        def __init__(self) -> None:
            super().__init__()
            self.title("문서 → 마크다운 변환기")
            self.geometry("960x720")
            self.resizable(True, True)
            self._last_output: Optional[str] = None
            self._build_ui()

        # ── UI construction ──────────────────────────────────────────────────

        def _build_ui(self) -> None:
            # Title
            ttk.Label(
                self,
                text="문서 → 마크다운 변환기",
                font=("TkDefaultFont", 15, "bold"),
            ).pack(pady=(10, 4))

            # ── Input file ──────────────────────────────────────────────────
            f_in = ttk.LabelFrame(self, text="입력 파일", padding=8)
            f_in.pack(fill=tk.X, padx=12, pady=3)
            self.in_var = tk.StringVar()
            ttk.Entry(f_in, textvariable=self.in_var).pack(
                side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6)
            )
            ttk.Button(f_in, text="찾아보기…", command=self._browse_in).pack(
                side=tk.RIGHT
            )

            # ── Output directory ────────────────────────────────────────────
            f_out = ttk.LabelFrame(self, text="출력 폴더", padding=8)
            f_out.pack(fill=tk.X, padx=12, pady=3)
            self.out_var = tk.StringVar()
            ttk.Entry(f_out, textvariable=self.out_var).pack(
                side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6)
            )
            ttk.Button(f_out, text="찾아보기…", command=self._browse_out).pack(
                side=tk.RIGHT
            )

            # ── Parser options ──────────────────────────────────────────────
            f_opt = ttk.LabelFrame(self, text="파서 옵션 (PDF 전용)", padding=8)
            f_opt.pack(fill=tk.X, padx=12, pady=3)
            self.v_docling = tk.BooleanVar(value=True)
            self.v_mupdf = tk.BooleanVar(value=True)
            self.v_plumber = tk.BooleanVar(value=True)
            ttk.Checkbutton(
                f_opt,
                text="docling  (구조·헤더 인식)",
                variable=self.v_docling,
            ).pack(side=tk.LEFT, padx=14)
            ttk.Checkbutton(
                f_opt,
                text="pymupdf4llm  (헤더 계층·표)",
                variable=self.v_mupdf,
            ).pack(side=tk.LEFT, padx=14)
            ttk.Checkbutton(
                f_opt,
                text="pdfplumber  (텍스트 정확도)",
                variable=self.v_plumber,
            ).pack(side=tk.LEFT, padx=14)

            # ── Convert button + progress ───────────────────────────────────
            f_mid = ttk.Frame(self, padding=(12, 4))
            f_mid.pack(fill=tk.X)
            self.btn = ttk.Button(
                f_mid, text="변환 시작", command=self._start, width=18
            )
            self.btn.pack(pady=(2, 4))
            self.pct_var = tk.DoubleVar(value=0.0)
            self.progress = ttk.Progressbar(
                f_mid, variable=self.pct_var, maximum=100
            )
            self.progress.pack(fill=tk.X, pady=(0, 2))
            self.status_var = tk.StringVar(value="준비")
            ttk.Label(f_mid, textvariable=self.status_var).pack()

            # ── Paned window: log / preview ─────────────────────────────────
            pw = ttk.PanedWindow(self, orient=tk.VERTICAL)
            pw.pack(fill=tk.BOTH, expand=True, padx=12, pady=(4, 0))

            f_log = ttk.LabelFrame(pw, text="로그")
            self.log_txt = scrolledtext.ScrolledText(
                f_log, height=7, wrap=tk.WORD, state="disabled"
            )
            self.log_txt.pack(fill=tk.BOTH, expand=True)
            pw.add(f_log, weight=1)

            f_prev = ttk.LabelFrame(pw, text="미리보기 (마크다운)")
            self.preview_txt = scrolledtext.ScrolledText(
                f_prev, height=12, wrap=tk.WORD, state="disabled"
            )
            self.preview_txt.pack(fill=tk.BOTH, expand=True)
            pw.add(f_prev, weight=2)

            # ── Bottom bar ──────────────────────────────────────────────────
            f_btm = ttk.Frame(self, padding=(12, 4, 12, 10))
            f_btm.pack(fill=tk.X)
            ttk.Button(
                f_btm, text="클립보드에 복사", command=self._copy
            ).pack(side=tk.LEFT)
            ttk.Button(
                f_btm, text="파일 열기", command=self._open_file
            ).pack(side=tk.LEFT, padx=6)
            self.out_label = ttk.Label(f_btm, text="", foreground="gray")
            self.out_label.pack(side=tk.LEFT)

        # ── File dialogs ─────────────────────────────────────────────────────

        def _browse_in(self) -> None:
            path = filedialog.askopenfilename(
                filetypes=[
                    ("지원 문서", "*.pdf *.docx *.hwp"),
                    ("PDF", "*.pdf"),
                    ("Word", "*.docx"),
                    ("HWP", "*.hwp"),
                    ("모든 파일", "*.*"),
                ]
            )
            if path:
                self.in_var.set(path)
                if not self.out_var.get():
                    self.out_var.set(str(Path(path).parent))

        def _browse_out(self) -> None:
            d = filedialog.askdirectory()
            if d:
                self.out_var.set(d)

        # ── Conversion ───────────────────────────────────────────────────────

        def _start(self) -> None:
            src = self.in_var.get().strip()
            out_dir = self.out_var.get().strip()

            if not src:
                messagebox.showerror("오류", "입력 파일을 선택하세요.")
                return
            if not os.path.isfile(src):
                messagebox.showerror(
                    "오류", f"파일을 찾을 수 없습니다:\n{src}"
                )
                return
            if not out_dir:
                out_dir = str(Path(src).parent)
                self.out_var.set(out_dir)
            os.makedirs(out_dir, exist_ok=True)

            self._log_clear()
            self._preview_set("")
            self._set_status("시작 중…", 0)
            self.btn.config(state="disabled")
            self._last_output = None
            self.out_label.config(text="")

            threading.Thread(
                target=self._run,
                args=(src, out_dir),
                daemon=True,
            ).start()

        def _run(self, src: str, out_dir: str) -> None:
            def cb(msg: str, pct: float) -> None:
                self.after(0, lambda m=msg, p=pct: self._set_status(m, p))
                self.after(0, lambda m=msg: self._log_append(m))

            try:
                ext = Path(src).suffix.lower()
                if ext == ".pdf":
                    conv = HybridPDFConverter(
                        use_docling=self.v_docling.get(),
                        use_mupdf=self.v_mupdf.get(),
                        use_plumber=self.v_plumber.get(),
                    )
                    md = conv.convert(src, cb)
                elif ext == ".docx":
                    md = DOCXConverter().convert(src, cb)
                elif ext == ".hwp":
                    md = HWPConverter().convert(src, cb)
                else:
                    raise ValueError(f"지원하지 않는 형식: {ext}")

                out_path = str(Path(out_dir) / (Path(src).stem + ".md"))
                Path(out_path).write_text(md, encoding="utf-8")

                self.after(0, lambda p=out_path, m=md: self._on_done(p, m))

            except Exception as exc:
                err = str(exc)
                self.after(0, lambda e=err: self._on_error(e))

        def _on_done(self, out_path: str, md: str) -> None:
            self._last_output = out_path
            self._log_append(f"\n✓ 저장 완료: {out_path}")
            self._preview_set(md)
            self.out_label.config(text=out_path)
            self.btn.config(state="normal")
            messagebox.showinfo("완료", f"변환이 완료되었습니다!\n\n저장 위치:\n{out_path}")

        def _on_error(self, err: str) -> None:
            self._log_append(f"\n✗ 오류: {err}")
            self._set_status("오류 발생", 0)
            self.btn.config(state="normal")
            messagebox.showerror("오류", f"변환에 실패했습니다:\n\n{err}")

        # ── UI helpers ───────────────────────────────────────────────────────

        def _set_status(self, msg: str, pct: float) -> None:
            self.status_var.set(msg)
            self.pct_var.set(pct)

        def _log_clear(self) -> None:
            self.log_txt.config(state="normal")
            self.log_txt.delete("1.0", tk.END)
            self.log_txt.config(state="disabled")

        def _log_append(self, msg: str) -> None:
            self.log_txt.config(state="normal")
            self.log_txt.insert(tk.END, msg + "\n")
            self.log_txt.see(tk.END)
            self.log_txt.config(state="disabled")

        def _preview_set(self, text: str) -> None:
            self.preview_txt.config(state="normal")
            self.preview_txt.delete("1.0", tk.END)
            self.preview_txt.insert("1.0", text)
            self.preview_txt.config(state="disabled")

        def _copy(self) -> None:
            content = self.preview_txt.get("1.0", tk.END).strip()
            if content:
                self.clipboard_clear()
                self.clipboard_append(content)
                messagebox.showinfo("복사 완료", "클립보드에 복사되었습니다.")
            else:
                messagebox.showwarning("알림", "복사할 내용이 없습니다.")

        def _open_file(self) -> None:
            if not self._last_output:
                messagebox.showwarning("알림", "변환된 파일이 없습니다.")
                return
            try:
                if sys.platform.startswith("linux"):
                    subprocess.Popen(["xdg-open", self._last_output])
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", self._last_output])
                else:
                    os.startfile(self._last_output)  # type: ignore[attr-defined]
            except Exception as exc:
                messagebox.showerror("오류", str(exc))


# ═══════════════════════════════════════════════════════════════════════════════
# CLI MODE
# ═══════════════════════════════════════════════════════════════════════════════

def cli_main() -> None:
    src = sys.argv[1]
    out_dir = sys.argv[2] if len(sys.argv) > 2 else str(Path(src).parent)
    os.makedirs(out_dir, exist_ok=True)

    ext = Path(src).suffix.lower()

    def cb(msg: str, pct: float) -> None:
        print(f"[{pct:3.0f}%] {msg}")

    if ext == ".pdf":
        md = HybridPDFConverter().convert(src, cb)
    elif ext == ".docx":
        md = DOCXConverter().convert(src, cb)
    elif ext == ".hwp":
        md = HWPConverter().convert(src, cb)
    else:
        print(f"지원하지 않는 형식: {ext}", file=sys.stderr)
        sys.exit(1)

    out_path = Path(out_dir) / (Path(src).stem + ".md")
    out_path.write_text(md, encoding="utf-8")
    print(f"\n저장 완료: {out_path}")


# ═══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    if len(sys.argv) > 1:
        cli_main()
        return

    if not HAS_TK:
        print(
            "tkinter를 찾을 수 없습니다.\n"
            "CLI 모드를 사용하세요:\n"
            "  python converter.py <파일경로> [출력폴더]",
            file=sys.stderr,
        )
        sys.exit(1)

    App().mainloop()


if __name__ == "__main__":
    main()
