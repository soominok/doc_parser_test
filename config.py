"""
config.py
─────────────────────────────────────────────────────────────────────────────
전체 파이프라인의 설정을 중앙 집중식으로 관리한다.
- 각 파서·프로세서는 이 모듈에서 설정을 읽어 동작한다.
- 환경변수(.env)를 우선으로 사용하고, 없으면 기본값을 사용한다.
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

# .env 파일 로드 (프로젝트 루트에 위치)
load_dotenv()


# ─────────────────────────────────────────────
# LLM 관련 설정
# ─────────────────────────────────────────────
@dataclass
class LLMConfig:
    """LLM 활성화 여부 및 API 키·모델 설정"""

    # "openai" | "anthropic" | "none"
    provider: Literal["openai", "anthropic", "none"] = os.getenv(
        "LLM_PROVIDER", "none"
    )

    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4o")          # Vision 지원 모델

    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    anthropic_model: str = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-6")

    # LLM을 호출하는 상황을 결정하는 임계값
    # - heading 판별 신뢰도가 이 값 미만이면 LLM 판단 요청
    heading_confidence_threshold: float = 0.6

    # - 표 셀이 병합되어 있거나 구조가 복잡할 때 LLM으로 재구성
    complex_table_llm: bool = True

    # Vision 입력 해상도 (DPI)
    page_render_dpi: int = 150


# ─────────────────────────────────────────────
# 헤딩 감지 관련 설정
# ─────────────────────────────────────────────
@dataclass
class HeadingConfig:
    """폰트 분석 기반 Heading 감지 설정"""

    # 폰트 크기 클러스터링 방법: "percentile" | "kmeans"
    clustering_method: Literal["percentile", "kmeans"] = "percentile"

    # 본문(body) 폰트 크기 기준: 상위 N%까지를 body로 간주
    body_percentile: float = 60.0

    # Heading 판별에 사용하는 가중치 (합 = 1.0)
    weight_font_size: float = 0.50    # 폰트 크기
    weight_bold: float = 0.25         # 굵기
    weight_position: float = 0.15     # 페이지 내 Y위치 (상단일수록 가중)
    weight_length: float = 0.10       # 텍스트 길이 (짧을수록 heading 가능성)

    # Heading으로 판별하는 최소 신뢰도 (0~1)
    min_confidence: float = 0.55

    # 최대 heading 레벨 (H1 ~ H{max_level})
    max_level: int = 4

    # 한 줄에 단어가 몇 개 이하면 heading 후보로 볼 것인가
    max_words_for_heading: int = 20

    # heading 후보 텍스트의 최대 문자 수
    max_chars_for_heading: int = 200


# ─────────────────────────────────────────────
# 표(Table) 관련 설정
# ─────────────────────────────────────────────
@dataclass
class TableConfig:
    """표 추출 및 Markdown 변환 설정"""

    # pdfplumber 표 감지 설정
    # snap_tolerance: 셀 경계선 허용 오차 (pt)
    snap_tolerance: int = 5
    join_tolerance: int = 3
    edge_min_length: int = 3

    # 표 셀 내 줄바꿈을 <br> 로 변환할지 여부
    newline_to_br: bool = False

    # 빈 셀을 채울 기본값
    empty_cell_placeholder: str = ""

    # Markdown 표 정렬: "left" | "center" | "right"
    default_alignment: Literal["left", "center", "right"] = "left"

    # 표 최소 행 수 (이보다 작으면 표로 인식하지 않음)
    min_rows: int = 2

    # 표 최소 열 수
    min_cols: int = 2


# ─────────────────────────────────────────────
# 파서 관련 설정
# ─────────────────────────────────────────────
@dataclass
class ParserConfig:
    """파서별 동작 옵션"""

    # Docling: GPU 가속 사용 여부 (CUDA 환경에서만 True)
    docling_use_gpu: bool = False

    # Docling: OCR 수행 여부 (스캔 PDF의 경우 True)
    docling_ocr_enabled: bool = True

    # PyMuPDF: 텍스트 블록 추출 시 이미지 포함 여부
    pymupdf_include_images: bool = False

    # pdfplumber: 페이지 단위 처리 시 최대 페이지 수 (None = 전체)
    max_pages: int | None = None


# ─────────────────────────────────────────────
# 출력 관련 설정
# ─────────────────────────────────────────────
@dataclass
class OutputConfig:
    """Markdown 출력 설정"""

    # 출력 디렉토리 (None이면 입력 PDF와 같은 위치)
    output_dir: Path | None = None

    # 파일명 접미사 (예: "문서.pdf" → "문서_parsed.md")
    suffix: str = "_parsed"

    # Markdown 헤딩 앞뒤로 빈 줄 추가 여부
    heading_blank_lines: bool = True

    # 표 앞뒤로 빈 줄 추가 여부
    table_blank_lines: bool = True

    # 연속된 빈 줄을 최대 몇 줄로 압축할지
    max_blank_lines: int = 2

    # 중간 결과물(JSON) 저장 여부 (디버깅용)
    save_intermediate: bool = False
    intermediate_dir: Path = Path("./debug_output")


# ─────────────────────────────────────────────
# 전체 설정 컨테이너
# ─────────────────────────────────────────────
@dataclass
class Config:
    """모든 설정을 묶는 루트 설정 클래스"""

    llm: LLMConfig = field(default_factory=LLMConfig)
    heading: HeadingConfig = field(default_factory=HeadingConfig)
    table: TableConfig = field(default_factory=TableConfig)
    parser: ParserConfig = field(default_factory=ParserConfig)
    output: OutputConfig = field(default_factory=OutputConfig)

    @property
    def llm_enabled(self) -> bool:
        """LLM 기능이 활성화되어 있는지 확인"""
        if self.llm.provider == "none":
            return False
        if self.llm.provider == "openai" and not self.llm.openai_api_key:
            return False
        if self.llm.provider == "anthropic" and not self.llm.anthropic_api_key:
            return False
        return True


# 전역 기본 설정 인스턴스 (모듈 전체에서 공유)
DEFAULT_CONFIG = Config()
