"""
RAG 챗봇 실행 스크립트

사용법:
    # 대화형 챗봇 실행
    python run_chatbot.py

    # 단일 질문 답변 (비대화형)
    python run_chatbot.py --query "연차 사용 규정이 어떻게 되나요?"

    # 멀티턴 대화 모드
    python run_chatbot.py --multi-turn

주의:
    - 먼저 run_pipeline.py를 실행하여 PDF를 처리해야 합니다.
    - OPENAI_API_KEY 환경변수가 설정되어 있어야 합니다.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.chatbot.rag_chain import RAGChatbot
from src.embedding.vector_store import VectorStoreManager
from src.utils.logger import get_logger

logger = get_logger("run_chatbot")


def print_answer(result: dict):
    """답변 결과를 포맷팅하여 출력합니다."""
    print("\n" + "=" * 60)
    print(f"질문: {result['query']}")
    print("-" * 60)
    print(f"\n{result['answer']}\n")

    if result.get("sources"):
        print("-" * 60)
        print("참조 문서:")
        for i, src in enumerate(result["sources"], 1):
            file_name = Path(src["file"]).name if src["file"] != "알 수 없음" else src["file"]
            heading = src.get("heading", "")
            score = src.get("score", 0)
            print(f"  {i}. {file_name}", end="")
            if heading:
                print(f" > {heading}", end="")
            print(f" (유사도: {score:.4f})")
    print("=" * 60 + "\n")


def interactive_mode(chatbot: RAGChatbot, multi_turn: bool = False):
    """대화형 챗봇 모드를 실행합니다."""
    mode_name = "멀티턴 대화" if multi_turn else "단일 질문"
    print(f"\n{'='*60}")
    print(f"  사내 문서 AI 비서 ({mode_name} 모드)")
    print(f"  - 질문을 입력하세요.")
    print(f"  - 종료: 'quit', 'exit', 'q'")
    if multi_turn:
        print(f"  - 대화 초기화: 'clear'")
    print(f"{'='*60}\n")

    while True:
        try:
            question = input("질문: ").strip()

            if not question:
                continue
            if question.lower() in ("quit", "exit", "q"):
                print("챗봇을 종료합니다.")
                break
            if multi_turn and question.lower() == "clear":
                chatbot.clear_history()
                print("대화 이력이 초기화되었습니다.\n")
                continue

            # 답변 생성
            if multi_turn:
                result = chatbot.ask_with_history(question)
            else:
                result = chatbot.ask(question)

            print_answer(result)

        except KeyboardInterrupt:
            print("\n\n챗봇을 종료합니다.")
            break
        except Exception as e:
            print(f"\n오류 발생: {e}\n")
            logger.error(f"챗봇 오류: {e}", exc_info=True)


def main():
    parser = argparse.ArgumentParser(
        description="RAG 기반 사내 문서 챗봇",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--query", "-q",
        type=str,
        help="단일 질문 (비대화형 모드)",
    )
    parser.add_argument(
        "--multi-turn", "-m",
        action="store_true",
        help="멀티턴 대화 모드 활성화 (이전 대화 맥락 유지)",
    )

    args = parser.parse_args()

    # 벡터 저장소 상태 확인
    vector_store = VectorStoreManager()
    try:
        stats = vector_store.get_collection_stats()
        doc_count = stats.get("document_count", 0)
        if doc_count == 0:
            print(
                "\n주의: 벡터 저장소가 비어있습니다.\n"
                "먼저 'python run_pipeline.py'를 실행하여 PDF를 처리해주세요.\n"
            )
            sys.exit(1)
        logger.info(f"벡터 저장소: {doc_count}개 문서 로드됨")
    except Exception as e:
        print(f"\n벡터 저장소 접근 오류: {e}")
        print("먼저 'python run_pipeline.py'를 실행하여 PDF를 처리해주세요.\n")
        sys.exit(1)

    # 챗봇 초기화
    chatbot = RAGChatbot(vector_store=vector_store)

    if args.query:
        # 단일 질문 모드
        result = chatbot.ask(args.query)
        print_answer(result)
    else:
        # 대화형 모드
        interactive_mode(chatbot, multi_turn=args.multi_turn)


if __name__ == "__main__":
    main()
