"""
llm/enhancer.py
─────────────────────────────────────────────────────────────────────────────
LLM(GPT-4o 또는 Claude)을 활용하여 파싱 결과의 정확도를 향상시킨다.

[사용 시나리오]
  1. Heading 신뢰도가 낮을 때 (confidence < threshold)
     → 페이지 이미지를 LLM에 전달하여 해당 텍스트가 heading인지 판별
  2. 복잡한 표 (셀 병합, 세로 텍스트)
     → 표 영역 이미지를 LLM에 전달하여 Markdown 표 재구성
  3. 전체 페이지 정제 (선택적)
     → 페이지 전체 텍스트를 LLM에 전달하여 Markdown으로 정제

[구현 방식]
  - OpenAI GPT-4o: 이미지 + 텍스트 멀티모달 입력 지원
  - Anthropic Claude: 이미지 + 텍스트 멀티모달 입력 지원
  - 페이지 이미지는 PyMuPDF로 렌더링하여 base64로 인코딩
  - 비용 절감을 위해 LLM 호출은 필요한 경우만 수행

[비용 주의사항]
  LLM_PROVIDER=none (기본값)으로 설정하면 LLM을 전혀 사용하지 않는다.
  API 비용이 발생하는 기능이므로 .env에서 명시적으로 활성화해야 한다.
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF (페이지 이미지 렌더링)
from loguru import logger
from PIL import Image

from config import Config, DEFAULT_CONFIG
from processors.merger import DocumentElement


# ─────────────────────────────────────────────
# 이미지 유틸
# ─────────────────────────────────────────────

def _render_page_to_base64(
    pdf_path: str,
    page_num: int,
    dpi: int = 150,
    crop_bbox: tuple[float, float, float, float] | None = None,
) -> str:
    """
    PDF 페이지(또는 특정 영역)를 PNG 이미지로 렌더링하고 base64로 인코딩한다.

    Parameters
    ----------
    pdf_path : str
        PDF 파일 경로
    page_num : int
        0-based 페이지 번호
    dpi : int
        렌더링 해상도 (높을수록 선명하나 토큰 비용 증가)
    crop_bbox : tuple | None
        크롭 영역 (x0, y0, x1, y1) in pt. None이면 전체 페이지.

    Returns
    -------
    str
        base64 인코딩된 PNG 이미지 문자열
    """
    doc = fitz.open(pdf_path)
    page = doc[page_num]

    matrix = fitz.Matrix(dpi / 72, dpi / 72)  # 72 DPI = 1x, 150 DPI = ~2x

    if crop_bbox:
        clip_rect = fitz.Rect(*crop_bbox)
        pix = page.get_pixmap(matrix=matrix, clip=clip_rect)
    else:
        pix = page.get_pixmap(matrix=matrix)

    doc.close()

    # PIL Image로 변환 후 base64 인코딩
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    buffer = io.BytesIO()
    img.save(buffer, format="PNG", optimize=True)
    return base64.b64encode(buffer.getvalue()).decode("utf-8")


# ─────────────────────────────────────────────
# OpenAI GPT-4o 클라이언트
# ─────────────────────────────────────────────

def _call_openai(
    prompt: str,
    image_b64: str | None,
    model: str,
    api_key: str,
) -> str:
    """GPT-4o API를 호출하여 응답 텍스트를 반환한다."""
    from openai import OpenAI

    client = OpenAI(api_key=api_key)

    messages_content: list[dict] = []

    if image_b64:
        messages_content.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:image/png;base64,{image_b64}",
                "detail": "high",  # 고해상도 분석
            },
        })

    messages_content.append({"type": "text", "text": prompt})

    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": messages_content}],
        max_tokens=4096,
        temperature=0,  # 결정적 출력을 위해 temperature=0
    )

    return response.choices[0].message.content or ""


# ─────────────────────────────────────────────
# Anthropic Claude 클라이언트
# ─────────────────────────────────────────────

def _call_anthropic(
    prompt: str,
    image_b64: str | None,
    model: str,
    api_key: str,
) -> str:
    """Claude API를 호출하여 응답 텍스트를 반환한다."""
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)

    content: list[dict] = []

    if image_b64:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": image_b64,
            },
        })

    content.append({"type": "text", "text": prompt})

    message = client.messages.create(
        model=model,
        max_tokens=4096,
        messages=[{"role": "user", "content": content}],
    )

    return message.content[0].text if message.content else ""


# ─────────────────────────────────────────────
# LLM 호출 통합 인터페이스
# ─────────────────────────────────────────────

def _call_llm(
    prompt: str,
    image_b64: str | None,
    config: Config,
) -> str:
    """설정된 LLM 제공자에 따라 적절한 클라이언트를 호출한다."""
    if config.llm.provider == "openai":
        return _call_openai(
            prompt=prompt,
            image_b64=image_b64,
            model=config.llm.openai_model,
            api_key=config.llm.openai_api_key,
        )
    elif config.llm.provider == "anthropic":
        return _call_anthropic(
            prompt=prompt,
            image_b64=image_b64,
            model=config.llm.anthropic_model,
            api_key=config.llm.anthropic_api_key,
        )
    else:
        raise ValueError(f"지원하지 않는 LLM 제공자: {config.llm.provider}")


# ─────────────────────────────────────────────
# 기능별 LLM 호출 함수
# ─────────────────────────────────────────────

HEADING_JUDGE_PROMPT = """
다음은 PDF 문서 페이지의 이미지와 해당 페이지에서 추출한 텍스트입니다.
아래 텍스트가 문서의 '제목(heading)'인지, 아니면 '본문(paragraph)'인지 판단해주세요.

