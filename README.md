# 사내 문서 RAG 챗봇 (PDF → Markdown → Embedding → Chatbot)

사내 PDF 문서를 AI가 이해할 수 있는 형태로 변환하고,
LangChain 기반 RAG 챗봇으로 직원들의 질문에 답변하는 시스템입니다.

## 프로젝트 구조

```
doc_parser_test/
├── config/
│   └── settings.py              # 전역 설정 (경로, 모델, 파라미터)
├── src/
│   ├── parsers/
│   │   ├── docling_parser.py    # Docling PDF→MD 변환 + PyMuPDF 메타데이터
│   │   └── image_extractor.py   # 이미지 추출 + Vision API 분석
│   ├── processing/
│   │   ├── heading_processor.py # Heading 계층 복원 (번호패턴+폰트 분석)
│   │   └── chunker.py           # Heading-Aware 청크 분할
│   ├── embedding/
│   │   └── vector_store.py      # ChromaDB 벡터 저장/검색
│   ├── chatbot/
│   │   └── rag_chain.py         # LangChain RAG 체인 + 프롬프트
│   ├── utils/
│   │   └── logger.py            # 로거 설정
│   └── pipeline.py              # 전체 파이프라인 오케스트레이터
├── data/
│   ├── input_pdfs/              # PDF 원본 입력 폴더 ← 여기에 PDF를 넣으세요
│   ├── output_md/               # 변환된 Markdown 출력
│   ├── images/                  # 추출된 이미지
│   └── vectorstore/             # ChromaDB 벡터 저장소
├── tests/
├── run_pipeline.py              # PDF 처리 실행 스크립트
├── run_chatbot.py               # 챗봇 실행 스크립트
├── requirements.txt             # 의존성 패키지
├── .env.example                 # 환경변수 템플릿
└── .gitignore
```

## 처리 파이프라인

```
PDF 입력
  ↓
[Docling] AI 기반 구조 분석 → Markdown (표, 리스트, heading 인식)
  ↓
[PyMuPDF] 폰트 크기/굵기 메타데이터 추출
  ↓
[HeadingProcessor] heading 계층 복원 (번호 패턴 + 폰트 크기 복합 분석)
  ↓
[ImageExtractor] 이미지 추출 → OpenAI Vision API로 내용 설명 생성 (선택)
  ↓
Markdown 파일 저장
  ↓
[MarkdownChunker] Heading-Aware 청크 분할 (heading 경로 메타데이터 포함)
  ↓
[VectorStoreManager] OpenAI Embedding → ChromaDB 저장
  ↓
[RAGChatbot] LangChain 체인: 검색 → 프롬프트 → GPT-4o → 답변
```

## 설치 및 실행

### 1. 환경 설정

```bash
# Python 가상환경 생성 (3.10 이상)
python -m venv .venv
source .venv/bin/activate    # Linux/Mac
# .venv\Scripts\activate     # Windows

# 의존성 설치
pip install -r requirements.txt

# 환경변수 설정
cp .env.example .env
# .env 파일을 열어 OPENAI_API_KEY 입력
```

### 2. PDF 파일 준비

```bash
# data/input_pdfs/ 디렉토리에 PDF 파일을 넣으세요
cp /path/to/your/documents/*.pdf data/input_pdfs/
```

### 3. PDF 처리 파이프라인 실행

```bash
# 모든 PDF 처리 (기본)
python run_pipeline.py

# 특정 PDF 하나만 처리
python run_pipeline.py --file data/input_pdfs/규정집.pdf

# Vision API로 이미지 내용까지 분석 (OpenAI API 비용 발생)
python run_pipeline.py --use-vision

# 벡터 저장소 초기화 후 재처리
python run_pipeline.py --reset
```

### 4. 챗봇 실행

```bash
# 대화형 챗봇
python run_chatbot.py

# 단일 질문
python run_chatbot.py --query "연차 사용 규정이 어떻게 되나요?"

# 멀티턴 대화 모드 (이전 대화 맥락 유지)
python run_chatbot.py --multi-turn
```

## 핵심 설계 결정

### PDF 파서: Docling 중심 + PyMuPDF 보완

| 역할 | 라이브러리 | 이유 |
|------|-----------|------|
| 문서 구조 + 표 | Docling | 가장 정확한 AI 기반 구조 분석 |
| 폰트 메타데이터 | PyMuPDF | 폰트 크기/굵기로 heading 계층 판단 |
| 이미지 → 텍스트 | PyMuPDF + OpenAI Vision | 이미지 추출 후 LLM으로 설명 생성 |
| Heading 계층 복원 | 커스텀 HeadingProcessor | 번호 패턴 + 폰트 크기 다중 전략 |

### Heading 계층 복원 전략

Docling이 `##` 단일 레벨로 출력하는 문제를 다중 전략으로 해결:

1. **번호 패턴 기반**: `제1장`→H1, `1.`→H2, `1.1`→H3, `(1)`→H3, `1.1.1`→H4
2. **폰트 크기 기반**: 본문 대비 1.8배→H1, 1.4배→H2, 1.15배→H3
3. **복합 판단**: 패턴 우선, 폰트 보조, 연속성 보정

### Chunking 전략

단순 고정 크기가 아닌 **Heading-Aware Recursive Chunking**:
- heading 경계에서 우선 분할 (의미 단위 보존)
- 각 chunk에 heading 경로 메타데이터 부여 (출처 추적)
- 표는 하나의 chunk에 보존
- overlap으로 경계 문맥 손실 방지

## 설정 변경

`config/settings.py`에서 주요 파라미터를 조정할 수 있습니다:

- `CHUNK_CONFIG`: 청크 크기, 겹침, 최소 크기
- `EMBEDDING_CONFIG`: 임베딩 모델, 검색 결과 수
- `LLM_CONFIG`: LLM 모델, temperature
- `HEADING_CONFIG`: heading 판단 기준 (폰트 비율, 번호 패턴)
