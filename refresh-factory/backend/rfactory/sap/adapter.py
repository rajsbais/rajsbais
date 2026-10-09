"""System model and adapter contracts.

`SourceAdapter` is deliberately read-only (no mutating method exists on it), which makes
"production read-only enforcement for extraction" a structural property, not a convention.
`TargetAdapter` adds writes and refuses to operate on production-role systems.
"""
from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Any, Callable, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class SystemRole(str, Enum):
    PRD = "PRD"
    QAS = "QAS"
    DEV = "DEV"
    UAT = "UAT"
    SBX = "SBX"
    TRN = "TRN"


class SapSystem(BaseModel):
    id: str = ""
    sid: str
    client: str
    role: SystemRole
    product: str = "SAP ECC 6.0 EHP8"
    release: str = "731"
    db_type: str = "HANA"
    db_version: str = "2.00.059"
    os: str = "SLES 15"
    app_servers: list[str] = Field(default_factory=list)
    owner: str = ""
    adapter: str = "simulated"  # simulated | rfc | odata | abap-agent
    writable_target_allowed: bool = True  # owner-controlled switch; ignored for PRD
    tags: list[str] = Field(default_factory=list)

    @property
    def is_production(self) -> bool:
        return self.role == SystemRole.PRD

    @property
    def can_be_write_target(self) -> bool:
        return (not self.is_production) and self.writable_target_allowed

    @property
    def family(self) -> str:
        """Product family drives the object model: ECC (KNA1/BKPF) vs S4 (Business Partner + ACDOCA)."""
        return "S4" if "S/4" in self.product.upper() else "ECC"

    @property
    def label(self) -> str:
        return f"{self.sid}/{self.client} ({self.role.value})"


class ProductionWriteBlocked(PermissionError):
    pass


class ChangeLogGap(RuntimeError):
    """The requested change-log position is older than the retained log (archived/purged change documents)."""


class TransientError(RuntimeError):
    """Retryable infrastructure error (RFC timeout, lock, etc.)."""


Row = dict[str, Any]


@runtime_checkable
class SourceAdapter(Protocol):
    system: SapSystem

    def reference_date(self) -> date: ...
    def discover(self) -> dict: ...
    def select(self, table: str, predicate: Callable[[Row], bool] | None = None) -> list[Row]: ...
    def lookup(self, table: str, field: str, value: Any) -> list[Row]: ...
    def get(self, table: str, key: tuple) -> Row | None: ...
    def count(self, table: str) -> int: ...
    def table_counts(self) -> dict[str, int]: ...
    def change_seq(self) -> int: ...
    def changes_since(self, seq: int) -> list[dict]: ...


@runtime_checkable
class TargetAdapter(SourceAdapter, Protocol):
    def assert_writable(self) -> None: ...
    def upsert(self, table: str, rows: list[Row]) -> None: ...
    def delete(self, table: str, key: tuple) -> None: ...
    def number_level(self, obj: str) -> int | None: ...
    def set_number_level(self, obj: str, level: int) -> None: ...
    def owner_of(self, table: str, key_str: str) -> str | None: ...
    def outbound_interfaces(self) -> list[dict]: ...


class ReadOnlyView:
    """Structural read-only guard handed to every extraction code path.

    Only the read methods of `SourceAdapter` are forwarded; anything else (upsert, delete, set_number_level ...)
    raises AttributeError, so a production source can never be written through this object.
    """

    _READ = {"system", "reference_date", "discover", "select", "lookup", "get", "count", "table_counts", "change_seq",
             "changes_since", "select_in", "select_between", "capabilities", "kind", "change_coverage"}

    def __init__(self, inner: SourceAdapter):
        object.__setattr__(self, "_inner", inner)

    def __getattr__(self, name: str):
        if name not in self._READ:
            raise AttributeError(f"{name!r} is not available on a read-only source view")
        return getattr(self._inner, name)

    def __setattr__(self, name, value):
        raise AttributeError("read-only view")
