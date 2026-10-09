"""Change-document reader (CDHDR) for remote SAP systems: tells the delta engine WHICH business objects changed since a watermark.

What is read, and what deliberately is not
  * CDHDR (object class, object id, change number, date, time, change indicator): enough to name the changed object. The user name,
    transaction code and CDPOS (old/new values, a cluster table on ECC) are NOT read: the delta engine only needs "this object changed",
    and the values would carry personal data the platform has no use for.
  * TCDOB (which tables belong to which object class) is read to learn what the system really logs. An object type counts as
    covered only if its header table is logged under the expected class. Everything else (immutable documents such as material
    documents, objects whose class is not known here) is reported as UNCOVERED and the delta engine compares it by content instead.

Why the watermark looks the way it does
  Change documents have no global sequence number: the watermark is the source system's own date and time (YYYYMMDDHHMMSS as an
  integer). A change document is written before its transaction commits, so a reader that trusts "now" can miss a change that becomes
  visible later with an earlier timestamp. The reader therefore never reads up to now: it stops `lag_seconds` behind the system clock
  and re-reads `overlap_seconds` before the previous watermark. Re-reading is harmless (the engine compares content hashes), missing is
  not. Anything longer than the lag (a transaction open for hours) is still missed: the periodic full sweep is the safety net.

STATUS: exercised only against FakeRfcTransport, which serves CDHDR/TCDOB from a simulated change log. Never run against a real system;
real CDHDR volumes, authorisations (S_TABU_DIS for the change-document classes) and object-id formats are unknown.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from ..adapter import ChangeLogGap

# object class -> (header table whose key is the OBJECTID, tables whose changes are logged under that class)
CLASSES: dict[str, tuple[str, tuple[str, ...]]] = {
    "DEBI": ("KNA1", ("KNA1", "KNB1", "KNVV", "KNBK", "ADRC")),
    "KRED": ("LFA1", ("LFA1", "LFB1", "LFBK", "ADRC")),
    "MATERIAL": ("MARA", ("MARA", "MAKT", "MARC")),
    "VERKBELEG": ("VBAK", ("VBAK", "VBAP")),
    "LIEFERUNG": ("LIKP", ("LIKP", "LIPS")),
    "FAKTBELEG": ("VBRK", ("VBRK", "VBRP")),
    "BELEG": ("BKPF", ("BKPF", "BSEG")),
    "EINKBELEG": ("EKKO", ("EKKO", "EKPO")),
}
HEADER_CLASS = {h: c for c, (h, _t) in CLASSES.items()}
FMT = "%Y%m%d%H%M%S"


def to_mark(dt: datetime) -> int:
    return int(dt.strftime(FMT))


def from_mark(mark: int) -> datetime:
    return datetime.strptime(str(int(mark)).rjust(14, "0"), FMT)


class ChangeDocReader:
    def __init__(self, adapter, *, lag_seconds: int = 120, overlap_seconds: int = 300, retention_days: int = 90):
        self.a = adapter
        self.lag, self.overlap, self.retention = lag_seconds, overlap_seconds, retention_days
        self._covered: dict[str, str] | None = None  # header table -> object class
        self.unreadable: str | None = None
        self.last: dict = {}

    # ------------------------------------------------------------ system clock and coverage
    def source_now(self) -> datetime:
        info = self.a._call("RFC_SYSTEM_INFO").get("RFCSI_EXPORT", {})
        d, t = str(info.get("RFCDATE", "")).strip(), str(info.get("RFCTIME", "")).strip()
        if len(d) != 8 or len(t) != 6:
            raise ChangeLogGap("the source system did not report its date and time (RFC_SYSTEM_INFO), so a change-document watermark cannot be set")
        return datetime.strptime(d + t, FMT)

    def coverage(self) -> dict[str, str]:
        """header table -> object class, for every class this system really logs for its expected header table."""
        if self._covered is None:
            try:
                rows = self.a._fetch("TCDOB", ["OBJECT", "IN", "("] + [_q(c) + ("," if i < len(CLASSES) - 1 else "") for i, c in enumerate(CLASSES)] + [")"],
                                     ["OBJECT", "TABNAME"])
            except Exception as e:  # noqa: BLE001 - authorisation, missing table, network
                self.unreadable = f"TCDOB could not be read ({type(e).__name__}: {e})"
                self._covered = {}
                return self._covered
            logged = {(r["OBJECT"], r["TABNAME"]) for r in rows}
            self._covered = {h: c for c, (h, _t) in CLASSES.items() if (c, h) in logged}
        return dict(self._covered)

    # ------------------------------------------------------------ watermark
    def watermark(self) -> int:
        """The position to hand to the delta engine BEFORE reading: the source clock minus the lag."""
        return to_mark(self.source_now() - timedelta(seconds=self.lag))

    def read(self, since: int) -> list[dict]:
        """Changed objects since `since` as [{table, op, key}] in the shape the delta engine already consumes. Raises ChangeLogGap when unsure."""
        cov = self.coverage()
        if not cov:
            raise ChangeLogGap(self.unreadable or "the system logs none of the object classes this platform can map")
        now = self.source_now()
        upto = now - timedelta(seconds=self.lag)
        lo = from_mark(since) - timedelta(seconds=self.overlap)
        if lo < now - timedelta(days=self.retention):
            raise ChangeLogGap(f"the watermark is older than the {self.retention}-day retention assumed for change documents")
        classes = sorted(cov.values())
        toks = ["UDATE", "BETWEEN", _q(lo.strftime("%Y%m%d")), "AND", _q(upto.strftime("%Y%m%d")), "AND", "OBJECTCLAS", "IN", "("]
        toks += [_q(c) + ("," if i < len(classes) - 1 else "") for i, c in enumerate(classes)] + [")"]
        try:
            rows = self.a._fetch("CDHDR", toks, ["OBJECTCLAS", "OBJECTID", "CHANGENR", "UDATE", "UTIME", "CHANGE_IND"])
        except Exception as e:  # noqa: BLE001 - too many rows, authorisation, network
            raise ChangeLogGap(f"CDHDR could not be read ({type(e).__name__}: {e}); falling back to a full compare") from e
        by_class = {c: h for h, c in cov.items()}
        out, seen = [], set()
        for r in rows:
            try:
                at = datetime.strptime(str(r["UDATE"]).replace("-", "") + (str(r["UTIME"]).strip() or "000000"), FMT)
            except ValueError:
                raise ChangeLogGap(f"change document {r['OBJECTCLAS']}/{r['CHANGENR']} has an unreadable date or time")
            if not lo < at <= upto:
                continue
            header = by_class[r["OBJECTCLAS"]]
            key = self._parse_id(header, r["OBJECTID"])
            op = (r["CHANGE_IND"] or "U").strip() or "U"
            sig = (header, tuple(key.values()), op)
            if sig in seen:
                continue
            seen.add(sig)
            out.append({"table": header, "op": op if op in ("I", "U", "D") else "U", "key": key})
        self.last = {"from": lo.isoformat(), "to": upto.isoformat(), "documents": len(rows), "objects": len(out), "classes": classes}
        return out

    def _parse_id(self, header: str, object_id: str) -> dict:
        """OBJECTID is the header key fields concatenated at their DDIC widths (no separators)."""
        meta = self.a.meta(header)
        oid, pos, key = str(object_id).rstrip(), 0, {}
        total = sum(meta.width(k) for k in meta.keys)
        padded = str(object_id).ljust(total)
        if len(oid) > total:
            raise ChangeLogGap(f"object id '{oid[:30]}' is longer than the {header} key ({total}): the key layout differs from the DDIC model")
        for k in meta.keys:
            w = meta.width(k)
            key[k] = meta.internal(k, padded[pos:pos + w])
            pos += w
        return key


def _q(v: str) -> str:
    return "'" + str(v).replace("'", "''") + "'"
