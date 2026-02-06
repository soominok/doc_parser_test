"""
프로젝트 공통 로거 설정
모든 모듈에서 일관된 로깅 포맷을 사용합니다.
"""
import logging
import sys


def get_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    """
    모듈별 로거를 생성합니다.

    Args:
        name: 로거 이름 (보통 __name__ 사용)
        level: 로깅 레벨

    Returns:
        설정된 Logger 인스턴스
    """
    logger = logging.getLogger(name)

    # 이미 핸들러가 있으면 중복 추가 방지
    if logger.handlers:
        return logger

    logger.setLevel(level)

    # 콘솔 핸들러 설정
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)

    # 로그 포맷: [시간] [레벨] [모듈명] 메시지
    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    return logger
