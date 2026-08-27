from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class StoredEvidence:
    storage_key: str
    encrypted: bool
    size: int


class EvidenceStorage(Protocol):
    """Boundary for a future encrypted local or S3-compatible evidence archive."""

    async def put(self, object_name: str, content: bytes) -> StoredEvidence: ...  # pragma: no cover

    async def get(self, storage_key: str) -> bytes: ...  # pragma: no cover

    async def delete(self, storage_key: str) -> None: ...  # pragma: no cover
