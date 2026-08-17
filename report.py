#!/usr/bin/env python3
"""Writes fix_report.md — which becomes the body of the pull request.

Audience: the client's own engineer reviewing an automated PR against their
production code. What they need, in order:

  1. Why does this PR exist? (what the vendor changed)
  2. What did it touch?
  3. What should I check before merging?
  4. What was the machine unsure about?

Point 4 matters most and is the easiest to omit. An agent that says "I left
src/catalog.js alone because its `price` field is an internal catalog column,
not the vendor param" is giving the reviewer the single most useful sentence
in the diff. That text comes from the agent's own summary, so it is passed
through rather than reformatted.
"""


def _agent_line(agent_result):
    if agent_result is None:
        return ("Deterministic pattern scanner (no LLM key configured). "
                "**This mode is imprecise — review every changed line.**")
    bits = [f"`{agent_result.backend}`"]
    if agent_result.model:
        bits.append(f"model `{agent_result.model}`")
    if agent_result.cli_version:
        bits.append(f"CLI `{agent_result.cli_version}`")
    return ", ".join(bits)


def _usage_line(agent_result):
    if agent_result is None:
        return None
    u = agent_result.usage
    parts = []
    if u.input_tokens is not None:
        parts.append(f"{u.input_tokens:,} in")
    if u.output_tokens is not None:
        parts.append(f"{u.output_tokens:,} out")
    if u.cached_input_tokens:
        parts.append(f"{u.cached_input_tokens:,} cached")
    if u.cost_usd is not None:
        parts.append(f"~${u.cost_usd:.4f}")
    return " / ".join(parts) if parts else None


def write_report(*, trigger, stats, agent_result, outcome, report_path):
    vendor = trigger.get("vendor", "vendor")
    lines = [
        "# API Change Auto-Fix",
        "",
        f"The **{vendor}** API changed. This PR updates this repository to "
        f"match the new version.",
        "",
        "## What changed upstream",
        "",
        f"- **Vendor:** {vendor}",
        f"- **API:** {trigger.get('api', 'n/a')}",
        f"- **Version:** `{trigger.get('old_version', '?')}` → "
        f"`{trigger.get('new_version', '?')}`",
        "",
    ]

    for c in (trigger.get("changes") or []):
        sev = c.get("severity", "unknown")
        marker = "**breaking**" if sev == "breaking" else sev
        lines.append(f"- [{marker}] {c.get('detail', '')}")

    lines += ["", "## What this PR changes", ""]

    changed = stats["changed_files"]
    new_files = [f for f in stats["new_files"] if f != "fix_report.md"]

    if not changed and not new_files:
        lines.append(
            "_No code changes were made._ The affected vendor field was not "
            "found in use in this repository. No action needed — this PR (if "
            "opened at all) can be closed."
        )
    else:
        lines.append(
            f"{len(changed) + len(new_files)} file(s) changed "
            f"(+{stats['lines_added']} / -{stats['lines_removed']} lines):"
        )
        lines.append("")
        for f in changed:
            lines.append(f"- `{f}`")
        for f in new_files:
            lines.append(f"- `{f}` _(new)_")

    if agent_result is not None and agent_result.final_message:
        lines += [
            "",
            "## Agent's summary",
            "",
            "> Written by the coding agent that made these edits, including "
            "anything it deliberately left alone.",
            "",
            agent_result.final_message.strip(),
        ]

    if outcome == "agent_failed":
        lines += [
            "",
            "## ⚠️ The agent did not complete successfully",
            "",
            f"```\n{(agent_result.error if agent_result else 'unknown error')[:1500]}\n```",
            "",
            "Any changes above are partial and should be treated with extra "
            "suspicion.",
        ]

    lines += ["", "## Before you merge", ""]
    if outcome == "fallback_deterministic":
        lines += [
            "- **This fix came from the fallback pattern scanner, not an agent.** "
            "It cannot tell the vendor's field from an identically named "
            "internal one. Check every changed line individually.",
            "- Configure `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` in this repo's "
            "secrets to get the far more accurate agent-based fix.",
        ]
    else:
        lines += [
            "- Confirm each changed line is genuinely a call to the vendor API, "
            "not an internal field that happens to share the name.",
            "- Run your test suite — this PR does not verify its own changes.",
            "- Check whether any affected call site was missed.",
        ]

    lines += ["", "---", ""]
    engine = _agent_line(agent_result)
    lines.append(f"**Fix produced by:** {engine}")
    usage = _usage_line(agent_result)
    if usage:
        lines.append(f"**Token usage:** {usage} _(billed to this repo's own "
                     f"API key)_")

    lines += [
        "",
        "_The scan and fix ran entirely on this repository's own CI runner, "
        "using this repository's own LLM credentials. No source code, prompt, "
        "or diff content was sent to the API-change detection provider — only "
        "an anonymous record of counts and token usage. That record is printed "
        "in full in this job's log._",
    ]

    report_path.write_text("\n".join(lines) + "\n")
    print(f"[ci-tool] Wrote fix report to {report_path}")
