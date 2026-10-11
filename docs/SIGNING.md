# Bus job signatures (format v2)

HMAC-SHA256 over a canonical field string. The hub (`hub/sigverify.py`) checks every job before approve and again inside `run_pane()`. The hub is always strict: no soft window, no date logic, and no off switch. `tools/Sign-BusJob.ps1` is the signer and the agent-side `-Verify` reference. The hub does not shell out to that script.

When signing (not `-Verify`), the script fills in `time` with the current US Eastern clock as ISO 8601 with an explicit offset, formatted with `InvariantCulture` so a locale such as `fi-FI` cannot change the separators. `routed` must already be exactly `<router> <ISO-8601 with offset>`, and that router name must match `-Owner` from `lanes.local.json` or the built-in table (defaults `Hub` / `Builder`). The signer then refreshes the routed ISO to the same clock as `time`. It refuses empty `routed`, a bare router name, datetime-only, `GB`, `GB, ...`, and any other form the hub would refuse. Exit 3. There is no GB alias. A `pane` value with surrounding whitespace is `BAD_PANE`; the signer never trims that field. Exit 3. P4c: the signer refuses with the hub's exact codes `MISSING_FIELD`, `BAD_PRIORITY`, `BAD_PANE`, and `BAD_VERB`. A header field that continues onto another line is `BAD_FIELD`; only `job` and `done-when` may span lines. `from` must equal `-Owner` exactly or `OWNER_MISMATCH`. Banned verbs come from `hub/banned-verbs.txt` only; the hub loads the same file and re-checks. A `time:` or `routed:` line with no space after the colon is refused with exit 3 and a clear message. The script never prints `SIGNED` for a block the hub would reject as `BAD_SIG`.

`-EnforceAt` on the script is optional. It must be a full ISO time with an offset. A missing, unreadable, or past value means enforce now. A future value only softens the agent-side check; it never changes the hub. The shipped default is to enforce now. See rule 13 in the user's bus protocol file.

`-TestKeyPath` is a command-line test seam only. When it is set, the script prints `TEST-KEY`. A `-Verify` result of OK with `-TestKeyPath` proves nothing about a real owner key.

Keys live in the user's `.grok` keys folder, one DPAPI blob per owner. Default filenames are `bus-hub.bin` and `bus-builder.bin`. A local `lanes.local.json` (gitignored) can rename owners, inboxes, and key files. This file never contains keys or signature values.

Format v1 is retired. The hub refuses a job that is not `v=2` as `SPOOFED` / `OLD_FORMAT`. There is no v1 fallback. Queued v1 jobs must be re-signed.

## Canonical format

Both the PowerShell signer and the Python verifier must produce the same bytes.

### Field order

Signed fields, in this order, and no others:

1. `v`
2. `id`
3. `from`
4. `to`
5. `lane`
6. `priority`
7. `time`
8. `routed`
9. `pane`
10. `job`
11. `done-when`

`v` must be `2`. `pane` is signed. If `pane` is missing, it is signed as the empty value; the hub then maps the pane from the signed `to` and `lane` only, using the fixed map in code: (`Build`, `lab`) → `1`, (`Build`, `ops`) → `2`. `sig` is never part of the canonical string.

`run_pane()` uses only that signed or derived pane. A UI or operator pane that does not match is refused (`PANE_MISMATCH`).

### Block lines (`key: value` vs continuation)

The signer (`Get-BlockMap`) and the hub (`blocks()`) use the same rule:

- A line that matches `^[a-z][a-z0-9-]*: ?` starts a new field. The key is lowercase letters, digits, and hyphens. The space after the colon is optional.
- Any other line is a continuation of the current field. It is appended with a single space.
- Only the signed field list above is hashed. Continuations are collapsed with the whitespace rule before hashing.
- A block starts at `id: ` and ends at the next `id: ` or a `## ` line. Lines are split on `\r?\n`. A lone CR is not a line break. The hub reads bus files with `newline=''` so that matches the script. Put `v: 2` after the `id:` line so it stays inside the block.

Author note: a lowercase `key:` line inside the job text starts a new field and drops out of the job.

### Field names

Canonical names are lowercase exactly as listed above. The HMAC line is `name=value` using that lowercase name.

### Missing or empty fields

A listed field that is absent or empty is still emitted as `name=` with nothing after the equals sign.

### Whitespace trimming

Each value is trimmed, then every run of whitespace (spaces, tabs, newlines) is collapsed to one space. Leading and trailing whitespace are dropped.

### Separator and line ending

Fields are joined with a single LF (`\n`, U+000A). There is no CR, and there is no trailing newline after the last field.

### Text encoding

The canonical string is UTF-8 with no BOM. That UTF-8 byte sequence is the HMAC-SHA256 message.

### Signature encoding

The `sig` field is the HMAC-SHA256 digest as 64 lowercase hex characters. Uppercase hex, Base64, prefixes, and any other encoding are non-canonical and fail closed.

## Golden vectors (test keys only)

These vectors fix the canonical string. Tests make a temporary test key, compute the HMAC, and delete the key afterwards. Real owner keys are never used. Signature hex is not stored here.

### Vector 1 — Hub, all signed fields filled

Bus fields:

```
id: vec-1
v: 2
from: Hub
to: Build
lane: lab
priority: normal
time: 2026-10-10 12:00
routed: 2026-10-10 12:01
pane: 1
job: list lab pane files
done-when: the pane lists its files
```

Canonical string (LF between lines, no trailing LF):

```
v=2
id=vec-1
from=Hub
to=Build
lane=lab
priority=normal
time=2026-10-10 12:00
routed=2026-10-10 12:01
pane=1
job=list lab pane files
done-when=the pane lists its files
```

### Vector 2 — Builder, inner whitespace collapsed

Bus fields:

```
id: vec-2
v: 2
from: Builder
to: Build
lane: lab
priority: urgent
time: 2026-10-10 12:05
routed: 2026-10-10 12:06
pane: 2
job: list   lab    pane files
done-when: the pane lists its files
```

Canonical string:

```
v=2
id=vec-2
from=Builder
to=Build
lane=lab
priority=urgent
time=2026-10-10 12:05
routed=2026-10-10 12:06
pane=2
job=list lab pane files
done-when=the pane lists its files
```

### Vector 3 — Hub, missing `routed` and `pane`

Bus fields (`routed` and `pane` omitted; both signed empty):

```
id: vec-3
v: 2
from: Hub
to: Build
lane: lab
priority: normal
time: 2026-10-10 12:10
job: list lab pane files
done-when: the pane lists its files
```

Canonical string (empty `routed` and `pane` still present):

```
v=2
id=vec-3
from=Hub
to=Build
lane=lab
priority=normal
time=2026-10-10 12:10
routed=
pane=
job=list lab pane files
done-when=the pane lists its files
```

### Vector 4 — Hub, continuation line plus signed `pane`

Bus fields (`job` continues on the next line; `pane` is its own signed key):

```
id: vec-4
v: 2
from: Hub
to: Build
lane: lab
priority: normal
time: 2026-10-10 12:15
routed: 2026-10-10 12:16
job: list lab
pane files
done-when: the pane lists its files
pane: 1
```

Canonical string (`pane` is hashed; the continuation is part of `job`):

```
v=2
id=vec-4
from=Hub
to=Build
lane=lab
priority=normal
time=2026-10-10 12:15
routed=2026-10-10 12:16
pane=1
job=list lab pane files
done-when=the pane lists its files
```

A further vector is made by `Sign-BusJob.ps1` during tests (temporary test key, `-TestKeyPath` only) and must verify in Python. The `-Verify` switch with real DPAPI is skipped off Windows.
