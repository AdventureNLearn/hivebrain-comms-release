# Scrub e2e is not a bus job

Do not sign this as a bus job. `pane: build` is BAD_PANE on the hub
`run_pane` path, and hub approve runs grok in plan mode
(`--permission-mode plan --max-turns 1`), which cannot execute the probe.

Run the PC check from the hub repo root with the hub's Python:

    <hub python> -m tools.scrub_e2e

It uses a temp state folder (never the live hub folder). The probe runs
through the real `run_pane` subprocess path. The child command is swapped
to the hub interpreter (`sys.executable`) plus `tools/scrub-probe.py`
through `set_test_grok_argv`. Then `apply_decision` writes the return file,
`.sessions.json`, and the `/api/state` payload in that temp folder.
Panes do not need `py`.

Prints PASS or FAIL for:

- `probe-line-3161` through `probe-line-3200`
- `<truncated>`
- `<home>` (`probe-home=<home>`)
- `<user>` (`probe-user=<user>`)
- no raw home or username anywhere

Exit 0 or 1. Do not put the hub interpreter path in a signed job.

Unit tests: `<hub python> -m unittest tests.test_scrub_tail -k probe`
