"""Leonervis Code public package API and metadata."""

from leonervis_code.session import (
    AutoCompactionCommitted,
    AutoCompactionNotApplied,
    AutoCompactionStarted,
    CompactContextResult,
    EffectiveContextInspection,
    ProjectSession,
    ResumeEffect,
    SessionResumeConflictError,
    SessionResumeContextError,
    SessionResumeResult,
)
from leonervis_code.session_store import SessionInfo, SessionLockedError, SessionStoreError

__version__ = "0.1.0"

__all__ = [
    "AutoCompactionCommitted",
    "AutoCompactionNotApplied",
    "AutoCompactionStarted",
    "CompactContextResult",
    "EffectiveContextInspection",
    "ProjectSession",
    "ResumeEffect",
    "SessionInfo",
    "SessionLockedError",
    "SessionResumeConflictError",
    "SessionResumeContextError",
    "SessionResumeResult",
    "SessionStoreError",
    "__version__",
]
