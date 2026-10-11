"""Install the Lead/Grok Bot map so existing tests keep passing."""

from hub import lanes

# Router tuple order is part of the existing proto tests: Grok Bot, then Lead.
LEGACY_DATA = {
    "status": "GROKBOT-STATUS.md",
    "lanes": [
        {
            "id": "GrokBot",
            "owner": "Grok Bot",
            "inbox": "GROKBOT-TO-BUILD.md",
            "outbox": "BUILD-TO-GROKBOT.md",
            "key": "bus-grokbot.bin",
        },
        {
            "id": "Lead",
            "owner": "Lead",
            "inbox": "LEAD-TO-BUILD.md",
            "outbox": "BUILD-TO-LEAD.md",
            "key": "bus-lead.bin",
        },
    ],
}

LEGACY = lanes.parse(LEGACY_DATA)


def install(spec=None):
    """Apply spec (default: Lead/Grok Bot) to the live hub modules."""
    if spec is None:
        spec = LEGACY
    import server
    from hub import jobs, protocol, sigverify

    return lanes.apply(
        spec,
        server=server,
        jobs=jobs,
        protocol=protocol,
        sigverify=sigverify,
    )
