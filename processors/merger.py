"""
processors/merger.py
─────────────────────────────────────────────────────────────────────────────
PyMuPDF, pdfplumber, docling 세 파서의 결과를 하나의 문서 구조로 통합한다.

[통합 전략]
  ┌─────────────┬──────────────────────────────────────────┐
  │ 파서         │ 기여하는 정보                            │
  ├─────────────┼──────────────────────────────────────────┤
  │ PyMuPDF     │ 폰트 크기/굵기 → heading 레벨 결정       │
  │ pdfplumber  │ 정확한 텍스트 + 표 추출                  │
  │ docling     │ 문서 구조(heading/text/table 레이블)      │
  └─────────────┴──────────────────────────────────────────┘

[문서 요소 타입]
  DocumentElement.kind:
    - "heading"  : 헤딩 (level=1~4)
    - "paragraph": 본문 단락
    - "table"    : 표 (Markdown 형식)
    - "list_item": 목록 항목
    - "caption"  : 그림/표 캡션
    - "header"   : 페이지 헤더 (일반적으로 출력 제외)
    - "footer"   : 페이지 푸터 (일반적으로 출력 제외)

[텍스트 정확도 우선순위]
  pdfplumber 텍스트 > docling 텍스트 > PyMuPDF 텍스트
  (같은 위치의 텍스트일 때 pdfplumber를 기준으로 교체)
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from config import Config, DEFAULT_CONFIG
from parsers.pdfplumber_parser import PdfPlumberResult, TextBlock
from parsers.pymupdf_parser import PyMuPDFResult, BlockInfo
from processors.heading_detector import HeadingDecision, HeadingDetector
from processors.table_processor import MarkdownTable, TableProcessor

ElementKind = Literal[
    "heading", "paragraph", "table", "list_item", "caption", "header", "footer"
]


# ─────────────────────────────────────────────
# 데이터 모델
# ─────────────────────────────────────────────

@dataclass
class DocumentElement:
    """
    최종 문서를 구성하는 요소 하나.
    모든 파서의 결과를 통합한 정규화된 형식.
    """

    kind: ElementKind           # 요소 유형
    text: str                   # 최종 텍스트 (표이면 Markdown 표 문자열)
    page_number: int            # 0-based 페이지 번호
    level: int | None = None    # heading이면 1~4, 그 외 None
    confidence: float = 1.0    # heading 판정 신뢰도
    source: str = "merged"     # 텍스트 출처 (어떤 파서에서 왔는가)
    # 페이지 내 정렬 순서 결정에 사용되는 y좌표
    y0: float = 0.0

    @property
    def is_skippable(self) -> bool:
        """페이지 헤더/푸터는 출력 시 보통 제외"""
        return self.kind in ("header", "footer")


@dataclass
class MergedDocument:
    """최종 통합 문서 구조"""

    pdf_path: str
    page_count: int
    elements: list[DocumentElement] = field(default_factory=list)


# ─────────────────────────────────────────────
# 내부 유틸: 텍스트 정규화 및 매칭
# ─────────────────────────────────────────────

def _normalize_text(text: str) -> str:
    """
    두 텍스트를 비교할 때 노이즈(공백, 대소문자)를 제거한 정규화 텍스트 반환.
    완전 일치 대신 느슨한 매칭에 사용.
    """
    return re.sub(r"\s+", "", text.lower())


def _text_similarity(a: str, b: str) -> float:
    """
    두 문자열의 유사도를 0~1로 반환한다 (간단한 문자 겹침 비율).
    pdfplumber 텍스트로 PyMuPDF 텍스트를 교체할 때 올바른 블록인지 확인하는 용도.
    """
    na = _normalize_text(a)
    nb = _normalize_text(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    # 짧은 쪽 기준으로 교집합 문자 비율 계산
    shorter, longer = (na, nb) if len(na) <= len(nb) else (nb, na)
    matches = sum(c in longer for c in shorter)
    return matches / len(longer)


def _find_best_pdfplumber_text(
    pymupdf_text: str,
    pdfplumber_blocks: list[TextBlock],
    similarity_threshold: float = 0.6,
) -> str | None:
    """
    PyMuPDF 블록 텍스트와 가장 유사한 pdfplumber 텍스트 블록을 찾아 반환한다.
    pdfplumber 텍스트가 더 정확하므로, 유사도 임계값 이상이면 대체한다.
    """
    best_score = similarity_threshold
    best_text: str | None = None

    for pb in pdfplumber_blocks:
        score = _text_similarity(pymupdf_text, pb.text)
        if score > best_score:
            best_score = score
            best_text = pb.text

    return best_text


# ─────────────────────────────────────────────
# 메인 병합 클래스
# ─────────────────────────────────────────────

class DocumentMerger:
    """
    세 파서의 결과를 받아 최종 MergedDocument를 생성한다.

    Parameters
    ----------
    pymupdf_result : PyMuPDFResult
        폰트 메타데이터 포함 블록 목록
    pdfplumber_result : PdfPlumberResult
        정확한 텍스트 + 표 목록
    docling_result : DoclingResult | None
        문서 구조 레이아웃 (없으면 폰트 기반만 사용)
    config : Config
        전역 설정
    """

    def __init__(
        self,
        pymupdf_result: PyMuPDFResult,
        pdfplumber_result: PdfPlumberResult,
        docling_result=None,
        config: Config = DEFAULT_CONFIG,
    ):
        self.pymupdf = pymupdf_result
        self.pdfplumber = pdfplumber_result
        self.docling = docling_result
        self.cfg = config

        # ── Heading 감지 ──────────────────────────
        self.heading_detector = HeadingDetector(
            pymupdf_result=pymupdf_result,
            docling_result=docling_result,
            config=config,
        )
        self.heading_decisions: dict[tuple[int, int], HeadingDecision] = {}

        # ── 표 처리 ──────────────────────────────
        self.table_processor = TableProcessor(config)

    def _run_heading_detection(self) -> None:
        """HeadingDetector를 실행하고 (page, block_idx) 키로 인덱싱한다."""
        decisions = self.heading_detector.detect_all()
        for d in decisions:
            key = (d.page_number, d.block_index)
            self.heading_decisions[key] = d

    def _get_docling_label_for_block(
        self,
        page_num: int,
        block_text: str,
    ) -> str | None:
        """
        PyMuPDF 블록과 텍스트가 유사한 docling 요소를 찾아 레이블을 반환한다.
        heading/list_item/caption 등을 구분하는 데 사용한다.
        """
        if self.docling is None:
            return None

        page_elems = self.docling.pages.get(page_num, [])
        for elem in page_elems:
            if _text_similarity(block_text, elem.text) >= 0.65:
                return elem.label

        return None

    def _kind_from_label(self, label: str | None) -> ElementKind:
        """docling 레이블을 ElementKind로 변환"""
        mapping: dict[str, ElementKind] = {
            "title": "heading",
            "section_header": "heading",
            "text": "paragraph",
            "list_item": "list_item",
            "caption": "caption",
            "page_header": "header",
            "page_footer": "footer",
            "figure": "paragraph",
            "formula": "paragraph",
            "code": "paragraph",
            "table": "table",
        }
        return mapping.get(label or "", "paragraph")

    def merge(self) -> MergedDocument:
        """
        모든 파서 결과를 통합하여 MergedDocument를 생성한다.

        처리 순서:
          1. Heading 감지 실행
          2. 표 처리 (pdfplumber 우선, docling 보완)
          3. 페이지별 블록 순회:
             a. 표 위치에 표 요소 삽입
             b. 텍스트 블록은 heading/paragraph/list_item 분류
             c. 텍스트 정확도 개선 (pdfplumber 텍스트로 교체)
          4. 페이지 헤더/푸터 필터링
        """
        # ── 1. Heading 감지 ────────────────────────
        self._run_heading_detection()

        # ── 2. 표 처리 ─────────────────────────────
        plumber_tables = self.table_processor.process_pdfplumber_tables(
            self.pdfplumber.tables
        )
        docling_tables = {}
        if self.docling:
            docling_tables = self.table_processor.process_docling_tables(
                self.docling.elements
            )
        merged_tables = self.table_processor.merge_tables(plumber_tables, docling_tables)

        # ── 3. 문서 요소 조립 ──────────────────────
        document = MergedDocument(
            pdf_path=self.pymupdf.pdf_path,
            page_count=self.pymupdf.page_count,
        )

        for page_num in range(self.pymupdf.page_count):
            page_blocks = self.pymupdf.pages.get(page_num, [])
            page_tables = merged_tables.get(page_num, [])
            pdfplumber_page_blocks = self.pdfplumber.text_blocks.get(page_num, [])

            # 표와 텍스트 블록을 y0 기준으로 정렬하여 삽입 순서 결정
            # (표를 "가상 블록"으로 변환하여 함께 정렬)
            table_map: dict[float, MarkdownTable] = {t.y0: t for t in page_tables}
            used_table_y0s: set[float] = set()

            for block in page_blocks:
                block_y0 = block.y0

                # ── 표 삽입 체크: 현재 블록 위치 이전에 아직 삽입 안 된 표가 있으면 먼저 삽입
                for table_y0 in sorted(table_map.keys()):
                    if table_y0 < block_y0 and table_y0 not in used_table_y0s:
                        table = table_map[table_y0]
                        document.elements.append(DocumentElement(
                            kind="table",
                            text=table.markdown,
                            page_number=page_num,
                            source=table.source,
                            y0=table_y0,
                        ))
                        used_table_y0s.add(table_y0)

                # ── 현재 텍스트 블록 처리 ──────────
                text = block.text.strip()
                if not text:
                    continue

                # pdfplumber 텍스트로 교체 시도 (정확도 향상)
                better_text = _find_best_pdfplumber_text(text, pdfplumber_page_blocks)
                final_text = better_text if better_text else text

                # heading 판정 결과 조회
                decision = self.heading_decisions.get((page_num, block.block_index))

                if decision and decision.is_heading:
                    document.elements.append(DocumentElement(
                        kind="heading",
                        text=final_text,
                        page_number=page_num,
                        level=decision.level,
                        confidence=decision.confidence,
                        source=decision.source,
                        y0=block_y0,
                    ))
                else:
                    # docling 레이블로 요소 유형 정밀 분류
                    docling_label = self._get_docling_label_for_block(page_num, text)
                    kind = self._kind_from_label(docling_label)

                    # heading으로 잘못 분류된 경우 paragraph로 복원
                    if kind == "heading" and not (decision and decision.is_heading):
                        kind = "paragraph"

                    document.elements.append(DocumentElement(
                        kind=kind,
                        text=final_text,
                        page_number=page_num,
                        source="pdfplumber" if better_text else "pymupdf",
                        y0=block_y0,
                    ))

            # 페이지 끝에 남은 표 삽입
            for table_y0 in sorted(table_map.keys()):
                if table_y0 not in used_table_y0s:
                    table = table_map[table_y0]
                    document.elements.append(DocumentElement(
                        kind="table",
                        text=table.markdown,
                        page_number=page_num,
                        source=table.source,
                        y0=table_y0,
                    ))

        return document
