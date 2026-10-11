# SPEC-HBC-TOKEN-HARDEN-1010 (Spec, rev 5 sealed Sat 10 Oct 2026, ~7:10 PM ET; rev 4 `166eed30…d825`, rev 3 `7fd5e451…8286`, rev 2 `749e9669…bcd3`, rev 1 `b7f8aef0…da96` in backups/)
Target class: ops-only. Owner: Grok Bot's lane. Source: Grok Bot's summary of the person's YES at 6:11 PM ET. The full brief with code line refs is on the person's PC (HBC-TOKEN-HARDEN-BRIEF.md in the handoff folder) and was not read by Spec. Where it differs from this spec, Implement flags the difference and Spec rules. Design only.

## Decision: PATCH, not a fresh build (RULED)
Why:
1. Four sealed specs are mid-pipeline. SIG-LOCK v2, L-PROTO rev 4, BUILD-CLAIM rev 3 and CONTROL-TAB rev 2 each have PC checklists and soak evidence in progress. A rewrite would reset all of that evidence and re-open bugs already fixed.
2. The change is local to three surfaces: token storage, how the shell passes the token, and the Start grok gate. A patch keeps the diff small and reviewable.
3. A rewrite adds risk without adding security. The real boundary is still the OS user or VM (R5).

A fresh build may be re-proposed later as its own spec, but only after every current spec has passed its soak.

## Rulings
- **R1 Token in memory only.**
  - The hub makes a new random token of at least 32 bytes, using the OS CSPRNG, at each start. It holds the token only in process memory.
  - No `.session-token` file is written. Any existing one is deleted at startup, and the deletion is logged without the value.
  - The token never appears in logs, the Control tab, error text or crash dumps the app writes.
  - **Handing the token to the shell:** the hub's stdout pipe or an inherited handle is allowed. A file, environment variable, command-line argument or URL is not allowed.
  - Which handoff method Implement uses is [UNKNOWN]. Implement names it, and Review checks it.
- **R2 Header only.**
  - The WebView2 shell attaches `X-Hbc-Token` to hub requests, and only to 127.0.0.1 on the hub port.
  - The `?token=` query and `{{TOKEN}}` in index.html are removed.
  - The hub refuses any request that carries a token in the URL, even when the token is valid, with 401 `TOKEN_IN_URL`.
  - Token checks use a constant-time compare.
  - A missing or wrong header gets 401.
  - CORS stays closed.
  - The header is never set for any other origin.
- **R3 Start grok gate.**
  - **Nonce:** the shell shows a native confirm. On OK, it asks the hub for a one-time nonce. The nonce lasts 60 seconds, is tied to that action and is used once.
  - **Start request:** the Start grok request carries the nonce, and the hub checks and burns it before it starts anything.
  - **Refusals:**
    - no nonce: `NO_NONCE`
    - reused nonce: `NONCE_USED`
    - expired nonce: `NONCE_EXPIRED`
    - nonce issued for another action: `NONCE_SCOPE`
  - The page's JavaScript can't mint a nonce by itself, because the nonce endpoint answers only the shell's native call path. How the hub tells that call path apart is Implement's choice: either a second in-memory shell secret that page script never sees, or a native-only channel. Implement names which one it uses.
- **R4 Carry forward unchanged.**
  - The behavior from #2, #4, #5, #7, #3 and #9 stays as it is.
  - The sealed specs stay as they are: SIG-LOCK v2, L-PROTO rev 4, BUILD-CLAIM rev 3, CONTROL-TAB rev 2, the env allowlist, the scrub, and auto-reopen off.
  - All their tests must still pass. Any change to one of them needs its own reseal.
- **R5 README.** Add a plain section, "What this does and doesn't protect". It says that the token stops other local web pages and casual misuse, but not malware or another program running as the same Windows user. It says the real boundary is a VM or a throwaway Windows user. No secrets or expanded paths go in the README.

- **R6 Nonce method (rev 2, accepted).** Implement's method is accepted.
  - The hub prints two secrets on stdout to the shell: the session token and an app key.
  - The app key never reaches the WebView.
  - After the native confirm, the shell POSTs `/api/nonce` with `X-Hbc-App-Key`.
  - The nonce is 128-bit, lasts 60 s, can be used once, and is bound to start-grok and that pane.
- **R7 Watcher claim credential (rev 2).** Claim-BusJob.ps1 stops reading `.session-token`, as part of this patch.
  - The watcher gets its **own claim key**, not the session token.
  - The claim key is valid only on `/api/claim` (least privilege).
  - The process that launches the watcher passes the claim key on an inherited handle or on stdin. It never goes in a file, an env var, an argument or a URL.
  - A watcher that wasn't launched this way has no key. Its claims are refused, so by the no-GO-no-action rule it does nothing (fail closed).
  - The claim key is regenerated at every hub start.
  - BUILD-CLAIM's file read is allowed only on the PC staging until this patch lands. It never merges.
  - New test 10: a claim without a key, or with the session token or the app key in place of the claim key, is refused. A claim with the claim key works. After the patch there is no `.session-token` read anywhere.