텍스트: "{text}"

응답 형식 (JSON만 출력):
{{
  "is_heading": true/false,
  "level": 1~4 또는 null (heading이 아니면 null),
  "reason": "판단 이유 한 줄"
}}
"""


def judge_heading_with_llm(
    text: str,
    pdf_path: str,
    page_num: int,
    bbox: tuple[float, float, float, float] | None,
    config: Config,
) -> dict[str, Any]:
    """
    LLM에 페이지 이미지와 텍스트를 전달하여 heading 여부를 판별한다.

    Returns
    -------
    dict
        {"is_heading": bool, "level": int|None, "reason": str}
    """
    try:
        image_b64 = _render_page_to_base64(
            pdf_path=pdf_path,
            page_num=page_num,
            dpi=config.llm.page_render_dpi,
            crop_bbox=bbox,  # 해당 텍스트 블록 영역만 크롭
        )
        prompt = HEADING_JUDGE_PROMPT.format(text=text)
        response = _call_llm(prompt, image_b64, config)

        # JSON 파싱
        # LLM이 markdown 코드 블록으로 감쌀 수 있으므로 정제
        clean = response.strip()
        if clean.startswith("```"):
            clean = "\n".join(clean.split("\n")[1:-1])

        return json.loads(clean)

    except Exception as e:
        logger.warning(f"LLM heading 판별 실패 (텍스트: {text[:50]}): {e}")
        return {"is_heading": False, "level": None, "reason": "LLM 오류"}


TABLE_RECONSTRUCT_PROMPT = """
다음은 PDF 문서에서 추출한 표 영역 이미지입니다.
이 표를 정확한 Markdown 형식으로 변환해주세요.

규칙:
1. 첫 번째 행을 헤더로 사용
2. 셀 병합이 있으면 내용을 반복하지 말고 가장 적절한 위치에 배치
3. 빈 셀은 빈 문자열로 처리
4. 셀 내 줄바꿈은 <br>로 표현
5. Markdown 표만 출력 (설명 없이)

예시 형식:
| 헤더1 | 헤더2 | 헤더3 |
|:---|:---|:---|
| 값1 | 값2 | 값3 |
"""


def reconstruct_table_with_llm(
    pdf_path: str,
    page_num: int,
    table_bbox: tuple[float, float, float, float],
    config: Config,
) -> str | None:
    """
    복잡한 표(셀 병합 등)를 LLM Vision으로 재구성하여 Markdown을 반환한다.

    Returns
    -------
    str | None
        Markdown 표 문자열. 실패 시 None.
    """
    try:
        image_b64 = _render_page_to_base64(
            pdf_path=pdf_path,
            page_num=page_num,
            dpi=config.llm.page_render_dpi,
            crop_bbox=table_bbox,
        )
        response = _call_llm(TABLE_RECONSTRUCT_PROMPT, image_b64, config)

        # Markdown 표인지 기본 검증
        if "|" not in response:
            logger.warning("LLM 표 재구성 결과가 Markdown 표 형식이 아닙니다.")
            return None

        return response.strip()

    except Exception as e:
        logger.warning(f"LLM 표 재구성 실패: {e}")
        return None


PAGE_REFINEMENT_PROMPT = """
다음은 PDF 문서 페이지에서 추출한 텍스트입니다.
텍스트의 구조를 파악하여 올바른 Markdown 형식으로 변환해주세요.

