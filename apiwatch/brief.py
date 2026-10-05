"""Produce a fix brief: everything a coding agent (or a human) needs to open a correct PR.

v0.1 writes the brief as Markdown. With the 'agent' extra and ANTHROPIC_API_KEY set,
`draft_patch` asks a model for a proposed diff — always a proposal, never applied.
"""
from __future__ import annotations

import os

from .models import CallSite, ChangeRecord, DriftEvent


def brief_for_record(rec: ChangeRecord, sites: list[CallSite]) -> str:
    lines = [
        f"# Fix brief: {rec.vendor} — {rec.summary}",
        "",
        f"- Surface: `{rec.surface}` ({rec.kind})",
        f"- Severity: **{rec.severity}**",
        f"- Effective: {rec.effective}",
        f"- Source: {rec.source_url or 'n/a'}  ·  verified: {('yes, by ' + rec.verified_by) if rec.human_verified else (f'reviewed against source {rec.verified_on} (AI-assisted)' if rec.verified else 'NO, unreviewed draft — confirm before acting')}",
        "",
        "## What changes",
        "",
        rec.detail or "(no detail recorded)",
        "",
        "## Where it lands in this repo",
        "",
    ]
    if not sites:
        lines.append("_No call sites matched. Either you're unaffected or the signatures need widening._")
    code = [s for s in sites if not s.in_test and s.tier == "use"]
    listed = [s for s in sites if not s.in_test and s.tier != "use"]
    tests = [s for s in sites if s.in_test]
    for group, heading in ((code, None),
                           (listed, f"Also listed ({len(listed)}): entries in model tables, enums or option lists. "
                                    "Update them so nothing offers the old value:"),
                           (tests, f"In tests ({len(tests)}); fix these after the code above:")):
        if not group:
            continue
        if heading:
            lines += ["", heading, ""]
        for s in group:
            if s.via:
                lines.append(f"- `{s.file}:{s.line}` uses `{s.via}`, which holds `{s.signature}` — `{s.snippet}`")
            else:
                where = f" ({s.cell})" if s.cell else ""
                lines.append(f"- `{s.file}:{s.line}`{where} matched `{s.signature}` — `{s.snippet}`")
    lines += ["", "## Suggested fix", "", rec.fix_hint or "(no hint recorded)", "",
              "## Acceptance", "",
              "- A test that exercises the changed surface and passes against the new behavior",
              "- The old behavior is not silently assumed anywhere else (search the signatures above)",
              "- PR description links the vendor source"]
    return "\n".join(lines)


def brief_for_drift(ev: DriftEvent, sites: list[CallSite]) -> str:
    lines = [
        f"# Drift brief: {ev.endpoint_id} — {ev.change} at `{ev.path}`",
        "",
        f"- Before: `{ev.before}`  ·  After: `{ev.after}`  ·  Severity: **{ev.severity}**",
        "",
        "## Where it lands in this repo",
        "",
    ]
    if not sites:
        lines.append("_No call sites matched the leaf name. Check for aliasing or destructuring._")
    for s in sites:
        lines.append(f"- `{s.file}:{s.line}` — `{s.snippet}`")
    lines += ["", "## Acceptance", "",
              "- Handle the new shape (nullable / renamed / removed) explicitly; no silent defaults",
              "- Add a fixture reflecting the new response and a test against it"]
    return "\n".join(lines)


PATCH_MODEL = "claude-opus-5"
PATCH_MODEL_ENV = "APIWATCH_PATCH_MODEL"          # override the model without a code change
FALLBACK_MODELS = {"claude-opus-5", "claude-fable-5-1"}   # models that take server-side refusal fallbacks


def draft_patch(brief: str, repo_context: str, client=None) -> str | None:
    """Optional: ask Claude for a proposed patch (a unified diff). Proposal only; never applied.

    Opt-in: runs only with the 'agent' extra installed and ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN set
    (or an explicit client). Returns None when not configured, when the model declines, or when the
    output was cut off — a truncated diff is worse than no diff.
    """
    if client is None:
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            return None
        try:
            import anthropic  # optional dependency
        except ImportError:
            return None
        client = anthropic.Anthropic()
    model = os.environ.get(PATCH_MODEL_ENV) or PATCH_MODEL
    extra = {}
    if model in FALLBACK_MODELS:
        # on a policy decline, the API re-runs the request on a fallback model within the same call
        extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    with client.beta.messages.stream(
        model=model,
        max_tokens=64000,
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
        system="You propose code changes as a unified diff against the files provided. Output the diff only, no prose.",
        messages=[{"role": "user", "content": f"BRIEF:\n{brief}\n\nRELEVANT FILES:\n{repo_context}"}],
        **extra,
    ) as stream:
        msg = stream.get_final_message()
    if msg.stop_reason != "end_turn":
        return None   # refusal (whole fallback chain declined) or max_tokens: no partial diffs
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text") or None
