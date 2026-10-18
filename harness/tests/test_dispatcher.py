"""Поведенческие проверки пула: параллельность, очередность и восстановление."""

import copy
import json
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from unittest.mock import Mock, patch

import pytest

from ai_team_harness import dispatcher
from ai_team_harness.store import Queue
from ai_team_harness.workspaces import resource_keys, task_name


def event(number, **changes):
    return (
        dict(key=f"issue:{number}:1", kind="issue", issue=number, item=f"item-{number}", context={})
        | changes
    )


def snapshot(count=3):
    return {
        "items": [
            {
                "id": f"item-{n}",
                "fieldValueByName": {"name": "AI Open"},
                "content": {
                    "number": n,
                    "state": "OPEN",
                    "title": f"Issue {n}",
                    "repository": {"nameWithOwner": "owner/repo"},
                    "labels": [{"name": "ai:managed"}],
                },
            }
            for n in range(1, count + 1)
        ],
        "prs": [],
    }


@pytest.fixture
def harness(tmp_path):
    queue = Queue(tmp_path / "queue.db")
    gh = Mock(repo="owner/repo")
    gh.snapshot.side_effect = lambda: copy.deepcopy(snapshot(5))
    cfg = dict(
        workers=3,
        state_dir=str(tmp_path),
        repository="owner/repo",
        trusted_users=["owner"],
        poll_seconds=1,
        required_checks=[],
    )
    obj = dispatcher.Dispatcher(cfg, queue, gh, Mock(), threading.Event(), -1, once=True)
    yield obj
    queue.close()


def test_three_jobs_overlap_and_once_leaves_remaining_queued(harness):
    barrier = threading.Barrier(3, timeout=3)
    observed = []

    def work(ev, *args):
        observed.append(ev["issue"])
        barrier.wait()

    with patch.object(dispatcher, "run_job", side_effect=work):
        assert harness.run() == 0
    assert sorted(observed) == [1, 2, 3]
    assert len(harness.queue.db.execute("SELECT * FROM jobs WHERE status='queued'").fetchall()) == 2


def test_capacity_refills_and_max_jobs_is_total(harness):
    harness.once, harness.max_jobs = False, 5
    active = 0
    peak = 0
    lock = threading.Lock()

    def work(*args):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.03)
        with lock:
            active -= 1

    with patch.object(dispatcher, "run_job", side_effect=work):
        assert harness.run() == 0
    assert peak == 3
    assert harness.started == 5


def test_failure_isolated_and_suppresses_ci_promotion(harness):
    def work(ev, *args):
        if ev["issue"] == 2:
            raise RuntimeError("model failed")

    with (
        patch.object(dispatcher, "run_job", side_effect=work),
        patch.object(dispatcher, "reconcile") as reconcile,
    ):
        assert harness.run() == 1
    statuses = {
        json.loads(row["payload"])["issue"]: row["status"]
        for row in harness.queue.db.execute("SELECT * FROM jobs")
    }
    assert statuses == {1: "done", 2: "blocked", 3: "done", 4: "queued", 5: "queued"}
    assert "issue:2" in harness.queue.reserved()
    harness.gh.status.assert_called_once_with(harness.snapshot, "item-2", "Blocked")
    assert "issue:1" in reconcile.call_args_list[0].args[-1]  # queued, ещё не проверено


def test_one_issue_and_branch_cannot_be_claimed_twice(harness):
    q = harness.queue
    q.enqueue(event(1))
    q.enqueue(event(1, key="feedback", kind="feedback", pr=20, branch="ai/1"))
    q.enqueue(event(2))
    first = q.claim()
    assert first["issue"] == 1
    assert q.claim()["issue"] == 2
    assert q.claim() is None
    q.finish(first["key"], "blocked", "interrupted")
    assert q.claim() is None
    q.retry(first["key"])
    # События одной задачи сохраняют FIFO: ревью и повтор не работают одновременно.
    second = q.claim()
    assert second["issue"] == 1
    assert q.claim() is None
    q.finish(second["key"], "done")
    assert q.claim()["issue"] == 1
    assert resource_keys({"pr": 20, "branch": "ai/1"}) == {"pr:20", "branch:ai/1"}


def test_retry_rules_and_recovery(harness):
    q = harness.queue
    q.enqueue(event(1))
    with pytest.raises(ValueError):
        q.retry("missing")
    with pytest.raises(ValueError):
        q.retry("issue:1:1")
    q.claim()
    q.recover()
    assert q.claim() is None
    q.retry("issue:1:1")
    assert q.claim()["issue"] == 1
    q.enqueue(event(2, key="review", kind="feedback"))
    q.finish("review", "blocked")
    q.retry("review")
    assert q.claim()["key"] == "review"


def test_stale_event_and_submit_failure(harness):
    harness.queue.enqueue(event(1))
    harness.snapshot = {"items": [], "prs": []}
    with ThreadPoolExecutor(3) as pool:
        harness.dispatch(pool)
    assert harness.failed
    harness.queue.enqueue(event(2))
    harness.snapshot = snapshot()
    pool = Mock()
    pool.submit.side_effect = RuntimeError("executor unavailable")
    harness.dispatch(pool)
    assert (
        harness.queue.db.execute("SELECT status FROM jobs WHERE key='issue:2:1'").fetchone()[0]
        == "blocked"
    )


