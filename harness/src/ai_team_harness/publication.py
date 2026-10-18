"""Ответы на ревью и перевод проверенного PR в готовность к ревью."""

from .events import labels, linked_issue
from .workspaces import resource_keys


def reply_decisions(event, result, gh, sha=None):
    feedback = {f["id"]: f for f in event.get("feedback", [])}
    for decision in result.get("decisions", []):
        if decision["decision"] == "fixed" and not sha:
            continue  # Never describe an unpublished local edit as a delivered fix.
        item = feedback[decision["id"]]
        comment = item["comment"]
        target = dict(
            event,
            key=item["key"],
            kind=comment["kind"],
            root_comment=comment.get("in_reply_to_id") or comment["id"],
        )
        body = decision["reply"]
        if comment["kind"] != "inline":
            body = f"Ответ на [замечание]({comment['html_url']}):\n\n" + body
        if decision["decision"] == "fixed":
            body += (
                f"\n\nИсправление: `{sha}`. Локальные проверки прошли; ожидает CI и ревью человека."
            )
        gh.reply(target, body)


def reconcile(snapshot, gh, cfg, state, excluded=frozenset()):
    required = set(cfg.get("required_checks", []))
    if not required:
        return  # Never treat an empty list as proof of passing CI.
    for pr in snapshot["prs"]:
        issue = linked_issue(pr)
        if (
            resource_keys({"issue": issue, "pr": pr["number"], "branch": pr["head"]["ref"]})
            & excluded
        ):
            continue
        if not issue or "ai:paused" in labels(pr):
            continue
        item = next(
            (
                i
                for i in snapshot["items"]
                if (i.get("content") or {}).get("number") == issue
                and (i.get("content") or {}).get("repository", {}).get("nameWithOwner") == gh.repo
            ),
            None,
        )
        if (
            not item
            or "ai:paused" in labels(item["content"])
            or (item.get("fieldValueByName") or {}).get("name") != "In Progress"
        ):
            continue
        sha = pr["head"]["sha"]
        verified = state / "verified" / str(pr["number"])
        if not verified.exists() or verified.read_text().strip() != sha:
            continue
        checks = gh.api(f"repos/{gh.repo}/commits/{sha}/check-runs?per_page=100")["check_runs"]
        passed = {
            c["name"] for c in checks if c["conclusion"] == "success" and c["status"] == "completed"
        }
        if not required <= passed:
            continue
        current = gh.api(f"repos/{gh.repo}/pulls/{pr['number']}")
        if (
            current["head"]["sha"] != sha
            or current["state"] != "open"
            or "ai:paused" in labels(current)
        ):
            continue
        if current["draft"]:
            gh.graphql(
                "mutation($id:ID!){markPullRequestReadyForReview(input:{pullRequestId:$id}){pullRequest{id}}}",
                id=current["node_id"],
            )
        gh.status(snapshot, item["id"], "Review")
