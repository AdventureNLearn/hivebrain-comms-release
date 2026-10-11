#!/usr/bin/env python3
"""Deterministic failing scrub probe for PC staging and `unittest -k probe`.

Prints more than 3000 numbered lines to stdout, prints the process home path
and USERNAME to stderr, and exits 1. The hub must store a scrubbed tail only.

Expected scrub_tail(stdout):
    probe-line-3161
    ...
    probe-line-3200
    <truncated>

Expected scrub_tail(stderr):
    probe-home=<home>
    probe-user=<user>

No raw home path or USERNAME may remain. apply_decision keeps the stdout tail
when stdout is non-empty, so the stored result is the truncated line tail plus
the <truncated> marker. Do not feed this through a bus job: pane: build is
BAD_PANE, and hub approve runs grok in plan mode. Run the PC check instead:

    <hub python> -m tools.scrub_e2e

The e2e swaps the child to sys.executable (the hub interpreter) plus
tools/scrub-probe.py through set_test_grok_argv. Do not put the hub
interpreter path in a signed job. Panes do not need py.
"""

import os
import sys
from pathlib import Path

LINE_COUNT = 3200


def main():
    for index in range(1, LINE_COUNT + 1):
        print("probe-line-%04d" % index)
    home = Path.home()
    user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    print("probe-home=%s" % home, file=sys.stderr)
    print("probe-user=%s" % user, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
