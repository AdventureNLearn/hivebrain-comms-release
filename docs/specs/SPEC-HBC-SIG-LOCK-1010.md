# SPEC-HBC-SIG-LOCK-1010: HiveBrainComms signature check as a hub hard lock (Spec, rev 3 sealed Sat 10 Oct 2026, ~1:55 PM ET; rev 2 `5f650298…65ef` and rev 1 `932553a7…25da` in backups/)
**Target class: ops-only.** The code goes into the private `hivebrain-comms` repo first. The public repo is updated only after the person's YES. This is a design only: Spec does not write code.
**Flow:** Spec brief, then Implement (private repo), then Review, then the person's YES, then the public repo.
Source: Lead's brief, 10 Oct 1:22 PM ET (the person asked the team to start).

## Goal
The hub (`server.py`) refuses to run any bus job that is unsigned or carries a bad signature. Only a job whose HMAC-SHA256 signature checks out against its sender's key reaches `run_pane()`. This holds no matter what any agent is told, including an instruction to ignore signatures, a manual approve, or a replayed job.

## Gap today (from Lead, not re-read by Spec)
`decide()` calls `check()` and then `run_pane()` on approve. `check()` validates the fields, the owner match, duplicates and the BAD-verb regex, but never verifies `sig`. HMAC checking exists only in `tools/Sign-BusJob.ps1`:
- One DPAPI-protected key per owner, in the user's `.grok` keys folder.
- Signed fields, in order: `id, from, to, lane, priority, time, routed, job, done-when`.
- The compare is constant-time.
- Today it gives a soft `SPOOFED-WARN` until the cutoff, then `SPOOFED` with exit 3.

Enforcement today depends on the build agent following the rules.

## Rulings
**(a) Where verification lives: native Python in the hub (RULED).**
- New module `hub/sigverify.py`, called by `decide()` and checked again inside `run_pane()`.
- Keys are read from the per-owner DPAPI blob in the user's `.grok` keys folder and unwrapped with `CryptUnprotectData` via `ctypes` (CurrentUser scope, no extra dependency).
- The key exists in memory only for the one HMAC computation. It is never written to disk, logged, cached across jobs, or put in an exception message.
- **Canonicalization must match the signer byte for byte.** Implement writes it once as a documented spec in `docs/SIGNING.md` covering:
  - field order
  - the separator and line ending
  - text encoding (UTF-8, no BOM)
  - how missing or empty fields are treated
  - whitespace trimming
  - the case of field names
  - the encoding of the sig (hex or base64)
- Both the PowerShell signer and the Python verifier follow that spec. **Golden test vectors** are made by `Sign-BusJob.ps1` and must verify in Python, and the other way round. If the two disagree, the job fails closed.
- `Sign-BusJob.ps1 -Verify` stays as the reference and the agent-side check. The hub does **not** shell out to it, which avoids the timeout and quoting risks of a subprocess on the approve path.
- If Implement finds that in-process DPAPI isn't workable, the fallback is the hub calling `-Verify` with a fixed argument list (no shell string), a 5-second timeout, and failing closed on any nonzero exit, timeout or error. Implement must state which path it used and why.

**(b) Fail closed (RULED).** Every one of these means **refuse, never run**:
- a missing `sig`
- a bad or tampered `sig`
- a missing or unreadable key file
- a DPAPI error
- an unknown owner, or a `from` that doesn't match the key's owner
- a parse error
- non-canonical input
- a verifier timeout or exception
- a duplicate or replayed `id`

Each refusal writes one line to `approvals.jsonl` with `decision: "SPOOFED"`, the job `id`, `from`, a short reason code (e.g. `NO_SIG`, `BAD_SIG`, `NO_KEY`, `OWNER_MISMATCH`, `PARSE`, `TIMEOUT`, `REPLAY`) and the time. It never logs the key, the sig value or the job body. A refused job is never queued, pasted or run.

**(c) The soft-warn window (RULED, rev 2: closed; no fixed date in the repo).**
- **The person's decision:** the hard lock has been ON on their PC since **2026-10-10 1:30 PM ET**. They updated rule 13 and `Sign-BusJob.ps1` with their YES.
- **Hub:** always strict. It has no soft window, no date logic and no off switch.
- **`Sign-BusJob.ps1`:** the shipped default is to **enforce now**, with no soft window. `enforceAt` is an optional setting.
  - When it is set, it must be a full ISO time with its offset, such as `2026-10-11T08:35:00-04:00`.
  - Any value that is missing, unreadable, or already in the past means enforce now.
  - A future value turns on the soft warning for the agent-side check only. It never changes the hub.
- **No fixed cutoff date** appears in the repo code, docs or tests. The earlier "08:35 UTC" and "8:35 AM ET" wording is removed everywhere.
- **Docs:** OPSEC.md and the README refer to "rule 13 in the user's bus protocol file", which is not in the repo, and describe the optional `enforceAt` setting together with its fail-closed default.

**(d) Approve can't bypass it (RULED).**
- `decide()` runs `sigverify` before showing anything for approval. An unverified job never shows as approvable.
- `run_pane()` checks the signature again right before running and refuses if it fails, so a manual approve, a direct API call or a stale queue entry can't skip the check.
- No config flag, environment variable, command-line argument or agent instruction can turn verification off. Any such setting must make the hub refuse to start, or be ignored and logged.

