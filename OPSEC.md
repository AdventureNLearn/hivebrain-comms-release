# OPSEC — HiveBrainComms

- Class: pattern-lab. One tree: this folder.
- Loopback only: 127.0.0.1:8107. Never bind 0.0.0.0.
- The bus inside `bus/` is fiction. The shipped default handoff is `bus/`. On the person's PC the Control tab may read the live handoff folder read-only when `control.local.json` (gitignored) points at it. The tab never writes that folder. `HBC_HANDOFF` is a hub-side fallback only and must match the same setting.
- No credentials. Sign-in is the person's step.
- `HBC_PASS_GROK_AUTH` is off by default. When set to 1/true/yes/on, Build terminals only also receive `XAI_API_KEY` and `GROK_API_KEY` from the parent. Lab panes never get them.
- Lab and Build cmd PATH is an allowlist (System32, System32\Wbem, PowerShell, then `%SystemRoot%`) plus only those absolute parent PATH folders that contain `git.exe`, `grok.exe`, or `wmic.exe`. `wmic.exe` is optional. The required/optional split does not change behaviour. `python.exe` and `py.exe` are not looked for. A folder that holds only `python.exe` or `py.exe` is not added. Folders under `...\Microsoft\WindowsApps` are never appended.
- `HBC_BUILD_AUTOREOPEN` is off by default. When set to 1/true/yes/on, the hub reopens recorded Build sessions after it binds on 127.0.0.1. Turn it on only in that PC's config.
- No voice recording. No send, post, pay, delete, git, or publish from a job.
- There is no Freebots tab. HiveBrain game chat is off. No feed, no watcher, no pane.
- Signature checking is a hub lock: always strict, fail closed, re-checked at run time, no soft window, no date logic, no off switch.
- `-EnforceAt` on `tools/Sign-BusJob.ps1` is optional. It must be a full ISO time with an offset. Missing, unreadable, or past values enforce now. A future value only softens the agent-side check. See rule 13 in the user's bus protocol file.
- `-TestKeyPath` is a command-line test seam only. No environment variable can supply a signing key. A `-Verify` OK with `-TestKeyPath` proves nothing about a real owner key.
- License of this project: TBD. The person decides.
