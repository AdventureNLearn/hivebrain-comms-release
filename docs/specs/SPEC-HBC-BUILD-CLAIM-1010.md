# SPEC-HBC-BUILD-CLAIM-1010 (Spec, rev 3 sealed Sat 10 Oct 2026, ~6:15 PM ET; rev 2 `71f7ff84…ddea`, rev 1 `cc80a94a…3bdd` in backups/)
Target class: ops-only. Owner: Grok Bot lane. Source: Grok Bot brief 3:32 PM ET, from Review's root cause of the false live rollback at 3:24 PM. Design only. Aligns with L-PROTO-1010 rev 4 (`fcba9145…c3aef`) and SIG-LOCK-1010 rev 3.

## Problem
After a restart, the Build pane's `grok --continue` watcher reads LEAD-TO-BUILD.md and GROKBOT-TO-BUILD.md directly. It never checks the hub's seen-list, so it re-runs jobs that have already finished.

## Goal
Every job runs **at most once**, even across restarts and crashes. The watcher never acts without the hub's go.

## (a) Primary: claim endpoint (RULED)
- **Endpoint:** `POST /api/claim` with body `{id, sig}` on the hub port, bound to 127.0.0.1 only. A non-localhost request is refused.
- **Order of checks:** the hub loads the job by `id` from the bus file and runs the full L-PROTO rev 4 check order: signature, then fields and priority, then time and age, then routed, then duplicate. The `sig` must match the job's stored signature. A mismatch is SPOOFED.
- **Approve gate:** the hub answers `go` only for a job the person approved through the existing approve flow, or one that the current policy allows without approval. Otherwise the answer is `WAIT NEEDS_APPROVE`.
- **Write before answering:** before it answers `go`, the hub records the id in the seen-list on disk as `CLAIMED`, with time and pane. The write is fsynced, using a temp file and rename. If the write fails, the answer is `REFUSED STORE_ERROR`, never `go`.
- **Replies are verdict-first:** `GO`, `WAIT NEEDS_APPROVE`, `DUPLICATE`, `SPOOFED`, `REFUSED STALE`, `REFUSED BAD_TIME`, `REFUSED MALFORMED_TIME`, or another rev 4 reason. A second claim on a `CLAIMED` or finished id gets `DUPLICATE`.
- **After the job:** when the return block is written, the hub moves the id from `CLAIMED` to its final verdict (RAN, STOPPED, ROLLED-BACK and so on) and records the content hash under rev 4 P2c.
- **Fail closed:** if the hub is down, unreachable or answers anything but `GO`, the watcher does nothing and writes no return for that job. The watcher prompt states this as a hard rule: **no GO, no action**.
- **Signing:** the claim endpoint never signs anything and never reveals keys or signatures in its replies or logs.

## (b) Defense in depth: archive and resume (RULED)
- Once a return block is written, the hub moves the finished job block out of the live bus file into `<FILE>-ARCHIVE-YYYY-MM-DD.md` (date in ET).
  - The move appends to the archive first, then rewrites the live file atomically, under a file lock.
  - Text that senders append during the move is kept.
  - A job whose return isn't written stays in the live file.
- On resume, the watcher reads the return files and the archive index first. It skips any id that already has a return, then claims each remaining id. The claim stays the real gate.

## Routing: watcher or hub (RULED, rev 2, on Implement's question 4:35 PM)
- **Signed fields decide; file location does not.** The bus file a job sits in isn't signed, so it can't decide where the job runs (SIG-LOCK rev 3). "Any job in a *-TO-BUILD.md file" is **not** the rule.
- **Implement's current rule is confirmed.** A job goes to the **watcher** when all three of these hold in its verified fields:
  - `to` is exactly `Build`
  - `lane` is not `lab`
  - `pane` is not `1`, and that includes an empty `pane`, which maps to the watcher
