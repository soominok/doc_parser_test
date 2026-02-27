"""
Multi-format law/regulation document parser.

지원 형식: PDF, DOCX, XLSX/XLS, HWP/HWPX, PPTX/PPT
- 표를 원래 위치에 마크다운 테이블로 삽입
- 제N조 외에 제Ⅻ조, 제A조 등 다양한 조항 번호 지원
- HWP/PPT는 LibreOffice를 통해 변환 후 처리
"""

import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import List, Tuple

import pdfplumber

try:
    from docx import Document as DocxDocument
    HAS_DOCX = True
except ImportError:
    HAS_DOCX = False

try:
    import openpyxl
    HAS_OPENPYXL = True
except ImportError:
    HAS_OPENPYXL = False

try:
    from pptx import Presentation
    HAS_PPTX = True
except ImportError:
    HAS_PPTX = False


# 조항 번호 패턴:
#   - 아라비아 숫자:          1, 2, 10 …
#   - 유니코드 로마 숫자(대): Ⅰ–Ⅻ  (U+2160–U+216B)
#   - 유니코드 로마 숫자(소): ⅰ–ⅻ  (U+2170–U+217B)
#   - 영문자:                 A, B, a, b …
ARTICLE_NUM = r'(?:[0-9]+|[\u2160-\u216B]+|[\u2170-\u217B]+|[A-Za-z]+)'


