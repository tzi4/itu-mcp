"""Runs in the importing host process; never calls an LLM API."""

from .service import PaperBridge
from .storage import DocumentStore, Rights

__all__ = ["PaperBridge", "DocumentStore", "Rights"]
__version__ = "0.1.0.dev5"
