# SPEC-L-PROTO-1010: bus protocol pass, fixing the soft fails from the 150-probe run (Spec, rev 6 sealed Sat 10 Oct 2026, ~7:05 PM ET; rev 5 `33ae0c6a…8434`, rev 4 `fcba9145…c3aef`, rev 3 `b647c542…3573`, rev 2 `b410707a…b6e5` and rev 1 `fd7869f6…d10b` in backups/)
**Target class: ops-only.** This is a design brief only. Implement edits `BUS-PROTOCOL.md` and the watcher; Review checks it. It does not block HBC PR #1.
Source: Lead job L-PROTO-1010 (10 Oct 2:19 PM ET), with Lead's rev 2 additions. Owner from 10 Oct 2:42 PM ET: **Grok Bot** (the person's order; HiveBrainComms stays with Grok Bot).
Spec has not read the current `BUS-PROTOCOL.md` text, the watcher code or the probe logs. Exact rule numbers, file paths and probe payloads are [UNKNOWN]; Implement maps each change onto the real text.
Compatibility: this pass sits on top of SIG-LOCK-1010 rev 3 (`ddaa6c18…5959`). Signature checks run first, and nothing here weakens them.

## Goal
Make the watcher refuse clearly and safely in the cases the probes showed were soft:
- a stop line inside a job
- a malformed stop line
- duplicates
- replies that bury their verdict
- an unchecked routed line (rule 12)

## Rule text changes (add to BUS-PROTOCOL.md)
**P1. Stop lines (fixes PRB-047).**
- A job whose body contains a **valid stop line** on any line, header or body, is **not run**. The reply verdict is `STOPPED`.
- The valid form is the protocol's exact stop token on its own line. The exact token is [UNKNOWN]; Implement copies it from the current protocol and quotes it in the rule.
- A **malformed stop line** is refused, never run. That means any line that starts with "stop" in any case but isn't the exact valid form, for example `stop:`, `STOP now`, `Stop` with trailing text, or `ST0P`. The reply verdict is `REFUSED`, with reason `MALFORMED_STOP`.
- The stop check runs **before** anything is pasted or run, and it can't be overridden by approve.

**P2. Duplicates (fixes PRB-108 and PRB-110).**
- A job whose `id` has already been seen (run, refused, stopped or spoofed) is not run. The reply verdict is `DUPLICATE`, and the reply names the original job `id` and its first verdict.
- A job with a new `id` but the same signed content as an earlier job (same canonical text apart from `id` and `time`) is also `DUPLICATE`, with reason `SAME_CONTENT`. [UNKNOWN] whether PRB-110 was this case or an id replay; both are covered.
- The seen-list survives watcher restarts and is never cleared silently. Its retention is [UNKNOWN]; it must be at least 30 days.

**P3. Verdict first (all replies).**
- Line 1 of every reply is exactly `VERDICT: <RAN | DENIED | REFUSED | STOPPED | DUPLICATE | SPOOFED> [<REASON_CODE>]`.
- Only after that come the job `id`, the details and the result.
- A reply without a valid line 1 is itself a protocol error. The watcher must never send one.

**P4. Rule 12 routed line is checked.**
- Every job must carry a `routed:` line in the form `routed: <router> <ISO time with offset>`. The router is `Grok Bot` or `Lead` (Lead jobs are already routed). The time must not be in the future and must not be later than the job's `time`.
- A missing or malformed `routed:` line, an unknown router, or a bad time is refused, with reason `NOT_ROUTED` or `BAD_ROUTED`, and not run.
- `routed` is already a signed field under SIG-LOCK, so it can't be changed after signing.

**P5. Priority values (rev 2).** `priority` must be exactly `urgent` or `normal`. Any other value, a missing value or a different case is refused with reason `BAD_PRIORITY` and not run.

**P6. Header and required fields (rev 2).**
- The protocol file header is updated from v1 to **BUS-PROTOCOL v2**, matching the signing format v2 in SIG-LOCK-1010 rev 3.
- The required-fields list in BUS-PROTOCOL.md is set to the same signed fields: `v, id, from, to, lane, priority, time, routed, pane, job, done-when`.
- **`routed` is required.** This matches rules 11–13. A job missing it is refused as `NOT_ROUTED` (P4).
- `pane` may be empty, as in SIG-LOCK rev 3.
- Any v1 wording left in the file is removed.

