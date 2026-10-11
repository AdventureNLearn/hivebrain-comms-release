# SPEC-HBC-CONTROL-TAB-1010 (Spec, rev 2 sealed Sat 10 Oct 2026, ~3:38 PM ET; rev 1 `511d3fe2…daed5` in backups/)
Target class: ops-only. Owner: Grok Bot lane. Source: Grok Bot brief, person approved 3:29 PM ET. Design only; Spec does not implement.

## Goal
Merge the WinForms "Hub" tab and the "Grok Bot" tab into one live **Control** tab. Retire the Hub tab from the UI. The hub server keeps running as the backend.

## Rulings
- **R1 Data source.** The tab reads from one new read-only hub endpoint on the hub port, `GET /status`, which returns JSON, plus the bus files read directly. No new listener port. Bind to 127.0.0.1 only. A failed read shows the section as UNKNOWN or red. It never shows the last good value as current; stale data shows its age.
- **R2 Refresh.** Health every 5 s; usage and job board every 10 s; status feed on file change or every 5 s. One read error does not blank the tab.
- **R3 Signing mode.** Shows v2 (or v1 if found, in red, since v1 is retired at the hub per SIG-LOCK rev 3).
- **R4 Silent Build kills (3:14 and 3:24 PM today).** The health strip logs every hub restart and every Build watcher exit with time and exit code to a local event log, and the tab shows the last 10. Hub restart must not kill Build; finding and fixing that cause is in scope as **task 0** (Implement reports the cause; no guessing).
- **R5 Usage limits per feature (rev 2, the person's amendment 3:29 PM; replaces CPU/RAM/disk/network).** One row per feature: SuperGrok/Grok chat, Imagine generations, X reads, X posts, Build/grok CLI, and Grok Heavy only if the account has it. Columns: used, limit, reset time (friendly ET), source, and the time it was read.
  - **Allowed sources:** values the app can read locally (for example a CLI status or usage command, or a local usage file), or values shown on a signed-in page inside the app's own WebView2 that the person is already viewing.
  - **Never:** reading or copying cookies, tokens or stored credentials, calling private or undocumented APIs with them, or logging in for the person.
  - **Not readable:** the row shows UNKNOWN with a link to that service's own usage or settings page. No guessed, estimated or cached-as-current numbers. A value older than 15 min shows its age.
  - Which feature has a readable source is [UNKNOWN] until Implement checks; Implement lists the source found for each row, or "none".
  - The process CPU/RAM view is dropped. HEALTH stays as in (1).
- **R6 Job board.** One row per job id across GROKBOT and Lead channels: id, sender, routed time (ISO with offset per L-PROTO P4a, shown as friendly ET), status in {queued, picked up, returned, DENIED, REFUSED, SPOOFED, DUPLICATE, STOPPED, ROLLED-BACK}. Status comes from the bus verdict line, never inferred. Click opens the full return read-only. Last 50 rows; older ones in the files.
- **R7 Needs-you.** Lists jobs awaiting the person's approve and any line in a return marked `NEEDS: YES`. Display only. The tab never approves; the existing approve flow is unchanged.
- **R8 Status feed.** New bus file `GROKBOT-STATUS.md`, Grok Bot appends, tab reads only (last 30 entries). File format: `- <ISO time> <one line>`.
- **R9 Buttons (the only writes).**
  - *Restart Build only:* stops and starts the Build watcher process only. It never stops the hub, approvals or the app. Confirm dialog first. A queued job isn't lost; the restart waits for a running job to finish or shows it and asks.
  - *Start All:* starts whatever of hub, Build watcher and app is not running. It never kills anything that is running.
  - Both log to `approvals.jsonl` with time and result. Neither touches signing keys.
- **R10 Screen safety.** No secrets, keys, tokens, signatures or expanded home paths on screen; paths show relative to the handoff folder. Job text is shown as plain text, never run as HTML or script.

## Done-when
1. The Control tab shows all six sections; the Hub and Grok Bot tabs are gone; the hub server still serves.
2. Tests: hub down turns health red within 10 s; killing Build turns it red and logs the exit; Restart Build only leaves the hub PID unchanged; Start All starts only missing parts; a SPOOFED and a DUPLICATE probe job show the right status; a feature with no readable source shows UNKNOWN with its usage-page link, and no credential store is read (code review check); `/status` refuses a non-localhost request; a job containing `<script>` shows as text.
3. Task 0: the cause of today's silent Build kills is found and fixed, and a hub restart now leaves Build alive (tested).
4. Pipeline: Implement, then Review, then Build PC test, then a soak (length set by Grok Bot; 24 h suggested), then the person's YES, then merge.

## Out of scope
Approving from the tab, editing bus files, remote access, any public push, new modes.

## [UNKNOWN]
Which features expose used/limit/reset locally or on a signed-in page; the root cause of the 3:14/3:24 kills.

LOCK: SPEC-HBC-CONTROL-TAB-1010 rev 2 is sealed by Spec.
