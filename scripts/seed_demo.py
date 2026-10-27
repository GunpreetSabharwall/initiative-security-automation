#!/usr/bin/env python3
"""Replay the Lakeshore demo history into this GitHub repository.

Lakeshore Components is a fictional company. This script recreates the issue and
pull request history of one of its initiatives (from seed/seed-plan.json, made by
agentic-earned-value/examples/portfolio/build_portfolio.py) so that Program Hub
and AEV read real GitHub data.

    python scripts/seed_demo.py seed      # create issues, PRs, merges, earlier reverts
    python scripts/seed_demo.py stage     # merge the next held PR (stage slot) for a demo
    python scripts/seed_demo.py stage 2   # merge the held PR of stage slot 2
    python scripts/seed_demo.py status    # print what exists and what is pending

Run it in GitHub Actions ("Seed demo" and "Prepare stage" workflows); it uses
the workflow's GITHUB_TOKEN. The repo must start EMPTY (no issues or PRs), so
GitHub's numbers match the plan. Re-running "seed" resumes where it stopped.

Repository setting needed once: Settings > Actions > General > Workflow
permissions > tick "Allow GitHub Actions to create and approve pull requests".
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
PLAN = json.loads((ROOT / "seed" / "seed-plan.json").read_text(encoding="utf-8"))
API = "https://api.github.com"
REPO = os.environ.get("GITHUB_REPOSITORY", "")
TOKEN = os.environ.get("GITHUB_TOKEN", "")
PAUSE = float(os.environ.get("SEED_PAUSE", "1.0"))  # seconds between writes (GitHub rate limits)

AGENT = {"name": "coding-agent", "email": "coding-agent@lakeshore.example"}
LABEL_COLORS = {
    "pod-portal": "1d76db", "pod-integrations": "5319e7", "agent-work": "c5def5",
    "human-work": "fef2c0", "mixed-work": "d4c5f9", "aev-agent": "0e8a16", "blocked": "b60205", "dependency": "d93f0b",
}

session = requests.Session()
session.headers.update({
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
})


class SeedError(RuntimeError):
    pass


def call(method: str, path: str, ok=(200, 201, 204), write=True, **kwargs):
    url = f"{API}/repos/{REPO}{path}"
    for attempt in range(6):
        resp = session.request(method, url, timeout=60, **kwargs)
        if resp.status_code in ok:
            if write:
                time.sleep(PAUSE)
            return resp.json() if resp.content else {}
        text = resp.text[:400]
        if resp.status_code == 403 and "not permitted to create or approve pull requests" in text:
            raise SeedError(
                "GitHub Actions may not create pull requests in this repo yet. Turn it on in "
                "Settings > Actions > General > Workflow permissions > 'Allow GitHub Actions to "
                "create and approve pull requests', then run this workflow again (it resumes)."
            )
        if resp.status_code in (403, 429, 500, 502, 503) or (resp.status_code == 405 and "merge" in path):
            wait = int(resp.headers.get("Retry-After", 0)) or 5 * 2 ** attempt
            print(f"  {method} {path}: HTTP {resp.status_code}, retrying in {wait}s ({text[:120]})")
            time.sleep(wait)
            continue
        raise SeedError(f"{method} {path} failed: HTTP {resp.status_code}: {text}")
    raise SeedError(f"{method} {path} kept failing; run the workflow again to resume.")


def get(path: str, ok=(200,)):
    url = f"{API}/repos/{REPO}{path}"
    resp = session.get(url, timeout=60)
    if resp.status_code == 404:
        return None
    if resp.status_code not in ok:
        raise SeedError(f"GET {path} failed: HTTP {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def dev_identity(login: str) -> dict:
    return {"name": login, "email": f"{login}@lakeshore.example"}


def main_sha() -> str:
    return get("/git/ref/heads/main")["object"]["sha"]


def ensure_labels() -> None:
    names = set(LABEL_COLORS)
    for step in PLAN["steps"]:
        if step["step"] == "issue":
            names.update(step["labels"])
    existing = {label["name"] for label in (get("/labels?per_page=100") or [])}
    for name in sorted(names - existing):
        color = LABEL_COLORS.get(name, "ededed" if not name.startswith("aev-points") else "bfdadc")
        call("POST", "/labels", ok=(201, 422), json={"name": name, "color": color})
        print(f"  label {name}")


def existing(number: int):
    """The issue or PR with this number, or None. (Both share one number sequence.)"""
    return get(f"/issues/{number}")


def check_number(step: dict, created_number: int) -> None:
    if created_number != step["number"]:
        raise SeedError(
            f"GitHub gave #{created_number} but the plan expects #{step['number']}. The repo was "
            "not empty when seeding started. Use a new, empty repository."
        )


def make_branch(branch: str) -> None:
    if get(f"/git/ref/heads/{branch}") is None:
        call("POST", "/git/refs", json={"ref": f"refs/heads/{branch}", "sha": main_sha()})


def merge(pr_number: int, title: str) -> None:
    pr = get(f"/pulls/{pr_number}")
    if pr["merged"]:
        return
    call("PUT", f"/pulls/{pr_number}/merge", json={"merge_method": "squash", "commit_title": f"{title} (#{pr_number})"})
    call("DELETE", f"/git/refs/heads/{pr['head']['ref']}", ok=(204, 422))


def close_if_open(issue_number: int) -> None:
    issue = get(f"/issues/{issue_number}")
    if issue["state"] == "open":
        call("PATCH", f"/issues/{issue_number}", json={"state": "closed", "state_reason": "completed"})


def do_issue(step: dict) -> None:
    if existing(step["number"]):
        return
    made = call("POST", "/issues", json={"title": step["title"], "body": step["body"], "labels": step["labels"]})
    check_number(step, made["number"])
    print(f"  issue #{made['number']} {step['title']}")


def do_pr(step: dict) -> None:
    n, issue = step["number"], step["issue"]
    found = existing(n)
    if found is None:
        branch = f"demo/pr-{n}"
        make_branch(branch)
        dev = dev_identity(step["developer"])
        author = AGENT if step["agent"] else dev
        message = step["title"]
        if step["agent"]:
            message += f"\n\nCo-authored-by: {dev['name']} <{dev['email']}>"
        content = (f"# {step['title']}\n\nWork for issue #{issue} of the Lakeshore Supplier Quality Portal "
                   f"(fictional demo).\n\nWritten by: {'coding agent, reviewed by ' + dev['name'] if step['agent'] else dev['name']}.\n")
        if get(f"/contents/{step['path']}?ref={branch}") is None:
            call("PUT", f"/contents/{step['path']}", json={
                "message": message, "branch": branch, "author": author, "committer": author,
                "content": base64.b64encode(content.encode()).decode(),
            })
        link = f"Fixes #{issue}" if step["link"] == "fixes" else f"Part of #{issue}"
        who = "Opened by the coding agent." if step["agent"] else "Opened by a developer."
        made = call("POST", "/pulls", json={"title": step["title"], "head": branch, "base": "main",
                                            "body": f"{link}\n\n{who}"})
        check_number(step, made["number"])
        labels = (["aev-agent"] if step["agent"] else []) + ["agent-work" if step["agent"] else "human-work"]
        call("POST", f"/issues/{n}/labels", json={"labels": labels})
        print(f"  PR #{n} {step['title']}" + (" (held for the stage)" if step["hold"] else ""))
    if step["hold"]:
        return
    merge(n, step["title"])
    if step["link"] == "fixes":
        close_if_open(issue)


def do_revert(step: dict, pr_paths: dict) -> None:
    n = step["number"]
    if existing(n) is None:
        original = get(f"/pulls/{step['reverts']}")
        branch = f"demo/revert-{step['reverts']}"
        make_branch(branch)
        path = pr_paths[step["reverts"]]
        current = get(f"/contents/{path}?ref={branch}")
        if current:
            call("DELETE", f"/contents/{path}", json={
                "message": f'Revert "{original["title"]}"', "branch": branch, "sha": current["sha"],
                "author": dev_identity("d-okafor"), "committer": dev_identity("d-okafor"),
            })
        made = call("POST", "/pulls", json={
            "title": f'Revert "{original["title"]}"', "head": branch, "base": "main",
            "body": f"Reverts {REPO}#{step['reverts']}\n\n{step['reason'].capitalize()}.",
        })
        check_number(step, made["number"])
        print(f"  PR #{n} revert of #{step['reverts']}: {step['reason']}")
    merge(n, f"Revert #{step['reverts']}")


def seed() -> None:
    print(f"Seeding {REPO} from seed/seed-plan.json ({len(PLAN['steps'])} steps)")
    ensure_labels()
    pr_paths = {s["number"]: s["path"] for s in PLAN["steps"] if s["step"] == "pr"}
    for step in PLAN["steps"]:
        if step["step"] == "issue":
            do_issue(step)
        elif step["step"] == "pr":
            do_pr(step)
        else:
            do_revert(step, pr_paths)
    held = sorted((s for s in PLAN["steps"] if s["step"] == "pr" and s["hold"]), key=lambda s: s.get("slot") or 0)
    slots = ", ".join(f"slot {s.get('slot') or 1}: PR #{s['number']} (issue #{s['issue']})" for s in held)
    print(f"Done. Held for demos: {slots}. Run 'Prepare stage' the day before each demo.")


def stage(slot: str = "") -> None:
    """Merge the held PR of one stage slot: the one asked for, else the next unused one."""
    held = sorted((s for s in PLAN["steps"] if s["step"] == "pr" and s["hold"]), key=lambda s: s.get("slot") or 0)
    if slot:
        held = [s for s in held if str(s.get("slot") or 1) == str(slot)]
        if not held:
            raise SeedError(f"There is no stage slot {slot}.")
    for candidate in held:
        pr = get(f"/pulls/{candidate['number']}")
        if pr is None:
            raise SeedError(f"PR #{candidate['number']} does not exist; run 'Seed demo' first.")
        if not pr["merged"]:
            break
    else:
        raise SeedError("Every stage slot has been used. Re-seed a fresh, empty repository for more demos.")
    held = candidate
    print(f"Stage slot {held.get('slot') or 1}: merging PR #{held['number']} ({held['title']}).")
    merge(held["number"], held["title"])
    close_if_open(held["issue"])
    print(f"Merged PR #{held['number']}; issue #{held['issue']} is accepted.")
    print(f"On stage: open PR #{held['number']}, click Revert, then merge the revert PR.")


def status() -> None:
    missing = [s["number"] for s in PLAN["steps"] if existing(s["number"]) is None]
    print(f"{len(PLAN['steps']) - len(missing)} of {len(PLAN['steps'])} planned items exist.")
    if missing:
        print("Missing: " + ", ".join(f"#{n}" for n in missing))


def run() -> int:
    if not REPO or not TOKEN:
        print("GITHUB_REPOSITORY and GITHUB_TOKEN must be set (they are, inside GitHub Actions).")
        return 2
    command = sys.argv[1] if len(sys.argv) > 1 else "status"
    try:
        if command not in ("seed", "stage", "status"):
            print("Usage: seed_demo.py seed | stage [slot] | status")
            return 2
        if command == "stage":
            stage(sys.argv[2] if len(sys.argv) > 2 else "")
        else:
            {"seed": seed, "status": status}[command]()
    except SeedError as exc:
        print(f"::error::{exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(run())
