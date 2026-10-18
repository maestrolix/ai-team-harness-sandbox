#!/bin/sh
set -eu
command -v python3 >/dev/null 2>&1 || { echo 'Нужен Python 3.11+.' >&2; exit 1; }
exec python3 - "$@" <<'PY'
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

if sys.version_info < (3, 11):
    sys.exit("Нужен Python 3.11+.")
import tomllib

SOURCE = "https://github.com/maestrolix/ai-team-harness-sandbox.git"
ROLES = ("analyst", "backend", "frontend", "fullstack", "devops", "qa")
STATUSES = ("Inbox", "AI Open", "In Progress", "Review", "Blocked", "Done")
START, END = "<!-- ai-team:start -->", "<!-- ai-team:end -->"


def run(args, cwd=None, data=None):
    result = subprocess.run(args, cwd=cwd, input=data, text=True,
                            capture_output=True, timeout=120)
    if result.returncode:
        raise RuntimeError(f"{args[0]}: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip()


def api(path, body=None):
    args = ["gh", "api", "--hostname", "github.com", path]
    if body is not None:
        args += ["--input", "-"]
    result = json.loads(run(args, data=json.dumps(body) if body is not None else None))
    if isinstance(result, dict) and result.get("errors"):
        raise RuntimeError(str(result["errors"]))
    return result


def graphql(query, **variables):
    return api("graphql", {"query": query, "variables": variables})["data"]


def repository_from_origin(origin):
    match = re.fullmatch(r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([\w.-]+/[\w.-]+?)(?:\.git)?/?", origin)
    if not match:
        raise ValueError("origin должен указывать на github.com через HTTPS или SSH.")
    return match[1]


def project_from_url(url):
    match = re.fullmatch(r"https://github\.com/(users|orgs)/([\w-]+)/projects/([1-9][0-9]*)(?:[/?#].*)?", url)
    if not match:
        raise ValueError("--project: нужна ссылка https://github.com/users/OWNER/projects/N или /orgs/OWNER/projects/N.")
    return ("user" if match[1] == "users" else "organization", match[2], int(match[3]))


def download(ref):
    if not re.fullmatch(r"[\w][\w./-]*", ref):
        raise ValueError("Недопустимый --ref; укажите ветку, тег или SHA.")
    paths = ["AGENTS.md"] + [f".codex/agents/{role}.toml" for role in ROLES]
    with tempfile.TemporaryDirectory(prefix="ai-team-source-") as tmp:
        run(["git", "init", "--quiet", tmp])
        run(["git", "fetch", "--quiet", "--depth=1", "--no-tags", SOURCE, ref], cwd=tmp)
        sha = run(["git", "rev-parse", "FETCH_HEAD"], cwd=tmp)
        files = {p: run(["git", "show", f"{sha}:{p}"], cwd=tmp) + "\n" for p in paths}
    return sha, files


def read_local(root, name):
    path = root / name
    for part in (path, *path.parents):
        if part == root:
            break
        if part.is_symlink():
            raise ValueError(f"Отказ от записи через symlink: {name}")
        if part != path and part.exists() and not part.is_dir():
            raise ValueError(f"Родительская папка занята файлом: {name}")
    if path.exists() and not path.is_file():
        raise ValueError(f"Ожидался обычный файл: {name}")
    return path.read_text() if path.exists() else None


def appended_block(existing, block):
    existing = existing or ""
    if START in existing or END in existing:
        if existing.count(START) != 1 or existing.count(END) != 1:
            raise ValueError("Повреждены границы блока AI-команды в AGENTS.md.")
        current = existing.split(START, 1)[1].split(END, 1)[0]
        if current != "\n" + block.rstrip() + "\n":
            raise ValueError("Блок AI-команды в AGENTS.md отличается; согласуйте изменения вручную.")
        return existing
    return existing + ("\n\n" if existing else "") + START + "\n" + block.rstrip() + "\n" + END + "\n"


def prepare_files(root, files, cfg):
    if (root / "AGENTS.override.md").exists():
        raise ValueError("AGENTS.override.md имеет приоритет: сначала согласуйте его с правилами команды.")
    # The skeleton's own development instructions do not belong to the application.
    instructions = files["AGENTS.md"].split("\n## Этот репозиторий\n", 1)[0]
    model_rule = next(line for line in files["AGENTS.md"].splitlines() if line.startswith("- Все роли используют"))
    instructions += "\n\n" + model_rule + "\n- Репозиторий и GitHub Project указаны в локальном `harness.toml`.\n"
    planned = {name: text for name, text in files.items() if name != "AGENTS.md" and not name.startswith("harness/")}
    for name, text in planned.items():
        current = read_local(root, name)
        if current is not None and current != text:
            raise ValueError(f"Файл уже существует и отличается: {name}. Установщик его не перезаписывает.")
    planned["AGENTS.md"] = appended_block(read_local(root, "AGENTS.md"), instructions)
    existing = read_local(root, "harness.toml")
    if existing is not None:
        old = tomllib.loads(existing)
        keys = ("repository", "project_owner_kind", "project_owner", "project_number")
        if any(old.get(k) != cfg[k] for k in keys):
            raise ValueError("harness.toml уже настроен на другой репозиторий или Project.")
        planned["harness.toml"] = existing  # Preserve local commands, users, paths and limits.
    else:
        planned["harness.toml"] = "# Локальные настройки; без токенов. check_command запускается без shell.\n"
        planned["harness.toml"] += "\n".join(f"{k} = {json.dumps(v, ensure_ascii=False)}" for k, v in cfg.items()) + "\n"
    ignored = read_local(root, ".gitignore") or ""
    suffix = "/harness.toml\n/.state/\n"
    if not ignored.endswith(suffix):
        ignored += ("\n" if ignored and not ignored.endswith("\n") else "") + suffix
    planned[".gitignore"] = ignored
    return planned


def configure_github(repository, repo_id, project):
    status = next((f for f in project["fields"]["nodes"] if f.get("name") == "Status"), None)
    if not status or "options" not in status:
        raise ValueError("В Project требуется single-select поле Status.")
    existing = status["options"]
    missing = [name for name in STATUSES if name not in {o["name"] for o in existing}]
    if missing:
        # Keep option IDs: recreating them would clear existing item statuses.
        options = [{k: o[k] for k in ("id", "name", "color", "description")} for o in existing]
        options += [{"name": name, "color": "GRAY", "description": "AI-команда"} for name in missing]
        graphql("""mutation($input:UpdateProjectV2FieldInput!){updateProjectV2Field(input:$input){projectV2Field{... on ProjectV2SingleSelectField{id}}}}""",
                input={"fieldId": status["id"], "singleSelectOptions": options})
    graphql("""mutation($p:ID!,$r:ID!){linkProjectV2ToRepository(input:{projectId:$p,repositoryId:$r}){repository{id}}}""",
            p=project["id"], r=repo_id)
    known, page = set(), 1
    while True:
        rows = api(f"repos/{repository}/labels?per_page=100&page={page}")
        known.update(row["name"].casefold() for row in rows)
        if len(rows) < 100:
            break
        page += 1
    for name in ("ai:managed", "ai:paused", *(f"role:{r}" for r in ROLES)):
        if name.casefold() not in known:
            api(f"repos/{repository}/labels", {"name": name, "color": "5319e7", "description": "AI-команда"})


def main():
    parser = argparse.ArgumentParser(description="Установить AI-команду в Git-репозиторий текущей папки; без commit, push и запуска задач.")
    parser.add_argument("--project", required=True, help="URL существующего GitHub Project")
    parser.add_argument("--ref", default="main", help="Ветка, тег или SHA каркаса (по умолчанию main)")
    parser.add_argument("--check-command", default="", help="Команда проверки приложения, например 'make check'; без shell")
    parser.add_argument("--required-check", action="append", default=[], help="Имя обязательной CI job; можно повторить")
    args = parser.parse_args()
    for tool in ("git", "gh", "codex"):
        if not shutil.which(tool):
            raise ValueError(f"Установите {tool} и повторите запуск.")
    kind, owner, number = project_from_url(args.project)
    root = Path(run(["git", "rev-parse", "--show-toplevel"])).resolve()
    repository = repository_from_origin(run(["git", "remote", "get-url", "origin"], cwd=root))
    run(["gh", "auth", "status", "--hostname", "github.com"])
    run(["codex", "login", "status"])
    login = api("user")["login"]
    repo = api(f"repos/{repository}")
    repository = repo["full_name"]
    if not repo.get("permissions", {}).get("push") or not repo.get("has_issues"):
        raise ValueError("Нужны доступ на запись к репозиторию и включённые Issues.")
    query = """query($owner:String!,$number:Int!){OWNER(login:$owner){projectV2(number:$number){id closed viewerCanUpdate fields(first:100){nodes{... on ProjectV2SingleSelectField{id name options{id name color description}}}}}}}"""
    account = graphql(query.replace("OWNER", kind), owner=owner, number=number)[kind]
    project = account and account["projectV2"]
    if not project or project["closed"] or not project["viewerCanUpdate"]:
        raise ValueError("Нужен открытый Project с доступом на запись (gh auth refresh -s project).")
    sha, files = download(args.ref)
    cfg = dict(repository=repository, project_owner=owner, project_owner_kind=kind,
               project_number=number, trusted_users=[login], workspace=".", state_dir=".state",
               poll_seconds=60, job_timeout_seconds=1800, check_timeout_seconds=300,
               check_command=shlex.split(args.check_command), required_checks=args.required_check)
    planned = prepare_files(root, files, cfg)  # Detect every local conflict before remote writes.
    print(f"Репозиторий: {repository}\nProject: {args.project}\nВерсия каркаса: {sha}", flush=True)
    configure_github(repository, repo["node_id"], project)
    for name, content in planned.items():
        path = root / name
        if read_local(root, name) == content:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
            tmp.write(content)
        os.replace(tmp.name, path)
    configured = tomllib.loads(planned["harness.toml"])
    print(f"\nГотово: {root}\nРоли можно использовать в новом чате Codex после подтверждения доверия проекту.")
    print("Существующий harness.toml сохраняется; параметры установки его не обновляют.")
    if not configured.get("check_command"):
        print("В harness.toml задайте check_command: публикация изменений кода пока заблокирована.")
    if not configured.get("required_checks"):
        print("В harness.toml задайте required_checks: автоматический перевод PR из Draft пока отключён.")
    print("Сохраните установленные файлы обычным PR в default branch; harness.toml остаётся локальным.")
    print("Установите отдельный runtime harness в виртуальное окружение:")
    print(f"  python3 -m pip install 'git+{SOURCE}@{sha}#subdirectory=harness'")
    print("Запускайте его с конфигом отдельного clone приложения:")
    print("  harness run --config harness.toml --scan-only")
    print("  harness run --config harness.toml --workers 1")
    print("Установщик не запускает модель, задачи или фоновую службу.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        sys.exit(f"Установка остановлена: {exc}\nПри ошибке доступа: gh auth login --scopes project; codex login.\nВыполненные изменения GitHub сохраняются; повторный запуск сверяет текущее состояние.")
PY
