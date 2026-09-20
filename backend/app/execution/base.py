from abc import ABC, abstractmethod

from ..models import AnalysisRequest, ExecutionOutcome


class ExecutionBackend(ABC):
    """执行后端抽象：真实 GEE 与离线兜底共用同一套 I/O 契约"""

    name: str = "base"

    @abstractmethod
    def execute(self, code: str, request: AnalysisRequest) -> ExecutionOutcome:
        raise NotImplementedError
