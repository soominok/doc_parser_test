# PDF → Markdown 변환 파이프라인

법률 문서, 내규, 매뉴얼 등 다양한 양식의 PDF를 고정밀 Markdown으로 자동 변환합니다.

---

## 파일 구조

```
doc_parser_test/
│
├── main.py                       # 메인 진입점 (CLI)
├── config.py                     # 전체 설정 중앙 관리
├── requirements.txt              # 의존 패키지 목록
├── .env.example                  # 환경변수 예시 (LLM API 키 등)
│
├── parsers/                      # 파서 모듈
│   ├── pymupdf_parser.py         # 폰트 크기·굵기 메타데이터 추출
│   ├── pdfplumber_parser.py      # 정확한 텍스트 + 표 추출
│   └── docling_parser.py         # 문서 구조(heading/table 레이블) 추출
│
├── processors/                   # 처리 모듈
│   ├── heading_detector.py       # 폰트+docling 정보로 heading 레벨 결정
│   ├── table_processor.py        # 표 → Markdown 표 변환
│   └── merger.py                 # 세 파서 결과 통합 → DocumentElement 목록
│
├── llm/
│   └── enhancer.py               # LLM(GPT-4o/Claude) 기반 정확도 보강
│
├── exporters/
│   └── markdown_exporter.py      # DocumentElement → .md 파일
│
└── tests/
    └── sample_pdfs/              # 테스트용 PDF 보관 경로
```

---

## 설계 원칙

| 파서 | 역할 | 강점 | 한계 |
|:---|:---|:---|:---|
| **PyMuPDF** | 폰트 메타데이터 | 폰트 크기·굵기·좌표 정확 | 구조 분류 없음 |
| **pdfplumber** | 텍스트·표 추출 | 텍스트 정확도 최고, 표 경계 인식 | heading 인식 불가 |
| **docling** | 문서 구조 분류 | heading/table/caption 레이블 | 텍스트 잘림·순서 역전 |
| **LLM** (선택) | 모호한 요소 보강 | 복잡한 표·불명확한 heading 처리 | API 비용 발생 |

**통합 전략**: docling의 구조 레이블 + pdfplumber의 텍스트 + PyMuPDF의 폰트 정보를 결합하여 세 파서의 단점을 상호 보완합니다.

---

## 설치

```bash
pip install -r requirements.txt
```

> docling은 첫 실행 시 딥러닝 모델을 자동 다운로드합니다 (~1GB).

---

## 사용법

### 단일 파일 변환
```bash
python main.py --input 계약서.pdf
# 결과: 계약서_parsed.md (같은 디렉토리에 생성)
```

### 디렉토리 일괄 변환
```bash
python main.py --input ./pdf_폴더/ --output ./markdown_폴더/
```

### 빠른 처리 (docling 제외, 폰트 기반만 사용)
```bash
python main.py --input 문서.pdf --no-docling
```

### LLM 보강 활성화
```bash
# .env 파일에 API 키 설정 후
LLM_PROVIDER=openai OPENAI_API_KEY=sk-... python main.py --input 문서.pdf
```

### 디버깅 (중간 결과 JSON 저장)
```bash
python main.py --input 문서.pdf --debug --verbose
# ./debug_output/문서_intermediate.json 생성
```

---

## 처리 흐름

```
PDF 입력
  │
  ├──▶ PyMuPDF     → 폰트 크기/굵기/좌표 → HeadingDetector
  ├──▶ pdfplumber  → 정확한 텍스트 + 표  → TableProcessor
  └──▶ docling     → heading/table 레이블 ┘
                                           │
                                    DocumentMerger
                                           │
                              (선택) LLM Enhancer
                                           │
                                  MarkdownExporter
                                           │
                                       .md 파일
```

---

## 설정 변경

`config.py`의 `Config` 클래스에서 세부 파라미터를 조정합니다.

```python
from config import Config

config = Config()

# heading 감지 민감도 조정 (낮을수록 더 많이 heading으로 분류)
config.heading.min_confidence = 0.50

# 최대 페이지 수 제한 (테스트용)
config.parser.max_pages = 10

# 출력 디렉토리 지정
from pathlib import Path
config.output.output_dir = Path("./output")
```

---

## 다음 단계 (RAG 파이프라인)

```
PDF → Markdown (현재 단계)
        │
        ▼
   Chunking (의미 단위 분할)
        │
        ▼
   Embedding (벡터 변환)
        │
        ▼
  Vector DB 저장
        │
        ▼
  LangChain RAG 챗봇
```