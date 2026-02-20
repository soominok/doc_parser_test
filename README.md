# 문서 → 마크다운 변환기 (Document to Markdown Converter)

PDF / DOCX / HWP 문서를 Markdown 파일로 변환하는 하이브리드 파서.
여러 라이브러리의 장점을 조합해 헤더 계층 구조, 표, 텍스트를 최대한 정확하게 추출합니다.

---

## 목차

1. [주요 기능](#주요-기능)
2. [하이브리드 전략](#하이브리드-전략)
3. [지원 형식](#지원-형식)
4. [설치 및 실행](#설치-및-실행)
   - [로컬 실행](#1-로컬-실행-python-직접)
   - [Docker GUI 모드](#2-docker-gui-모드)
   - [Docker CLI 모드](#3-docker-cli-모드)
   - [Docker Compose](#4-docker-compose)
5. [GUI 사용법](#gui-사용법)
6. [CLI 사용법](#cli-사용법)
7. [라이브러리별 역할](#라이브러리별-역할)
8. [파일 구조](#파일-구조)
9. [플랫폼별 X11 설정](#플랫폼별-x11-설정)
10. [의존성](#의존성)

---

## 주요 기능

- **하이브리드 PDF 파싱**: docling + pymupdf4llm + pdfplumber를 조합해 구조·헤더 계층·텍스트를 최적으로 추출
- **헤더 계층 자동 복원**: 퍼지 매칭(SequenceMatcher)으로 문서 구조에 맞는 H1 ~ H6 레벨 결정
- **표 중복 열 자동 제거**: pymupdf4llm이 생성하는 좌우 빈 열 문제를 자동으로 정리
- **DOCX 네이티브 변환**: 단락 순서·헤더 레벨·표·리스트를 그대로 보존
- **HWP 텍스트 추출**: pyhwp CLI + OLE 스트림 파싱 이중 시도
- **tkinter GUI**: Python 표준 GUI 라이브러리로 파일 선택·변환·미리보기 제공
- **CLI 모드**: GUI 없이 터미널에서 바로 변환 가능
- **Docker 지원**: X11 포워딩으로 컨테이너 안에서 GUI 실행 가능

---

## 하이브리드 전략

### PDF 변환 파이프라인

```
입력 PDF
   │
   ├─▶ [docling]       → 문서 구조 분석, 헤더·표·리스트 요소 감지
   ├─▶ [pymupdf4llm]   → 폰트 크기 기반 헤더 계층(#/##/###) 추출, 표 Markdown 생성
   └─▶ [pdfplumber]    → 정확한 줄바꿈 텍스트, 이미지 내 텍스트
          │
          ▼
       [병합 로직]
          │
          ├─ docling 요소 목록을 뼈대(backbone)로 사용
          ├─ 각 헤더 텍스트를 pymupdf4llm 헤더 맵과 퍼지 매칭(≥ 0.80) → H1~H6 부여
          ├─ 매칭 실패 시 docling nesting depth + 1 로 fallback
          ├─ docling 표 export → 빈 열 중복 제거(fix_table_cols)
          └─ docling 요소 없을 때: pymupdf4llm → pdfplumber 순서로 fallback
          │
          ▼
       출력 Markdown
```

### 각 라이브러리의 역할

| 라이브러리 | 주요 역할 | 보완하는 단점 |
|-----------|----------|-------------|
| **docling** | 문서 구조 파악, 헤더·표·리스트 감지 | 헤더 레벨 없음 → pymupdf4llm으로 보완 |
| **pymupdf4llm** | 헤더 계층(H1/H2/H3) 결정, 표 Markdown | 표 좌우 빈 열 → fix_table_cols로 제거 |
| **pdfplumber** | 정확한 줄바꿈, 이미지 텍스트 | 헤더 없음 → fallback으로만 사용 |

---

## 지원 형식

| 형식 | 확장자 | 파서 |
|------|--------|------|
| PDF | `.pdf` | docling + pymupdf4llm + pdfplumber (하이브리드) |
| Word | `.docx` | python-docx |
| 한글 | `.hwp` | pyhwp (hwp5txt CLI + OLE fallback) |

---

## 설치 및 실행

### 1. 로컬 실행 (Python 직접)

**요구사항**: Python 3.10 이상

```bash
# 저장소 클론
git clone <repository-url>
cd doc_parser_test

# 의존성 설치 (시간이 걸릴 수 있음 — docling이 ML 모델을 포함)
pip install -r requirements.txt

# GUI 실행
python converter.py

# CLI 실행
python converter.py document.pdf
python converter.py document.pdf ./output
```

> **첫 실행 시 주의**: docling은 처음 실행할 때 AI 모델을 자동 다운로드합니다. 인터넷 연결과 여유 저장 공간(~2 GB)이 필요합니다.

---

### 2. Docker GUI 모드

#### Linux

```bash
# 이미지 빌드 (최초 1회)
docker build -t doc-to-md .

# GUI 실행 스크립트 사용 (권장)
chmod +x run.sh
./run.sh

# 또는 직접 실행
xhost +local:docker
docker run --rm \
  -e DISPLAY=$DISPLAY \
  -v /tmp/.X11-unix:/tmp/.X11-unix \
  -v $(pwd)/output:/data/output \
  -v $(pwd)/docs:/data/docs \
  --network host \
  doc-to-md
xhost -local:docker
```

#### macOS

```bash
# 1. XQuartz 설치: https://www.xquartz.org/
# 2. XQuartz 환경설정 → 보안 → "네트워크 클라이언트 허용" 체크
# 3. 터미널에서:
defaults write org.xquartz.X11 nolisten_tcp 0
xhost + 127.0.0.1

# 4. 실행
DISPLAY=host.docker.internal:0 ./run.sh
```

#### Windows (WSLg / WSL2)

```bash
# Windows 11 + WSLg: DISPLAY 자동 설정됨
./run.sh

# VcXsrv / X410 사용 시:
DISPLAY=:0.0 ./run.sh
```

---

### 3. Docker CLI 모드

```bash
./run.sh document.pdf                  # 같은 폴더에 .md 저장
./run.sh document.pdf ./output         # 출력 폴더 지정
./run.sh /abs/path/file.pdf /abs/output
```

또는 `docker run` 직접:

```bash
docker run --rm \
  -v /path/to/file.pdf:/data/file.pdf:ro \
  -v /path/to/output:/data/output \
  doc-to-md \
  /data/file.pdf /data/output
```

---

### 4. Docker Compose

```bash
# 문서를 ./docs 폴더에 넣은 뒤:
docker compose up

# 특정 문서 폴더를 마운트하려면:
DOCS_DIR=/path/to/your/docs docker compose up
```

변환된 파일은 `./output` 폴더에 저장됩니다.

---

## GUI 사용법

```
┌─────────────────────────────────────────────────────────┐
│           문서 → 마크다운 변환기                          │
├─────────────────────────────────────────────────────────┤
│ 입력 파일   [경로 표시 영역........................] [찾기] │
│ 출력 폴더   [경로 표시 영역........................] [찾기] │
├─ 파서 옵션 (PDF 전용) ──────────────────────────────────┤
│ [✓] docling    [✓] pymupdf4llm    [✓] pdfplumber        │
│                  [ 변환 시작 ]                           │
│ ══════════════════════════════════════  75%             │
│ 상태: docling 파싱 중…                                   │
├─ 로그 ──────────────────────────────────────────────────┤
│  [ 10%] pymupdf4llm 파싱 중…                            │
│  [ 30%] docling 파싱 중…                                │
│  ...                                                    │
├─ 미리보기 (마크다운) ───────────────────────────────────┤
│  # 제목                                                  │
│  ## 1. 서론                                              │
│  본문 텍스트...                                          │
│  | 열1 | 열2 |                                          │
│  | --- | --- |                                          │
├─────────────────────────────────────────────────────────┤
│ [클립보드에 복사]  [파일 열기]  ./output/document.md     │
└─────────────────────────────────────────────────────────┘
```

**사용 순서:**

1. **입력 파일** `[찾아보기…]` 클릭 → PDF/DOCX/HWP 파일 선택
2. **출력 폴더** 확인 (자동으로 입력 파일 폴더로 설정됨)
3. **파서 옵션** 체크박스 조절 (기본: 3개 모두 활성화, PDF만 적용됨)
4. **`[변환 시작]`** 클릭
5. 진행률 및 로그 확인
6. 완료 후 **미리보기**에서 결과 확인
7. **`[클립보드에 복사]`** 또는 **`[파일 열기]`** 로 결과 활용

---

## CLI 사용법

```bash
# 기본 (출력 파일은 입력 파일과 같은 폴더에 저장)
python converter.py input.pdf

# 출력 폴더 지정
python converter.py input.pdf ./output

# DOCX / HWP도 동일한 방식
python converter.py report.docx ./output
python converter.py document.hwp ./output
```

출력 파일명은 자동으로 `<원본파일명>.md` 로 결정됩니다.

---

## 라이브러리별 역할

### docling

```
장점:  헤더 인식 가능, 전체 텍스트·표 추출 우수
단점:  헤더 계층(H1/H2/H3) 없음, 텍스트 자동 줄바꿈 발생
역할:  문서 구조 backbone — SectionHeader / TextItem / TableItem 요소 목록 제공
```

### pymupdf4llm

```
장점:  헤더 계층 인식 (매우 큰 장점), 표 추출 우수
단점:  표 좌우에 빈 열이 추가되는 경우 있음
역할:  헤더 레벨(#/##/###) 결정용 매핑 테이블 제공
       fix_table_cols()로 중복 빈 열 자동 제거
```

### pdfplumber

```
장점:  정확한 위치에서 줄바꿈, 이미지 내 텍스트 인식
단점:  헤더 구분 없음
역할:  docling / pymupdf4llm 모두 실패 시 최종 fallback
```

### python-docx

```
역할:  DOCX 네이티브 파싱
       - Heading 스타일 → H1~H6 직접 변환
       - 문서 순서(단락+표) 보존
       - 전체 bold 단락 → 비공식 소제목 처리
```

### pyhwp

```
역할:  HWP 텍스트 추출
       1순위: hwp5txt CLI 사용
       2순위: olefile로 OLE 스트림 직접 파싱
       → 짧은 줄·문장부호 없는 줄을 소제목으로 휴리스틱 변환
```

---

## 파일 구조

```
doc_parser_test/
├── converter.py        # 메인 코드 (파서 + 병합 로직 + tkinter GUI, 단일 파일)
├── requirements.txt    # Python 의존성
├── Dockerfile          # Docker 이미지 정의
├── docker-compose.yml  # Docker Compose 설정 (X11 포워딩 포함)
├── run.sh              # Linux/macOS 실행 헬퍼 스크립트
├── docs/               # 변환할 문서를 여기에 넣기
└── output/             # 변환된 .md 파일 저장 위치
```

### converter.py 내부 구조

```
converter.py
├── 유틸리티 함수
│   ├── clean_md()              — 마크다운 공백 정리
│   ├── fix_table_cols()        — pymupdf4llm 빈 열 제거
│   ├── list_table_to_md()      — pdfplumber 표 → Markdown
│   └── extract_mupdf_headers() — pymupdf4llm 헤더 맵 추출
│
├── PDF 파서
│   ├── DoclingParser           — docling 기반 구조 분석
│   ├── MuPDFParser             — pymupdf4llm 기반 헤더 계층
│   └── PlumberParser           — pdfplumber 기반 텍스트
│
├── 변환기
│   ├── HybridPDFConverter      — 3개 파서 병합 로직
│   ├── DOCXConverter           — python-docx 기반 변환
│   └── HWPConverter            — pyhwp 기반 변환
│
├── App (tkinter.Tk)            — GUI 애플리케이션 (HAS_TK=True 시)
│
├── cli_main()                  — CLI 진입점
└── main()                      — GUI/CLI 자동 전환
```

---

## 플랫폼별 X11 설정

| 플랫폼 | X 서버 | 설정 |
|--------|--------|------|
| **Linux** | 내장 X11 | `xhost +local:docker` (run.sh 자동 처리) |
| **macOS** | [XQuartz](https://www.xquartz.org/) | 보안 탭 → 네트워크 클라이언트 허용, `DISPLAY=host.docker.internal:0` |
| **Windows 11** | WSLg (내장) | `DISPLAY=:0` 자동 설정 |
| **Windows 10** | [VcXsrv](https://sourceforge.net/projects/vcxsrv/) / [X410](https://x410.dev/) | `DISPLAY=<호스트IP>:0.0` |

---

## 의존성

```
docling>=2.0.0      — 문서 구조 파싱 (ML 모델 포함, 첫 실행 시 다운로드)
pymupdf4llm>=0.0.17 — 헤더 계층 추출, 표 Markdown
pdfplumber>=0.10.3  — 정확한 텍스트 추출
python-docx>=1.1.2  — DOCX 파싱
pyhwp>=0.1b15       — HWP 텍스트 추출
olefile>=0.47       — HWP OLE 스트림 fallback
```

Python 표준 라이브러리만 사용한 GUI: `tkinter` (별도 설치 불필요)

> Docker 이미지에는 위 모든 의존성과 함께 CJK 폰트(`fonts-noto-cjk`), poppler,
> X11 런타임 라이브러리가 포함되어 있습니다.
