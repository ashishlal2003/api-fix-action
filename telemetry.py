#!/usr/bin/env python3
"""Telemetry — what this action reports back to Zenik, and what it never does.

This is the ONE place where data flows from a client's runner back to us, so
the rules are enforced here in code rather than promised in a doc:

  1. The payload is a CLOSED ALLOWLIST. build_payload() constructs a fixed
     set of keys. There is no passthrough, no **kwargs, no "extra" dict.
     Adding a field is a deliberate edit to this file, reviewable in a diff.

  2. Nothing that describes the client's code is included. Not file paths,
     not file names, not the diff, not source snippets, not branch or repo
     names, not the agent's prose output. Counts and enums only.

  3. Repo identity is a SALTED HASH, never the name. It is stable, so a
     dashboard can count distinct repos and track one over time, but it is
     not reversible to a name. The salt is the client key, so hashes are not
     even comparable across clients.

  4. Sending NEVER blocks or fails the client's build. Every failure path
     here is swallowed. If our endpoint is down, the client still gets their
     fix and their PR. Our metrics are not worth breaking someone's CI.

What we do send is deliberately boring: did a fix happen, how many files,
was a PR opened, how many tokens. That is what a usage dashboard needs and
it reveals nothing about what the client's software does.
"""
import hashlib
import json
import os
import urllib.error
import urllib.request

SCHEMA_VERSION = 1

# Short by design. This call is a side effect, not part of the job: if it
# cannot complete quickly we drop it rather than delay the client's run.
TELEMETRY_TIMEOUT_SECONDS = 5

# Outcome enum — the single field that answers "did this run do anything?"
OUTCOME_FIXED = "fixed"                   # agent made edits
OUTCOME_NO_USAGES = "no_usages_found"     # ran, found nothing to change
OUTCOME_AGENT_FAILED = "agent_failed"     # agent errored or timed out
OUTCOME_NO_CHANGE = "no_change"           # trigger said nothing changed
OUTCOME_FALLBACK = "fallback_deterministic"  # no agent key; legacy path ran


def hash_repo(repo_full_name, client_key):
    """Stable, non-reversible repo identifier.

    Salted with the client key so the same repo under two different clients
    hashes differently, and so a hash cannot be checked against a rainbow
    table of common `owner/name` strings.
    """
    if not repo_full_name:
        return None
    salt = client_key or "unsalted"
    digest = hashlib.sha256(f"{salt}:{repo_full_name}".encode()).hexdigest()
    return f"sha256:{digest[:32]}"


def change_ids(trigger):
    """Stable identifiers for WHICH vendor changes this run responded to.

    Derived purely from the vendor's public API spec — this is our own data
    coming back to us, and contains nothing about the client.
    """
    ids = []
    for c in (trigger.get("changes") or []):
        ctype = c.get("type", "unknown")
        field_name = c.get("field", "")
        method = (c.get("method") or "").upper()
        path = c.get("path", "")
        ids.append(f"{ctype}:{field_name}:{method} {path}".strip())
    return ids


def build_payload(
    *,
    trigger,
    client_key,
    repo_full_name,
    run_id,
    outcome,
    files_changed_count,
    lines_added,
    lines_removed,
    pr_expected,
    duration_seconds,
    agent_result=None,
):
    """Construct the complete telemetry payload.

    Every key in the returned dict is written literally below. If a value
    isn't here, it isn't sent.
    """
    agent_block = {
        "backend": None,
        "model": None,
        "cli_version": None,
    }
    usage_block = {
        "input_tokens": None,
        "cached_input_tokens": None,
        "output_tokens": None,
        "reasoning_output_tokens": None,
        "cost_usd": None,
    }

    if agent_result is not None:
        agent_block["backend"] = agent_result.backend
        agent_block["model"] = agent_result.model
        agent_block["cli_version"] = agent_result.cli_version
        usage_block = agent_result.usage.as_dict()

    return {
        "schema_version": SCHEMA_VERSION,

        # Who and when.
        "client_key": client_key,
        "repo_hash": hash_repo(repo_full_name, client_key),
        "run_id": run_id,

        # Which vendor change this run was reacting to (our own data).
        "vendor": trigger.get("vendor"),
        "api": trigger.get("api"),
        "old_version": trigger.get("old_version"),
        "new_version": trigger.get("new_version"),
        "change_ids": change_ids(trigger),

        # What the agent was and what it cost.
        "agent": agent_block,
        "usage": usage_block,

        # What happened — counts only, never names.
        "outcome": outcome,
        "files_changed_count": files_changed_count,
        "lines_added": lines_added,
        "lines_removed": lines_removed,
        "pr_expected": pr_expected,
        "duration_seconds": duration_seconds,
    }


def send(payload, api_url, client_key):
    """POST the payload. Returns True on success, False otherwise — never raises.

    Telemetry is best-effort by design. Every exception below is caught and
    logged, because the alternative is our reporting endpoint being able to
    fail a client's CI run.
    """
    if not api_url:
        print("[telemetry] no ZENIK_API_URL configured; skipping report")
        return False

    url = api_url.rstrip("/") + "/v1/telemetry"
    body = json.dumps(payload).encode()

    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {client_key}",
            "User-Agent": "zenik-api-fix-action/1",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=TELEMETRY_TIMEOUT_SECONDS) as resp:
            if 200 <= resp.status < 300:
                print(f"[telemetry] reported run outcome={payload['outcome']} "
                      f"files={payload['files_changed_count']}")
                return True
            print(f"[telemetry] endpoint returned HTTP {resp.status}; ignoring")
            return False
    except urllib.error.HTTPError as e:
        print(f"[telemetry] HTTP {e.code} from endpoint; ignoring")
    except urllib.error.URLError as e:
        print(f"[telemetry] could not reach endpoint ({e.reason}); ignoring")
    except Exception as e:  # noqa: BLE001 - never let telemetry break the build
        print(f"[telemetry] unexpected error ({type(e).__name__}); ignoring")
    return False


def print_payload(payload):
    """Echo exactly what we're about to send, into the client's own CI log.

    Deliberate transparency: a client can read their workflow log and see
    the complete telemetry body, rather than taking our word for it.
    """
    print("[telemetry] payload (this is everything that is sent):")
    for line in json.dumps(payload, indent=2).splitlines():
        print(f"  {line}")


def env_repo_full_name():
    """GitHub sets GITHUB_REPOSITORY to 'owner/name' on every runner."""
    return os.environ.get("GITHUB_REPOSITORY")


def env_run_id():
    run_id = os.environ.get("GITHUB_RUN_ID")
    return f"gh-run-{run_id}" if run_id else None
