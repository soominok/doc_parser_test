"""
LangChain 기반 RAG 챗봇 모듈

RAG (Retrieval-Augmented Generation) 파이프라인:
1. 사용자 질문 수신
2. 벡터 저장소에서 관련 문서 청크 검색 (Retrieval)
3. 검색된 문맥 + 질문을 LLM에 전달 (Augmented Generation)
4. LLM이 문맥 기반 답변 생성

프롬프트 설계 원칙:
- 검색된 문서에 근거한 답변만 생성 (할루시네이션 방지)
- 답변 불가 시 솔직히 "해당 정보가 문서에 없다"고 응답
- 출처(heading 경로, 파일명) 명시
- 한국어 답변
"""
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from config.settings import LLM_CONFIG, EMBEDDING_CONFIG
from src.embedding.vector_store import VectorStoreManager
from src.utils.logger import get_logger

logger = get_logger(__name__)

# ============================================================
# RAG 시스템 프롬프트
# ============================================================
SYSTEM_PROMPT = """당신은 사내 문서 기반 AI 비서입니다. 아래 규칙을 반드시 준수하세요.

## 역할
- 사내 문서에 기반하여 직원들의 질문에 정확하게 답변합니다.
- 제공된 '참고 문서' 내용만을 근거로 답변합니다.

## 답변 규칙
1. **근거 기반 답변**: 반드시 아래 제공된 '참고 문서'에 있는 내용만으로 답변하세요.
2. **출처 명시**: 답변 후 반드시 출처를 표기하세요. 형식: [출처: 파일명 > 섹션명]
3. **정보 부재 시**: 참고 문서에 해당 정보가 없으면, "제공된 문서에서 해당 정보를 찾을 수 없습니다."라고 솔직하게 답변하세요. 절대 추측하거나 지어내지 마세요.
4. **표 데이터**: 표에서 가져온 정보는 표 형식을 유지하여 보여주세요.
5. **명확한 언어**: 간결하고 명확한 한국어로 답변하세요.
6. **구조화된 답변**: 내용이 길면 번호나 불릿으로 구조화하세요.

## 참고 문서
{context}
"""

# ============================================================
# 사용자 질문 프롬프트
# ============================================================
HUMAN_PROMPT = """질문: {question}

위의 참고 문서를 근거로 답변해주세요. 답변 마지막에 출처를 명시해주세요."""


