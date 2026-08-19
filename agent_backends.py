#!/usr/bin/env python3
"""Agent backend adapters — the part of the action that actually edits code.

Design note (why this file exists at all):

The previous version of this tool grepped for a field name and asked an LLM
to rewrite the matched lines. That approach cannot see indirection (a field
set inside a wrapper), cannot tell a vendor API param from an identically
named internal field, and only looks at the file globs it was hardcoded
with. See mock-client-repo/FIXTURE.md for the measured failure rates.

Instead we hand the task to a real coding agent CLI running on the client's
own runner, with its own filesystem tools. We do NOT ship an agent: we shell
out to whichever one the client already pays for, selected by which API key
they configured. Their key, their model, their runner, their bill.

Two backends are supported, and they differ in more than just their flags:

    Codex (OpenAI)        codex exec --json         → JSONL event stream
    Claude Code (Anthropic)  claude -p --output-format json → one JSON object

Both are normalized into AgentResult below, so the rest of the action never
branches on which agent ran.

Verify before first real use: CLI package names, flag spellings, and the
exact JSON field names below were taken from each vendor's published docs.
They are stable interfaces, but a CLI major version bump can move them. Both
adapters degrade to "usage unknown" rather than crashing if a field is
missing — a fix that worked must never be reported as a failure because we
could not parse a token count.
"""
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Optional

# Pinned CLI versions. Unpinned installs mean a vendor's release can silently
# change behavior inside a client's CI run, which is exactly the kind of
# surprise a tool operating on someone else's codebase must not have.
#
# What a bump can break, concretely: the flag spellings in each backend's
# run(), and the JSON field names each _parse() reads. Both are documented
# per-backend below. Codex 0.147 already rejected `--ask-for-approval never`
# once; that is the class of change a pin exists to stop from landing
# mid-run inside someone else's CI.
#
# Bumping these is a deliberate edit: change the version, then re-run the
# fixture end-to-end for that backend before shipping it.
CODEX_VERSION = "0.148.0"
CLAUDE_VERSION = "2.1.235"

CODEX_PACKAGE = f"@openai/codex@{CODEX_VERSION}"
CLAUDE_PACKAGE = f"@anthropic-ai/claude-code@{CLAUDE_VERSION}"

# Wall-clock ceiling for an agent run. A hung agent must not hold a client's
# CI runner open indefinitely.
DEFAULT_TIMEOUT_SECONDS = 900


@dataclass
class AgentUsage:
    """Token accounting, normalized across backends.

    Field-name mapping (their name -> ours):

      Codex   input_tokens            -> input_tokens
              cached_input_tokens     -> cached_input_tokens
              output_tokens           -> output_tokens
              reasoning_output_tokens -> reasoning_output_tokens
              (no cost reported)      -> cost_usd stays None

      Claude  usage.input_tokens              -> input_tokens
              usage.cache_read_input_tokens   -> cached_input_tokens
              usage.output_tokens             -> output_tokens
              (no separate reasoning count)   -> reasoning_output_tokens None
              total_cost_usd                  -> cost_usd

    cost_usd is deliberately left None for Codex rather than derived from a
    hardcoded price table: a stale price table reports confident wrong
    numbers, which is worse for a billing dashboard than an honest null.
    """
    input_tokens: Optional[int] = None
    cached_input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    reasoning_output_tokens: Optional[int] = None
    cost_usd: Optional[float] = None

    def as_dict(self):
        return {
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_output_tokens": self.reasoning_output_tokens,
            "cost_usd": self.cost_usd,
        }


@dataclass
class AgentResult:
    """Outcome of one agent run, backend-independent."""
    backend: str
    ok: bool
    usage: AgentUsage = field(default_factory=AgentUsage)
    final_message: str = ""
    error: str = ""
    model: Optional[str] = None
    cli_version: Optional[str] = None
    duration_seconds: Optional[float] = None


