"""Classify the repos listed in an awesome-stars readme as dead / dying / alive.

Dead   = gone (deleted or private), archived by the owner, or no push in >= 3 years.
Dying  = last push between 2 and 3 years ago.
Alive  = pushed within the last 2 years.

stdout is one "owner/repo" per line, nothing else, so it pipes straight into an
unstar step. The report and progress go to stderr, a linked markdown version to
report.md, and the full data to dead_stars.json.

    python3 dead_stars.py readme.md > dead.txt
    xargs -n1 -I{} gh api -X DELETE /user/starred/{} < dead.txt
"""

import json
import re
import subprocess
import sys
from datetime import datetime, timezone

DEAD_DAYS = 365 * 3
DYING_DAYS = 365 * 2
BATCH = 40

ENTRY = re.compile(r"^- \[[^\]]+\]\(https://github\.com/([^/)]+)/([^/)#?]+)\)", re.M)

FIELDS = """
    nameWithOwner
    isArchived
    isFork
    isEmpty
    stargazerCount
    pushedAt
    defaultBranchRef { target { ... on Commit { committedDate } } }
"""


def gh_graphql(query):
    proc = subprocess.run(
        ["gh", "api", "graphql", "-f", f"query={query}"],
        capture_output=True,
        text=True,
    )
    out = proc.stdout.strip()
    if not out:
        sys.exit(f"gh failed: {proc.stderr.strip()}")
    return json.loads(out)


def fetch(repos):
    """Return {(owner, name): node-or-None} for one batch."""
    parts = []
    for i, (owner, name) in enumerate(repos):
        parts.append(
            f'r{i}: repository(owner: "{owner}", name: "{name}") {{{FIELDS}}}'
        )
    body = gh_graphql("query {" + "\n".join(parts) + "}")
    data = body.get("data") or {}
    return {
        repos[i]: data.get(f"r{i}")
        for i in range(len(repos))
    }


def days_since(iso, now):
    return (now - datetime.fromisoformat(iso.replace("Z", "+00:00"))).days


def main(readme, emit, max_rows):
    with open(readme, encoding="utf-8") as fh:
        text = fh.read()

    seen, repos = set(), []
    for owner, name in ENTRY.findall(text):
        key = (owner, name.removesuffix(".git"))
        if key not in seen:
            seen.add(key)
            repos.append(key)

    now = datetime.now(timezone.utc)
    rows = []
    for start in range(0, len(repos), BATCH):
        batch = repos[start : start + BATCH]
        result = fetch(batch)
        for key in batch:
            node = result.get(key)
            listed = "/".join(key)
            if node is None:
                rows.append(
                    dict(listed=listed, current=listed, status="dead",
                         reason="gone (deleted, renamed away, or private)",
                         days=None, stars=None, archived=False)
                )
                continue

            pushed = node["pushedAt"]
            ref = node.get("defaultBranchRef") or {}
            target = ref.get("target") or {}
            # pushedAt moves on any branch or tag; the default branch commit
            # date is the honest signal for "is anyone still shipping this".
            latest = target.get("committedDate") or pushed
            age = days_since(latest, now) if latest else None

            if node["isArchived"]:
                status, reason = "dead", "archived by owner"
            elif node["isEmpty"]:
                status, reason = "dead", "empty repository"
            elif age is None:
                status, reason = "dead", "no commits found"
            elif age >= DEAD_DAYS:
                status, reason = "dead", f"no commits in {age // 365}y"
            elif age >= DYING_DAYS:
                status, reason = "dying", f"no commits in {age // 30}mo"
            else:
                status, reason = "alive", f"last commit {age}d ago"

            rows.append(
                dict(listed=listed, current=node["nameWithOwner"], status=status,
                     reason=reason, days=age, stars=node["stargazerCount"],
                     archived=node["isArchived"])
            )
        print(f"  checked {min(start + BATCH, len(repos))}/{len(repos)}",
              file=sys.stderr)

    with open("dead_stars.json", "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2)

    with open("report.md", "w", encoding="utf-8") as md:
        for status in ("dead", "dying"):
            group = sorted(
                (r for r in rows if r["status"] == status),
                key=lambda r: (-(r["days"] or 10**6), -(r["stars"] or 0)),
            )
            md.write(f"## {status.title()} ({len(group)})\n\n")
            md.write("| Repository | Why | Stars |\n| --- | --- | --- |\n")
            for r in group[:max_rows]:
                name = r["current"]
                link = f"[{name}](https://github.com/{name})"
                if name != r["listed"]:
                    link += f" (listed as `{r['listed']}`)"
                stars = "" if r["stars"] is None else f"{r['stars']:,}"
                md.write(f"| {link} | {r['reason']} | {stars} |\n")
            # A GitHub issue body caps at 65536 characters, so long runs get cut
            # here rather than truncated mid-table by the API.
            if len(group) > max_rows:
                md.write(f"\n{len(group) - max_rows} more, "
                         "listed in full in `dead.txt` and `dead_stars.json`.\n")
            md.write("\n")

    for status in ("dead", "dying"):
        group = [r for r in rows if r["status"] == status]
        group.sort(key=lambda r: (-(r["days"] or 10**6), -(r["stars"] or 0)))
        print(f"\n## {status.upper()} ({len(group)})\n", file=sys.stderr)
        for r in group:
            moved = "" if r["current"] == r["listed"] else f"  -> now {r['current']}"
            stars = "" if r["stars"] is None else f"  {r['stars']}*"
            print(f"{r['listed']}\t{r['reason']}{stars}{moved}", file=sys.stderr)

    counts = {s: sum(1 for r in rows if r["status"] == s) for s in ("dead", "dying", "alive")}
    print(f"\ntotal={len(rows)} " + " ".join(f"{k}={v}" for k, v in counts.items()),
          file=sys.stderr)

    for r in rows:
        if r["status"] in emit:
            print(r["current"])


if __name__ == "__main__":
    argv = sys.argv[1:]
    emit = {"dead", "dying"} if "--include-dying" in argv else {"dead"}
    paths = [a for a in argv if not a.startswith("--")]
    limit = next((a for a in argv if a.startswith("--max-rows=")), None)
    main(paths[0] if paths else "readme.md", emit,
         int(limit.split("=", 1)[1]) if limit else 10**6)
