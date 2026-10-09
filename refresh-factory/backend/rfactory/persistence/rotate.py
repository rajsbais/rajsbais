"""Offline key rotation for the state database.

  python -m rfactory.persistence.rotate --data-dir DIR --rotate-data-key
  python -m rfactory.persistence.rotate --data-dir DIR --new-key-env NEW_KEY_VAR     # re-wrap the data key under a new KEK

The old KEK is taken from RFACTORY_STATE_KEY or the key file. After a KEK rotation with --new-key-env, start the platform with
RFACTORY_STATE_KEY set to the new value. If the KEK lives in the key file (no environment variable), pass --write-key-file and the file is replaced.
Stop the platform first: this tool does not coordinate with a running writer.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from cryptography.fernet import Fernet

from .store import LocalKek, StateStore, StoreError


def rotate(data_dir: Path, new_kek: bytes | None = None, rotate_data_key: bool = False, write_key_file: bool = False) -> dict:
    data_dir = Path(data_dir)
    db = data_dir / "state.db"
    kf = db.with_suffix(".key")
    old = os.environ.get("RFACTORY_STATE_KEY", "").encode() or (kf.read_bytes().strip() if kf.exists() else b"")
    if not old:
        raise StoreError("no current key: set RFACTORY_STATE_KEY or provide the key file")
    st = StateStore.open_for_maintenance(db, old)
    out: dict = {}
    if rotate_data_key:
        out["blobs_reencrypted"] = st.rotate_dek()
    if new_kek is not None:
        Fernet(new_kek)  # validates the key format before anything is changed
        st.rotate_kek(LocalKek(new_kek))
        if write_key_file:
            tmp = kf.with_suffix(".key.new")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(new_kek)
            os.replace(tmp, kf)
        out["kek_rotated"] = True
    if not out:
        raise StoreError("nothing to do: pass --rotate-data-key and/or --new-key-env")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--rotate-data-key", action="store_true")
    ap.add_argument("--new-key-env", help="name of an environment variable holding the new Fernet key")
    ap.add_argument("--write-key-file", action="store_true")
    a = ap.parse_args(argv)
    new = os.environ[a.new_key_env].encode() if a.new_key_env else None
    try:
        print(rotate(Path(a.data_dir), new, a.rotate_data_key, a.write_key_file))
    except StoreError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
