# Business process analysis from the database footprint

What a Basis-driven process review does with TAANA, DB05, DB02, DB15 and the workload transactions, the platform
does through the read-only add-on's aggregate module (`Z_SDTF_AGGREGATE`: COUNT per group, pushed down to the
system under one snapshot), so the same analysis runs on the simulated landscape today and on NPL / A4H once the
add-on is installed. Nothing is sampled on the platform side: every figure is a grouped count computed in the
source. Enterprise analyzer → *Business process analysis* (`docs/screenshots/factory-29-process-analysis.png`);
`GET /systems/{id}/process-analysis`;
`sdtf process-analysis --system <id> [--area O2C|P2P|R2R] [--bukrs 5000,1000] [--out report.md]`.

| Objective | Basis transactions | Here | Status |
|---|---|---|---|
| Identify process variances | TAANA | **Variants**: VBAK by AUART and by VKORG + AUART, LIKP by LFART, VBRK by FKART, EKKO by BSART and by EKORG + BSART, MSEG by BWART, RBKP by RBSTAT, BKPF by BLART, by BUKRS + BLART and by AWTYP, BSEG by KOART; each with the share, the cumulative share and the number of variants that make 80 % of the volume | IMPLEMENTED (simulated add-on; same contract on a live system) |
| Understand data selectivity | DB05 | **Selectivity**: distinct KUNNR in VBAK, MATNR in VBAP and MSEG, LIFNR in EKKO, HKONT / KUNNR / LIFNR in BSEG, with rows per value and the top value's share | IMPLEMENTED |
| Detect process overloads | DB02 + DB15 | **Growth**: rows per fiscal year of every table that carries a year, from the discovery statistics, with the year-over-year change; **links**: the business objects that populate each table, from the canonical model | IMPLEMENTED |
| Size the archiving and the historical scope | TAANA + DB15 | **Age profile**: BKPF, VBRK, EKKO and MSEG per year with the share older than the retention horizon (default 7 years) | IMPLEMENTED |
| Understand user execution | ST03N, STAD | **Not available**: workload statistics are not table reads (statistics cluster); the analysis says so instead of guessing. An import of the ST03N transaction profile export is planned | PLANNED |
| Workflow and interface frequencies | SWI1, SWI2_FREQ, WE02, BD87 | **Not available** through the add-on; the discovery lists the RFC destinations, IDoc partners and background jobs | PLANNED |

Company codes can be pushed down (`bukrs=5000,1000`): tables with a company-code field (VBAK via BUKRS_VF, LIKP,
VBRK, EKKO, MSEG, RBKP, BKPF, BSEG) are filtered in the system; VBAP has none and is counted whole. A table the
technical user may not read (`S_TABU_NAM`) or that does not exist on the release is listed under *not readable*
with the add-on's error, and the rest of the analysis still comes back.

How to read it for a carve-out or an S/4HANA move:

* a long tail of order, document or movement types beyond the 80 % line is a harmonisation candidate before the
  rules are written (one target variant per process, the rest mapped or excluded);
* a high archivable share of BKPF or MSEG argues for a historical policy of *open items and balances* or *fiscal
  years* instead of the full history, and sizes the archiving run before the move;
* low selectivity (few customers or materials behind most lines) tells how many master records the scope really
  carries, and where the shared-object dispositions will concentrate;
* fast-growing tables point at the processes that need the delta cycles most during the near-zero-downtime window.

Verified on the synthetic landscape (`backend/tests/test_process_analysis.py`: variant totals and the 80 % line
against the record store, selectivity and age against direct counts, pushdown, an unauthorised table reported,
API and CLI). Not yet run against NPL or A4H.
