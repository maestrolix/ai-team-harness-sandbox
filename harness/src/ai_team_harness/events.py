"""Преобразование снимка GitHub в события и проверка актуальности ревью."""

import hashlib
import re

from .constants import MARKER


def labels(obj):
    raw = obj.get("labels", [])
    return {v["name"] for v in (raw.get("nodes", []) if isinstance(raw, dict) else raw)}


def published(review):
    return bool(review.get("submitted_at")) and review.get("state") in {
        "COMMENTED",
        "CHANGES_REQUESTED",
        "APPROVED",
        "DISMISSED",
    }


def feedback_groups(pr, cfg):
    reviews = {c["id"] for c in pr["discussion"] if c["kind"] == "review" and published(c)}
    groups = {}
    for c in pr["discussion"]:
        body = c.get("body") or ""
        if (
            not body.strip()
            or MARKER in body
            or c.get("user", {}).get("login") not in cfg["trusted_users"]
        ):
            continue
        review = c["id"] if c["kind"] == "review" else c.get("pull_request_review_id")
        if c["kind"] in {"review", "inline"} and review not in reviews:
            continue
        identity = f"{c['kind']}:{c['id']}"
        changed = c.get("updated_at") or c.get("submitted_at") or c.get("created_at")
        fingerprint = hashlib.sha256(f"{changed}:{body}".encode()).hexdigest()[:16]
        group = f"review:{review}" if review else identity
        groups.setdefault(group, []).append(
            {"id": identity, "key": f"pr:{pr['number']}:{identity}:{fingerprint}", "comment": c}
        )
    return groups


def check_feedback(event, pr, cfg):
    current = {f["key"] for group in feedback_groups(pr, cfg).values() for f in group}
    if not {f["key"] for f in event["feedback"]} <= current:
        raise RuntimeError(
            "Review feedback changed, disappeared or is no longer trusted; process its latest batch"
        )


def observe(snapshot, queue, cfg):
    """Only trusted comments and explicitly opted-in, ready Issues produce jobs."""
    with queue.db:
        _observe(snapshot, queue, cfg)


def _observe(snapshot, queue, cfg):
    repo = cfg["repository"]
    item_by_issue = {}
    for item in snapshot["items"]:
        issue = item.get("content") or {}
        if issue.get("repository", {}).get("nameWithOwner") != repo:
            continue
        n = issue["number"]
        item_by_issue[n] = item["id"]
        status = (item.get("fieldValueByName") or {}).get("name")
        ready = (
            status == "AI Open"
            and issue["state"] == "OPEN"
            and "ai:managed" in labels(issue)
            and "ai:paused" not in labels(issue)
        )
        version = queue.transition(f"issue:{n}", "ready" if ready else "not-ready")
        if ready and version:
            queue.enqueue(
                {
                    "key": f"issue:{n}:{version}",
                    "kind": "issue",
                    "issue": n,
                    "item": item["id"],
                    "context": issue,
                }
            )
    for pr in snapshot["prs"]:
        if "ai:paused" in labels(pr) or not pr["head"]["ref"].startswith("ai/"):
            continue
        linked = linked_issue(pr)
        groups = feedback_groups(pr, cfg)
        claimed = queue.claimed_feedback(pr, groups)
        for group, feedback in groups.items():
            feedback = [f for f in feedback if f["key"] not in claimed]
            if not feedback:
                continue
            fingerprint = hashlib.sha256(
                "\n".join(sorted(f["key"] for f in feedback)).encode()
            ).hexdigest()[:16]
            queue.enqueue(
                {
                    "key": f"pr:{pr['number']}:batch:{group}:{fingerprint}",
                    "kind": "feedback",
                    "pr": pr["number"],
                    "issue": linked,
                    "item": item_by_issue.get(linked),
                    "branch": pr["head"]["ref"],
                    "head": pr["head"]["sha"],
                    "feedback": feedback,
                    "batch": group,
                    "context": {"pr": pr},
                }
            )


def refresh(event, snapshot, cfg):
    """Refresh queued work: a previous job may have advanced the same PR."""
    if event.get("pr"):
        pr = next((p for p in snapshot["prs"] if p["number"] == event["pr"]), None)
        if not pr:
            raise RuntimeError("PR is closed or no longer managed")
        check_feedback(event, pr, cfg)
        if linked_issue(pr) != event.get("issue"):
            raise RuntimeError("PR issue link changed; inspect its scope before retrying")
        event = dict(event, branch=pr["head"]["ref"], head=pr["head"]["sha"], context={"pr": pr})
    else:
        item = next((i for i in snapshot["items"] if i["id"] == event["item"]), None)
        if not item or (item.get("fieldValueByName") or {}).get("name") != "AI Open":
            raise RuntimeError("Issue is no longer in AI Open")
        issue = item.get("content") or {}
        if issue.get("repository", {}).get("nameWithOwner") != cfg[
            "repository"
        ] or "ai:managed" not in labels(issue):
            raise RuntimeError("Issue is no longer opted in")
        event = dict(event, context=issue)
    return event


def linked_issue(pr):
    match = re.search(r"<!-- ai-team-issue:(\d+) -->", pr.get("body") or "")
    return int(match[1]) if match else None