규칙:
1. 제목/소제목은 #, ##, ### 등으로 표시
2. 일반 단락은 그대로 유지
3. 목록은 - 또는 1. 형식으로 표시
4. 표가 있으면 Markdown 표로 변환
5. 원문 내용을 변경하지 말 것 (구조만 변환)
6. 페이지 번호, 머리글/바닥글은 제외

--- 원문 텍스트 ---
{raw_text}
--- 끝 ---

Markdown만 출력하세요.
"""


def refine_page_with_llm(
    raw_text: str,
    pdf_path: str,
    page_num: int,
    config: Config,
) -> str | None:
    """
    페이지 전체 텍스트를 LLM에 전달하여 Markdown으로 정제한다.
    파서 결과 품질이 낮을 때 최후 수단으로 사용한다.

    Returns
    -------
    str | None
        정제된 Markdown 문자열. 실패 시 None.
    """
    try:
        # 이미지도 함께 제공하여 맥락 파악 지원
        image_b64 = _render_page_to_base64(
            pdf_path=pdf_path,
            page_num=page_num,
            dpi=config.llm.page_render_dpi,
        )
        prompt = PAGE_REFINEMENT_PROMPT.format(raw_text=raw_text)
        response = _call_llm(prompt, image_b64, config)
        return response.strip()

    except Exception as e:
        logger.warning(f"LLM 페이지 정제 실패 (페이지 {page_num}): {e}")
        return None


# ─────────────────────────────────────────────
# 통합 강화 클래스
# ─────────────────────────────────────────────

class LLMEnhancer:
    """
    MergedDocument의 신뢰도 낮은 요소를 LLM으로 보강한다.

    사용법:
        enhancer = LLMEnhancer(config)
        enhanced = enhancer.enhance(merged_document, pdf_path)
    """

    def __init__(self, config: Config = DEFAULT_CONFIG):
        self.config = config
        self.enabled = config.llm_enabled

    def enhance(self, elements: list[DocumentElement], pdf_path: str) -> list[DocumentElement]:
        """
        요소 목록에서 신뢰도 낮은 heading과 빈 표를 LLM으로 보강한다.

        Parameters
        ----------
        elements : list[DocumentElement]
            DocumentMerger가 생성한 요소 목록
        pdf_path : str
            원본 PDF 경로 (이미지 렌더링에 사용)

        Returns
        -------
        list[DocumentElement]
            보강된 요소 목록
        """
        if not self.enabled:
            logger.info("LLM 기능 비활성화 (LLM_PROVIDER=none). LLM 없이 진행합니다.")
            return elements

        threshold = self.config.llm.heading_confidence_threshold
        enhanced = []

        for elem in elements:
            # ── heading 신뢰도 낮은 경우 재판정 ──
            if (
                elem.kind == "heading"
                and elem.confidence < threshold
                and elem.confidence > 0  # 0이면 이미 확실한 non-heading
            ):
                logger.debug(
                    f"LLM heading 재판정: '{elem.text[:40]}' "
                    f"(신뢰도: {elem.confidence:.2f})"
                )
                result = judge_heading_with_llm(
                    text=elem.text,
                    pdf_path=pdf_path,
                    page_num=elem.page_number,
                    bbox=None,
                    config=self.config,
                )

                if result.get("is_heading"):
                    elem.level = result.get("level") or elem.level
                    elem.source = "llm"
                else:
                    elem.kind = "paragraph"
                    elem.level = None
                    elem.source = "llm"

            enhanced.append(elem)

        return enhanced
