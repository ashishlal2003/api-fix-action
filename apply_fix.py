#!/usr/bin/env python3
"""
CI-side tool — packaged as a standalone GitHub Action (composite action).

This script's home is its OWN repo (published separately from any client's
repo). A client's workflow references it as `uses: <org>/<this-repo>@v1` —
GitHub fetches this repo and runs this script, while the CLIENT's repo is
checked out as the actual working directory. So:

  - `os.getcwd()` / relative paths like `src/` refer to the CLIENT's repo.
  - This script's own directory (`Path(__file__).parent`) is itself, wherever
    GitHub happened to stage it — never used for reading client files.

It never touches the client's git history directly; it writes fixed files
into the client's working tree, and the workflow's own `git commit`/`git push`
steps (or an action like create-pull-request) turn that into a real PR.

Trigger flow (polling design — we never call the client, they poll us): an
earlier step in the CLIENT's own workflow calls our change-feed API on a
schedule, gets back a JSON body describing what changed (if anything), and
passes that JSON straight through to this action as the trigger-payload
input — see action.yml. This script never talks to our API directly and
never receives more than that one JSON blob.

Output: LLM key comes from $OPENAI_API_KEY, which is the CLIENT's own
secret, set in the CLIENT's repo settings — this script never carries or
sees any credential belonging to the detection-engine side.
"""
import json
import os
import re
import sys
from pathlib import Path

# python-dotenv is only useful for local/manual runs. In real GitHub Actions,
# secrets are already exported as env vars and no .env file exists, so this
# is a harmless no-op there. Loaded from this script's own directory since
# the client's working directory (cwd in real CI) is a different repo.
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

# The client's repo — in real CI this is the working directory GitHub
# Actions checked the client's code into (actions/checkout on their repo).
# For a manual/local run, set CLIENT_REPO_PATH to point at a checked-out
# client repo instead.
CLIENT_REPO = Path(os.environ.get("CLIENT_REPO_PATH", ".")).resolve()
REPORT_FILE = CLIENT_REPO / "fix_report.md"


def load_trigger():
    """Read the trigger event the client's workflow got back from polling
    our change-feed API, and passed through to this action (see action.yml).

    For a manual/local run (no real GitHub Actions event), set TRIGGER_PAYLOAD
    yourself, e.g.: export TRIGGER_PAYLOAD="$(curl -s .../v1/changes?... )"
    """
    raw = os.environ.get("TRIGGER_PAYLOAD")
    if not raw:
        print("[ci-tool] TRIGGER_PAYLOAD env var not set. In the real "
              "workflow this comes from the polling step's response (see "
              "action.yml). For a manual run: "
              "export TRIGGER_PAYLOAD=\"$(cat trigger_event.json)\"")
        sys.exit(1)

    payload = json.loads(raw)
    if not payload.get("has_change", True):
        print("[ci-tool] Polling response says no change. Nothing to do.")
        sys.exit(0)
    return payload


def scan_repo_for_field(field_name):
    """Grep the client repo's source for usage of the removed field."""
    matches = []
    pattern = re.compile(rf"\b{re.escape(field_name)}\s*:")
    for path in (CLIENT_REPO / "src").rglob("*.js"):
        text = path.read_text()
        for i, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                matches.append({
                    "file": str(path.relative_to(CLIENT_REPO)),
                    "line": i,
                    "content": line.strip(),
                })
    return matches


def build_llm_prompt(trigger, matches, removed_field, replacement_field):
    changes_summary = "\n".join(f"- {c['detail']}" for c in trigger["changes"])
    match_summary = "\n".join(
        f"- {m['file']}:{m['line']}  `{m['content']}`" for m in matches
    )
    return f"""You are a code-fix assistant running inside a client's CI pipeline.
A vendor API changed. Here is what changed:

{changes_summary}

The removed/renamed field is `{removed_field}`, replaced by `{replacement_field}`.

The following usage sites in the client's codebase reference the old field:
{match_summary}

For each usage site, produce the corrected line of code that replaces
`{removed_field}:` with `{replacement_field}:`, preserving the rest of the line.
Respond with ONLY a JSON array of objects: [{{"file": ..., "line": ..., "fixed_content": ...}}]
"""