- **R8 How the watcher proves itself (rev 3; replaces R7's key handoff, everything else in R7 stays).** The watcher is grok running inside the hub's PTY pane, so no claim key is handed to it at all. These options are rejected:
  - Writing a key into the pane's stdin. It would land in the agent's context, the scrollback and any transcript.
  - A one-read inherited pipe. Claims happen many times per session, and handle inheritance through grok's own child processes isn't reliable.

  **Ruled method: a local named pipe with a process-ancestry check.**
  - **The pipe:** the hub serves claims on a named pipe whose name has a random suffix. The pipe name is not a secret and may be passed to the pane in an env var. The pipe's ACL allows the current Windows user only. It is local-only by nature: remote clients are rejected (`PIPE_REJECT_REMOTE_CLIENTS`).
  - **The ancestry check:** for each connection the hub gets the client's process id (`GetNamedPipeClientProcessId`) and walks the parent chain. The claim is accepted only if that process descends from the Build pane process the hub itself spawned. Otherwise the reply is `REFUSED NOT_WATCHER`.
  - **Fail closed:** if the chain can't be read, or a parent has exited, the claim is refused.
  - **Tokens:** Claim-BusJob.ps1 connects to the pipe and sends `{id, sig}`. The session token and the app key are never used for claims.
  - **HTTP:** `/api/claim` over HTTP is removed, or refuses every request.
  - **Boundary:** this protects against other local pages and processes outside the pane. As R5 says, it does not protect against malware running as the same user.
  - **Test 10, replaced:**
    - The helper run from the Build pane gets GO.
    - The same helper run from another terminal gets `NOT_WATCHER`.
    - A claim over HTTP is refused.
    - The pipe ACL denies other users.
    - No key appears in the pane, the scrollback or the logs.
- **Spec in the repo (rev 3).** Implement copies this sealed file unchanged into the private repo at `docs/specs/SPEC-HBC-TOKEN-HARDEN-1010.md`, so Review's cloud run can check its SHA. The other sealed HBC specs go there as well. They contain no secrets or personal data.

- **R9 WebSocket `/term` (rev 4; option (i) accepted, with conditions).** WebView2 can't add the header on a WebSocket upgrade, so a ticket in the first frame is allowed as a narrow exception to R2 for `/term` only. Option (ii) is rejected, because handshake headers end up in logs and devtools more easily than a frame body does.
  - **How the ticket is minted:** only the shell mints a ticket, through the app-key path. The ticket is 128-bit random, valid for 60 s, used once, and bound to `/term` and one pane id.
  - **How the hub checks the ticket:**
    - It is burned on first use, whatever the outcome.
    - It works for a single connection only.
    - If the Origin header isn't the app's own origin, the connection is refused.
    - Until a valid ticket frame arrives, the hub sends nothing and ignores all input.
    - If no valid ticket arrives within 5 s, the socket is closed.
    - A wrong, reused, expired or wrong-pane ticket closes the socket, with reasons `BAD_TICKET`, `TICKET_USED`, `TICKET_EXPIRED` and `TICKET_SCOPE`.
    - The ticket value never appears in logs or on screen.
  - **What the ticket can't do:** a ticket only **attaches** to an existing pane. Spawning a pane or starting grok still needs the R3 start-grok nonce, issued after the native confirm. `/term` never spawns on a ticket alone.
  - **What page script still can't do:** it may see a live ticket, but it can't mint one, can't reuse it, and can't widen its scope. This risk is accepted and goes in the README section.
  - **Test 11:**
    - A connection with no ticket is closed after 5 s.
    - A reused ticket, an expired ticket, a ticket for the wrong pane, and a connection from a wrong Origin are each refused.
    - A ticket can't spawn a pane.
    - A valid ticket attaches once.
    - The ticket value is absent from the logs.

- **R10 What triggers a ticket mint (rev 5).** If page script can ask for a ticket and get one, it can attach to the Build terminal and type into it whenever it likes. Limiting requests to the hub's own address doesn't change that, so page-requested mints are **not accepted**.
  - **Only the shell starts a mint, and only on a native event:**
    - **(a)** the person opens or focuses a terminal tab in the shell's own UI, through a click or a keyboard shortcut handled by the shell;
    - **(b)** the shell itself sees that the visible terminal tab's socket has dropped and reconnects it.
  - **Delivery:** the shell then posts the ticket to the page with `PostWebMessage`, for that pane only.
  - **Page requests:** the page has no message that makes a ticket. Any page request for one is ignored and logged.
  - **Rate limit:** for (b), at most 3 tickets per pane per minute. After that the shell stops until the next native event from the person.
  - **Which panes:** a mint is only for a pane the shell shows as open: `1`, `2`, `build`, and `build2` once BUILD2-ARCHIVE lands. It is never for a hidden or closed pane.
  - **Delivery check:** the shell posts only to the top-level document at `http://127.0.0.1:8107/`. It never posts to frames or other origins.
  - **Every other R9 limit still applies.**
  - **Test 12:**
    - A page script that posts a ticket request gets nothing.
    - A shell tab click attaches the pane.
    - A forced socket drop reconnects up to 3 times in a minute and then stops.
    - No ticket is minted for a pane that isn't open.

## Tests
1. After start, there is no `.session-token` on disk, and a stale one is removed.
2. A grep of logs, the Control tab, and the README and docs finds no token value.
3. A request without the header gets 401. A wrong header gets 401. A valid token in the URL gets 401 `TOKEN_IN_URL`.
4. A page script calling the nonce endpoint is refused.
5. Start grok without a nonce, with a reused nonce, with an expired nonce, or with a nonce issued for another action is refused with the matching code.
6. Start grok after the native confirm works once.
7. After a hub restart, the old token is rejected.
8. The full test suites of every carried-forward spec still pass.
9. A non-localhost request is refused.

## Done-when
- Tests 1–12 pass.
- The README section is in place.
- The pipeline is followed: Implement, Review, Build's PC test after GB-INTEG11-STAGE, a 24 h soak, then the person's YES. No merge before that.

## Out of scope
- a fresh build
- a public push
- changes to signing keys
- new features

LOCK: SPEC-HBC-TOKEN-HARDEN-1010 rev 5 is sealed by Spec.
