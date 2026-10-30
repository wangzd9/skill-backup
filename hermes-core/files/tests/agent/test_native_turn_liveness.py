"""Exact native turn liveness remains authoritative without Relay telemetry."""
from agent import relay_runtime as runtime


def lease(profile="profile", session="session"):
    return runtime.ConversationLease(profile_key=profile, session_id=session,
        platform="test", host=runtime.NoopRelayRuntime(profile, "test"), session=None)


def state(coordinator, turn="turn", profile="profile", session="session"):
    return coordinator.turn_state(profile_key=profile, session_id=session, turn_id=turn)


def test_exact_native_identity_transitions_without_telemetry():
    coordinator = runtime.RelaySessionCoordinator()
    assert state(coordinator) == "unknown"
    turn = coordinator.begin_turn(lease(), turn_id="turn", task_id="task")
    try:
        assert state(coordinator) == "active"
        assert state(coordinator, "another-turn") == "unknown"
        assert state(coordinator, profile="another-profile") == "unknown"
        assert state(coordinator, session="another-session") == "unknown"
    finally:
        coordinator.end_turn(turn, outcome="success")
    assert state(coordinator) == "ended"
    coordinator.end_turn(turn, outcome="success")
    assert state(coordinator) == "ended"


def test_concurrent_turn_is_live_even_when_instrumentation_skips_it():
    coordinator = runtime.RelaySessionCoordinator()
    first = coordinator.begin_turn(lease(), turn_id="turn", task_id="one")
    second = coordinator.begin_turn(lease(), turn_id="second", task_id="two")
    try:
        assert not second.relay_enabled
        assert state(coordinator) == state(coordinator, "second") == "active"
        coordinator.end_turn(second, outcome="failed")
        assert state(coordinator) == "active"
        assert state(coordinator, "second") == "ended"
    finally:
        coordinator.end_turn(second, outcome="failed")
        coordinator.end_turn(first, outcome="success")


def test_reused_exact_identity_stays_active_until_every_native_owner_ends():
    coordinator = runtime.RelaySessionCoordinator()
    first = coordinator.begin_turn(lease(), turn_id="turn", task_id="one")
    second = coordinator.begin_turn(lease(), turn_id="turn", task_id="two")
    try:
        coordinator.end_turn(second, outcome="failed")
        assert state(coordinator) == "active"
    finally:
        coordinator.end_turn(second, outcome="failed")
        coordinator.end_turn(first, outcome="success")
    assert state(coordinator) == "ended"


def test_completed_history_is_bounded_and_active_turns_never_expire():
    coordinator = runtime.RelaySessionCoordinator()
    active = coordinator.begin_turn(lease(session="working"), turn_id="turn", task_id="work")
    try:
        for index in range(4097):
            turn = coordinator.begin_turn(lease(), turn_id=str(index), task_id="completed")
            coordinator.end_turn(turn, outcome="success")
        assert state(coordinator, "0") == "unknown"
        assert state(coordinator, "4096") == "ended"
        assert state(coordinator, session="working") == "active"
    finally:
        coordinator.end_turn(active, outcome="success")


def test_turn_started_before_source_activation_is_unknown_after_finalization():
    coordinator = runtime.RelaySessionCoordinator()
    old_turn = runtime.RelayTurnContext(lease=lease(), turn_id="turn", task_id="old")
    # The prior implementation only registered instrumentation object identity.
    coordinator._active_turns[("profile", "session")] = {id(old_turn)}
    old_turn._active_registered = True
    coordinator.end_turn(old_turn, outcome="failed")
    assert state(coordinator) == "unknown"
