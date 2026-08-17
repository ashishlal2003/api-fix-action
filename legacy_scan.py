#!/usr/bin/env python3
"""Deterministic regex fallback — the ORIGINAL implementation, kept as a net.

This runs only when the client has configured no agent API key at all. It is
retained so the action degrades instead of hard-failing, not because it is
good. Its accuracy on the reference fixture is poor by construction:

    mock-client-repo/FIXTURE.md, measured:
      3 correct fixes, 3 HARMFUL false positives (renames an unrelated
      internal catalog field, breaking working code), 3 spurious matches
      inside comments, and 5 real call sites missed entirely.

The false positives are the reason this is a fallback and not a default. A
missed fix leaves a known problem in place; a wrong fix introduces a new one
into code that was previously correct.

Two things were hardcoded in the original and are now derived:
  - the replacement field name (was literally "amount")
  - nothing else; the file glob is still src/**/*.js, which is exactly the
    blindness described above. Not worth improving — the agent path is the
    real answer.
"""
import re


def scan_repo_for_field(client_repo, field_name):
    """Grep the client repo's source for usage of the removed field."""
    matches = []
    pattern = re.compile(rf"\b{re.escape(field_name)}\s*:")
    for path in (client_repo / "src").rglob("*.js"):
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                matches.append({
                    "file": str(path.relative_to(client_repo)),
                    "line": i,
                    "content": line.strip(),
                })
    return matches


def infer_replacement(trigger, removed_field):
    """Get the field callers should migrate to, from the spec diff.

    The detection engine resolves this (see detect.py's infer_successor) and
    ships it as `successor_field`. This was previously hardcoded to "amount".
    Falls back to the removed/added pairing for older payloads that predate
    the field. Returns None when it genuinely can't be determined — the
    caller must not guess.
    """
    removed_op = None
    for c in (trigger.get("changes") or []):
        if c.get("type") == "param_removed" and c.get("field") == removed_field:
            if c.get("successor_field"):
                return c["successor_field"]
            removed_op = (c.get("method"), c.get("path"))
            break
    if removed_op is None:
        return None

    added = [
        c.get("field") for c in (trigger.get("changes") or [])
        if c.get("type") == "param_added"
        and (c.get("method"), c.get("path")) == removed_op
        and c.get("field")
    ]
    return added[0] if len(added) == 1 else None


def apply_fixes_in_place(client_repo, matches, removed_field, replacement_field):
    """Rewrite the client's actual source files so a real commit can be made."""
    by_file = {}
    for m in matches:
        by_file.setdefault(m["file"], []).append(m)

    changed_files = []
    for rel_file, file_matches in by_file.items():
        src_path = client_repo / rel_file
        lines = src_path.read_text().splitlines()
        for m in file_matches:
            idx = m["line"] - 1
            lines[idx] = re.sub(
                rf"\b{re.escape(removed_field)}\s*:",
                f"{replacement_field}:",
                lines[idx],
            )
        src_path.write_text("\n".join(lines) + "\n")
        changed_files.append(rel_file)
        print(f"[legacy-scan] Applied fix to {rel_file}")
    return changed_files


def run_deterministic_fallback(trigger, client_repo):
    """Returns True if any file was modified."""
    removed_fields = [
        c["field"] for c in (trigger.get("changes") or [])
        if c.get("type") == "param_removed" and c.get("field")
    ]
    if not removed_fields:
        print("[legacy-scan] No removed params in trigger event. Nothing to fix.")
        return False

    removed_field = removed_fields[0]
    replacement_field = infer_replacement(trigger, removed_field)
    if replacement_field is None:
        print(f"[legacy-scan] Could not determine what '{removed_field}' was "
              f"renamed to from the spec diff. Refusing to guess — no changes made.")
        return False

    print(f"[legacy-scan] Scanning {client_repo} for '{removed_field}' "
          f"-> '{replacement_field}'...")
    matches = scan_repo_for_field(client_repo, removed_field)
    print(f"[legacy-scan] Found {len(matches)} candidate site(s).")
    for m in matches:
        print(f"  - {m['file']}:{m['line']}  {m['content']}")

    if not matches:
        return False

    print("[legacy-scan] WARNING: this scanner cannot distinguish the vendor's "
          "field from an identically named internal one, and only looks at "
          "src/**/*.js. Review the resulting diff carefully.")
    apply_fixes_in_place(client_repo, matches, removed_field, replacement_field)
    return True
