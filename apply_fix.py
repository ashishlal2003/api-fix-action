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
input — see action.yml. This script never talks to our API to ASK for work;
its only outbound call is a best-effort telemetry POST after the fact.

HOW THE FIX IS PRODUCED
-----------------------
A coding agent CLI (Codex or Claude Code) runs on this runner, in the
client's checkout, driven by the CLIENT's own API key. Which agent runs is
determined by which key the client configured — see agent_backends.py. The
agent reads the repo with its own tools and edits files directly; we do not
grep for the field ourselves, because grepping cannot see indirection or
distinguish a vendor field from an identically named internal one.

If no agent key is configured, we fall back to the original deterministic
regex path so the action degrades instead of failing the client's build.
That path is known-imprecise — see mock-client-repo/FIXTURE.md — and exists
only as a safety net.

WHAT LEAVES THE RUNNER
----------------------
The client's code, the prompt, the agent's output, and the diff all stay on
this runner. The only thing sent to Zenik is a fixed-schema telemetry record
of counts and token usage — see telemetry.py, which enumerates every field
and prints the exact payload into the client's own CI log.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# python-dotenv is only useful for local/manual runs. In real GitHub Actions
# secrets arrive as env vars and no .env file exists.
#
# `override=False` matters: a .env sitting next to this script must never
# win over what the caller's workflow explicitly passed in. Without it, a
# leftover local .env silently supplies an API key the client did not
# configure — which, for a tool that picks its agent backend based on which
# key is present, means running (and billing) an agent nobody asked for.
# Real env vars always take precedence.
from dotenv import load_dotenv

# ZENIK_NO_DOTENV=1 skips the .env entirely. Useful for exercising the
# no-agent-configured path on a developer machine that happens to have keys
# in a local .env, without having to move the file.
if os.environ.get("ZENIK_NO_DOTENV") != "1":
    load_dotenv(Path(__file__).parent / ".env", override=False)

sys.path.insert(0, str(Path(__file__).parent))
import telemetry  # noqa: E402
from agent_backends import select_backend  # noqa: E402
from legacy_scan import run_deterministic_fallback  # noqa: E402
from prompt import build_fix_prompt  # noqa: E402
from report import write_report  # noqa: E402

# The client's repo — in real CI this is the working directory GitHub
# Actions checked the client's code into (actions/checkout on their repo).
# For a manual/local run, set CLIENT_REPO_PATH to point at a checked-out
# client repo instead.
CLIENT_REPO = Path(os.environ.get("CLIENT_REPO_PATH", ".")).resolve()


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


def git_diff_stats():
    """Count what actually changed in the working tree.

    Deliberately returns COUNTS and file paths separately: the paths are used
    locally to write the report the client reads, while only the counts are
    ever passed to telemetry. See telemetry.py for why that split matters.
    """
    def _git(*args):
        try:
            proc = subprocess.run(
                ["git", *args], cwd=CLIENT_REPO,
                capture_output=True, text=True, timeout=60,
            )
            return proc.stdout if proc.returncode == 0 else ""
        except Exception:
            return ""

    changed = [f for f in _git("diff", "--name-only").splitlines() if f.strip()]
    untracked = [
        f for f in _git("ls-files", "--others", "--exclude-standard").splitlines()
        if f.strip() and not f.endswith("fix_report.md")
    ]

    added = removed = 0
    for line in _git("diff", "--numstat").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            try:
                added += int(parts[0])
                removed += int(parts[1])
            except ValueError:
                pass  # binary files show as '-'

    return {
        "changed_files": changed,
        "new_files": untracked,
        "lines_added": added,
        "lines_removed": removed,
    }


def run_agent(trigger):
    """Run the client's configured coding agent over their checkout."""
    preference = os.environ.get("AGENT_BACKEND", "").strip() or None
    model = os.environ.get("AGENT_MODEL", "").strip() or None
    try:
        timeout = int(os.environ.get("AGENT_TIMEOUT_SECONDS", "") or 900)
    except ValueError:
        timeout = 900

    backend = select_backend(preference=preference, timeout=timeout, model=model)
    if backend is None:
        return None

    print(f"[ci-tool] Using agent backend: {backend.name}")
    if not backend.ensure_installed():
        print(f"[ci-tool] Could not install {backend.package}.")
        from agent_backends import AgentResult
        return AgentResult(
            backend=backend.name, ok=False,
            error=f"failed to install {backend.package}",
        )

    prompt = build_fix_prompt(trigger)
    print(f"[ci-tool] Handing the migration task to {backend.name} "
          f"(sandboxed to this checkout)...")

    started = time.time()
    result = backend.run(prompt, workdir=str(CLIENT_REPO))
    result.duration_seconds = round(time.time() - started, 1)

    if result.ok:
        print(f"[ci-tool] Agent finished in {result.duration_seconds}s.")
    else:
        print(f"[ci-tool] Agent did not complete successfully: {result.error}")
    return result


def main():
    started = time.time()
    trigger = load_trigger()

    client_key = os.environ.get("ZENIK_CLIENT_KEY", "")
    api_url = os.environ.get("ZENIK_API_URL", "")

    agent_result = run_agent(trigger)

    if agent_result is None:
        # No agent key configured — fall back to the legacy deterministic
        # path rather than failing the client's build outright.
        print("[ci-tool] No agent API key configured (set OPENAI_API_KEY or "
              "ANTHROPIC_API_KEY). Falling back to the deterministic scanner, "
              "which is significantly less accurate.")
        fallback_outcome = run_deterministic_fallback(trigger, CLIENT_REPO)
        outcome = (telemetry.OUTCOME_FALLBACK if fallback_outcome
                   else telemetry.OUTCOME_NO_USAGES)
    elif not agent_result.ok:
        outcome = telemetry.OUTCOME_AGENT_FAILED
    else:
        outcome = None  # determined below from the actual diff

    stats = git_diff_stats()
    touched = stats["changed_files"] + stats["new_files"]

    if outcome is None:
        outcome = (telemetry.OUTCOME_FIXED if touched
                   else telemetry.OUTCOME_NO_USAGES)

    write_report(
        trigger=trigger,
        stats=stats,
        agent_result=agent_result,
        outcome=outcome,
        report_path=CLIENT_REPO / "fix_report.md",
    )

    duration = round(time.time() - started, 1)
    payload = telemetry.build_payload(
        trigger=trigger,
        client_key=client_key,
        repo_full_name=telemetry.env_repo_full_name(),
        run_id=telemetry.env_run_id(),
        outcome=outcome,
        files_changed_count=len(touched),
        lines_added=stats["lines_added"],
        lines_removed=stats["lines_removed"],
        pr_expected=bool(touched),
        duration_seconds=duration,
        agent_result=agent_result,
    )
    telemetry.print_payload(payload)
    telemetry.send(payload, api_url=api_url, client_key=client_key)

    # Surface whether a PR should follow, so the workflow can gate the
    # create-pull-request step instead of opening an empty PR.
    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:
        with open(gh_output, "a") as f:
            f.write(f"files-changed={len(touched)}\n")
            f.write(f"outcome={outcome}\n")

    print(f"[ci-tool] Done. outcome={outcome} files_changed={len(touched)}")

    # A failed agent run is a real failure and should be visible in the
    # client's CI, but only AFTER telemetry and the report are written.
    if outcome == telemetry.OUTCOME_AGENT_FAILED:
        sys.exit(1)


if __name__ == "__main__":
    main()