class MultiFormatLawParser:
    """PDF, DOCX, XLSX, HWP/HWPX, PPTX/PPT 파일을 마크다운으로 변환하는 파서."""

    # ──────────────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────────────

    def parse(self, file_path: str) -> str:
        ext = Path(file_path).suffix.lower()
        print(f"분석 시작: {os.path.basename(file_path)}  [{ext}]")

        if ext == '.pdf':
            return self._parse_pdf(file_path)
        elif ext == '.docx':
            return self._parse_docx(file_path)
        elif ext in ('.xlsx', '.xls'):
            return self._parse_excel(file_path)
        elif ext in ('.hwp', '.hwpx'):
            return self._parse_hwp(file_path)
        elif ext in ('.pptx', '.ppt'):
            return self._parse_ppt(file_path)
        else:
            raise ValueError(f"지원하지 않는 파일 형식: {ext}")

    # ──────────────────────────────────────────────────────────────────────────
    # PDF
    # ──────────────────────────────────────────────────────────────────────────

    def _parse_pdf(self, file_path: str) -> str:
        items = self._extract_pdf_content(file_path)
        return self._render_items(items)

    def _extract_pdf_content(self, file_path: str) -> List[Tuple[str, str]]:
        """페이지 순서와 Y 좌표를 기준으로 텍스트·표를 정렬해 반환."""
        keyed: List[Tuple[float, str, str]] = []  # (sort_key, type, content)

        with pdfplumber.open(file_path) as pdf:
            for page_num, page in enumerate(pdf.pages):

                # ── 표 추출 ────────────────────────────────────────────────
                table_bboxes = []
                for tbl in page.find_tables():
                    rows = tbl.extract()
                    if rows:
                        md = self._table_to_markdown(rows)
                        bbox = tbl.bbox  # (x0, top, x1, bottom)
                        sort_key = page_num * 1_000_000 + bbox[1]
                        keyed.append((sort_key, 'table', md))
                        table_bboxes.append(bbox)

                # ── 텍스트 추출 (표 영역 제외) ─────────────────────────────
                words = page.extract_words(x_tolerance=3, y_tolerance=3)
                line_buckets: dict = {}

                for word in words:
                    # 표 bbox 내부에 속하는 단어는 건너뜀
                    in_table = any(
                        word['x0'] >= bbox[0] - 2
                        and word['x1'] <= bbox[2] + 2
                        and word['top'] >= bbox[1] - 2
                        and word['bottom'] <= bbox[3] + 2
                        for bbox in table_bboxes
                    )
                    if not in_table:
                        # 3pt 단위로 Y를 버킷화하여 같은 줄로 묶음
                        y_key = round(word['top'] / 3) * 3
                        line_buckets.setdefault(y_key, []).append(word)

                for y_key in sorted(line_buckets):
                    ws = sorted(line_buckets[y_key], key=lambda w: w['x0'])
                    line_text = ' '.join(w['text'] for w in ws).strip()
                    if line_text:
                        sort_key = page_num * 1_000_000 + y_key
                        keyed.append((sort_key, 'text', line_text))

        keyed.sort(key=lambda x: x[0])
        return [(ctype, content) for _, ctype, content in keyed]

    # ──────────────────────────────────────────────────────────────────────────
    # DOCX
    # ──────────────────────────────────────────────────────────────────────────

    def _parse_docx(self, file_path: str) -> str:
        if not HAS_DOCX:
            raise ImportError("python-docx를 설치하세요: pip install python-docx")

        doc = DocxDocument(file_path)
        NS = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
        items: List[Tuple[str, str]] = []

        def get_text(el) -> str:
            return ''.join(
                node.text for node in el.iter(f'{{{NS}}}t') if node.text
            ).strip()

        for child in doc.element.body:
            tag = child.tag.split('}')[-1] if '}' in child.tag else child.tag

            if tag == 'p':
                text = get_text(child)
                if text:
                    items.append(('text', text))

            elif tag == 'tbl':
                rows = []
                for tr in child.iter(f'{{{NS}}}tr'):
                    row = [get_text(tc) for tc in tr.iter(f'{{{NS}}}tc')]
                    if any(row):
                        rows.append(row)
                if rows:
                    items.append(('table', self._table_to_markdown(rows)))

        return self._render_items(items)

    # ──────────────────────────────────────────────────────────────────────────
    # Excel
    # ──────────────────────────────────────────────────────────────────────────

    def _parse_excel(self, file_path: str) -> str:
        if not HAS_OPENPYXL:
            raise ImportError("openpyxl을 설치하세요: pip install openpyxl")

        wb = openpyxl.load_workbook(file_path, data_only=True)
        items: List[Tuple[str, str]] = []

        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            items.append(('text', f'# 시트: {sheet_name}'))

            rows = []
            for row in ws.iter_rows(values_only=True):
                cells = [str(c) if c is not None else '' for c in row]
                if any(cells):
                    rows.append(cells)
            if rows:
                items.append(('table', self._table_to_markdown(rows)))

        return self._render_items(items)

    # ──────────────────────────────────────────────────────────────────────────
    # HWP / HWPX  →  LibreOffice → DOCX → 파싱
    # ──────────────────────────────────────────────────────────────────────────

    def _parse_hwp(self, file_path: str) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            self._libreoffice_convert(file_path, 'docx', tmp)
            converted = os.path.join(tmp, Path(file_path).stem + '.docx')
            if not os.path.exists(converted):
                raise FileNotFoundError(
                    f"LibreOffice 변환 실패: {converted} 파일이 생성되지 않았습니다."
                )
            return self._parse_docx(converted)

    # ──────────────────────────────────────────────────────────────────────────
    # PPT / PPTX
    # ──────────────────────────────────────────────────────────────────────────

    def _parse_ppt(self, file_path: str) -> str:
        ext = Path(file_path).suffix.lower()
        # 구형 .ppt 포맷은 LibreOffice로 pptx 변환 후 처리
        if ext == '.ppt':
            with tempfile.TemporaryDirectory() as tmp:
                self._libreoffice_convert(file_path, 'pptx', tmp)
                converted = os.path.join(tmp, Path(file_path).stem + '.pptx')
                return self._parse_pptx(converted)
        return self._parse_pptx(file_path)

    def _parse_pptx(self, file_path: str) -> str:
        if not HAS_PPTX:
            raise ImportError("python-pptx를 설치하세요: pip install python-pptx")

        prs = Presentation(file_path)
        items: List[Tuple[str, str]] = []

        for i, slide in enumerate(prs.slides, 1):
            items.append(('text', f'# 슬라이드 {i}'))

            for shape in slide.shapes:
                if shape.has_text_frame:
                    for para in shape.text_frame.paragraphs:
                        text = ''.join(r.text for r in para.runs).strip()
                        if text:
                            items.append(('text', text))

                if shape.has_table:
                    rows = [
                        [cell.text.strip() for cell in row.cells]
                        for row in shape.table.rows
                    ]
                    if rows:
                        items.append(('table', self._table_to_markdown(rows)))

        return self._render_items(items)

    # ──────────────────────────────────────────────────────────────────────────
    # 공통 유틸리티
    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _libreoffice_convert(src: str, fmt: str, out_dir: str) -> None:
        result = subprocess.run(
            ['libreoffice', '--headless', '--convert-to', fmt,
             '--outdir', out_dir, src],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"LibreOffice 변환 실패 ({fmt}): {result.stderr.strip()}"
            )

    @staticmethod
    def _table_to_markdown(rows: List[List]) -> str:
        """2차원 리스트를 마크다운 테이블 문자열로 변환."""
        if not rows:
            return ""

        def clean(cell) -> str:
            if cell is None:
                return ''
            return str(cell).replace('\n', ' ').replace('|', '\\|').strip()

        cleaned = [[clean(c) for c in row] for row in rows if row]
        if not cleaned:
            return ""

        cols = max(len(r) for r in cleaned)
        cleaned = [r + [''] * (cols - len(r)) for r in cleaned]

        header, *body = cleaned
        sep = ['---'] * cols
        lines = [
            '| ' + ' | '.join(header) + ' |',
            '| ' + ' | '.join(sep)    + ' |',
        ] + ['| ' + ' | '.join(r) + ' |' for r in body]
        return '\n'.join(lines)

    def _render_items(self, items: List[Tuple[str, str]]) -> str:
        """
        텍스트 블록은 _refine_text()로 정제하고,
        표는 원래 위치에 그대로 삽입한다.
        """
        result: List[str] = []
        text_buf: List[str] = []

        def flush():
            if text_buf:
                result.append(self._refine_text('\n'.join(text_buf)))
                text_buf.clear()

        for ctype, content in items:
            if ctype == 'text':
                text_buf.append(content)
            else:  # 'table'
                flush()
                result.append('\n' + content + '\n')

        flush()
        return '\n'.join(result)

    def _refine_text(self, text: str) -> str:
        # ── 1단계: 제어 문자 제거 ────────────────────────────────────────────
        text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)

        # ── 2단계: 소프트 줄바꿈 병합 ────────────────────────────────────────
        # 새 블록 시작 패턴: 원형 숫자, 번호 목록, 조항 번호
        NEW_BLOCK = re.compile(
            rf'^(?:[①-⑳]|\d+[).]|\(\d+\)|'
            rf'제\s?{ARTICLE_NUM}\s?[장절관조])'
        )

        merged: List[str] = []
        for raw in text.split('\n'):
            curr = raw.strip()
            if not curr:
                continue
            if merged and not NEW_BLOCK.match(curr):
                prev = merged[-1]
                # 이전 줄이 문장 종결 부호로 끝나지 않고 헤더도 아니면 합침
                if not re.search(r'[.?!:]$', prev) and not prev.startswith('#'):
                    merged[-1] = prev + ' ' + curr
                    continue
            merged.append(curr)

        # ── 3단계: 계층 헤딩 적용 ────────────────────────────────────────────
        final: List[str] = []
        for line in merged:
            # 장 → H1
            if re.match(rf'^제\s?{ARTICLE_NUM}\s?장', line):
                final.append(f'\n# {line}')

            # 절·관 → H2
            elif re.match(rf'^제\s?{ARTICLE_NUM}\s?[절관]', line):
                final.append(f'\n## {line}')

            # 조항 → H3  (괄호 제목이 있으면 [ ] 형식으로 변환)
            elif re.match(rf'^제\s?{ARTICLE_NUM}\s?조', line):
                formatted = re.sub(
                    rf'^(제\s?{ARTICLE_NUM}\s?조)\s*\((.*?)\)\s*',
                    r'\1 [\2]\n',
                    line
                )
                final.append(f'\n### {formatted}')

            # 원형 숫자·번호 목록 → 줄바꿈 유지
            elif re.match(r'^(?:[①-⑳]|\d+[).]|\(\d+\))', line):
                final.append(line)

            else:
                final.append(line)

        return '\n'.join(final)


# ──────────────────────────────────────────────────────────────────────────────
# 실행 진입점
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    parser = MultiFormatLawParser()

    INPUT_DIR  = 'data/input_files'
    OUTPUT_DIR = 'data/output_mds'

    SUPPORTED = {'.pdf', '.docx', '.xlsx', '.xls', '.hwp', '.hwpx', '.pptx', '.ppt'}

    for root, _, files in os.walk(INPUT_DIR):
        target_files = [f for f in files if Path(f).suffix.lower() in SUPPORTED]
        if not target_files:
            continue

        # 입력 디렉터리 구조를 출력 디렉터리에 그대로 반영
        rel = os.path.relpath(root, INPUT_DIR)
        out_dir = OUTPUT_DIR if rel == '.' else os.path.join(OUTPUT_DIR, rel)
        os.makedirs(out_dir, exist_ok=True)

        for filename in target_files:
            in_path  = os.path.join(root, filename)
            out_path = os.path.join(out_dir, Path(filename).stem + '.md')
            try:
                md = parser.parse(in_path)
                with open(out_path, 'w', encoding='utf-8') as f:
                    f.write(md)
                print(f"완료: {out_path}")
            except Exception as e:
                print(f"실패 ({filename}): {e}")
