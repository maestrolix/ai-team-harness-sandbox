"""Восстанавливаемая публикация запланированных и отложенных Issue."""

import hashlib
import json

from .constants import MARKER
from .events import labels


def publish_issues(event, issues, gh, snapshot, state, guard, kind="deferred"):
    key = hashlib.sha256(event["key"].encode()).hexdigest()[:24]
    manifest = state / kind / f"{key}.json"
    if manifest.exists():
        if json.loads(manifest.read_text()) != issues:
            raise RuntimeError(
                f"{kind.title()} issue list changed on retry; inspect the saved manifest"
            )
    elif issues:
        manifest.parent.mkdir(exist_ok=True)
        manifest.write_text(json.dumps(issues, ensure_ascii=False))
    if not issues:
        return []
    if not any(o["name"] == "Inbox" for o in snapshot["field"]["options"]):
        raise RuntimeError(f"Project has no Inbox for {kind} issues")
    existing = gh.pages(f"repos/{gh.repo}/issues?state=all")
    sources = ", ".join(
        f"#{n}" for n in dict.fromkeys(filter(None, [event.get("issue"), event.get("pr")]))
    )
    urls = []
    for index, issue in enumerate(issues):
        guard()
        stamp = f"<!-- ai-team-{kind}:{key}:{index} -->"
        matches = [
            i for i in existing if not i.get("pull_request") and stamp in (i.get("body") or "")
        ]
        if len(matches) > 1:
            raise RuntimeError(f"Duplicate {kind} issues found; inspect before continuing")
        if matches:
            created = matches[0]
            if created["state"] != "open" or "ai:paused" in labels(created):
                raise RuntimeError(
                    f"{kind.title()} issue was closed or paused; inspect before continuing"
                )
        else:
            attempt = manifest.with_name(f"{key}-{index}.attempt")
            if attempt.exists():
                raise RuntimeError(
                    "Previous issue creation has an unknown outcome; inspect GitHub before retrying"
                )
            title = issue["title"]
            body = f"{MARKER}\n{stamp}\nИсточник: {sources}\n\n{issue['body']}"
            if kind == "deferred":
                title = f"Bug: {title}"
                body += f"\n\nПричина переноса: {issue['reason']}"
            elif index and issues[0]["role"] == "parent":
                body += f"\n\nОбщая задача: {urls[0]}"
            attempt.touch(exist_ok=False)
            created = gh.api(f"repos/{gh.repo}/issues", {"title": title, "body": body})
            existing.append(created)
        guard()
        item = next(
            (
                i
                for i in snapshot["items"]
                if (i.get("content") or {}).get("number") == created["number"]
                and (i.get("content") or {}).get("repository", {}).get("nameWithOwner") == gh.repo
            ),
            None,
        )
        if item:
            status = (item.get("fieldValueByName") or {}).get("name")
            if status and status != "Inbox" and "ai:managed" not in labels(created):
                raise RuntimeError(
                    f"{kind.title()} issue moved before publication completed; inspect its status"
                )
            if not status:
                gh.status(snapshot, item["id"], "Inbox")
        else:
            added = gh.graphql(
                """mutation($p:ID!,$c:ID!){addProjectV2ItemById(
              input:{projectId:$p,contentId:$c}){item{id}}}""",
                p=snapshot["project"],
                c=created["node_id"],
            )
            gh.status(snapshot, added["addProjectV2ItemById"]["item"]["id"], "Inbox")
        # Make the issue eligible for future AI Open only after placing it in Inbox.
        guard()
        wanted = ["ai:managed"]
        if kind == "planned" and issue["role"] != "parent":
            wanted.append(f"role:{issue['role']}")
        missing = [label for label in wanted if label not in labels(created)]
        if missing:
            gh.api(f"repos/{gh.repo}/issues/{created['number']}/labels", {"labels": missing})
        urls.append(created["html_url"])
    return urls
