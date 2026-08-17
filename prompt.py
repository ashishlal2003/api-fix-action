#!/usr/bin/env python3
"""Builds the task prompt handed to the coding agent.

Scope is deliberately BROAD: we describe what the vendor changed and ask the
agent to make the codebase correct against the new spec, rather than handing
it a list of pre-grepped lines to rewrite. The narrow approach is what the
old regex implementation did, and it structurally cannot find a field that
is set inside a wrapper or in a language we didn't glob for.

The prompt spends most of its length on what NOT to change. That is not
padding — an agent with file-write access and a rename task will happily
rename an identically-named internal field and break working code. See
mock-client-repo/FIXTURE.md, case 2, where the naive scanner does exactly
that. Precision is worth more here than recall: a missed call site is a bug
that still needs fixing, but a wrongly renamed internal field is a bug the
tool INTRODUCED into code that was previously correct.
"""


def _describe_changes(trigger):
    lines = []
    for c in (trigger.get("changes") or []):
        severity = c.get("severity", "unknown")
        detail = c.get("detail") or ""
        method = (c.get("method") or "").upper()
        path = c.get("path") or ""
        field_name = c.get("field")
        bits = [f"- [{severity}]"]
        if method or path:
            bits.append(f"{method} {path}:".strip())
        bits.append(detail)
        if field_name:
            bits.append(f"(field: `{field_name}`)")
        lines.append(" ".join(b for b in bits if b))
    return "\n".join(lines) if lines else "- (no change details provided)"


def _rename_hints(trigger):
    """State the migration target for each removed param.

    The old implementation hardcoded `replacement_field = "amount"`. The
    detection engine now resolves this from the spec (see detect.py's
    infer_successor) and ships it as `successor_field`.

    It is given to the agent as a HINT, not a fact. The spec tells us which
    param replaces which on the wire; it does not tell us how this particular
    codebase reaches that wire. The agent is told to confirm against the code.
    """
    hints = []
    for c in (trigger.get("changes") or []):
        if c.get("type") != "param_removed" or not c.get("field"):
            continue
        method = (c.get("method") or "").upper()
        path = c.get("path") or ""
        field_name = c["field"]
        successor = c.get("successor_field")

        if successor:
            hints.append(
                f"- On {method} {path}: `{field_name}` was removed. Callers should "
                f"now send `{successor}` instead. Note this is a change to the "
                f"WIRE FIELD NAME in the request payload — it does not mean every "
                f"variable or property named `{field_name}` should be renamed."
            )
        else:
            hints.append(
                f"- On {method} {path}: `{field_name}` was removed, and the spec "
                f"diff does not identify a direct replacement. Work out the "
                f"correct migration from the change list above and from how this "
                f"codebase uses the field. If you cannot determine it "
                f"confidently, make no change and say so."
            )
    return "\n".join(hints) if hints else "- (no removed params in this change)"


def build_fix_prompt(trigger):
    vendor = trigger.get("vendor", "the vendor")
    api = trigger.get("api", "")
    old_v = trigger.get("old_version", "?")
    new_v = trigger.get("new_version", "?")

    return f"""You are running inside a CI job on a repository that depends on the
{vendor} API. That vendor has shipped a breaking API change, and your job is to
update this repository so it remains correct against the NEW version of the API.

## What changed

Vendor: {vendor}
API area: {api}
Version: {old_v} -> {new_v}

{_describe_changes(trigger)}

## Likely renames inferred from the spec diff

{_rename_hints(trigger)}

## Your task

Find EVERY place in this repository that is affected by the change above, and
fix it. Work from the code itself — do not assume the affected sites all look
alike or all live in one directory.

**Edit the files directly.** You are running unattended in a CI job: there is
no human available to approve anything, and a plan or summary on its own has
no effect. Making zero edits is only correct if this repository genuinely does
not call the changed API. If it does call it, you must apply the changes to
the files themselves before you finish.

Be thorough about where you look:

- Every language in the repo, not just the most common one. Vendor SDKs exist
  for many languages and a repo often calls the same API from more than one.
- Indirect call sites. The vendor field may be set in a wrapper, helper,
  factory, builder, or config object far from where the SDK is imported.
  Follow the data flow.
- Different syntactic forms of the same thing: object literal keys, keyword
  arguments, string dict keys, spread/merge into a payload, variables that
  are later passed through.
- Type definitions, interfaces, schemas, and JSDoc/docstring annotations that
  declare the old field.
- Tests, fixtures, mocks, snapshots, and factories that assert on the old
  field. If you change the source but not the tests, you break the build.

## What you must NOT change — read this carefully

The single worst outcome is renaming something that only LOOKS related. A
field with the same name is not necessarily the vendor's field.

- Only change a field when it is genuinely part of a payload sent to, or a
  response received from, the {vendor} API. Trace it to an actual vendor call.
- Do NOT rename identically-named fields that belong to this codebase's own
  domain models, database rows, internal catalogs, config files, or UI state.
  A repo can easily have its own unrelated field with the same name, and
  renaming it silently breaks working code.
- Do NOT rename local variables, function parameters, or helper names just
  because they contain the field name. Only the wire-format field the vendor
  receives needs to change.
- Do NOT reformat, refactor, restructure, or "improve" code you are not
  fixing. Keep the diff limited to this migration.
- Do NOT edit files under .git/, node_modules/, vendor/, dist/, or build/.
- Do NOT modify CI workflow files, or any file containing credentials.

This caution applies to INDIVIDUAL ambiguous sites, not to the task as a
whole. When you are genuinely unsure whether one particular site is the
vendor's field or an unrelated internal one, leave that site alone and note it
in your summary — then carry on and fix the sites you ARE confident about. A
single wrongly renamed field is worse than a single missed one, but skipping
the whole migration is worse than either: the repository stays broken against
the new API and nobody is told why.

## When you are done

Make the edits directly to the files. Then reply with a short summary
containing:

1. Each file you changed and what you changed in it.
2. Any place you found that looked related but you deliberately left alone,
   and why.
3. Anything a human reviewer should verify before merging.

Keep the summary concise and factual — it becomes the body of a pull request
that a human will read.
"""
