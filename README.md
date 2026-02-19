# PDF → Markdown 변환 파이프라인

법률 문서, 내규, 매뉴얼 등 다양한 양식의 PDF를 고정밀 Markdown으로 자동 변환합니다.

---

## 파일 구조

```
doc_parser_test/
│
├── Dockerfile                    # 멀티스테이지 빌드 (builder + runtime)
├── docker-compose.yml            # CLI 배치 모드 + REST API 서버 모드
├── docker-entrypoint.sh          # 컨테이너 시작 스크립트 (APP_MODE 분기)
├── .dockerignore                 # Docker 빌드 제외 목록
│
├── main.py                       # 메인 진입점 (CLI)
├── config.py                     # 전체 설정 중앙 관리 (환경변수/Docker 경로 지원)
├── requirements.txt              # 의존 패키지 목록
├── .env.example                  # 환경변수 예시 (LLM API 키 등)
│
├── api/
│   └── app.py                    # FastAPI REST API (Docker API 모드)
│                                 #   POST /parse          - PDF → Markdown 텍스트 반환
│                                 #   POST /parse/file     - PDF → 파일 저장
│                                 #   POST /parse/batch    - 여러 PDF → ZIP 반환
│                                 #   POST /jobs           - 비동기 작업 등록
│                                 #   GET  /jobs/{id}      - 작업 상태 조회
│                                 #   GET  /jobs/{id}/result - 결과 다운로드
│
├── parsers/
│   ├── pymupdf_parser.py         # 폰트 크기·굵기 메타데이터 추출
│   ├── pdfplumber_parser.py      # 정확한 텍스트 + 표 추출
│   └── docling_parser.py         # 문서 구조(heading/table 레이블) 추출
│
├── processors/
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
├── data/                         # Docker 볼륨 마운트 경로 (gitignore)
│   ├── input/                    # ← PDF 파일 여기에 넣기
│   ├── output/                   # ← 변환된 .md 파일 출력
│   └── debug/                    # ← 디버그 JSON (--debug 옵션)
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

## 설치 및 실행

### 방법 1: Docker (권장)

의존성 충돌·시스템 패키지 설치 없이 바로 실행할 수 있습니다.

```bash
# 1) 데이터 디렉토리 생성 + 환경변수 설정
mkdir -p ./data/input ./data/output ./data/debug
cp .env.example .env   # 필요시 LLM API 키 입력

# 2) 이미지 빌드 (최초 1회, ~수 분 소요)
docker-compose build

# 3-A) CLI 배치 모드: ./data/input/ 의 PDF를 ./data/output/ 으로 변환
cp 계약서.pdf ./data/input/
docker-compose run --rm parser

# 3-B) 특정 파일만 처리
docker-compose run --rm parser --input /app/input/계약서.pdf

# 3-C) docling 없이 빠른 처리
docker-compose run --rm parser --no-docling

# 3-D) REST API 서버 실행
docker-compose up api
# → http://localhost:8000/docs 에서 Swagger UI 확인
```

#### Docker API 서버 사용 예시 (curl)

```bash
# 단일 파일 변환 → Markdown 텍스트 반환
curl -X POST http://localhost:8000/parse \
     -F "file=@계약서.pdf" \
     --output 계약서.md

# 여러 파일 일괄 변환 → ZIP 반환
curl -X POST http://localhost:8000/parse/batch \
     -F "files=@문서1.pdf" \
     -F "files=@문서2.pdf" \
     --output 결과.zip

# 비동기 처리 (대용량 PDF)
curl -X POST http://localhost:8000/jobs -F "file=@대용량.pdf"
# → {"job_id": "abc-123", ...}
curl http://localhost:8000/jobs/abc-123          # 상태 조회
curl http://localhost:8000/jobs/abc-123/result   # 결과 다운로드
```

#### 주요 Docker 환경변수

| 환경변수 | 기본값 | 설명 |
|:---|:---|:---|
| `APP_MODE` | `cli` | `cli` (배치) 또는 `api` (REST 서버) |
| `LLM_PROVIDER` | `none` | `openai` / `anthropic` / `none` |
| `DOCLING_USE_GPU` | `false` | GPU 가속 (NVIDIA 환경) |
| `DOCLING_OCR_ENABLED` | `true` | OCR 활성화 (스캔 PDF) |
| `MAX_PAGES` | `0` (전체) | 처리 최대 페이지 수 |
| `SAVE_INTERMEDIATE` | `false` | 디버그 JSON 저장 |
| `WORKER_THREADS` | `2` | API 동시 처리 스레드 수 |

---

### 방법 2: 로컬 직접 실행

```bash
pip install -r requirements.txt
# docling 첫 실행 시 모델 자동 다운로드 (~1GB)

# 단일 파일
python main.py --input 계약서.pdf

# 디렉토리 일괄 변환
python main.py --input ./pdf_폴더/ --output ./markdown_폴더/

# 빠른 처리 (docling 제외)
python main.py --input 문서.pdf --no-docling

# 디버깅
python main.py --input 문서.pdf --debug --verbose
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