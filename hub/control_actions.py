"""R9 Restart Build only and Start All. Logs to project-root approvals.jsonl."""

import json
from datetime import datetime
from pathlib import Path

from hub.control_config import assert_not_handoff_write, resolve_handoff

# Hard-coded until BUILD-CLAIM (#9). No env or config switch.
BUILD_CLAIM_READY = False
HELD_UNTIL_BUILD_CLAIM = "held until BUILD-CLAIM"


def _stamp():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log_action(log_path, action, result, extra=None, handoff=None):
    if handoff is not None:
        assert_not_handoff_write(log_path, handoff)
    row = {"time": _stamp(), "action": action, "result": result}
    if extra:
        row.update(extra)
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")
    return row


class ProcessManager:
    """Real managers implement these. Tests use FakeProcessManager."""

    def hub_running(self):
        raise NotImplementedError

    def start_hub(self):
        raise NotImplementedError

    def build_running(self):
        raise NotImplementedError

    def focus_build_tab(self):
        raise NotImplementedError

    def has_restart_route(self):
        return False

    def restart_build_pane(self):
        raise NotImplementedError

    def stop_hub(self):
        raise RuntimeError("R9 never stops the hub")

    def running_job(self):
        return None

    def claim_state(self):
        return None

    def wait_until_build_idle(self):
        return True


class FakeProcessManager(ProcessManager):
    def __init__(self, hub=True, build=True, hub_pid=100, job=None, restart_route=True):
        self.hub = hub
        self.build = build
        self._hub_pid = hub_pid
        self.job = job
        self.restart_route = restart_route
        self.started = []
        self.stopped = []
        self.focused = []
        self.restarted_pane = False
        self.waited = False
        self.unknown_claim = False

    def hub_running(self):
        return self.hub

    def start_hub(self):
        self.hub = True
        self.started.append("hub")

    def build_running(self):
        return self.build

    def focus_build_tab(self):
        self.focused.append("build-tab")
        self.build = True

    def has_restart_route(self):
        return self.restart_route

    def restart_build_pane(self):
        self.restarted_pane = True
        return True

    def running_job(self):
        return self.job

    def claim_state(self):
        if self.unknown_claim:
            return None
        if self.job:
            job_id = self.job.get("id") if isinstance(self.job, dict) else self.job
            return {"id": job_id, "claimed": True, "returned": False}
        return {"id": "", "claimed": False, "returned": False}

    def wait_until_build_idle(self):
        self.waited = True
        if getattr(self, "idle_timeout", False):
            return False
        self.job = None
        return True


def restart_build_only(manager, log_path, confirm, handoff=None):
    """Ask the hub to restart its build pane. Never spawn grok. Never touch hub PID.

    Mid-job wait/ask uses BUILD-CLAIM (claimed and not returned) or a
    running marker the server checks too. Unknown claim → refuse.
    """
    if not BUILD_CLAIM_READY:
        return log_action(
            log_path,
            "restart-build",
            "disabled",
            {"reason": HELD_UNTIL_BUILD_CLAIM},
            handoff=handoff,
        )
    if not manager.has_restart_route():
        return log_action(
            log_path,
            "restart-build",
            "disabled",
            {"reason": "no route"},
            handoff=handoff,
        )
    claim = manager.claim_state() if hasattr(manager, "claim_state") else None
    if claim is None:
        return log_action(
            log_path,
            "restart-build",
            "disabled",
            {"reason": HELD_UNTIL_BUILD_CLAIM},
            handoff=handoff,
        )
    job = None
    if claim.get("claimed") and not claim.get("returned"):
        job = {"id": claim.get("id") or "claimed"}
    else:
        job = manager.running_job()
    if job:
        label = job.get("id") if isinstance(job, dict) else job
        if not confirm(
            f"Job {label} is picked up. Wait for it to finish, then restart Build only?"
        ):
            return log_action(log_path, "restart-build", "cancelled", {"reason": "confirm"}, handoff=handoff)
        idle = manager.wait_until_build_idle()
        if not idle:
            still = manager.running_job() or job
            still_id = still.get("id") if isinstance(still, dict) else still
            if not confirm(
                f"Job {still_id} is still picked up. Restart Build only anyway?"
            ):
                return log_action(
                    log_path,
                    "restart-build",
                    "cancelled",
                    {"reason": "still running"},
                    handoff=handoff,
                )
    elif not confirm("Restart the hub Build pane only? Hub, approvals, and the app stay up."):
        return log_action(log_path, "restart-build", "cancelled", {"reason": "confirm"}, handoff=handoff)
    ok = manager.restart_build_pane()
    if not ok:
        return log_action(log_path, "restart-build", "error", {"reason": "pane"}, handoff=handoff)
    return log_action(log_path, "restart-build", "ok", handoff=handoff)


def start_all(manager, log_path, confirm, handoff=None):
    """Start a missing hub and open the Build tab. Never spawn grok. Never kill."""
    if not confirm("Start the hub if it is down, and open the Build tab?"):
        return log_action(log_path, "start-all", "cancelled", {"reason": "confirm"}, handoff=handoff)
    started = []
    if not manager.hub_running():
        manager.start_hub()
        started.append("hub")
    manager.focus_build_tab()
    return log_action(
        log_path,
        "start-all",
        "ok",
        {"started": started, "build": "tab"},
        handoff=handoff,
    )


def action_log_path(root, environ=None):
    cfg = resolve_handoff(root, environ=environ)
    return cfg["approvals"]