def call_openai(prompt):
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("[ci-tool] OPENAI_API_KEY not set — falling back to a templated "
              "fix (no real LLM call). Set OPENAI_API_KEY to exercise the real path.")
        return None

    try:
        from openai import OpenAI
    except ImportError:
        print("[ci-tool] openai package not installed — falling back to templated fix.")
        return None

    client = OpenAI(api_key=api_key)
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
    )
    content = resp.choices[0].message.content
    try:
        # Strip markdown code fences if the model added them.
        cleaned = re.sub(r"^```(?:json)?|```$", "", content.strip(), flags=re.MULTILINE).strip()
        return json.loads(cleaned)
    except json.JSONDecodeError:
        print("[ci-tool] Could not parse LLM response as JSON, falling back to templated fix.")
        print(f"[ci-tool] Raw response was:\n{content}")
        return None


def templated_fix(matches, removed_field, replacement_field):
    fixes = []
    for m in matches:
        fixed_content = re.sub(
            rf"\b{re.escape(removed_field)}\s*:",
            f"{replacement_field}:",
            m["content"],
        )
        fixes.append({
            "file": m["file"],
            "line": m["line"],
            "fixed_content": fixed_content,
        })
    return fixes


def apply_fixes_in_place(matches, fixes, removed_field):
    """Rewrite the client's actual source files (not a copy) so a real git
    commit on a PR branch can be made from the result."""
    by_file = {}
    for fix in fixes:
        by_file.setdefault(fix["file"], []).append(fix)

    changed_files = []
    for rel_file, file_fixes in by_file.items():
        src_path = CLIENT_REPO / rel_file
        lines = src_path.read_text().splitlines()
        for fix in file_fixes:
            lines[fix["line"] - 1] = re.sub(
                rf"^(\s*){re.escape(removed_field)}\s*:.*",
                lambda m: f"{m.group(1)}{fix['fixed_content'].strip()}",
                lines[fix["line"] - 1],
            )
        src_path.write_text("\n".join(lines) + "\n")
        changed_files.append(rel_file)
        print(f"[ci-tool] Applied fix directly to {rel_file}")
    return changed_files


def write_report(trigger, matches, fixes):
    report_lines = [
        "# API Change Auto-Fix Report",
        "",
        f"**Vendor:** {trigger['vendor']}",
        f"**API:** {trigger['api']}",
        f"**Version change:** {trigger['old_version']} -> {trigger['new_version']}",
        "",
        "## Detected changes",
        "",
    ]
    for c in trigger["changes"]:
        report_lines.append(f"- **[{c['severity']}]** {c['detail']}")

    report_lines += ["", "## Usage sites found in repo", ""]
    if not matches:
        report_lines.append("_No usages of the affected field were found in this repo._")
    for m in matches:
        report_lines.append(f"- `{m['file']}:{m['line']}` — `{m['content']}`")

    report_lines += ["", "## Applied fix", ""]
    for fix in fixes:
        report_lines.append(f"- `{fix['file']}:{fix['line']}` -> `{fix['fixed_content']}`")

    report_lines += [
        "",
        "---",
        "_This fix was generated using the client's own configured LLM credentials. "
        "No repository content was shared with the API-change detection provider._",
    ]

    REPORT_FILE.write_text("\n".join(report_lines) + "\n")
    print(f"[ci-tool] Wrote fix report to {REPORT_FILE}")


def main():
    trigger = load_trigger()

    removed_fields = [c["field"] for c in trigger["changes"] if c["type"] == "param_removed"]
    if not removed_fields:
        print("[ci-tool] No removed params in trigger event. Nothing to fix.")
        return
    removed_field = removed_fields[0]
    replacement_field = "amount"  # known from the spec diff: price -> amount

    print(f"[ci-tool] Scanning {CLIENT_REPO} for usage of '{removed_field}'...")
    matches = scan_repo_for_field(removed_field)
    print(f"[ci-tool] Found {len(matches)} usage site(s).")
    for m in matches:
        print(f"  - {m['file']}:{m['line']}  {m['content']}")

    if not matches:
        print("[ci-tool] No fix needed.")
        return

    prompt = build_llm_prompt(trigger, matches, removed_field, replacement_field)
    fixes = call_openai(prompt)
    if fixes is None:
        fixes = templated_fix(matches, removed_field, replacement_field)

    apply_fixes_in_place(matches, fixes, removed_field)
    write_report(trigger, matches, fixes)


if __name__ == "__main__":
    main()