**Rev 3 rulings (on Review's PR #3 findings at `824d23f`):**

**P2a. Retention and job age (RULED).**
- The seen-list keeps every id for **at least 30 days**. It is never cleared silently, and any pruning is logged.
- **Job-age window:** a job whose signed `time` is more than **24 hours** older than the watcher's clock is refused, with reason `STALE`.
- A job whose `time` is more than **5 minutes** in the future is refused, with reason `BAD_TIME`.
- The PR's 7-day retention is not accepted.

**P2b. Spoofed jobs don't enter the seen-list (RULED, amends rev 2).**
- Ids from jobs refused as `SPOOFED` are **not** added to the seen-list. This stops an attacker poisoning the list by sending a forged job with a real job's id ahead of time.
- Spoofed refusals are still logged in `approvals.jsonl`.
- Ids from jobs that were RAN, REFUSED (for a non-signature reason), STOPPED or DUPLICATE are added.

**P2c. What gets recorded (RULED, rev 4: option (b), made explicit).** Review raised this on PR #3 at `6c63869`. The seen-list has two separate parts:
- **Id list (for the audit trail; every id can be used only once):** records the id of every job with a final verdict of RAN, DENIED (a person denied it at approve), STOPPED, DUPLICATE, or REFUSED for any reason that isn't the signature. A recorded id is never accepted again, so a later job with that id gets DUPLICATE.
- **Content list (for SAME_CONTENT):** records a content hash only for jobs that were **RAN, DENIED or STOPPED**. It doesn't record one for REFUSED (for example `BAD_TIME`, `STALE`, `MALFORMED_TIME`, `NOT_ROUTED`, `BAD_ROUTED`, `BAD_PRIORITY`, `MALFORMED_STOP`, `SEEN_LIST`) or for DUPLICATE.
- **Corrected re-sign:** after a refusal for a clock or format reason, the sender re-signs the corrected job with a **new id**. The convention is the old id plus `-r2`, `-r3` and so on, and the reply notes the link. A corrected job can't run twice because its content was never recorded as done. It still needs a valid signature and the person's approve before it runs.
- SPOOFED ids stay out of both lists (P2b).
- `DENIED` is added to the P3 verdict list.

**P4b. Router name is exact (RULED, rev 5; option (a)).**
- The only accepted forms are `routed: Grok Bot <ISO-8601 with offset>` and `routed: Lead <ISO-8601 with offset>`.
- There are no aliases: `GB`, `grok bot` and `GrokBot` are all refused with `BAD_ROUTED`.
- The routing reason goes in the job text, not in the `routed:` line.
- **The signer is at least as strict as the hub.** `Sign-BusJob.ps1` refuses to sign an empty or bare `routed` value, or any form the hub would refuse. It never produces a job that the hub would then refuse.
- Grok Bot switches its jobs to the exact form before #11 goes live. Jobs already finished on the live bus need no migration.
- **Test PRB-R3:** a job with `routed: GB, ...` is refused with `BAD_ROUTED` by both the hub and the signer.

**P4c. Signer scope (RULED, rev 6, widens P4b).** The signer refuses to sign anything the hub would refuse on content. It runs the same checks, with the same reason codes:
- **Fields:** any required field missing gets `MISSING_FIELD`.
- **Priority:** anything other than `urgent` or `normal` gets `BAD_PRIORITY`.
- **Pane:** anything other than empty, `1` or `2` gets `BAD_PANE`.
- **Multi-line values:** any header field whose value runs onto a continuation line, `pane` included, is refused with `BAD_FIELD`. Only `job` and `done-when` may span lines.
- **Banned words:** job text matching the banned-verb list gets `BAD_VERB`. The signer and the hub load that list from **one shared file in the repo**, so the two copies can't drift.
- **Owner:** `from` must equal `-Owner` exactly, or the job gets `OWNER_MISMATCH`.
- **Routing and time:** the routed and time checks in P4a and P4b apply.

The signer is only an early check. The hub repeats every check and is still the gate. A signed job never skips a hub check.

Test PRB-G1: for each of the cases above, the signer refuses and writes no `sig`. A hand-built job carrying a forged `sig` and the same defect is refused by the hub with the same reason.

**P4a. Time format (RULED).**
- The canonical format for both `time` and the time in `routed:` is **ISO 8601 with an explicit offset, in US Eastern time**, for example `2026-10-10T15:05:00-04:00`.
- `routed:` is written as `routed: <Grok Bot|Lead> <ISO-8601 with offset>`.
- The PR's `h:mm AM/PM ET` form carries no date and no exact offset, so the 24-hour age window and replay checks can't work with it. It is **not accepted** in v2.
- A missing date, missing offset or unreadable time is refused, with reason `MALFORMED_TIME`.
- Replies and logs may also show a friendly ET form for people to read, but signing and checks use only the ISO value.
- `Sign-BusJob.ps1` fills in both times automatically, so senders don't type them.
- BUS-PROTOCOL.md rules 11–13 are updated to quote the ISO form.

**Order of checks:** signature, then required fields and priority (P6, P5), then time and age (P4a, P2a), then routed (P4), then duplicate (P2), then stop (P1), then the existing check() rules, then run. Each failure stops at the first refusal and replies verdict first.

## Probe tests (one per fail, plus one per new rule)
| Probe | Input | Expected |
|---|---|---|
| PRB-047a | a signed job with the valid stop line in its body | `VERDICT: STOPPED`, not run |
| PRB-047b | a signed job with a malformed stop line (`stop: now`) | `VERDICT: REFUSED MALFORMED_STOP`, not run |
| PRB-108 | the same signed job sent twice (same `id`) | second reply `VERDICT: DUPLICATE`, naming the original id and verdict, not run |
| PRB-110 | the same content resent with a new `id` and time | `VERDICT: DUPLICATE SAME_CONTENT`, not run |
| PRB-V1 | any of the above, plus a normal valid job | line 1 of every reply matches the `VERDICT:` pattern |
| PRB-R1 | a signed job with its `routed:` line missing | `VERDICT: REFUSED NOT_ROUTED` |
| PRB-R2 | a signed job with `routed:` naming an unknown router or a future time | `VERDICT: REFUSED BAD_ROUTED` |
| PRB-P1 | a signed job with `priority: high` | `VERDICT: REFUSED BAD_PRIORITY`, not run |
| PRB-H1 | read the BUS-PROTOCOL.md header and required-fields list | the header says v2, and the list matches the 11 signed fields with `routed` required |
| PRB-T1 | a signed job whose `time` is 25 hours old | `VERDICT: REFUSED STALE` |
| PRB-T2 | a signed job using `3:05 PM ET` (no date or offset) | `VERDICT: REFUSED MALFORMED_TIME` |
| PRB-S1 | a spoofed job carrying id X, then a validly signed job with id X | first `VERDICT: SPOOFED`; second `VERDICT: RAN` (not DUPLICATE) |
| PRB-C1 | a job refused STALE, then the same content re-signed with id `<old>-r2` and a fresh time | first `REFUSED STALE`; second `RAN` after the person approves (not DUPLICATE) |
| PRB-C2 | the refused job resent with its **old** id | `VERDICT: DUPLICATE` |
| PRB-R3 | `routed: GB, <reason>` | signer refuses to sign; hub `VERDICT: REFUSED BAD_ROUTED` |
| PRB-G1 | each signer-scope defect (missing field, bad priority, bad pane, pane continuation line, banned verb, from != -Owner) | signer refuses with the matching code, no sig; hub refuses the same defect |
| PRB-OK | a valid, signed, routed, new job with no stop line | `VERDICT: RAN` |

The probes use test keys and test panes only. No real owner keys and no secrets go in the probe files or logs.

## Done-when (rev 2: PC-first pipeline)
1. **Implement:** BUS-PROTOCOL.md contains the P1–P6 rule text and the order of checks, with the exact stop token quoted, a v2 header, and `routed` required. The watcher follows them.
2. **Review:** passes the change and re-runs all 17 probes (G1, R3, 047a, 047b, 108, 110, V1, R1, R2, P1, H1, T1, T2, S1, C1, C2, OK) plus the original PRB-047, 108 and 110 cases.
3. **Build stages it on the person's PC** through the bus and runs smoke tests there: the 17 probes, plus one real routed job end to end. Cloud or Linux runs don't count as this step.
4. **Soak:** it runs live on the PC with no protocol errors and no wrong RAN verdicts. The soak length is [UNKNOWN], for Grok Bot to set; a suggested minimum is 24 hours.
5. **The person says YES**, and only then does the merge or push to any repo happen.
6. Throughout, no secrets or expanded home paths appear in the protocol file, probes, logs or the PR.

## Out of scope
New verbs, lane changes, SIG-LOCK changes, the grok.me app, and the public repo without the person's YES.

## Done-when lock
**The write is finished when** BUS-PROTOCOL.md is v2 with P1–P6, the watcher passes all 17 probes on the person's PC through Build, the soak is clean, and the person has said YES before the merge.

LOCK: SPEC-L-PROTO-1010 is sealed by Spec. Implement quotes this file's SHA-256 at hand-off.
