"""Windows child-env allowlist. One definition for pane_child_env and term_child_env.

Merge with PR #2: delete the inline WIN_CHILD_ENV tuple in server.py from that
PR and `from hub.win_child_env import WIN_CHILD_ENV` instead. Do not keep two
tuples. TERM_EXTRA_ENV stays on the terminal PR only.
"""

WIN_CHILD_ENV = (
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "LOCALAPPDATA",
    "APPDATA",
)