def test_stop_and_deadline_do_not_start_jobs(harness):
    harness.stop.set()
    pool = Mock()
    harness.dispatch(pool)
    pool.submit.assert_not_called()
    assert harness.run() == 0
    harness.stop.clear()
    harness.deadline = 0
    assert harness.run() == 0
    harness.gh.snapshot.assert_not_called()


def test_pause_stops_all_workers_and_keeps_local_results(harness):
    def work(ev, cfg, snap, workspaces, stop, slot, fd):
        if slot == 1:
            (harness.state / "PAUSE").touch()
        if stop.wait(2):
            raise RuntimeError("cancelled")
        raise AssertionError("Cancellation was not delivered")

    with patch.object(dispatcher, "run_job", side_effect=work):
        assert harness.run() == 1
    assert not harness.running
    harness.gh.reply.assert_not_called()


def test_poll_failure_cancels_active_work(harness):
    harness.once = False
    harness.cfg["poll_seconds"] = 0
    harness.gh.snapshot.side_effect = [snapshot(), RuntimeError("GitHub unavailable")]

    def work(ev, cfg, snap, workspaces, stop, *args):
        stop.wait(2)
        raise RuntimeError("cancelled")

    with (
        patch.object(dispatcher, "run_job", side_effect=work),
        pytest.raises(RuntimeError, match="unavailable"),
    ):
        harness.run()
    assert not harness.running
    harness.gh.reply.assert_not_called()


def test_empty_once_and_idle_wait(harness):
    harness.gh.snapshot.return_value = {"items": [], "prs": []}
    harness.gh.snapshot.side_effect = None
    assert harness.run() == 0
    harness.once = False
    harness.stop.clear()
    timer = threading.Timer(0.05, harness.stop.set)
    timer.start()
    try:
        assert harness.run() == 0
    finally:
        timer.join()


def test_snapshot_owned_by_each_worker(harness):
    def work(ev, cfg, snap, *args):
        ev["context"]["title"] = "modified"
        snap["items"].clear()

    with patch.object(dispatcher, "run_job", side_effect=work):
        assert harness.run() == 0
    assert len(harness.snapshot["items"]) == 5
    assert harness.snapshot["items"][0]["content"]["title"] == "Issue 1"


def test_resource_validation():
    assert task_name({"pr": 4}) == "pr-4"
    for ev in ({}, {"issue": "../bad"}, {"issue": True}, {"pr": -1}):
        with pytest.raises(ValueError):
            task_name(ev)


def test_busy_slot_is_kept_while_another_slot_is_filled(harness):
    first = event(1)
    harness.queue.enqueue(first)
    harness.queue.claim()
    pending = Future()
    harness.running[pending] = first, 1
    harness.queue.enqueue(event(2))
    harness.snapshot = snapshot()
    pool = Mock()
    pool.submit.return_value = Future()
    harness.dispatch(pool)
    assert harness.running[pending] == (first, 1)
    assert pool.submit.call_args.args[-2] == 2
    assert len(harness.running) == 2


def test_refreshed_branch_cannot_overlap_another_running_pr(harness):
    first = event(1, branch="ai/busy")
    harness.queue.enqueue(first)
    harness.queue.claim()
    harness.queue.enqueue(event(2, branch="ai/old"))
    harness.snapshot = snapshot()

    def refresh(ev, *args):
        return ev | {"branch": "ai/busy"}

    pool = Mock()
    with patch.object(dispatcher, "refresh", side_effect=refresh):
        harness.dispatch(pool)
    pool.submit.assert_not_called()
    assert harness.started == 0
    row = harness.queue.db.execute(
        "SELECT status,payload FROM jobs WHERE key='issue:2:1'"
    ).fetchone()
    assert row["status"] == "queued"
    assert json.loads(row["payload"])["branch"] == "ai/busy"


def test_acceptance_deadline_drains_workers_without_polling_early(harness):
    from concurrent.futures import wait as real_wait

    harness.once = False
    harness.deadline = 1
    harness.cfg["poll_seconds"] = 60
    clock = [0]
    entered = threading.Barrier(4, timeout=3)
    release = threading.Event()
    waits = 0

    def work(*args):
        entered.wait()
        assert release.wait(3)

    def tick(futures, timeout):
        nonlocal waits
        waits += 1
        if waits == 1:
            entered.wait()
            clock[0] = 2  # Приём закрыт, но три задачи всё ещё выполняются.
        else:
            release.set()
            assert not real_wait(futures, timeout=3).not_done

    with (
        patch.object(dispatcher, "run_job", side_effect=work),
        patch.object(dispatcher.time, "monotonic", side_effect=lambda: clock[0]),
        patch.object(dispatcher, "wait", side_effect=tick),
    ):
        assert harness.run() == 0
    harness.gh.snapshot.assert_called_once()
    assert harness.started == 3
    assert waits == 2
