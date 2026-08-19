#!/usr/bin/env python3
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

    # An agent that ran successfully but changed nothing is ambiguous: it
    # either correctly found no affected code, or it silently no-op'd (e.g.
    # it produced a plan instead of edits). Those need different responses,
    # so make the distinction loud in the log rather than reporting a clean
    # "no usages found" for both.
    if (agent_result is not None and agent_result.ok and not touched):
        print("[ci-tool] NOTE: the agent completed without editing any files. "
              "If this repository does call the changed API, that is a silent "
              "no-op rather than a clean result — check the agent summary "
              "above for whether it explained why.")

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
