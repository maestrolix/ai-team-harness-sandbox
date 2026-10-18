"""Клиент GitHub: чтение Project, проверка полномочий и идемпотентные ответы."""

import json
import subprocess
import time

from .constants import MARKER
from .events import labels, published
from .processes import command


class GitHub:
    def __init__(self, cfg):
        self.cfg, self.repo = cfg, cfg["repository"]

    def api(self, path, body=None, method=None):
        method = method or ("POST" if body is not None else "GET")
        args = ["gh", "api", path, "--method", method]
        if body is not None:
            args += ["--input", "-"]
        read_only = method == "GET" or (
            path == "graphql" and (body or {}).get("query", "").lstrip().startswith("query")
        )

        def request():
            return command(args, data=json.dumps(body) if body is not None else None)

        try:
            raw = request()
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            transient = isinstance(exc, subprocess.TimeoutExpired) or any(
                value in str(exc).lower()
                for value in ("timeout", "connection reset", "unexpected eof")
            )
            if not read_only or not transient:
                raise
            time.sleep(1)
            raw = request()  # Только одна повторная попытка чтения; запись не повторяется.
        return json.loads(raw) if raw else None

    def pages(self, path):
        result, page = [], 1
        while True:
            sep = "&" if "?" in path else "?"
            rows = self.api(f"{path}{sep}per_page=100&page={page}")
            result.extend(rows)
            if len(rows) < 100:
                return result
            page += 1

    def graphql(self, query, **variables):
        data = self.api("graphql", {"query": query, "variables": variables})
        if data.get("errors"):
            raise RuntimeError(str(data["errors"]))
        return data["data"]

    def discussion(self, number):
        # Read the publication barrier first. Pending comments must not enter even
        # the model's context; fetch each submitted review's complete comment list.
        reviews = self.pages(f"repos/{self.repo}/pulls/{number}/reviews")
        result = []
        for review in reviews:
            if not published(review):
                continue
            result.append(dict(review, kind="review"))
            for comment in self.pages(
                f"repos/{self.repo}/pulls/{number}/reviews/{review['id']}/comments"
            ):
                result.append(dict(comment, kind="inline", pull_request_review_id=review["id"]))
        result.extend(
            dict(c, kind="conversation")
            for c in self.pages(f"repos/{self.repo}/issues/{number}/comments")
        )
        return result

    def snapshot(self):
        owner_kind = self.cfg.get("project_owner_kind", "user")
        if owner_kind not in ("user", "organization"):
            raise ValueError("project_owner_kind must be user or organization")
        query = """query($owner:String!,$number:Int!,$cursor:String){ OWNER(login:$owner){
          projectV2(number:$number){id fields(first:100){nodes{... on ProjectV2SingleSelectField{id name options{id name}}}}
          items(first:100,after:$cursor){pageInfo{hasNextPage endCursor} nodes{id
            fieldValueByName(name:"Status"){... on ProjectV2ItemFieldSingleSelectValue{name}}
            content{... on Issue{number title body url state updatedAt repository{nameWithOwner}
              labels(first:100){nodes{name}}}}}}}}}""".replace("OWNER", owner_kind)
        cursor, items = None, []
        while True:
            project = self.graphql(
                query,
                owner=self.cfg["project_owner"],
                number=self.cfg["project_number"],
                cursor=cursor,
            )[owner_kind]["projectV2"]
            if not project:
                raise RuntimeError("Project not accessible")
            items.extend(project["items"]["nodes"])
            page = project["items"]["pageInfo"]
            if not page["hasNextPage"]:
                break
            cursor = page["endCursor"]
        status = next(f for f in project["fields"]["nodes"] if f.get("name") == "Status")
        required = {"Inbox", "AI Open", "In Progress", "Review", "Blocked", "Done"}
        if not required <= {o["name"] for o in status["options"]}:
            raise RuntimeError("Project Status is missing required options")
        prs = []
        for pr in self.pages(f"repos/{self.repo}/pulls?state=open"):
            if (
                "ai:managed" not in labels(pr)
                or (pr["head"].get("repo") or {}).get("full_name") != self.repo
            ):
                continue
            pr["discussion"] = self.discussion(pr["number"])
            prs.append(pr)
        return {"project": project["id"], "field": status, "items": items, "prs": prs}

    def status(self, snapshot, item_id, name):
        if not item_id:
            return
        option = next(o["id"] for o in snapshot["field"]["options"] if o["name"] == name)
        self.graphql(
            """mutation($p:ID!,$i:ID!,$f:ID!,$o:String!){updateProjectV2ItemFieldValue(
          input:{projectId:$p,itemId:$i,fieldId:$f,value:{singleSelectOptionId:$o}}){projectV2Item{id}}}""",
            p=snapshot["project"],
            i=item_id,
            f=snapshot["field"]["id"],
            o=option,
        )

    def guard(self, event):
        for n in set(filter(None, [event.get("issue"), event.get("pr")])):
            issue = self.api(f"repos/{self.repo}/issues/{n}")
            if (
                "ai:paused" in labels(issue)
                or "ai:managed" not in labels(issue)
                or issue["state"] != "open"
            ):
                raise RuntimeError(f"Paused or closed: #{n}")
        if event.get("pr"):
            pr = self.api(f"repos/{self.repo}/pulls/{event['pr']}")
            if (pr["head"].get("repo") or {}).get("full_name") != self.repo or pr["head"][
                "sha"
            ] != event["head"]:
                raise RuntimeError("PR head changed; requeue with current context")

    def reply(self, event, body):
        stamp = f"{MARKER}\n<!-- event:{event['key']} -->"
        if event.get("kind") == "inline":
            path = f"repos/{self.repo}/pulls/{event['pr']}/comments"
            existing = self.pages(path)
            payload = {"body": f"{stamp}\n{body}", "in_reply_to": event["root_comment"]}
        else:
            path = f"repos/{self.repo}/issues/{event.get('pr') or event['issue']}/comments"
            existing = self.pages(path)
            payload = {"body": f"{stamp}\n{body}"}
        if not any(stamp in (c.get("body") or "") for c in existing):
            self.api(path, payload)
