"""
이미지 추출 및 Vision API 기반 텍스트 변환 모듈

Docling의 한계:
- 이미지는 ![image](...) 플레이스홀더로만 출력
- 이미지 내 텍스트(차트, 다이어그램, 스크린샷 등)를 추출하지 못함

해결 전략:
1. PyMuPDF로 PDF에서 이미지를 바이너리로 추출
2. OpenAI Vision API(GPT-4o)로 이미지 내용을 자연어로 설명
3. 설명 텍스트를 Markdown에 삽입하여 검색 가능하게 만듦
"""
import base64
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.utils.logger import get_logger

logger = get_logger(__name__)


class ImageExtractor:
    """
    PDF에서 이미지를 추출하고 Vision API로 내용을 분석합니다.
    """

    def __init__(self, images_dir: str | Path, vision_model: str = "gpt-4o"):
        """
        Args:
            images_dir: 추출된 이미지를 저장할 디렉토리
            vision_model: Vision 분석에 사용할 OpenAI 모델
        """
        self.images_dir = Path(images_dir)
        self.images_dir.mkdir(parents=True, exist_ok=True)
        self.vision_model = vision_model

    def extract_images_from_pdf(self, pdf_path: str | Path) -> list[dict]:
        """
        PyMuPDF를 사용하여 PDF에서 모든 이미지를 추출합니다.

        Args:
            pdf_path: PDF 파일 경로

        Returns:
            list[dict]: 각 이미지 정보
                - "image_path": 저장된 이미지 파일 경로
                - "page_num": 이미지가 위치한 페이지 번호
                - "image_index": 페이지 내 이미지 순서
                - "width": 이미지 너비
                - "height": 이미지 높이
                - "image_bytes": 이미지 바이트 데이터 (Vision API 전송용)
        """
        import fitz  # PyMuPDF

        pdf_path = Path(pdf_path)
        doc = fitz.open(str(pdf_path))
        pdf_stem = pdf_path.stem  # 파일명 (확장자 제외)
        extracted_images = []

        for page_num, page in enumerate(doc):
            # 페이지에서 이미지 목록 가져오기
            image_list = page.get_images(full=True)

            for img_idx, img_info in enumerate(image_list):
                xref = img_info[0]  # 이미지 참조 번호

                try:
                    # 이미지 바이트 데이터 추출
                    base_image = doc.extract_image(xref)
                    image_bytes = base_image["image"]
                    image_ext = base_image["ext"]  # png, jpeg 등
                    width = base_image["width"]
                    height = base_image["height"]

                    # 너무 작은 이미지 건너뛰기 (아이콘, 장식 등)
                    if width < 50 or height < 50:
                        continue

                    # 이미지 파일로 저장
                    image_filename = f"{pdf_stem}_p{page_num + 1}_img{img_idx + 1}.{image_ext}"
                    image_path = self.images_dir / image_filename

                    with open(image_path, "wb") as f:
                        f.write(image_bytes)

                    extracted_images.append({
                        "image_path": str(image_path),
                        "page_num": page_num + 1,
                        "image_index": img_idx + 1,
                        "width": width,
                        "height": height,
                        "image_bytes": image_bytes,
                    })

                except Exception as e:
                    logger.warning(
                        f"이미지 추출 실패 (페이지 {page_num + 1}, "
                        f"이미지 {img_idx + 1}): {e}"
                    )

        doc.close()
        logger.info(
            f"이미지 추출 완료: {pdf_path.name} → {len(extracted_images)}개 이미지"
        )
        return extracted_images

    def describe_image_with_vision(
        self,
        image_bytes: bytes,
        context_hint: str = "",
    ) -> str:
        """
        OpenAI Vision API를 사용하여 이미지 내용을 자연어로 설명합니다.

        Args:
            image_bytes: 이미지 바이너리 데이터
            context_hint: 이미지가 포함된 문서의 맥락 힌트 (선택)

        Returns:
            이미지에 대한 자연어 설명 문자열
        """
        try:
            from openai import OpenAI

            client = OpenAI()  # OPENAI_API_KEY 환경변수 사용

            # 이미지를 base64로 인코딩
            base64_image = base64.b64encode(image_bytes).decode("utf-8")

            # Vision API 프롬프트 구성
            system_prompt = (
                "당신은 사내 문서의 이미지를 분석하는 전문가입니다. "
                "이미지에 포함된 모든 텍스트, 표, 차트, 다이어그램의 내용을 "
                "정확하고 상세하게 한국어로 설명해주세요. "
                "차트나 그래프의 경우 주요 수치와 트렌드를 포함해주세요."
            )

            user_content = [
                {
                    "type": "text",
                    "text": (
                        f"이 이미지의 내용을 상세히 설명해주세요."
                        f"{f' 문서 맥락: {context_hint}' if context_hint else ''}"
                    ),
                },
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{base64_image}",
                        "detail": "high",  # 고해상도 분석
                    },
                },
            ]

            response = client.chat.completions.create(
                model=self.vision_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                max_tokens=1024,
            )

            description = response.choices[0].message.content.strip()
            return description

        except ImportError:
            logger.error("openai 패키지가 설치되지 않았습니다: pip install openai")
            return "[이미지 설명 생성 실패: openai 패키지 미설치]"
        except Exception as e:
            logger.error(f"Vision API 호출 실패: {e}")
            return f"[이미지 설명 생성 실패: {e}]"

    def process_images_for_document(
        self,
        pdf_path: str | Path,
        use_vision_api: bool = True,
    ) -> list[dict]:
        """
        PDF에서 이미지를 추출하고, Vision API로 각 이미지를 분석합니다.

        Args:
            pdf_path: PDF 파일 경로
            use_vision_api: Vision API 사용 여부
                (False면 이미지 추출만 하고 설명은 생성하지 않음)

        Returns:
            list[dict]: 각 이미지의 추출 결과 + 설명
                - 위의 extract 결과 + "description": str
        """
        images = self.extract_images_from_pdf(pdf_path)

        if not use_vision_api:
            for img in images:
                img["description"] = f"[이미지: 페이지 {img['page_num']}]"
            return images

        logger.info(f"Vision API로 {len(images)}개 이미지 분석 중...")

        for i, img in enumerate(images):
            logger.info(
                f"  이미지 분석 ({i + 1}/{len(images)}): "
                f"페이지 {img['page_num']}, {img['width']}x{img['height']}"
            )
            description = self.describe_image_with_vision(
                image_bytes=img["image_bytes"],
                context_hint=Path(pdf_path).stem,
            )
            img["description"] = description
            # 메모리 절약: 분석 완료 후 바이트 데이터 제거
            del img["image_bytes"]

        logger.info("모든 이미지 분석 완료")
        return images

    def inject_image_descriptions_into_markdown(
        self,
        markdown_text: str,
        image_descriptions: list[dict],
    ) -> str:
        """
        Markdown 텍스트의 이미지 플레이스홀더를
        Vision API 설명으로 대체합니다.

        Docling은 이미지를 <!-- image --> 또는 ![image](...) 형태로 출력합니다.
        이를 실제 설명 텍스트로 교체합니다.

        Args:
            markdown_text: Docling이 생성한 Markdown
            image_descriptions: process_images_for_document()의 결과

        Returns:
            이미지 설명이 삽입된 Markdown
        """
        import re

        # 이미지를 페이지 번호 순으로 정렬
        sorted_images = sorted(image_descriptions, key=lambda x: (x["page_num"], x["image_index"]))

        # Docling의 이미지 플레이스홀더 패턴들
        # 패턴 1: <!-- image --> 스타일
        # 패턴 2: ![image](경로) 스타일
        image_patterns = [
            r"<!--\s*image\s*-->",
            r"!\[(?:image|그림|figure|img)[^\]]*\]\([^)]*\)",
        ]

        combined_pattern = "|".join(image_patterns)
        matches = list(re.finditer(combined_pattern, markdown_text, re.IGNORECASE))

        if not matches and sorted_images:
            # 플레이스홀더가 없으면 문서 끝에 이미지 설명을 추가
            logger.warning(
                "Markdown에 이미지 플레이스홀더가 없습니다. "
                "문서 끝에 이미지 설명을 추가합니다."
            )
            appendix = "\n\n---\n\n## 문서 내 이미지 설명\n\n"
            for img in sorted_images:
                appendix += (
                    f"### 이미지 (페이지 {img['page_num']})\n\n"
                    f"{img['description']}\n\n"
                )
            return markdown_text + appendix

        # 플레이스홀더를 이미지 설명으로 순차 대체
        result = markdown_text
        offset = 0  # 대체로 인한 위치 이동 보정

        for i, match in enumerate(matches):
            if i >= len(sorted_images):
                break

            img = sorted_images[i]
            # 이미지 설명을 blockquote 형태로 삽입
            replacement = (
                f"\n> **[이미지 - 페이지 {img['page_num']}]**\n"
                f"> {img['description']}\n"
            )

            start = match.start() + offset
            end = match.end() + offset
            result = result[:start] + replacement + result[end:]
            offset += len(replacement) - (match.end() - match.start())

        return result
