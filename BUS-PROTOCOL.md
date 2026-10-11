# BUS-PROTOCOL v2

Ops-only bus protocol for the lab watcher. Signature checks run first (SIG-LOCK format v2). Nothing here weakens them.

This file is v2. It keeps live rules 1–10, amends rules 3 and 11–13 to the ISO form, and adds rules 14–18. The header and required-field list below are the P6 fold-in (not a numbered rule).

## Required fields

The required-fields list is the same 11 signed fields as format v2:

- `v`
- `id`
- `from`
- `to`
- `lane`
- `priority`
- `time`
- `routed`
- `pane`
- `job`
- `done-when`

`routed` is required. A job missing it is refused as `NOT_ROUTED` (rule 17).
`pane` may be empty. When it is empty the hub maps the pane from the signed `to` and `lane`.
`time` and the time inside `routed` must satisfy rules 11–13 as amended (ISO-8601 with an explicit offset).

## Order of checks

Each failure stops at the first refusal and replies verdict-first (rule 16):

1. Signature (SIG-LOCK). Failures reply `SPOOFED` and are not run.
2. Required fields (header list), P4c field codes, and rule 18 (priority).
3. Time and age (rules 11–13 / P4a / P2a).
4. Rule 17 (routed).
5. Rule 15 (duplicate), amending rule 3.
6. Rule 14 (stop).
7. Existing `check()` rules (owner, banned verbs, pane map). The hub re-checks everything the signer already refused.
8. Run.

Approve cannot skip an earlier refusal.

## Rule 3 (AMENDED)

This amends live rule 3 (DUPLICATE). It does not replace it.

Amendment (P2c): a job whose `id` is already on the seen-list is not run. Each id can be used once only. The reply verdict is `DUPLICATE` and names the original job `id` and its first verdict. A new `id` with the same signed content (canonical text apart from `id`, `time`, and `routed`) is `DUPLICATE` with reason `SAME_CONTENT` only when that content hash was recorded.

The id list records `RAN`, `DENIED`, `STOPPED`, `DUPLICATE`, and `REFUSED` for non-signature reasons. The content-hash list records only `RAN`, `DENIED`, and `STOPPED`. It leaves out `BAD_TIME`, `STALE`, `MALFORMED_TIME`, `NOT_ROUTED`, `BAD_ROUTED`, `BAD_PRIORITY`, `MALFORMED_STOP`, `SEEN_LIST`, and `DUPLICATE`. A corrected re-sign uses a new id (`<old>-r2` and up). The reply notes the link as `prior-id: <old>`. `SPOOFED` stays out of both lists and is still logged in `approvals.jsonl`.

The list keeps only ids, content hashes, expiry timestamps, and an optional claim flag. It stores no job text. Ids are kept for at least 30 days (`SEEN_RETENTION_DAYS`). The list is never cleared silently; any pruning is logged. A plain-id file from main is migrated with no ids lost. A legacy `{` line is migrated only when the file has no valid JSON rows; otherwise the mix alarms. A BOM on the first legacy id is stripped. A file that cannot be read (including a `UnicodeDecodeError`) fails closed, raises an alarm, and is not rewritten. Writes are atomic (`temp` file plus `os.replace`, retried on a short backoff).

Before `run_pane` the watcher atomically writes an on-disk claim for the id (no content hash). If that write fails, the job is not run. After the run the claim is finalized with the verdict and hash. A claimed-but-unfinished id (crash or restart between run and record) counts as used (`DUPLICATE`) and raises an alarm. If the finalize write fails after a run, the return file is `VERDICT: RAN` plus `RECORD: FAILED` (never a bare `RAN`) and the API status matches. A transient failure uses up the id; a retry must use `<old>-r2` and up.

