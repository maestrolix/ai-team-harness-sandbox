"""JSON-контракт ответа агента и проверка решений перед публикацией."""

from .constants import ISSUE_ROLES, ROLES


def result_schema(event):
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "outcome": {"type": "string", "enum": ["ready", "answer", "blocked"]},
            "summary": {"type": "string"},
            "reply": {"type": "string"},
            "checks": {"type": "array", "items": {"type": "string"}},
            "deferred_issues": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "title": {"type": "string"},
                        "body": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["title", "body", "reason"],
                },
            },
        },
        "required": ["outcome", "summary", "reply", "checks", "deferred_issues"],
    }
    if event.get("kind") == "issue":
        schema["properties"]["planned_issues"] = {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                    "role": {"type": "string", "enum": ISSUE_ROLES},
                },
                "required": ["title", "body", "role"],
            },
        }
        schema["required"].append("planned_issues")
    if event.get("feedback"):
        schema["properties"]["decisions"] = {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    "role": {"type": "string", "enum": ROLES},
                    "decision": {
                        "type": "string",
                        "enum": ["fixed", "answered", "disagreed", "needs_input"],
                    },
                    "reply": {"type": "string"},
                },
                "required": ["id", "role", "decision", "reply"],
            },
        }
        schema["required"].append("decisions")
    return schema


def validate_result(event, result):
    if result.get("outcome") not in {"ready", "answer", "blocked"}:
        raise ValueError("Invalid Codex result")
    deferred = result.get("deferred_issues", [])
    if not isinstance(deferred, list) or (deferred and result["outcome"] == "blocked"):
        raise ValueError("A blocked result cannot defer requirements")
    for issue in deferred:
        if not isinstance(issue, dict) or any(
            not isinstance(issue.get(k), str) or not issue[k].strip()
            for k in ("title", "body", "reason")
        ):
            raise ValueError(
                "Deferred issues need a title, requirement/expected behavior, and reason"
            )
        if len(issue["title"]) > 240:
            raise ValueError("Deferred issue title is too long")
    titles = [" ".join(i["title"].casefold().split()) for i in deferred]
    if len(titles) != len(set(titles)):
        raise ValueError("Duplicate deferred issue titles")
    planned = result.get("planned_issues", [])
    if not isinstance(planned, list):
        raise ValueError("Planned issues must be a list")
    if planned and (
        event.get("kind") != "issue"
        or event.get("feedback")
        or result["outcome"] != "answer"
        or deferred
    ):
        raise ValueError("Planned issues require an issue answer without fixes or deferrals")
    for issue in planned:
        if (
            not isinstance(issue, dict)
            or any(
                not isinstance(issue.get(k), str) or not issue[k].strip()
                for k in ("title", "body", "role")
            )
            or issue["role"] not in ISSUE_ROLES
            or len(issue["title"]) > 240
        ):
            raise ValueError("Planned issues need a short title, body and supported role")
    roles = [i["role"] for i in planned]
    if len(roles) != len(set(roles)) or ("parent" in roles and roles[0] != "parent"):
        raise ValueError("Use one planned issue per role, with the optional parent first")
    if not event.get("feedback"):
        return
    decisions = result.get("decisions", [])
    ids = [d["id"] for d in decisions]
    if len(ids) != len(set(ids)) or set(ids) != {f["id"] for f in event["feedback"]}:
        raise ValueError(
            "Exactly one decision is required for every feedback ID, with no foreign IDs"
        )
    for d in decisions:
        if (
            d.get("role") not in ROLES
            or d.get("decision") not in {"fixed", "answered", "disagreed", "needs_input"}
            or not d.get("reply", "").strip()
        ):
            raise ValueError("Invalid feedback decision")
    kinds = {d["decision"] for d in decisions}
    if "needs_input" in kinds and result["outcome"] != "blocked":
        raise ValueError("Unresolved business decisions must block publication")
    if result["outcome"] != "blocked" and (result["outcome"] == "ready") != ("fixed" in kinds):
        raise ValueError("Ready requires a confirmed fix; answers/disagreement must use answer")
