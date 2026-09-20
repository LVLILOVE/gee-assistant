from .base import ExecutionBackend
from .gee import GEEBackend
from .offline import OfflineBackend

_BACKENDS: dict[str, ExecutionBackend] = {
    "offline": OfflineBackend(),
    "gee": GEEBackend(),
}


def get_backend(name: str | None = None) -> ExecutionBackend:
    key = name or "offline"
    return _BACKENDS.get(key, _BACKENDS["offline"])


__all__ = ["ExecutionBackend", "OfflineBackend", "GEEBackend", "get_backend"]
