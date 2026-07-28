#!/usr/bin/env python3
"""Automated WCAG-AA contrast audit of the Direction-B design system (DS-3, #92).

Parses the *real* token values out of ``bookkeeper_ui/static/app.css`` — the light
``:root`` block and the ``:root[data-theme="dark"]`` block — and checks every
meaningful colour pairing the UI actually renders, in BOTH themes, against the
WCAG 2.x relative-luminance contrast formula.

Three categories, three bars:

* **TEXT**  — foreground text on its surface. Bar: **4.5:1** (WCAG 1.4.3, normal text).
* **STATE** — a colour that is the *sole* carrier of state/meaning: the trust-state
  card accents, the safe-signal LEDs, the focus ring. Bar: **3.0:1** (WCAG 1.4.11).
* **STRUCT** — neutral, non-state-conveying structure (hairline row rules, control
  borders identifiable by label/layout, decorative pill outlines). WCAG does not
  require 3:1 for these (they are not the only means of identifying the component),
  so they are **reported, never gated**.

Exit status is non-zero if any TEXT pairing is < 4.5 or any STATE pairing is < 3.0,
so this doubles as a regression guard if run in CI. It asserts no colour in the
pytest suite (per the design-system spec's "no test asserts a color" rule) — it is a
standalone audit whose output is attached to the PR.

Usage:  python scripts/contrast_audit.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

CSS_PATH = Path(__file__).resolve().parent.parent / "bookkeeper_ui" / "static" / "app.css"

TEXT_MIN = 4.5
STATE_MIN = 3.0


# ── colour maths (WCAG 2.x) ────────────────────────────────────────────────
def _expand_hex(h: str) -> str:
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return h


def _srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(hex_colour: str) -> float:
    h = _expand_hex(hex_colour)
    r, g, b = (int(h[i : i + 2], 16) / 255 for i in (0, 2, 4))
    r, g, b = (_srgb_to_linear(c) for c in (r, g, b))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(fg: str, bg: str) -> float:
    l1, l2 = relative_luminance(fg), relative_luminance(bg)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


# ── token parsing (straight from app.css) ──────────────────────────────────
def _extract_block(css: str, start_marker: str) -> str:
    """Return the body of the first brace-delimited block after ``start_marker``."""
    i = css.index(start_marker)
    open_brace = css.index("{", i)
    depth, j = 0, open_brace
    while j < len(css):
        if css[j] == "{":
            depth += 1
        elif css[j] == "}":
            depth -= 1
            if depth == 0:
                return css[open_brace + 1 : j]
        j += 1
    raise ValueError(f"unterminated block for {start_marker!r}")


_VAR_RE = re.compile(r"--([\w-]+)\s*:\s*(#[0-9a-fA-F]{3,8})\b")


def parse_tokens(css: str) -> tuple[dict[str, str], dict[str, str]]:
    light = dict(_VAR_RE.findall(_extract_block(css, "\n:root {")))
    dark = dict(_VAR_RE.findall(_extract_block(css, ':root[data-theme="dark"] {')))
    # dark inherits any token it does not itself override (matches the cascade).
    merged_dark = {**light, **dark}
    return light, merged_dark


# ── the pairings the UI actually renders ───────────────────────────────────
# (foreground token, background token, human label)
TEXT_PAIRS = [
    ("ink", "panel", "heading on card"),
    ("ink", "panel-2", "heading on inset strip"),
    ("ink", "bg", "heading on page"),
    ("ink-2", "panel", "body on card"),
    ("ink-2", "panel-2", "body on inset strip"),
    ("ink-2", "bg", "body on page"),
    ("muted", "panel", "secondary on card"),
    ("muted", "panel-2", "secondary on inset strip"),
    ("muted", "bg", "lead / secondary on page"),
    ("faint", "panel", "tertiary (ids/keys) on card"),
    ("faint", "panel-2", "tertiary on inset strip"),
    ("faint", "bg", "tertiary on page"),
    ("accent", "panel", "in-content link / pulse count on card"),
    ("accent", "bg", "in-content link / count on page"),
    ("accent-2", "panel", "link hover on card"),
    ("accent-2", "accent-tint", "chip text (source-owner / check-name / suggest cap)"),
    ("on-accent", "accent", "button label on accent fill"),
    ("proposed-2", "proposed-tint", "PROPOSED pill text"),
    ("confirmed-2", "confirmed-tint", "CONFIRMED pill / status text"),
    ("flagged-2", "flagged-tint", "flagged pill / floor-inert text"),
    ("danger-2", "danger-tint", "danger pill / status text"),
    ("confirmed-2", "panel", "win-state heading / met mark on card"),
    ("danger-2", "panel", "unmet mark / unmet-close on card"),
    ("flagged", "panel", "reject-button text on white fill"),
    ("ink", "confirmed-tint", "closed-banner body on tint"),
    ("ink", "danger-tint", "loud backup-banner body on tint"),
    ("ink-2", "confirmed-tint", "banner paragraph on tint"),
    ("ink-2", "danger-tint", "banner paragraph on danger tint"),
    ("ink-2", "flagged-tint", "divergence-banner body on tint"),
]

STATE_PAIRS = [
    ("accent", "panel", "focus ring / brand glyph / active nav on card"),
    ("accent", "bg", "focus ring on page"),
    ("proposed", "panel", "proposed card left-accent / state dot"),
    ("confirmed", "panel", "confirmed card left-accent / backed-up LED"),
    ("flagged", "panel", "flagged card left-accent / pending LED"),
    ("danger", "panel", "danger card left-accent / not-backed LED"),
    ("confirmed", "confirmed-tint", "backed-up LED on chip tint"),
    ("flagged", "flagged-tint", "pending LED on chip tint"),
    ("danger", "danger-tint", "not-backed LED on chip tint"),
    ("proposed", "proposed-tint", "state dot on suggest-block tint"),
]

# Reported only — neutral structure, not a sole state carrier (not gated).
STRUCT_PAIRS = [
    ("line", "panel", "hairline row rule on card"),
    ("line-2", "panel", "control / card border on card"),
    ("line", "bg", "hairline on page"),
    ("line-2", "bg", "border on page"),
    ("proposed-line", "proposed-tint", "proposed pill outline"),
    ("confirmed-line", "confirmed-tint", "confirmed pill outline"),
    ("flagged-line", "flagged-tint", "flagged pill outline"),
    ("danger-line", "danger-tint", "danger pill outline"),
]


def _row(fg, bg, label, ratio, bar, ok):
    status = "PASS" if ok else "FAIL"
    barcol = f">={bar:.1f}" if bar else "  -  "
    return f"  [{status}] {ratio:5.2f}:1  {barcol}  {fg:>14} on {bg:<14} {label}"


def audit_theme(name: str, tok: dict[str, str]) -> int:
    failures = 0
    print(f"\n=== {name} theme ===")

    def check(pairs, bar, gated):
        nonlocal failures
        for fg, bg, label in pairs:
            if fg not in tok or bg not in tok:
                print(f"  [SKIP] token missing: --{fg} / --{bg}")
                continue
            ratio = contrast_ratio(tok[fg], tok[bg])
            ok = (not gated) or ratio >= bar
            if gated and not ok:
                failures += 1
            print(_row(fg, bg, label, ratio, bar, ok))

    print("\n TEXT (bar 4.5:1)")
    check(TEXT_PAIRS, TEXT_MIN, gated=True)
    print("\n STATE — sole colour carrier of meaning (bar 3.0:1)")
    check(STATE_PAIRS, STATE_MIN, gated=True)
    print("\n STRUCT — neutral structure, reported only (not gated)")
    check(STRUCT_PAIRS, 0, gated=False)
    return failures


def main() -> int:
    css = CSS_PATH.read_text(encoding="utf-8")
    light, dark = parse_tokens(css)
    print(f"contrast audit · {CSS_PATH.relative_to(CSS_PATH.parents[2])}")
    print(f"parsed {len(light)} light tokens · {len(dark)} dark tokens (merged)")

    failures = audit_theme("LIGHT", light) + audit_theme("DARK", dark)

    print("\n" + "=" * 60)
    if failures:
        print(f"RESULT: FAIL — {failures} gated pairing(s) below their WCAG bar.")
        return 1
    print("RESULT: PASS — every gated text/state pairing clears its WCAG bar "
          "in both themes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