class RAGChatbot:
    """
    LangChain 기반 RAG 챗봇.

    구성:
    - Retriever: VectorStoreManager의 유사도 검색
    - LLM: OpenAI GPT-4o (또는 설정된 모델)
    - Chain: Retrieval → Prompt → LLM → 답변
    """

    def __init__(
        self,
        vector_store: VectorStoreManager = None,
        model_name: str = None,
        temperature: float = None,
    ):
        """
        Args:
            vector_store: 벡터 저장소 매니저 (None이면 자동 생성)
            model_name: LLM 모델명 (기본: gpt-4o)
            temperature: LLM temperature (기본: 0.1)
        """
        self.vector_store = vector_store or VectorStoreManager()
        self.model_name = model_name or LLM_CONFIG["model_name"]
        self.temperature = temperature if temperature is not None else LLM_CONFIG["temperature"]

        self._chain = None
        self._chat_history = []  # 대화 이력 (멀티턴 지원)

    def _build_chain(self):
        """
        LangChain RAG 체인을 구성합니다.

        체인 구조:
        질문 → Retriever → 문맥 포맷팅 → Prompt → LLM → 답변
        """
        if self._chain is not None:
            return self._chain

        from langchain_openai import ChatOpenAI
        from langchain_core.prompts import ChatPromptTemplate
        from langchain_core.runnables import RunnablePassthrough, RunnableLambda
        from langchain_core.output_parsers import StrOutputParser

        # LLM 초기화
        llm = ChatOpenAI(
            model=self.model_name,
            temperature=self.temperature,
            max_tokens=LLM_CONFIG.get("max_tokens", 2048),
        )

        # Retriever 가져오기
        retriever = self.vector_store.get_retriever(
            top_k=EMBEDDING_CONFIG["search_top_k"]
        )

        # 프롬프트 템플릿 구성
        prompt = ChatPromptTemplate.from_messages([
            ("system", SYSTEM_PROMPT),
            ("human", HUMAN_PROMPT),
        ])

        # 검색된 문서를 문맥 문자열로 포맷팅하는 함수
        def format_docs(docs):
            formatted = []
            for i, doc in enumerate(docs, 1):
                source = doc.metadata.get("source_file", "알 수 없음")
                heading = doc.metadata.get("heading_path", "")
                source_info = f"[문서 {i}] 출처: {Path(source).name}"
                if heading:
                    source_info += f" > {heading}"

                formatted.append(f"{source_info}\n{doc.page_content}")

            return "\n\n---\n\n".join(formatted)

        # RAG 체인 구성
        # RunnablePassthrough: 입력을 그대로 전달
        # format_docs: 검색 결과를 문자열로 변환
        self._chain = (
            {
                "context": retriever | RunnableLambda(format_docs),
                "question": RunnablePassthrough(),
            }
            | prompt
            | llm
            | StrOutputParser()
        )

        logger.info(f"RAG 체인 구성 완료 (모델: {self.model_name})")
        return self._chain

    def ask(self, question: str) -> dict:
        """
        질문에 대한 RAG 기반 답변을 생성합니다.

        처리 과정:
        1. 질문으로 벡터 저장소 검색
        2. 검색된 문맥을 프롬프트에 포함
        3. LLM으로 답변 생성
        4. 답변 + 출처 정보 반환

        Args:
            question: 사용자 질문

        Returns:
            dict: {
                "answer": str,        # LLM 답변
                "sources": list,      # 참조한 문서 출처 목록
                "query": str,         # 원본 질문
            }
        """
        chain = self._build_chain()

        logger.info(f"질문 처리: {question[:80]}...")

        # 답변 생성
        answer = chain.invoke(question)

        # 출처 정보 별도 수집 (검색된 문서의 메타데이터)
        sources = self._get_sources_for_query(question)

        # 대화 이력에 추가
        self._chat_history.append({
            "question": question,
            "answer": answer,
        })

        logger.info(f"답변 생성 완료 ({len(answer)}자)")

        return {
            "answer": answer,
            "sources": sources,
            "query": question,
        }

    def ask_with_history(self, question: str) -> dict:
        """
        대화 이력을 반영한 답변을 생성합니다 (멀티턴 대화).

        이전 대화 맥락을 포함하여 후속 질문을 처리합니다.
        예: "위에서 말한 규정의 예외 사항은?" 같은 후속 질문

        Args:
            question: 사용자 질문

        Returns:
            dict: ask()과 동일한 형식
        """
        from langchain_openai import ChatOpenAI
        from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
        from langchain_core.messages import HumanMessage, AIMessage
        from langchain_core.runnables import RunnablePassthrough, RunnableLambda
        from langchain_core.output_parsers import StrOutputParser

        llm = ChatOpenAI(
            model=self.model_name,
            temperature=self.temperature,
            max_tokens=LLM_CONFIG.get("max_tokens", 2048),
        )

        retriever = self.vector_store.get_retriever(
            top_k=EMBEDDING_CONFIG["search_top_k"]
        )

        # 대화 이력 포함 프롬프트
        prompt = ChatPromptTemplate.from_messages([
            ("system", SYSTEM_PROMPT),
            MessagesPlaceholder(variable_name="chat_history"),
            ("human", HUMAN_PROMPT),
        ])

        def format_docs(docs):
            formatted = []
            for i, doc in enumerate(docs, 1):
                source = doc.metadata.get("source_file", "알 수 없음")
                heading = doc.metadata.get("heading_path", "")
                source_info = f"[문서 {i}] 출처: {Path(source).name}"
                if heading:
                    source_info += f" > {heading}"
                formatted.append(f"{source_info}\n{doc.page_content}")
            return "\n\n---\n\n".join(formatted)

        # 대화 이력을 LangChain 메시지 형식으로 변환
        history_messages = []
        for h in self._chat_history[-5:]:  # 최근 5턴만 사용 (토큰 절약)
            history_messages.append(HumanMessage(content=h["question"]))
            history_messages.append(AIMessage(content=h["answer"]))

        # 체인 실행
        chain = (
            {
                "context": RunnableLambda(lambda x: x["question"]) | retriever | RunnableLambda(format_docs),
                "question": RunnableLambda(lambda x: x["question"]),
                "chat_history": RunnableLambda(lambda x: x["chat_history"]),
            }
            | prompt
            | llm
            | StrOutputParser()
        )

        answer = chain.invoke({
            "question": question,
            "chat_history": history_messages,
        })

        sources = self._get_sources_for_query(question)

        self._chat_history.append({
            "question": question,
            "answer": answer,
        })

        return {
            "answer": answer,
            "sources": sources,
            "query": question,
        }

    def _get_sources_for_query(self, query: str) -> list[dict]:
        """
        쿼리에 대한 검색 결과의 출처 정보를 수집합니다.

        Args:
            query: 검색 쿼리

        Returns:
            출처 정보 목록
        """
        results = self.vector_store.similarity_search(query)

        sources = []
        for r in results:
            sources.append({
                "file": r["metadata"].get("source_file", "알 수 없음"),
                "heading": r["metadata"].get("heading_path", ""),
                "score": r["score"],
            })

        return sources

    def clear_history(self):
        """대화 이력을 초기화합니다."""
        self._chat_history = []
        logger.info("대화 이력 초기화")
