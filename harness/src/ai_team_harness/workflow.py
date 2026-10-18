"""Полный цикл одной задачи: агент, проверки, commit, PR и ответы."""

import hashlib
import time
from pathlib import Path

from .constants import MARKER
from .control import current as current_control
from .events import check_feedback, labels
from .issues import publish_issues
from .processes import command
from .publication import reply_decisions
from .results import validate_result
from .runner import run_codex


def execute(event, cfg, gh, snapshot, state, *, prepared):
    """Публиковать только проверенный результат из уже подготовленной рабочей копии."""
    workspace = Path(cfg["workspace"]).resolve()
    branch, default = prepared
    if event["kind"] == "issue":
        event = dict(
            event,
            context=dict(
                event["context"],
                discussion=gh.pages(f"repos/{gh.repo}/issues/{event['issue']}/comments"),
            ),
        )
    item = next((i for i in snapshot["items"] if i["id"] == event.get("item")), {})
    previous_status = (item.get("fieldValueByName") or {}).get("name", "Review")
    gh.status(snapshot, event.get("item"), "In Progress")
    last_guard = [0.0]

    def guard(force=False):
        control = current_control.get()
        if control:
            control.check()
        if (state / "PAUSE").exists():
            raise RuntimeError("Global PAUSE file is present")
        if force or time.monotonic() - last_guard[0] >= 10:
            gh.guard(event)  # API errors stop work instead of ignoring a possible pause.
            last_guard[0] = time.monotonic()

    run_dir = (
        state
        / "runs"
        / f"{hashlib.sha256(event['key'].encode()).hexdigest()[:16]}-{time.time_ns()}"
    )
    result = run_codex(workspace, event, cfg, run_dir, guard)
    guard(True)
    validate_result(event, result)

    def guard_feedback():
        if event.get("feedback"):
            check_feedback(
                event, dict(event["context"]["pr"], discussion=gh.discussion(event["pr"])), cfg
            )

    if result["outcome"] == "blocked":
        guard_feedback()
        reply_decisions(event, result, gh)
        raise RuntimeError(result["reply"] or result["summary"])

    def git(*args):
        return command(["git", *args], cwd=workspace)

    if git("branch", "--show-current") != branch:
        raise RuntimeError("Agent changed the branch")
    if event.get("head") and git("rev-parse", "HEAD") != event["head"]:
        raise RuntimeError("Agent changed the reviewed commit; preserve and inspect it")
    dirty = bool(git("status", "--porcelain"))
    if dirty and result["outcome"] != "ready":
        raise RuntimeError("An answer-only result unexpectedly changed files")
    if event.get("feedback") and result["outcome"] == "ready" and not dirty:
        raise RuntimeError("Feedback marked fixed without changing the reviewed revision")
    changed = dirty or bool(git("diff", f"origin/{default}...HEAD", "--stat"))
    if changed and result["outcome"] == "ready":
        git("diff", "--check")
        if not cfg.get("check_command"):
            raise RuntimeError("Configure check_command before publishing code")
        checks = command(
            cfg["check_command"],
            cwd=workspace,
            timeout=cfg.get("check_timeout_seconds", 90),
            include_stderr=True,
            guard=guard,
        )
        (run_dir / "checks.txt").write_text(checks)
        result["reply"] += (
            "\n\nЛокальная проверка: `" + " ".join(cfg["check_command"]) + "` — успешно."
        )
        guard(True)
    guard_feedback()
    deferred_urls = publish_issues(
        event, result.get("deferred_issues", []), gh, snapshot, state, guard
    )
    if deferred_urls:
        result["reply"] += (
            "\n\nВынесены в связанные Issue некритичные недоработки (не исправлены):\n"
            + "\n".join(f"- {url}" for url in deferred_urls)
        )
    planned = result.get("planned_issues", [])
    planned_urls = publish_issues(event, planned, gh, snapshot, state, guard, kind="planned")
    if planned_urls:
        result["reply"] += (
            "\n\nПодготовлены задачи в Inbox (реализация не запускалась):\n"
            + "\n".join(
                f"- {issue['role']}: {issue['title']} — {url}"
                for issue, url in zip(planned, planned_urls, strict=True)
            )
        )
    guard(True)
    guard_feedback()
    if dirty:
        git("add", "--all")
        git("diff", "--cached", "--check")
        git(
            "-c",
            "user.name=AI Team",
            "-c",
            "user.email=ai-team@users.noreply.github.com",
            "commit",
            "-m",
            f"AI: task #{event.get('issue') or event.get('pr')}",
        )
    # A retry may find a previously committed but unpublished change.
    changed = bool(git("diff", f"origin/{default}...HEAD", "--stat"))
    published_sha = None
    if changed and result["outcome"] == "ready":
        guard(True)
        if event.get("pr"):
            current = gh.api(f"repos/{gh.repo}/pulls/{event['pr']}")
            if not current["draft"]:
                gh.graphql(
                    "mutation($id:ID!){convertPullRequestToDraft(input:{pullRequestId:$id}){pullRequest{id}}}",
                    id=current["node_id"],
                )
        git("push", "-u", "origin", branch)
        prs = gh.api(f"repos/{gh.repo}/pulls?state=open&head={gh.repo.split('/')[0]}:{branch}")
        if prs:
            pr = prs[0]
        else:
            pr = gh.api(
                f"repos/{gh.repo}/pulls",
                {
                    "title": f"AI: {result['summary'][:180]}",
                    "head": branch,
                    "base": default,
                    "draft": True,
                    "body": f"{MARKER}\n<!-- ai-team-issue:{event['issue']} -->\nCloses #{event['issue']}\n\n{result['reply']}",
                },
            )
        gh.api(f"repos/{gh.repo}/issues/{pr['number']}/labels", {"labels": ["ai:managed"]})
        verified = state / "verified"
        verified.mkdir(exist_ok=True)
        (verified / str(pr["number"])).write_text(git("rev-parse", "HEAD"))
        published_sha = git("rev-parse", "HEAD")
        if event.get("pr"):
            event["head"] = (
                published_sha  # Failure reporting must guard our new head, not the old revision.
            )
        result["reply"] += (
            f"\n\nPR: {pr['html_url']}\nCommit: `{git('rev-parse', 'HEAD')}`. Ожидает CI и ревью человека."
        )
    # guard() used the old SHA; after our own push only recheck labels/closed state.
    for n in set(filter(None, [event.get("issue"), event.get("pr")])):
        current = gh.api(f"repos/{gh.repo}/issues/{n}")
        if (
            "ai:paused" in labels(current)
            or "ai:managed" not in labels(current)
            or current["state"] != "open"
        ):
            raise RuntimeError("Paused/closed before reply; branch changes were preserved")
    if event.get("pr"):
        current = gh.api(f"repos/{gh.repo}/pulls/{event['pr']}")
        if current["head"]["sha"] != (published_sha or event["head"]):
            raise RuntimeError("PR head changed before replies; inspect the preserved result")
    guard_feedback()
    reply_decisions(event, result, gh, published_sha)
    gh.reply(event, result["reply"])
    if result["outcome"] == "answer" or not changed:
        gh.status(snapshot, event.get("item"), previous_status if event.get("pr") else "Review")
    return result
