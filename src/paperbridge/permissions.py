"""Operator-recorded document/provider permissions, never model-supplied grants."""
from __future__ import annotations

import json
from .browser import RetrievalUnavailable
from .identifiers import normalize_doi
from .storage import Rights


def read_rights(store, doi: str, provider: str) -> Rights:
    """Read operator configuration, never a tool-supplied permission grant."""
    try:
        record = None
        # An explicit document record overrides the operator's optional
        # provider policy, including explicit denial. Neither is an MCP input.
        for filename, key in [("publisher-rights.json", normalize_doi(doi)),
                              ("publisher-policy.json", provider)]:
            path = store.root / filename
            if not path.exists():
                continue
            with path.open("rb") as stream:
                raw = stream.read(1_000_001)
            if len(raw) > 1_000_000:
                raise ValueError()
            record = json.loads(raw).get(key)
            if record is not None:
                break
        if record is None:
            raise RetrievalUnavailable("document_permission_required")
        rights = Rights(**record)
    except RetrievalUnavailable:
        raise
    except (OSError, ValueError, TypeError, AttributeError):
        raise RetrievalUnavailable("document_permission_required") from None
    if not rights.local_processing or not rights.external_model:
        raise RetrievalUnavailable("document_permission_required")
    return rights

