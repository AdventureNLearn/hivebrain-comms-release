# Private-repo tests for the hub signature lock.

from tests.legacy_lanes import install as install_legacy_lanes
from tests.live_guard import install_live_state_guard

install_live_state_guard()
install_legacy_lanes()
