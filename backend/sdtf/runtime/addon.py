"""One way to get an add-on client for a registered system: the simulated add-on over the system's record store
for synthetic systems and simulated transports, pyrfc for a live one. Shared by discovery, process analysis
and the reconciliation views."""

from __future__ import annotations

from sqlalchemy.orm import Session

from ..catalog.store import RecordStore
from ..models import SapSystem
from .rfc import AbapAddonClient, RfcTransport, SimulatedAbapAddon, make_transport


def uses_simulator(system: SapSystem) -> bool:
    rfc = (system.meta or {}).get("rfc") or {}
    return system.connector == "SYNTHETIC" or rfc.get("transport") == "simulated" or (system.connector == "API" and not rfc)


def transport_for(session: Session, system: SapSystem, tables: list[str] | None = None) -> RfcTransport:
    if uses_simulator(system):
        rfc = (system.meta or {}).get("rfc") or {}
        return SimulatedAbapAddon(RecordStore.load(session, system.id, tables=tables), allowed_tables=set(rfc["allowed_tables"]) if rfc.get("allowed_tables") else None)
    return make_transport(system.sid, system.meta, store_loader=lambda: RecordStore.load(session, system.id, tables=tables))


def client_for(session: Session, system: SapSystem, tables: list[str] | None = None, package_size: int | None = None, snapshot: bool = True) -> tuple[AbapAddonClient, str]:
    """An add-on client with a snapshot open on `tables` (every table when None), and the transport's name."""
    transport = transport_for(session, system, tables)
    client = AbapAddonClient(transport, package_size=package_size)
    if snapshot:
        client.open_snapshot(tables)
    return client, getattr(transport, "name", "?")
