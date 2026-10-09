# The ABAP loader (write side for a non-production SAP system)

**Status: the ABAP in `docs/abap/ZRF_LOADER.abap` has never been compiled or run.** It is the contract the platform follows, written so that the system's owner can read exactly what it does. Standard RFC cannot write arbitrary tables, so a custom function group is unavoidable. Install it **only in a sandbox** (for example the NPL ABAP trial), read it first, and expect to fix syntax errors.

## What the platform guarantees on its side
- Only a connected RFC system that is **not production** can be requested as a target, and only a **different person with `target:approve`** (security officer) can enable it. The requester can attest that outbound interfaces and jobs of the system are inactive; without that attestation every refresh is held at the release gate (the platform cannot see a remote system's interfaces).
- Before every run the platform runs the handshake `ZRF_PING`: protocol `RFL-1`, the SID and client it expects, a client category that is **not `P`**, writes enabled by the SAP-side owner, and the allow-listed tables. Anything else blocks writing.
- Writes go through `ZRF_UPSERT`, `ZRF_DELETE` and `ZRF_NR_SET` only (the function allow-list), in batches of at most 200 rows, each preceded by a dry run of the same rows, and are all-or-nothing per batch. The adapter keeps a record of every write.
- Revoking write access (UI or `POST /api/systems/{sid}/write-revoke`) takes effect immediately.

## What the SAP-side owner controls
Nothing is writable until the owner: creates the objects listed at the top of the ABAP file, switches the loader on (`ZRF_CFG`, `ENABLED = 'X'`), puts each table on the allow list (`ZRF_ALLOW`), and gives the connected user `S_TABU_NAM` activity 02 for those tables. Every call is logged in `ZRF_LOG`.

## Steps (sandbox)
1. Create the DDIC objects (SE11), the function group `ZRF` and the four RFC-enabled function modules (SE80/SE37) from the ABAP file; fix what does not compile; test each module in SE37, first with `IV_DRY_RUN = 'X'`.
2. Add a few tables to `ZRF_ALLOW` (start with one demo table such as `SCARR`), then set `ZRF_CFG-ENABLED = 'X'`.
3. In the platform: connect the system (Landscape), **Request write access**, then sign in as the security officer and **Approve**.
4. Run a small refresh into it, and read `ZRF_LOG`.

## Known gaps
Number ranges are raised by updating `NRIV` directly (unverified; buffers need a reset, and `NUMBER_RANGE_INTERVAL_UPDATE` should replace it). Ownership/reservations are not modelled for a remote target, and the platform cannot inspect outbound interfaces (hence the attestation). Lean client, post-copy and full refresh remain simulator-only.