class AgentBackend:
    """Base class. Subclasses own their CLI's install, invocation, parsing."""

    name = "base"
    env_key = None          # the client env var that selects this backend
    package = None          # npm package providing the CLI
    binary = None           # executable name once installed

    def __init__(self, timeout=DEFAULT_TIMEOUT_SECONDS, model=None):
        self.timeout = timeout
        self.model = model

    @classmethod
    def is_configured(cls):
        return bool(os.environ.get(cls.env_key))

    #: Version this backend is pinned to. Set by each subclass.
    version = None

    def ensure_installed(self):
        """Make the PINNED CLI version available, installing it if needed.

        Checking only for the binary's presence would defeat the pin: a
        runner image that ships its own copy of the CLI, or a cached global
        npm dir from an earlier run, would supply an arbitrary version and we
        would use it without noticing. So a CLI already on PATH is accepted
        only when its version matches the pin.

        A version we cannot parse is treated as a mismatch and reinstalled.
        Guessing "close enough" from an unreadable version string is how the
        pin quietly stops meaning anything.
        """
        found = shutil.which(self.binary)
        if found:
            current = self._installed_version()
            if current == self.version:
                print(f"[agent:{self.name}] {self.binary} {current} "
                      f"already on PATH (matches pin)")
                return True
            print(f"[agent:{self.name}] {self.binary} on PATH reports "
                  f"{current or 'an unreadable version'}, but this action is "
                  f"pinned to {self.version}; installing the pinned version")

        print(f"[agent:{self.name}] installing {self.package}...")
        proc = subprocess.run(
            ["npm", "install", "-g", self.package],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            print(f"[agent:{self.name}] install failed:\n{proc.stderr[-2000:]}")
            return False

        if not shutil.which(self.binary):
            print(f"[agent:{self.name}] {self.binary} still not on PATH after install")
            return False

        # Confirm we actually got the pinned version. npm can resolve to
        # something else (a dist-tag alias, a stale global prefix earlier on
        # PATH); running an unexpected version is exactly what the pin is
        # meant to prevent, so say so rather than proceeding silently.
        installed = self._installed_version()
        if installed != self.version:
            print(f"[agent:{self.name}] WARNING: expected {self.version} after "
                  f"install but {self.binary} reports "
                  f"{installed or 'an unreadable version'}. Continuing, but the "
                  f"flag and JSON-field assumptions in this adapter were "
                  f"written against {self.version}.")
        return True

    def _installed_version(self):
        """Bare version number from `<binary> --version`, or None.

        The CLIs do not agree on format — Codex prints `0.148.0`, Claude Code
        prints `2.1.235 (Claude Code)` — so pull out the first dotted numeric
        token rather than comparing the raw string.
        """
        raw = self._cli_version()
        if not raw:
            return None
        m = re.search(r"\d+\.\d+\.\d+", raw)
        return m.group(0) if m else None

    def _cli_version(self):
        try:
            proc = subprocess.run(
                [self.binary, "--version"],
                capture_output=True, text=True, timeout=30,
            )
            return proc.stdout.strip() or None
        except Exception:
            return None

    def run(self, prompt, workdir):
        raise NotImplementedError


class CodexBackend(AgentBackend):
    """OpenAI Codex CLI.

    Invocation:  codex exec --json --sandbox workspace-write "<prompt>"

    `workspace-write` lets the agent edit files in the checkout but not
    escape it. We deliberately do NOT use danger-full-access: the agent
    never needs to run arbitrary commands to apply a source edit, and the
    weaker sandbox is far easier to defend at a client security review.

    stdout is JSONL. We care about the terminal `turn.completed` event,
    which carries the usage object; `turn.failed` means the run errored.
    """

    name = "codex"
    env_key = "OPENAI_API_KEY"
    package = CODEX_PACKAGE
    version = CODEX_VERSION
    binary = "codex"

    def run(self, prompt, workdir):
        # `--sandbox workspace-write` grants write access to the checkout, but
        # the APPROVAL POLICY is separate and defaults to `on-request`: the
        # agent pauses to ask a human before acting. In CI there is nobody to
        # ask, so it summarizes what it would do, exits 0, and edits nothing —
        # a silent no-op that looks like success. `-a never` makes it act
        # within the sandbox without asking; anything the sandbox blocks
        # simply fails back to the model instead of prompting.
        # Codex's default Linux sandbox backend is bubblewrap, which needs to
        # create a network namespace. On GitHub-hosted runners (and any host
        # with AppArmor's unprivileged userns restrictions) that is denied,
        # and bwrap fails every filesystem operation with:
        #     bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted
        # The agent then runs, reads nothing, edits nothing, and reports the
        # sandbox as broken. This is a known Codex bug, not a misconfiguration
        # on the caller's side (openai/codex issues #17337, #16334, #15982).
        #
        # The actual fix is applied BEFORE this runs — see the "Prepare Linux
        # sandbox" step in action.yml, which installs bubblewrap and loads the
        # AppArmor profile that lets it create user namespaces. That is what
        # OpenAI documents for Ubuntu 24.04 (which is what ubuntu-latest is),
        # so the sandbox works properly rather than being worked around.
        #
        # The `features.use_legacy_landlock=true` flag is deliberately NOT set
        # here. It is accepted by this CLI version but Landlock also fails on
        # GitHub runners ("every command, including /bin/true, fails before
        # execution"), and openai/codex#18800 indicates the flag is being
        # removed. Fixing the bubblewrap prerequisites is the durable answer.
        #
        # We do NOT fall back to --sandbox danger-full-access. The agent runs
        # in a job that also holds the caller's API keys, and silently
        # dropping confinement is the kind of downgrade a caller would never
        # discover. If the sandbox can't start, the run fails — see
        # _detect_sandbox_failure below.
        cmd = [
            self.binary, "exec",
            "--json",
            "--sandbox", "workspace-write",
            "--ask-for-approval", "never",
            "--skip-git-repo-check",
        ]
        if self.model:
            cmd += ["--model", self.model]
        cmd.append(prompt)

        # The API key is scoped to THIS subprocess only — never exported
        # into the job environment, where repo-controlled code (build
        # scripts, test hooks, other actions) could read it. This mirrors
        # OpenAI's own CI guidance for codex.
        env = dict(os.environ)
        env["CODEX_API_KEY"] = os.environ.get("OPENAI_API_KEY", "")

        try:
            proc = subprocess.run(
                cmd, cwd=workdir, env=env,
                capture_output=True, text=True, timeout=self.timeout,
            )

            # CLI flags move between versions. Rather than failing a whole CI
            # run on an unrecognized argument, drop the optional flag and go
            # again — the approval flag is a belt-and-braces default, so a
            # build of the CLI that doesn't accept it is not a reason to give
            # up on the migration.
            if proc.returncode != 0 and "unexpected argument" in (proc.stderr or ""):
                bad = self._unknown_flag(proc.stderr)
                # Never strip a security-relevant flag to make the run
                # succeed. Dropping --sandbox or the Landlock selector would
                # silently downgrade confinement, which is precisely what
                # this action must not do behind the caller's back.
                if bad in ("--sandbox", "-c", "--config"):
                    print(f"[agent:{self.name}] this CLI build rejected the "
                          f"sandbox flag '{bad}'. Refusing to retry without "
                          f"it — that would run the agent unconfined.")
                elif bad and bad in cmd:
                    idx = cmd.index(bad)
                    # Drop the flag and its value if it takes one.
                    trimmed = cmd[:idx] + cmd[idx + 2:] if (
                        idx + 1 < len(cmd) and not cmd[idx + 1].startswith("-")
                    ) else cmd[:idx] + cmd[idx + 1:]
                    print(f"[agent:{self.name}] this CLI build rejected '{bad}'; "
                          f"retrying without it")
                    proc = subprocess.run(
                        trimmed, cwd=workdir, env=env,
                        capture_output=True, text=True, timeout=self.timeout,
                    )
        except subprocess.TimeoutExpired:
            return AgentResult(
                backend=self.name, ok=False,
                error=f"agent timed out after {self.timeout}s",
                cli_version=self._cli_version(),
            )

        return self._parse(proc)

    @staticmethod
    def _unknown_flag(stderr):
        """Pull the offending flag out of a clap-style argument error."""
        m = re.search(r"unexpected argument '([^']+)'", stderr or "")
        return m.group(1) if m else None

    @staticmethod
    def _detect_sandbox_failure(final_message, stderr):
        """Spot a run where the sandbox itself was broken.

        Returns an explanatory error string, or None if the sandbox was fine.
        These markers appear when the sandbox backend cannot start, so the
        agent is alive but every file operation it attempts is denied.
        """
        haystack = f"{final_message or ''}\n{stderr or ''}".lower()
        markers = (
            "rtm_newaddr",                 # bwrap loopback setup denied
            "bwrap:",                      # any bubblewrap failure
            "failed to create sandbox",
            "sandbox was denied",
            "landlock: failed",            # landlock backend refused to start
            "failed to apply landlock",
        )
        # Note: a bare mention of "landlock" is NOT a failure marker — it is
        # the backend this action deliberately selects, so it can legitimately
        # appear in a healthy run's output.
        if not any(m in haystack for m in markers):
            return None
        return (
            "the agent's filesystem sandbox failed to start on this runner, so "
            "no files could be read or written. This is a known Codex issue "
            "with its bubblewrap backend under AppArmor's unprivileged "
            "user-namespace restrictions (openai/codex#17337).\n"
            "The fix is to install bubblewrap and load the "
            "bwrap-userns-restrict AppArmor profile BEFORE this step — the "
            "'Prepare Linux sandbox for the agent' step in action.yml does "
            "exactly that, and probes it with "
            "`bwrap --unshare-all --dev-bind / / /bin/true`. If you are "
            "running apply_fix.py directly rather than through the action, "
            "that preparation has not happened; run it, or use a runner image "
            "where bubblewrap can create a user namespace.\n"
            "Note: switching to the Landlock backend is NOT the answer — it "
            "also fails on GitHub runners, and openai/codex#18800 indicates "
            "the flag is being removed. This action does not fall back to "
            "running unsandboxed."
        )

    def _parse(self, proc):
        usage = AgentUsage()
        final_message = ""
        failed_reason = ""
        saw_completed = False
        model = self.model

        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            etype = event.get("type", "")

            if etype == "turn.completed":
                saw_completed = True
                u = event.get("usage") or {}
                usage.input_tokens = u.get("input_tokens")
                usage.cached_input_tokens = u.get("cached_input_tokens")
                usage.output_tokens = u.get("output_tokens")
                usage.reasoning_output_tokens = u.get("reasoning_output_tokens")
            elif etype == "turn.failed":
                err = event.get("error") or {}
                failed_reason = err.get("message") or json.dumps(err)[:300]
            elif etype == "thread.started":
                model = event.get("model") or model
            elif etype == "item.completed":
                item = event.get("item") or {}
                if item.get("type") == "agent_message":
                    final_message = item.get("text") or final_message

        ok = proc.returncode == 0 and saw_completed and not failed_reason
        error = failed_reason
        if not ok and not error:
            error = (proc.stderr or "").strip()[-1000:] or f"exit code {proc.returncode}"

        # A broken sandbox makes Codex exit 0 having done nothing: it reports
        # the failure in prose and completes the turn "successfully". Left
        # alone that surfaces as outcome=no_usages_found, which is
        # indistinguishable from "this repo doesn't use the changed API" —
        # the worst possible confusion, because it says all-clear when the
        # migration never ran. Detect it and fail explicitly.
        sandbox_error = self._detect_sandbox_failure(final_message, proc.stderr)
        if ok and sandbox_error:
            ok = False
            error = sandbox_error

        # Echo what the agent said into the client's own workflow log.
        # Without this, a run that completes cleanly but edits nothing is
        # undiagnosable: the summary explaining WHY would otherwise only
        # reach fix_report.md, which dies with the runner when no PR opens.
        if final_message:
            print(f"[agent:{self.name}] agent summary:")
            for line in final_message.strip().splitlines():
                print(f"  | {line}")
        else:
            print(f"[agent:{self.name}] agent returned no summary text.")

        # On failure, stderr usually holds the actual reason (a rejected flag,
        # an auth problem, a sandbox denial). Print it rather than only the
        # truncated copy that reaches the report.
        if not ok and proc.stderr:
            print(f"[agent:{self.name}] stderr:")
            for line in proc.stderr.strip().splitlines()[-25:]:
                print(f"  | {line}")

        return AgentResult(
            backend=self.name, ok=ok, usage=usage,
            final_message=final_message, error=error,
            model=model, cli_version=self._cli_version(),
        )


class ClaudeCodeBackend(AgentBackend):
    """Anthropic Claude Code CLI.

    Invocation:
        claude -p "<prompt>" --output-format json --permission-mode acceptEdits

    `acceptEdits` auto-approves file reads/writes/edits in the working
    directory without interactive prompts, while still requiring approval
    for arbitrary shell commands — the closest equivalent to Codex's
    workspace-write sandbox, and chosen for the same reason.

    stdout is a single JSON object (not a stream), with usage nested under
    `usage` and a client-side cost estimate at `total_cost_usd`.
    """

    name = "claude-code"
    env_key = "ANTHROPIC_API_KEY"
    package = CLAUDE_PACKAGE
    version = CLAUDE_VERSION
    binary = "claude"

    def run(self, prompt, workdir):
        cmd = [
            self.binary,
            "-p", prompt,
            "--output-format", "json",
            "--permission-mode", "acceptEdits",
        ]
        if self.model:
            cmd += ["--model", self.model]

        env = dict(os.environ)  # ANTHROPIC_API_KEY already read by the CLI

        try:
            proc = subprocess.run(
                cmd, cwd=workdir, env=env,
                capture_output=True, text=True, timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            return AgentResult(
                backend=self.name, ok=False,
                error=f"agent timed out after {self.timeout}s",
                cli_version=self._cli_version(),
            )

        return self._parse(proc)

    def _parse(self, proc):
        usage = AgentUsage()
        payload = None

        # Normally the whole of stdout is one JSON object, but tolerate
        # leading noise by scanning for the last parseable line too.
        raw = proc.stdout.strip()
        if raw:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                for line in reversed(raw.splitlines()):
                    line = line.strip()
                    if line.startswith("{"):
                        try:
                            payload = json.loads(line)
                            break
                        except json.JSONDecodeError:
                            continue

        if payload is None:
            err = (proc.stderr or "").strip()[-1000:] or "could not parse agent output"
            return AgentResult(
                backend=self.name, ok=False, error=err,
                cli_version=self._cli_version(),
            )

        u = payload.get("usage") or {}
        usage.input_tokens = u.get("input_tokens")
        usage.cached_input_tokens = u.get("cache_read_input_tokens")
        usage.output_tokens = u.get("output_tokens")
        usage.cost_usd = payload.get("total_cost_usd")

        model = self.model
        model_usage = payload.get("modelUsage") or {}
        if model_usage:
            model = next(iter(model_usage), model)

        is_error = payload.get("is_error", False)
        subtype = payload.get("subtype", "")
        ok = proc.returncode == 0 and not is_error and subtype != "error"

        final_message = payload.get("result") or ""

        # Same reasoning as the Codex adapter: surface what the agent said
        # into the caller's own workflow log, so a run that completes but
        # edits nothing is diagnosable without opening a PR to read the report.
        if final_message:
            print(f"[agent:{self.name}] agent summary:")
            for line in final_message.strip().splitlines():
                print(f"  | {line}")
        if not ok and proc.stderr:
            print(f"[agent:{self.name}] stderr:")
            for line in proc.stderr.strip().splitlines()[-25:]:
                print(f"  | {line}")

        return AgentResult(
            backend=self.name, ok=ok, usage=usage,
            final_message=final_message,
            error="" if ok else (final_message or subtype or "agent reported an error"),
            model=model, cli_version=self._cli_version(),
        )


BACKENDS = {
    CodexBackend.name: CodexBackend,
    ClaudeCodeBackend.name: ClaudeCodeBackend,
}


def select_backend(preference=None, timeout=DEFAULT_TIMEOUT_SECONDS, model=None):
    """Pick a backend from the client's configured keys.

    Selection order:
      1. An explicit `agent-backend` input, if that backend's key is present.
      2. Whichever single key is configured.
      3. Both keys present and no preference -> Codex, since the existing
         onboarding docs and workflow are written around OPENAI_API_KEY.

    Returns None when no key is configured at all; the caller falls back to
    the legacy deterministic path rather than failing the client's build.
    """
    if preference:
        cls = BACKENDS.get(preference)
        if cls is None:
            print(f"[agent] unknown backend '{preference}'; "
                  f"expected one of {sorted(BACKENDS)}")
            return None
        if not cls.is_configured():
            print(f"[agent] backend '{preference}' requested but {cls.env_key} is not set")
            return None
        return cls(timeout=timeout, model=model)

    configured = [cls for cls in BACKENDS.values() if cls.is_configured()]
    if not configured:
        return None
    if len(configured) > 1:
        print("[agent] both OPENAI_API_KEY and ANTHROPIC_API_KEY are set; "
              "defaulting to codex. To run the other one, either unset the "
              "key you do not want or pass the 'agent-backend' input.")
        return CodexBackend(timeout=timeout, model=model)
    return configured[0](timeout=timeout, model=model)
