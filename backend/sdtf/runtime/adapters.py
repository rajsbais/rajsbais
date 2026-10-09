"""SAP connectivity adapter contracts.

Only the SyntheticStoreExtractor is implemented. The RFC, OData and CDS adapters define the production
contract (partitioning, snapshot consistency, throttling, checkpoint/restart) and raise NotImplementedError
so that nobody can mistake them for working connectors. See docs/02-sap-connectivity-design.md.
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


class RfcExtractor:
    """ABAP/RFC-based extraction through a released read module in the SAP add-on (planned).

    Contract: the add-on exposes an RFC-enabled function that accepts (table, selection predicate, package
    size, cursor) and returns rows with an application-level consistency token. Authorization is enforced by
    SAP (S_TABU_NAM / S_RFC) under a least-privilege technical user. Throttling via package size and a
    server-side work-process budget.
    """

    name = "RFC"
    status = "PLANNED"

    def __init__(self, destination: str):
        self.destination = destination

    def snapshot(self) -> str:
        raise NotImplementedError("RFC adapter is planned; no SAP connectivity exists in this build")

    def partitions(self) -> list[Partition]:
        raise NotImplementedError("RFC adapter is planned; no SAP connectivity exists in this build")

    def extract(self, partition: Partition):
        raise NotImplementedError("RFC adapter is planned; no SAP connectivity exists in this build")


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
    "RFC": {"status": "PLANNED", "description": "ABAP add-on + RFC read module", "class": RfcExtractor},
    "ODATA": {"status": "PLANNED", "description": "Released OData APIs", "class": ODataExtractor},
    "CDS": {"status": "PLANNED", "description": "CDS views / ODP / supported HANA access", "class": CdsExtractor},
}
