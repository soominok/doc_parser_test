# PDF → Markdown 하이브리드 파서

법률, 내규, 매뉴얼 등 다양한 형식의 PDF를 **수작업 없이 자동으로** 정확한 마크다운으로 변환합니다.
RAG(Retrieval-Augmented Generation) 파이프라인의 전처리 단계로 설계되었습니다.

---

## 핵심 특징

| 기능 | 설명 |
|------|------|
| **Heading 계층 인식** | 폰트 크기·굵기를 분석하여 H1/H2/H3 자동 구분 |
| **표 추출** | 복잡한 셀 병합, 다단 표도 마크다운 표로 변환 |
| **다양한 레이아웃 지원** | 단일/다단 컬럼, 복잡한 문서 구조 처리 |
| **하이브리드 파싱** | 3개 라이브러리 조합으로 정확도 극대화 |
| **LLM 후처리** | Claude API로 잔여 오류 자동 수정 (선택) |
| **웹 UI** | 브라우저 기반 UI, 별도 서버 설치 불필요 |

---

## 라이브러리 조합 전략

```
PDF 파일
   │
   ├─► [Docling]      문서 구조 분석
   │     • heading/표/리스트/단락 분류
   │     • 요소 읽기 순서 파악
   │     • 표 마크다운 변환
   │
   ├─► [PyMuPDF]      폰트 정보 분석
   │     • 폰트 크기 분포 → body 크기 자동 감지
   │     • 크기 클러스터링 → H1/H2/H3 경계 결정
   │     • 굵기(bold) 감지 → 소제목 처리
   │
   ├─► [pdfplumber]   텍스트 정확도 보완
   │     • 문자 단위 추출 → 텍스트 잘림 보완
   │     • 표 데이터 재추출 (Docling 표 오류 시)
   │
   └─► [Claude API]   LLM 최종 정제 (선택)
         • heading 레벨 오류 수정
         • 잘린 문장 이어붙이기
         • 페이지 번호/머리말 제거
```

### 각 라이브러리의 역할

| 라이브러리 | 장점 | 단점 | 이 프로젝트에서 역할 |
|-----------|------|------|---------------------|
| **Docling** | 구조 파악 정확, 요소 분류 우수 | 텍스트 잘림, heading 계층 없음 | 구조 추출 (뼈대 역할) |
| **PyMuPDF** | 폰트 정보 정밀, 속도 빠름 | 구조 인식 불가 | heading 레벨 결정 |
| **pdfplumber** | 텍스트 정확, 표 경계 감지 | heading 인식 불가 | 텍스트 보완 |
| **Claude API** | 문맥 이해, 오류 수정 | 비용, 속도 | 최종 품질 향상 |

---

## 파일 구조

```
doc_parser_test/
├── pdf_parser.py        # 단일 파일 (전체 코드)
├── requirements.txt     # 의존 라이브러리
└── README.md
```

### `pdf_parser.py` 내부 구성

```python
pdf_parser.py
├── 1. 데이터 클래스
│     └── DocElement          # 단일 문서 요소 (타입/텍스트/폰트 정보)
│
├── 2. FontAnalyzer           # PyMuPDF 기반 폰트 분석기
│     ├── _analyze_font_distribution()  # 전체 폰트 분포 분석
│     ├── _cluster_sizes()              # 유사 폰트 크기 클러스터링
│     ├── get_page_text_with_fonts()    # 페이지별 텍스트+폰트 추출
│     └── resolve_heading_level()       # 폰트→H1/H2/H3 결정
│
├── 3. DoclingExtractor       # Docling 기반 구조 추출기
│     ├── _build_converter()            # Docling 설정
│     └── extract()                     # 구조 추출 → DocElement 리스트
│
├── 4. PlumberExtractor       # pdfplumber 기반 정밀 텍스트 추출기
│     ├── extract_page_text()           # 페이지 전체 텍스트
│     ├── extract_page_tables()         # 페이지 표 목록
│     └── _table_to_markdown()          # 표 → 마크다운 변환
│
├── 5. HybridParser           # 핵심 통합 파서
│     ├── parse()                       # 메인 파싱 (4단계 파이프라인)
│     ├── _resolve_heading_levels()     # heading 레벨 결정 (PyMuPDF 활용)
│     ├── _fuzzy_find()                 # 퍼지 텍스트 매칭
│     ├── _enhance_text_with_plumber()  # 텍스트 정확도 보완
│     ├── _clean_text()                 # 텍스트 정리
│     └── _build_markdown()            # 최종 마크다운 조합
│
├── 6. LLMRefiner             # Claude API 후처리기
│     ├── refine()                      # 청크 단위 LLM 정제
│     ├── _call_api()                   # Claude API 호출
│     └── _split_into_chunks()          # heading 기준 청크 분할
│
├── 7. create_ui()            # Gradio 웹 UI
│
└── 8. main()                 # 진입점
```

---

## 설치

```bash
# 저장소 클론
git clone <repository-url>
cd doc_parser_test

# 의존 라이브러리 설치
pip install -r requirements.txt
```

> **참고**: `docling` 첫 실행 시 AI 모델을 다운로드합니다 (~수백 MB).

---

## 실행

```bash
python pdf_parser.py
```

브라우저가 자동으로 열리며 `http://localhost:7860` 에서 UI에 접근합니다.

---

## 사용 방법

1. **PDF 업로드**: 왼쪽 패널에서 PDF 파일을 드래그하거나 선택
2. **변환 시작**: "변환 시작" 버튼 클릭
3. **결과 확인**: 오른쪽 패널에서 마크다운 소스 및 렌더링 미리보기 확인
4. **다운로드**: 결과 파일 다운로드 버튼으로 `.md` 파일 저장

### LLM 정제 활성화 (선택)

1. "LLM 정제" 아코디언 펼치기
2. "Claude API로 최종 정제 활성화" 체크
3. Anthropic API Key 입력 (`sk-ant-...`)
4. 변환 시작

---

## Heading 계층 결정 알고리즘

```
1. 문서 전체 폰트 크기 수집
2. 가장 빈도 높은 크기 = body 크기
3. body보다 큰 크기들을 내림차순 정렬
4. 유사 크기(0.8pt 이내) 클러스터링
   예: [18.0, 17.8, 14.0, 12.5, 12.3]
       → [[18.0, 17.8], [14.0], [12.5, 12.3]]
5. 클러스터 순서대로 H1/H2/H3 배정
6. bold + body 크기 텍스트 → H3 (소제목)
```

---

## 향후 확장 계획

이 파서는 RAG 파이프라인의 첫 단계입니다:

```
[1단계] PDF → Markdown    ← 현재 구현
[2단계] Markdown → Chunks (semantic chunking)
[3단계] Chunks → Embeddings (embedding model)
[4단계] 챗봇 (LangChain + RAG)
```

---

## 문제 해결

| 증상 | 원인 | 해결 |
|------|------|------|
| Docling 첫 실행 느림 | 모델 다운로드 | 최초 1회만 발생, 이후 캐시 사용 |
| heading 레벨 오류 | 특수 폰트 / 한글 폰트 | LLM 정제 활성화 |
| 표가 깨짐 | 복잡한 셀 병합 | pdfplumber 표 재추출 자동 시도 |
| 텍스트 순서 뒤바뀜 | 다단 컬럼 레이아웃 | Docling이 읽기 순서 자동 보정 |
