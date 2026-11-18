# 청년 주거 에이전트

청년이 전월세 계약 전에 쓰는 AI 에이전트입니다.
지역·건물명, 보증금, 나이·소득 구간을 넣으면 전세가율로 계약 위험도를 진단하고,
관련 법·가이드 근거와 신청할 수 있는 청년 주거 지원 정책을 찾아
"계약 전 체크리스트 + 신청할 정책 목록"을 보여줍니다.

> 대학 수업(생성형 AI 활용) 기말 팀 과제입니다. 실제 서비스가 아니며, 시연과 테스트에는 가상 데이터만 씁니다.

무엇을 어떻게 만드는지(기능, 구조, 연결 규칙, 코딩 규칙)는 [AGENTS.md](AGENTS.md)에 있습니다.

## 준비

- Python 3.11 이상
- API 키
  - OpenAI API 키
  - 공공데이터포털 국토교통부 실거래가 API 인증키
  - 온통청년 청년정책 Open API 인증키

## 설치

```bash
git clone <저장소 주소>
cd salzip

python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

## 환경 변수

`.env.example`을 복사해 `.env`를 만들고 키를 채웁니다. `.env`는 커밋하지 않습니다.

```bash
cp .env.example .env
```

| 변수 | 설명 |
|---|---|
| `OPENAI_API_KEY` | 에이전트가 쓰는 OpenAI API 키 |
| `LLM_MODEL` | 사용할 GPT 모델 ID |
| `MOLIT_API_KEY` | 국토교통부 실거래가 API 인증키 |
| `YOUTHCENTER_API_KEY` | 온통청년 청년정책 API 인증키 |
| `AGENT_MAX_STEPS` | 에이전트 루프 최대 반복 횟수 |

## 실행

```bash
streamlit run app.py
```

## 테스트

```bash
# 기본: 외부 API를 부르지 않는 테스트만 실행
pytest

# 외부 API를 실제로 부르는 테스트만 실행 (키 필요)
pytest -m external
```

## 폴더 구조

```
app.py          Streamlit 화면
agent/          에이전트 루프, 시스템 프롬프트, 도구 등록
tools/          LLM이 골라 쓰는 도구 (시세, 법, 정책, 보고서)
rag/            문서 수집·분할·임베딩·검색
data/docs/      RAG 원본 문서 (출처 표기 필수)
tests/          도구별 테스트, 시나리오 테스트
```

## 역할

| 담당 | 맡은 부분 |
|---|---|
| A | 에이전트 코어, 화면, `submit_report` |
| B | `search_price`, `calc_jeonse_ratio` |
| C | `search_law`, RAG |
| D | `search_policy`, `check_eligibility`, 테스트 시나리오 |

## 브랜치와 커밋

- `main`에 바로 커밋하지 않습니다. 각자 자기 브랜치를 만들어 작업하고,
  PR을 열어 리뷰를 받은 뒤 `main`에 합칩니다. 자세한 규칙은 AGENTS.md 6장 Git을 봅니다.
- 커밋 메시지는 한국어로, 앞에 종류를 붙입니다: `기능:`, `수정:`, `리팩터링:`, `테스트:`, `문서:`