The Build pane watcher claims through `POST /api/claim` `{id, sig, nonce}` on the hub port (127.0.0.1 only; `Host` must be localhost). That endpoint reuses the same on-disk claim (`claim_seen` / `finalize_seen`). It never signs and never reveals keys or signatures. Routing (BUILD-CLAIM rev 3, sha256 `5c2fcc499acf9f26e6b34005a68bd8bdca4deea8911d7528dcc0d0e79072455b`): signed fields decide; the bus file does not. One function (`routes_to_watcher`) is called by the hub, `/api/claim`, and the Control tab. Verified `to` is trimmed, then compared case-sensitively. `pane` must be empty, `1`, or `2`; anything else (including `01`, ` 2x`, and unicode digits) is `BAD_PANE`. The watcher gets a job only when verified `to` is exactly `Build`, `lane` is not `lab`, and `pane` is empty or `2` (the pane-2 ruling is unchanged). `pane` `1` and every other valid hub job go to hub `run_pane`. `/api/claim` answers `REFUSED WRONG_PATH` to a hub-routed job. `run_pane` refuses a watcher-routed job. The watcher prompt ignores hub-routed jobs in its files. Both paths write `CLAIMED` to the same seen-list, fsynced, before they run. A Build-lane approve records an approval bound to the job `id` plus `sig` (or content hash) for 24 hours and does **not** call `run_pane`. An approval with no bind, or with an unreadable time, does not grant `GO`. The watcher does not make the HTTP call itself. It always runs `tools/Claim-BusJob.ps1` (Windows PowerShell 5.1 and pwsh 7; `powershell -NoProfile -ExecutionPolicy Bypass -File`), which posts the job `id`, `sig`, and a per-claim nonce to localhost `/api/claim`. The `GO` reply carries an HMAC over `id`, nonce, and verdict, keyed with the hub session token from `.session-token`. The helper requires HTTP 200 and a valid HMAC; anything else is exit 3. It never prints the token. Hub down prints `HUB_DOWN`. A refused token is `REFUSED TOKEN`. Exit 0 is `GO`. Exit 2 is NO-GO (hub answered with 200 and a valid HMAC, not `GO`). Exit 3 is ERROR and fails closed. Hard rule: **no GO, no action**. If the hub is down, unreachable, or answers anything but `GO`, the watcher does nothing and writes no return for that job. An id that already has a return or an archive entry is NO-GO. The watcher's no-GO-no-action rule is advisory for the grok session; archiving plus `DUPLICATE` are the backstop. A leftover `CLAIMED` id with no return is never run again automatically; the Control tab shows it as `CLAIMED-NOT-RETURNED` in NEEDS-YOU. Redo it with `<old>-r2`. `#6` auto-reopen stays behind `HBC_BUILD_AUTOREOPEN`, which defaults to off. A normal approve after `GO` is `DUPLICATE`, not `SPOOFED REPLAY`.

