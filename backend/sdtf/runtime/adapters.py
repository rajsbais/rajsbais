"""SAP connectivity adapter contracts.

Implemented: `SyntheticStoreExtractor` (in-platform record store) and `RfcExtractor` (SAP add-on through RFC,
runtime/extraction.py + runtime/rfc.py; verified against the simulated add-on, not yet against a live SAP
system). The OData and CDS adapters define the production contract (partitioning, snapshot consistency,
throttling, checkpoint/restart) and raise NotImplementedError so that nobody can mistake them for working
connectors. See docs/02-sap-connectivity-design.md.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Protocol


@dataclass(frozen=True)
class Partition:
    id: str
    table: str
    object_type: str
    predicate: dict = field(default_factory=dict)
    est_rows: int = 0


@dataclass
class ExtractedRecord:
    table: str
    key: str
    payload: dict
    object_node: str


class Extractor(Protocol):
    name: str
    status: str  # IMPLEMENTED | SIMULATED | PLANNED

    def snapshot(self) -> str: ...
    def partitions(self) -> list[Partition]: ...
    def extract(self, partition: Partition) -> Iterable[ExtractedRecord]: ...


class ODataExtractor:
    """Released OData / API extraction (planned). Suitable for master data and open documents where SAP
    provides released read APIs; not suitable for bulk historical line items."""

    name = "ODATA"
    status = "PLANNED"

    def __init__(self, base_url: str):
        self.base_url = base_url

    def snapshot(self) -> str:
        raise NotImplementedError("OData adapter is planned")

    def partitions(self) -> list[Partition]:
        raise NotImplementedError("OData adapter is planned")

    def extract(self, partition: Partition):
        raise NotImplementedError("OData adapter is planned")


class CdsExtractor:
    """CDS-view / SQL-level extraction via supported HANA access or ODP (planned). Used for high-volume
    historical tables with pushdown of company-code / fiscal-year predicates."""

    name = "CDS"
    status = "PLANNED"

    def __init__(self, connection: str):
        self.connection = connection

    def snapshot(self) -> str:
        raise NotImplementedError("CDS adapter is planned")

    def partitions(self) -> list[Partition]:
        raise NotImplementedError("CDS adapter is planned")

    def extract(self, partition: Partition):
        raise NotImplementedError("CDS adapter is planned")


ADAPTER_REGISTRY = {
    "SYNTHETIC": {"status": "SIMULATED", "description": "In-platform synthetic record store; no SAP system involved"},
    "RFC": {"status": "IMPLEMENTED", "description": "ABAP add-on read modules over RFC (pyrfc) or the simulated add-on; not yet verified against a live SAP system", "note": "unverified against a live SAP system"},
    "ODATA": {"status": "PLANNED", "description": "Released OData APIs", "class": ODataExtractor},
    "CDS": {"status": "PLANNED", "description": "CDS views / ODP / supported HANA access", "class": CdsExtractor},
}