**(e) `pane` is a signed field (RULED, rev 3).** Review found this on PR #1 at `5c1b618`. `pane` decides which terminal a job runs in. If it stays unsigned, someone could take a validly signed job and run it in a different pane, which is the very kind of bypass this lock exists to stop. So `pane` joins the signed fields.
- **Signed fields (format v2):** `v, id, from, to, lane, priority, time, routed, pane, job, done-when`, in that order. `v` is the format version and must be `2`.
- **Missing `pane`:** it is signed as the empty value. The hub then picks the pane only from signed fields (`to` and `lane`) under a fixed mapping in code, never from anything unsigned.
- **Format v1 is retired at the hub.** A job without `v=2`, or signed under the v1 field list, is refused as `SPOOFED`, reason `OLD_FORMAT`. There is no fallback to v1. The person's hard lock stays strict.
- **SIGNING.md becomes version 2.** It records the new field order and the handling of `v` and `pane`, and all golden vectors are re-cut with test keys. `Sign-BusJob.ps1` signs and verifies v2 only.
- **Hub check:** `run_pane()` uses the pane value from the verified job, re-checked at run time, and nothing else. Any pane chosen by the UI or an operator is refused unless it matches the signed `pane`.
- **Rollout:** jobs already queued in v1 format are refused after the update, so their senders must re-sign them in v2. This goes in the PR notes.

## Tests (required, automated, in the private repo)
1. **A valid signed job runs:** it is accepted and logged as approved, and `run_pane()` is reached (or its test double is).
2. **An unsigned job is refused** with `SPOOFED` and `NO_SIG`, and is not run.
3. **A tampered job line is refused** (`BAD_SIG`). Run this once for each signed field, changing one character.
4. **A wrong-owner key is refused** (`OWNER_MISMATCH` or `BAD_SIG`): the job is signed with owner A's key but claims `from` B.
5. **A missing key file is refused** (`NO_KEY`), and the hub doesn't crash.
6. **Approve can't bypass it:** force-approve an unsigned job through the approve path, or call `run_pane()` directly. It is refused and logged, and not run.
7. **Replay is refused:** the same signed job `id` sent twice is refused the second time (`REPLAY`).
8. **Cross-check:** at least 3 golden vectors from `Sign-BusJob.ps1` verify in Python, and at least 1 Python-made vector verifies with `-Verify`.
9. **Settings can't disable it:** an env var or flag meant to turn verification off has no effect, and the test confirms refusal still happens. A future `enforceAt` value also has no effect on the hub.
11. **Script default:** with no `enforceAt` set, an unreadable value, or a past value, `Sign-BusJob.ps1 -Verify` returns SPOOFED (exit 3) on a bad signature, with no soft warning. Only a future `enforceAt` gives the soft warning.
12. **Pane tamper is refused:** change only `pane` on a signed v2 job. It is refused with `BAD_SIG` and does not run.
13. **Old format is refused:** a v1-signed job (old field list, no `v`) is refused with `OLD_FORMAT`.
14. **Pane override is refused:** running a verified job in any pane other than its signed one (or the mapped pane, when `pane` is empty) is refused.
10. **No secrets in logs:** a scan of `approvals.jsonl` and test output finds no key bytes, sig values or job bodies.

Test keys are made just for the tests, in a temporary folder, and deleted afterwards. They are never real owner keys and are never committed.

## Docs
- **OPSEC.md:** signature checking is a **hub lock** (fail closed, re-checked at run time, no off switch). The docs refer to rule 13 in the user's bus protocol file, and describe the optional `enforceAt` setting with its enforce-now default, as in (c).
- **README "Limits":** list the items that still depend on rules rather than code:
  - agents following lane and routing rules
  - the BAD-verb list being complete
  - DPAPI keys only protecting against other Windows users, not against code running as the same user
  - a stolen key can sign valid jobs
  - the bus folder's own file permissions
- **docs/SIGNING.md:** the canonical format and the golden vectors (test keys only).

## Constraints
- No secrets, keys, sig values, personal data or expanded home paths in code comments, docs, tests, logs or commits. Refer to "the user's `.grok` keys folder".
- Keep the change small: a new verifier module, calls in `decide()` and `run_pane()`, tests and docs. Don't change other bus behaviour.
- The public repo is updated only after the person's YES.

## Done-when
1. The hub refuses unsigned and bad-signature jobs, and runs only verified ones, whatever agents are told.
2. Tests 1–14 pass in the private repo, and Review has run them.
3. The soft window is closed. The hub is always strict, the script's shipped default is to enforce now, `enforceAt` is optional and documented, and no fixed date appears in the repo.
4. OPSEC.md, the README Limits section and docs/SIGNING.md (version 2, with re-cut vectors) are updated.
5. `pane` is signed, and pane tampering or override is refused.

## Out of scope
- Key rotation, new key creation and multi-machine key sync.
- Changes to the BAD-verb list, lanes or routing.
- The grok.me app version of HiveBrainComms.
- Any public-repo push without the person's YES.

## First slice
Write docs/SIGNING.md and the golden vectors, then `sigverify.py` with tests 1–5 and 8. Then the `decide()` and `run_pane()` wiring with tests 6, 7 and 9, then logs (test 10) and docs.

## Inputs
`server.py` (`decide`, `check`, `run_pane`), `tools/Sign-BusJob.ps1`, `approvals.jsonl`, OPSEC.md, README, and rule 13 (in the user's bus protocol file, not in the repo). All are in the private `hivebrain-comms` repo, and Spec has not read them: [UNKNOWN] exact paths and versions.

## Done-when lock
**The write is finished when** the hub refuses every unsigned or bad-signature job, whatever any agent is told. Tests 1–14 show each refusal and that approve can't bypass the check. The soft window is closed: the hub is always strict and the script enforces now by default.

LOCK: SPEC-HBC-SIG-LOCK-1010 is sealed by Spec. Implement quotes this file's SHA-256 at hand-off.
