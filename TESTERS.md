# Testers

HiveBrainComms is a local command center. It listens on 127.0.0.1 only.

## Requirements

- Windows
- WebView2 runtime
- .NET 8 SDK
- Python 3
- Grok CLI

## Install

Clone the repo and open the folder. From that folder, build the app, install the hub packages, then start it:

```
dotnet build shell\Shell.csproj -c Release
pip install -r requirements.txt
Open-HiveBrainComms-Shell.cmd
```

The launcher starts the hub if it is down, then opens the app. It stops with "Shell.dll is not built yet" until the build command has been run. Paths inside the launchers are relative to the folder they sit in.

The hub page is http://127.0.0.1:8107/. `HBC_HUB_PORT` is process-only (never `setx`): set it in the launching process for that session only. A missing or refused value stays 8107 and is logged. The app window is HiveBrainComms. Closing the console that started the launcher does not close the app.

The .NET 8 SDK is required because the app runs through the `dotnet` host. Start All turns the hub on when it is down and focuses an existing Build session instead of opening a second one. If the hub is down, or a tab fails to load, the app says so on its status line.

`HBC_BUILD_AUTOREOPEN` is off unless the PC config sets it to 1/true/yes/on. When it is on, a hub restart reopens recorded Build sessions after the port is bound. Leave it off on machines that should not auto-reopen. Hub restarts and Build watcher exits are appended to `.hub-events.jsonl` in the hub folder (time and exit code only). `close_pane` has no route yet, so closing the Build tab does not close Build.

Lab and Build cmd PATH is an allowlist (System32, System32\Wbem, PowerShell, then `%SystemRoot%`) plus only those parent PATH directories that contain `git.exe`, `grok.exe`, or `wmic.exe`. `wmic.exe` is optional. The required/optional split is documentation only and does not change behaviour: every name is looked up with `isfile`, and a miss never fails the hub. `python.exe` and `py.exe` are not look-for names. A folder that holds only `python.exe` or `py.exe` is not added. Any folder under `...\Microsoft\WindowsApps` is skipped. The whole parent PATH is never copied.

## Smoke list

In the Build pane, with `HBC_PASS_GROK_AUTH` unset:

1. `where git` — finds git. Required.
2. `where grok` — finds the installed `grok.exe`. Required.
3. `grok --version` — signed-in banner (on-disk login under `GROK_HOME` or `%USERPROFILE%\.grok`). Required.
4. `where wmic` — optional. `wmic.exe` is gone on current Windows. Missing is OK.
5. `where python` is not a PATH check. Store aliases under `Microsoft\WindowsApps` are ignored.
6. Do not set API keys for this check. Panes do not need `py`.

## Scrub probe

A bus job cannot run this check: `pane: build` is BAD_PANE on `run_pane`, and hub approve runs grok in plan mode. From the hub repo root, run it with the hub's Python:

    <hub python> -m tools.scrub_e2e

That uses a temp state folder (never the live hub folder), runs the probe with `sys.executable` (the hub interpreter) through the real `run_pane` subprocess path via `set_test_grok_argv`, then `apply_decision` so the return file, `.sessions.json`, and `/api/state` payload are written in that temp folder. It prints PASS or FAIL for `probe-line-3161` through `probe-line-3200`, `<truncated>`, `<home>`, `<user>`, and no raw home or username. Exit 0 or 1. Panes do not need `py`.

Do not put the hub interpreter path in a signed job. The probe itself prints 3200 stdout lines, prints the home path and USERNAME on stderr, and exits 1.

Expected result after `scrub_tail`:

- stdout tail: `probe-line-3161` through `probe-line-3200`, then `<truncated>`
- stderr: `probe-home=<home>` and `probe-user=<user>`
- The raw home path and USERNAME must not appear

See `tools/scrub-probe-job.example.md`. Unit test: `<hub python> -m unittest tests.test_scrub_tail -k probe`.

## Sign-in

Each tester signs in with their own accounts. None are shared. The app does not see the password.
