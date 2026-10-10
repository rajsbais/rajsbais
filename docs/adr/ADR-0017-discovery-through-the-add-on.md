# ADR-0017: Discovery through the read-only add-on

## Status
Accepted (verified on the simulated add-on; not yet run against NPL or A4H).

## Context
Discovery operated on the platform's record store: a full copy of the source's tables, which exists for the
synthetic landscape and never for a real system. Connecting NPL or A4H would have produced an empty landscape.
Scanning a real system's tables into the platform to discover it is neither acceptable (volume, footprint) nor
necessary: the figures discovery needs are counts and distributions the database can compute.

## Decision
An RFC source is discovered through the add-on (`discovery/rfc_discovery.py`), with the same snapshot structure
as the record-store discovery so every page and agent reads both alike:

* the organisational tables (T001, T001K, T001W, TVKO, T024E, TKA01, TKA02, CSKS, CEPC) and the interface tables
  (RFCDES, EDPP1, TBTCO) are read in full through `Z_SDTF_READ_PACKAGE`; a table above 50,000 rows is sampled
  with one package and said so;
* the tables of the catalogue are sized with `Z_SDTF_TABLE_METADATA` (DD02L existence, `DB_GET_TABLE_SIZE` rows
  and size); the custom tables come from DD02L (`TABNAME Z*/Y*`, `TABCLASS TRANSP`, `AS4LOCAL A`, capped at 200)
  with their texts from DD02T and their fields from DD03L;
* the distribution by company code and fiscal year is counted in the system with `Z_SDTF_AGGREGATE` (group by the
  organisational or year field; plants mapped to company codes through T001K);
* the business object inventory is counted the same way, and every header row of every business object is read
  with the dependent rows its status and company codes need (items, company-code segments, document flow), read by
  key in chunks of the headers: the scope engine classifies from this instance index, so it has to be complete.
  A quick look (`?sample=N`, `--sample N`) reads only the first N instances per object type, extrapolates the open
  and shared figures and labels them (`sampled`, `open_estimated`, `shared_estimated`, `read.complete=false`); the
  scope evaluation and the manifest creation refuse to work from a sampled discovery (409) until a full one runs;
* the cross-company document count is one `COUNT` with `BVORG NE ''`; complexity uses the same formula as before;
* what the technical user may not read (`S_TABU_NAM`) is listed under `read.unreadable` with the add-on's error
  and the rest is still discovered; a live source without a destination fails the discovery honestly (502).

`POST /systems/{id}/discover` picks the path by connector (RFC → add-on, else record store) and `?path=` overrides
it; `sdtf discover --system` does the same. The simulated add-on answers DD02L, DD02T and DD03L from the table
catalogue and the store's custom tables, so the DDIC reads are exercised on the synthetic landscape; on a real
system the technical user needs `S_TABU_NAM` for the DD* tables as well.

## Consequences
* A full discovery of a live source transfers the header rows of every business object plus their dependent rows
  (the line items of the documents among them): this is the instance index the carve-out is scoped from, and the
  read metrics (`read.rows_read`, `read.rfc_calls`) say what it cost. Reading the open-item status from BSID / BSIK
  instead of BSEG, and the document company codes from aggregates, is the next reduction (backlog).
* A sampled discovery is a quick look only; its estimates are labelled and the scope engine refuses it.
* The record-store discovery stays for synthetic systems and API targets (their copies are the platform's own).
