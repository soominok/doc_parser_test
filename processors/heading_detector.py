"""
processors/heading_detector.py
─────────────────────────────────────────────────────────────────────────────
폰트 메타데이터 + docling 레이아웃 정보를 통합하여
각 텍스트 블록의 Heading 여부와 레벨(H1~H4)을 결정한다.

[알고리즘 개요]
  1. PyMuPDF 폰트 크기 히스토그램으로 '본문 폰트 크기' 기준선 산출
  2. 각 텍스트 블록에 대해 4가지 신호를 점수화:
       - 폰트 크기 (큰 폰트 → 점수 높음)
       - 굵기 (bold → 점수 가산)
       - 페이지 내 Y위치 (상단일수록 가산)
       - 텍스트 길이 (짧을수록 heading 가능성)
  3. docling이 section_header/title로 분류한 경우 강제 heading 처리
  4. 최종 점수가 임계값 이상이면 heading으로 판정
  5. 폰트 크기 내림차순으로 H1~H4 레벨 할당

[설계 원칙]
  - docling 결과를 1차 정답으로 사용하되, 폰트 정보로 레벨 보정
  - 폰트 정보만으로도 독립적으로 동작 가능 (docling 없이도 사용 가능)
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Sequence

import numpy as np

from config import Config, DEFAULT_CONFIG
from parsers.pymupdf_parser import BlockInfo, PyMuPDFResult


# ─────────────────────────────────────────────
# 데이터 모델
# ─────────────────────────────────────────────

@dataclass
class HeadingDecision:
    """하나의 텍스트 블록에 대한 Heading 판정 결과"""

    page_number: int
    block_index: int
    text: str
    is_heading: bool
    level: int | None          # heading이면 1~4, 아니면 None
    confidence: float          # 0.0 ~ 1.0
    font_size: float
    is_bold: bool
    source: str = "auto"       # "docling" | "font_heuristic" | "llm"

    @property
    def markdown_prefix(self) -> str:
        """Markdown heading 기호 반환 (예: "## ")"""
        if not self.is_heading or self.level is None:
            return ""
        return "#" * self.level + " "


# ─────────────────────────────────────────────
# 본문 폰트 크기 기준선 계산
# ─────────────────────────────────────────────

def _compute_body_font_size(
    histogram: dict[float, int],
    body_percentile: float = 60.0,
) -> float:
    """
    폰트 크기 히스토그램에서 본문(body) 기준 폰트 크기를 계산한다.

    본문 폰트 크기 = 등장 횟수 기준 상위 body_percentile%까지의 최대값
    → 가장 많이 쓰인 폰트 크기들을 본문으로 간주
    """
    if not histogram:
        return 10.0  # 기본값

    # 전체 문자 수
    total = sum(histogram.values())
    if total == 0:
        return 10.0

    # 등장 횟수 내림차순 정렬
    sorted_sizes = sorted(histogram.items(), key=lambda x: x[1], reverse=True)

    cumulative = 0
    threshold = total * (body_percentile / 100.0)

    for size, count in sorted_sizes:
        cumulative += count
        if cumulative >= threshold:
            return size

    # fallback: 중앙값
    all_sizes = []
    for size, count in histogram.items():
        all_sizes.extend([size] * count)
    return statistics.median(all_sizes)


def _build_size_level_map(
    heading_sizes: list[float],
    max_level: int = 4,
) -> dict[float, int]:
    """
    heading 후보 폰트 크기 목록을 레벨(H1~H{max_level})에 매핑한다.
    큰 폰트 → H1, 작은 폰트 → H2, ...
    """
    unique_sizes = sorted(set(heading_sizes), reverse=True)  # 크기 내림차순
    size_to_level: dict[float, int] = {}

    for i, size in enumerate(unique_sizes):
        level = min(i + 1, max_level)  # 1-based, max_level 초과 방지
        size_to_level[size] = level

    return size_to_level


# ─────────────────────────────────────────────
# 블록 단위 점수 계산
# ─────────────────────────────────────────────

def _score_block(
    block: BlockInfo,
    body_font_size: float,
    page_height: float,
    cfg,
) -> float:
    """
    하나의 블록이 heading일 가능성 점수를 계산한다. (0.0 ~ 1.0)

    Parameters
    ----------
    block : BlockInfo
        PyMuPDF 파서가 추출한 블록
    body_font_size : float
        본문 기준 폰트 크기
    page_height : float
        페이지 높이 (pt) - Y위치 정규화에 사용
    cfg : HeadingConfig
        가중치 설정
    """
    score = 0.0

    # ── 1. 폰트 크기 점수 ──────────────────────
    # 본문보다 몇 pt 큰가?
    size_diff = block.dominant_font_size - body_font_size
    if size_diff <= 0:
        size_score = 0.0
    elif size_diff >= 8:
        size_score = 1.0
    else:
        size_score = size_diff / 8.0  # 0~8pt 차이를 0~1로 정규화
    score += cfg.weight_font_size * size_score

    # ── 2. 굵기 점수 ──────────────────────────
    bold_score = 1.0 if block.is_bold else 0.0
    # 본문과 같은 크기여도 bold이면 점수 가산
    if block.dominant_font_size <= body_font_size and block.is_bold:
        bold_score = 0.5  # 작은 폰트 bold는 부분 점수만
    score += cfg.weight_bold * bold_score

    # ── 3. Y위치 점수 (상단일수록 heading 가능성) ──
    if page_height > 0:
        relative_y = block.y0 / page_height  # 0(최상단) ~ 1(최하단)
        # 상단 30% 이내면 점수 가산
        position_score = max(0.0, 1.0 - (relative_y / 0.3))
    else:
        position_score = 0.0
    score += cfg.weight_position * position_score

    # ── 4. 텍스트 길이 점수 (짧을수록 heading 가능성) ──
    word_count = len(block.text.split())
    char_count = len(block.text.strip())

    if word_count <= 5:
        length_score = 1.0
    elif word_count <= cfg.max_words_for_heading:
        length_score = 1.0 - (word_count - 5) / (cfg.max_words_for_heading - 5)
    else:
        length_score = 0.0

    # 너무 긴 텍스트는 heading 아님
    if char_count > cfg.max_chars_for_heading:
        length_score = 0.0

    score += cfg.weight_length * length_score

    return min(1.0, max(0.0, score))


# ─────────────────────────────────────────────
# 메인 감지기
# ─────────────────────────────────────────────

class HeadingDetector:
    """
    PyMuPDF 결과와 Docling 결과를 통합하여 Heading을 감지한다.

    사용법:
        detector = HeadingDetector(pymupdf_result, docling_result, config)
        decisions = detector.detect_all()
    """

    def __init__(
        self,
        pymupdf_result,          # PyMuPDFResult
        docling_result=None,     # DoclingResult | None
        config: Config = DEFAULT_CONFIG,
    ):
        self.pymupdf = pymupdf_result
        self.docling = docling_result
        self.cfg = config.heading

        # 본문 폰트 크기 계산
        self.body_font_size = _compute_body_font_size(
            pymupdf_result.font_size_histogram,
            self.cfg.body_percentile,
        )

        # docling heading 텍스트 집합 (대소문자 무시 매칭용)
        self._docling_heading_texts: set[str] = set()
        self._docling_heading_levels: dict[str, int] = {}
        if docling_result:
            self._build_docling_heading_index()

    def _build_docling_heading_index(self) -> None:
        """
        docling 결과에서 heading으로 분류된 요소들을
        텍스트 키로 빠르게 조회할 수 있도록 인덱스를 구축한다.
        """
        for elem in self.docling.elements:
            if elem.is_heading and elem.text:
                key = elem.text.strip().lower()
                self._docling_heading_texts.add(key)
                if elem.level is not None:
                    self._docling_heading_levels[key] = elem.level

    def _is_docling_heading(self, text: str) -> tuple[bool, int | None]:
        """docling이 heading으로 분류한 텍스트인지 확인"""
        key = text.strip().lower()
        if key in self._docling_heading_texts:
            level = self._docling_heading_levels.get(key)
            return True, level
        return False, None

    def _collect_heading_sizes(self, decisions_raw: list[tuple[BlockInfo, float]]) -> dict[float, int]:
        """
        heading 후보(점수 임계값 이상)들의 폰트 크기를 수집하여
        레벨 매핑에 사용할 크기 목록을 반환한다.
        """
        heading_sizes = [
            block.dominant_font_size
            for block, score in decisions_raw
            if score >= self.cfg.min_confidence
        ]
        return heading_sizes

    def detect_all(self) -> list[HeadingDecision]:
        """
        모든 페이지의 블록에 대해 Heading 감지를 수행하고
        결과를 페이지·블록 순서대로 반환한다.
        """
        # ── 1단계: 모든 블록에 점수 부여 ──────────
        scored_blocks: list[tuple[BlockInfo, float, str]] = []

        for page_num, blocks in self.pymupdf.pages.items():
            # 페이지 높이: 블록 bbox의 최대 y 값으로 근사
            page_height = max((b.bbox[3] for b in blocks), default=842.0)

            for block in blocks:
                text = block.text.strip()
                if not text:
                    continue

                # docling이 heading으로 분류했는지 먼저 확인
                is_docling_h, docling_level = self._is_docling_heading(text)

                if is_docling_h:
                    # docling 결과 신뢰 → 강제 heading
                    scored_blocks.append((block, 1.0, "docling"))
                else:
                    # 폰트 기반 점수 계산
                    score = _score_block(block, self.body_font_size, page_height, self.cfg)
                    scored_blocks.append((block, score, "font_heuristic"))

        # ── 2단계: heading 후보 폰트 크기 → 레벨 매핑 ──
        heading_sizes = [
            block.dominant_font_size
            for block, score, _ in scored_blocks
            if score >= self.cfg.min_confidence
        ]
        size_to_level = _build_size_level_map(heading_sizes, self.cfg.max_level)

        # ── 3단계: 최종 HeadingDecision 생성 ──────
        decisions: list[HeadingDecision] = []

        for block, score, source in scored_blocks:
            text = block.text.strip()
            is_heading = score >= self.cfg.min_confidence

            level: int | None = None
            if is_heading:
                # docling에서 레벨 제공 → 우선 사용
                _, docling_level = self._is_docling_heading(text)
                if docling_level is not None:
                    level = docling_level
                else:
                    # 폰트 크기 기반 레벨 할당
                    level = size_to_level.get(block.dominant_font_size, self.cfg.max_level)

            decisions.append(HeadingDecision(
                page_number=block.page_number,
                block_index=block.block_index,
                text=text,
                is_heading=is_heading,
                level=level,
                confidence=score,
                font_size=block.dominant_font_size,
                is_bold=block.is_bold,
                source=source,
            ))

        return decisions
