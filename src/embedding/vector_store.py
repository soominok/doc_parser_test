"""
Embedding 및 벡터 저장소 모듈

역할:
1. 청크 텍스트를 벡터(embedding)로 변환
2. ChromaDB에 벡터 + 메타데이터 저장
3. 유사도 검색 (쿼리 → 관련 청크 검색)

사용 모델:
- OpenAI text-embedding-3-small (1536차원, 비용 효율적)
- 대안: text-embedding-3-large (3072차원, 더 정확)

벡터 저장소:
- ChromaDB (로컬 파일 기반, 별도 서버 불필요)
- 대안: FAISS, Pinecone, Weaviate
"""
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from config.settings import EMBEDDING_CONFIG, VECTORSTORE_DIR
from src.processing.chunker import Chunk
from src.utils.logger import get_logger

logger = get_logger(__name__)


class VectorStoreManager:
    """
    ChromaDB 기반 벡터 저장소를 관리합니다.

    주요 기능:
    - 청크 임베딩 및 저장
    - 유사도 검색
    - 컬렉션 관리 (생성, 삭제, 초기화)
    """

    def __init__(
        self,
        persist_directory: str | Path = None,
        collection_name: str = None,
        embedding_model: str = None,
    ):
        """
        Args:
            persist_directory: ChromaDB 저장 경로 (기본: data/vectorstore)
            collection_name: 컬렉션 이름 (기본: company_docs)
            embedding_model: OpenAI 임베딩 모델명
        """
        self.persist_directory = str(persist_directory or VECTORSTORE_DIR)
        self.collection_name = collection_name or EMBEDDING_CONFIG["collection_name"]
        self.embedding_model = embedding_model or EMBEDDING_CONFIG["model_name"]

        self._vectorstore = None
        self._embedding_function = None

    def _get_embedding_function(self):
        """
        OpenAI 임베딩 함수를 초기화합니다.
        LangChain의 OpenAIEmbeddings 래퍼를 사용합니다.
        """
        if self._embedding_function is None:
            from langchain_openai import OpenAIEmbeddings

            self._embedding_function = OpenAIEmbeddings(
                model=self.embedding_model,
                # chunk_size: 한 번의 API 호출에 보낼 텍스트 수
                # OpenAI API는 한 번에 최대 2048개 텍스트를 처리 가능
                chunk_size=500,
            )
            logger.info(f"임베딩 모델 초기화: {self.embedding_model}")

        return self._embedding_function

    def _get_vectorstore(self):
        """
        ChromaDB 벡터 저장소를 초기화하거나 기존 저장소를 로드합니다.
        """
        if self._vectorstore is None:
            from langchain_chroma import Chroma

            self._vectorstore = Chroma(
                collection_name=self.collection_name,
                embedding_function=self._get_embedding_function(),
                persist_directory=self.persist_directory,
            )
            logger.info(
                f"ChromaDB 벡터 저장소 초기화: {self.persist_directory} "
                f"(컬렉션: {self.collection_name})"
            )

        return self._vectorstore

    def add_chunks(self, chunks: list[Chunk]) -> int:
        """
        청크 목록을 임베딩하여 벡터 저장소에 추가합니다.

        각 청크는 다음과 같이 저장됩니다:
        - document: 청크 텍스트
        - metadata: 출처 파일, heading 경로, 청크 인덱스 등
        - id: 고유 식별자 (source_file + chunk_index)

        Args:
            chunks: Chunk 객체 목록

        Returns:
            추가된 청크 수
        """
        if not chunks:
            logger.warning("추가할 청크가 없습니다.")
            return 0

        from langchain_core.documents import Document

        vectorstore = self._get_vectorstore()

        # Chunk → LangChain Document 변환
        documents = []
        ids = []

        for chunk in chunks:
            doc = Document(
                page_content=chunk.content,
                metadata=chunk.metadata,
            )
            documents.append(doc)

            # 고유 ID 생성: 파일명_청크인덱스
            source = chunk.metadata.get("source_file", "unknown")
            idx = chunk.metadata.get("chunk_index", 0)
            doc_id = f"{Path(source).stem}_{idx}"
            ids.append(doc_id)

        # 벡터 저장소에 추가 (배치 단위로 임베딩 + 저장)
        vectorstore.add_documents(documents=documents, ids=ids)

        logger.info(f"벡터 저장소에 {len(documents)}개 청크 추가 완료")
        return len(documents)

    def similarity_search(
        self,
        query: str,
        top_k: int = None,
        filter_metadata: dict = None,
    ) -> list[dict]:
        """
        쿼리와 유사한 청크를 검색합니다.

        Args:
            query: 검색 쿼리 텍스트
            top_k: 반환할 결과 수 (기본: 5)
            filter_metadata: 메타데이터 필터 (특정 파일에서만 검색 등)

        Returns:
            list[dict]: 검색 결과 목록
                - "content": 청크 텍스트
                - "metadata": 메타데이터
                - "score": 유사도 점수 (낮을수록 유사)
        """
        top_k = top_k or EMBEDDING_CONFIG["search_top_k"]
        vectorstore = self._get_vectorstore()

        # 유사도 점수 포함 검색
        results = vectorstore.similarity_search_with_relevance_scores(
            query=query,
            k=top_k,
            filter=filter_metadata,
        )

        search_results = []
        for doc, score in results:
            search_results.append({
                "content": doc.page_content,
                "metadata": doc.metadata,
                "score": score,
            })

        logger.info(
            f"검색 완료: '{query[:50]}...' → {len(search_results)}개 결과 "
            f"(상위 유사도: {search_results[0]['score']:.4f})"
            if search_results else f"검색 완료: '{query[:50]}...' → 결과 없음"
        )

        return search_results

    def get_retriever(self, top_k: int = None, filter_metadata: dict = None):
        """
        LangChain Retriever 객체를 반환합니다.
        LangChain 체인에서 직접 사용할 수 있습니다.

        Args:
            top_k: 검색 결과 수
            filter_metadata: 메타데이터 필터

        Returns:
            LangChain Retriever
        """
        top_k = top_k or EMBEDDING_CONFIG["search_top_k"]
        vectorstore = self._get_vectorstore()

        search_kwargs = {"k": top_k}
        if filter_metadata:
            search_kwargs["filter"] = filter_metadata

        return vectorstore.as_retriever(
            search_type="similarity",
            search_kwargs=search_kwargs,
        )

    def get_collection_stats(self) -> dict:
        """벡터 저장소의 현재 상태를 반환합니다."""
        vectorstore = self._get_vectorstore()
        collection = vectorstore._collection

        return {
            "collection_name": self.collection_name,
            "document_count": collection.count(),
            "persist_directory": self.persist_directory,
        }

    def reset_collection(self):
        """
        컬렉션을 초기화(삭제 후 재생성)합니다.
        주의: 모든 저장된 벡터가 삭제됩니다.
        """
        from langchain_chroma import Chroma
        import chromadb

        client = chromadb.PersistentClient(path=self.persist_directory)

        # 기존 컬렉션 삭제
        try:
            client.delete_collection(self.collection_name)
            logger.info(f"컬렉션 삭제됨: {self.collection_name}")
        except Exception:
            pass

        # 벡터 저장소 재초기화
        self._vectorstore = None
        logger.info(f"벡터 저장소 초기화 완료: {self.collection_name}")