- Every other job goes to the **hub's `run_pane`**.
- This one rule lives in a single function. The hub, `/api/claim` and the Control tab all call that function.
- **Exactly one path runs each job:**
  - `/api/claim` answers `REFUSED WRONG_PATH` to any job that routes to the hub.
  - `run_pane` refuses any job that routes to the watcher.
  - The watcher ignores jobs in its files that route to the hub.
- **One seen-list for both paths:** `run_pane` writes `CLAIMED` to the same seen-list, fsynced, before it runs, just as the claim does. A job can't run once through each path.
- **Exact matching (rev 3, at Review's request):**
  - `to` is compared case-sensitively after leading and trailing spaces are trimmed. `build` or `BUILD` is not `Build`, so the job goes to the hub path.
  - `pane` must be empty, `1` or `2`. Any other value, such as `3`, ` 2a`, `-1` or `01`, is refused as `BAD_PANE` (fail closed) and never routed.
- **Tests:**
  - 15. `to: build` goes to the hub, `pane: 3` is refused `BAD_PANE`, and `pane: 01` is refused `BAD_PANE`.
  - 12. A watcher-routed job sent to `run_pane` is refused, and a hub-routed job sent to `/api/claim` gets `WRONG_PATH`.
  - 13. A job with `pane: 2` goes to the watcher. A job with `pane: 1` and a job with `lane: lab` go to the hub.
  - 14. Editing `to` or `pane` on a job gives `SPOOFED`.

## Crash rule (RULED)
- An id left `CLAIMED` with no return, after a crash or restart, is **never run again automatically**.
- The Control tab shows it as `CLAIMED-NOT-RETURNED` in NEEDS-YOU.
- To redo it, the sender re-signs it under a new id (`<old>-r2`) per rev 4. The person approves it as usual.
- If any rollback step is part of such a job, it waits for the person.

## Auto-reopen
#6's auto-reopen stays behind `HBC_BUILD_AUTOREOPEN`, which defaults to off. It may be turned on only after this spec's done-when passes, with the person's YES.

## Tests
1. **Restart mid-job:** restart the hub while a claimed job runs. After the restart, the job is not run twice, and a second claim gets DUPLICATE.
2. **Crash between claim and run:** kill the watcher right after GO. On resume the job is not run, and it shows CLAIMED-NOT-RETURNED.
3. **Resume with old jobs still in the file:** put 5 finished jobs back into the live file and resume the watcher. 0 run, and each gets DUPLICATE or is skipped by return lookup.
4. **Bad signature:** a claim with the wrong sig gets SPOOFED, and the id doesn't enter the seen-list (rev 4 P2b).
5. **Clock refusals:** a stale job gets STALE, and a future job gets BAD_TIME. A `-r2` re-sign of the same content can then be claimed (rev 4 P2c).
6. **Hub down:** the watcher takes no action and writes no return.
7. **Not approved:** a job that needs approval gets WAIT NEEDS_APPROVE until it is approved, then GO.
8. **Store failure:** a read-only seen-list gives STORE_ERROR, never GO.
9. **Localhost only:** a non-localhost request to `/api/claim` is refused.
10. **Archive:** after a return, the block is in the dated archive and gone from the live file. A sender's append made during the move is kept.
11. **Replay of the 3:24 PM case:** re-run today's restart sequence. No rollback is triggered.

## Done-when
1. Tests 1–15 pass.
2. The watcher prompt and BUS-PROTOCOL.md state the no-GO-no-action rule.
3. Pipeline: Implement, then Review, then Build's PC test (including test 11), then a 24 h soak, then the person's YES, then the merge.

## Out of scope
Auto-reopen going on, any public push, and any change to signing keys.

## [UNKNOWN]
- Whether Build's `grok --continue` session can make a local HTTP call itself, or needs a small helper script. Implement reports which one it uses.

LOCK: SPEC-HBC-BUILD-CLAIM-1010 rev 3 is sealed by Spec.
