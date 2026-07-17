"""Leonervis Code public package API and metadata."""

from leonervis_code.session import ProjectSession
from leonervis_code.session_store import SessionInfo, SessionLockedError, SessionStoreError

__version__ = "0.1.0"

__all__ = [
    "ProjectSession",
    "SessionInfo",
    "SessionLockedError",
    "SessionStoreError",
    "__version__",
]