Once a return block is written, the hub moves the finished job out of the live bus file into `<FILE>-ARCHIVE-YYYY-MM-DD.md` (date in ET). Every writer (hub, signer, senders' helper `tools/Append-BusJob.ps1`) takes the same `<FILE>.lock`. The archive does the read and replace inside that lock. Retries are capped with a wall-clock deadline; giving up raises an alarm. The hub lock wait is bounded; a lock that cannot be opened (including on Windows) is an alarm. Senders must append through `tools/Append-BusJob.ps1` (Windows PowerShell 5.1). A prose append that skips that helper can still lose text in the replace window (P5a). A job whose return is not written stays in the live file. On resume the watcher reads the return files and the archive index first, skips any id that already has a return, then claims each remaining id.

The history check (approvals.jsonl has history and the seen file is missing) runs inside `_load_seen` / `record_seen` / `claim_seen`. While that alarm is up, nothing recreates the seen file or the marker. A missing `.init` next to a seen file that exists and parses (plain or JSON) is an upgrade from main: write `.init` once and carry on. If `.init` is missing and history exists, and any id approved or run in the last 24 hours is missing from the seen file, that is an alarm and the watcher fails closed.

First run vs a deleted file: a first run is when neither the seen-list file nor its init marker (the seen filename plus `.init`) exists, and `approvals.jsonl` has no history. That absence is normal and does not alarm. The first successful read writes the init marker (and an empty seen file if the file was missing). After that, if the seen-list file is missing while the marker remains, or while `approvals.jsonl` has history and the seen file is missing, that is an alarm and the watcher fails closed. An alarm is logged once per state change, not on every `/api/state` poll. A `SEEN_CLAIM` row is also once per state change. After a clean read with no leftover claims, the alarm state returns to ok so a later failure can alarm again.

See rule 15.

## Rule 9 (cited)

Every reply ends with `stop`. That token, trimmed and case-sensitive, is the valid stop line. See rule 14.

## Rule 11 (AMENDED)

This amends live rule 11 (time). It does not replace it.

Amendment: `time` is ISO 8601 with an explicit offset in US Eastern time, for example `2026-10-10T15:05:00-04:00`. Only the offsets `-04:00` and `-05:00` are allowed. `Z`, any other offset, non-ASCII digits, and impossible dates (month 13, offset `-24:00`) are `MALFORMED_TIME`. A missing date, missing offset, or unreadable time is `MALFORMED_TIME`. The `h:mm AM/PM ET` form is not accepted for signing or checks. Replies and logs may also show a friendly ET form for people to read.

A signed `time` more than 24 hours older than the watcher's clock is `STALE`. A signed `time` more than 5 minutes in the future is `BAD_TIME`.

## Rule 12 (AMENDED)

This amends live rule 12 (routed). It does not replace it.

Amendment: `routed:` is written as `routed: <Grok Bot|Lead> <ISO-8601 with offset>`, for example `routed: Lead 2026-10-10T15:05:00-04:00`. The time uses the same ISO form as rule 11. It must not be in the future and must not be later than the job's `time`. There are no aliases: `GB`, `grok bot`, and `GrokBot` are `BAD_ROUTED`. The signer is at least as strict as the hub and will not sign an empty, bare, datetime-only, or `GB` routed value.

P4c (rev 6). The signer refuses with the hub's exact codes: `MISSING_FIELD`, `BAD_PRIORITY`, `BAD_PANE`, and `BAD_VERB` (banned words in the job text). A header field that continues onto another line is `BAD_FIELD`; only `job` and `done-when` may span lines. `from` must equal `-Owner` exactly (`GrokBot` → `Grok Bot`, `Lead` → `Lead`) or the refusal is `OWNER_MISMATCH`. The banned-verb list lives in one shared file, `hub/banned-verbs.txt`, which both the signer and the hub load. The hub still re-checks every field after the signature.

A missing `routed:` line is `NOT_ROUTED`. If `routed` has no router name, names an unknown router, or has a bad time, it is `BAD_ROUTED`.

## Rule 13 (AMENDED)

This amends live rule 13. It does not replace it.

Amendment: times used with this rule, including `-EnforceAt` on the signer, are ISO 8601 with an explicit offset, for example `2026-10-10T15:05:00-04:00`. `Sign-BusJob.ps1` fills in `time` in that form when it signs. `routed` must already be `Grok Bot` or `Lead` plus that ISO form.

## Rule 14. Stop lines (P1)

A job whose body contains a **valid stop line** on any line, header or body, is **not run**. The reply verdict is `STOPPED`.

The valid form is the exact token `stop` on its own line, trimmed, case-sensitive. Rule 9: every reply ends with `stop`.

A **malformed stop line** is refused, never run. That means any line that starts with "stop" in any case but is not the exact valid form, for example `stop:`, `STOP now`, `Stop` with trailing text, or `ST0P`. The reply verdict is `REFUSED`, with reason `MALFORMED_STOP`.

The stop check runs **before** anything is pasted or run, and it cannot be overridden by approve.

## Rule 15. Duplicates (P2)

See the rule 3 amendment (P2c). A seen `id` replies `DUPLICATE` and names the original `id` and first verdict. Same content with a new `id` replies `DUPLICATE SAME_CONTENT` only when the content hash was recorded (`RAN`, `DENIED`, `STOPPED`). A corrected re-sign uses `<old>-r2` and up and the reply notes `prior-id: <old>`. A transient seen-list failure uses up the id; retry with `-r2`. `SPOOFED` is on neither list.

## Rule 16. Verdict first (P3)

Line 1 of every reply is exactly `VERDICT: <RAN | REFUSED | STOPPED | DUPLICATE | SPOOFED | DENIED> [<REASON_CODE>]`.

Only after that come the job `id`, the details and the result. Rule 9: the reply ends with `stop`.

A reply without a valid line 1 is itself a protocol error. The watcher must never send one.

## Rule 17. Routed line (P4)

See the rule 12 amendment. `routed` is a signed field and cannot be changed after signing.

## Rule 18. Priority values (P5)

`priority` must be exactly `urgent` or `normal`. Any other value, a missing value or a different case is refused with reason `BAD_PRIORITY` and not run.
