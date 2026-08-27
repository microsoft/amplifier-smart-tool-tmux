# Upstream offer: a machine-checkable conformance kit for the smart-tools spec

> **Status: PREPARED, NOT SENT.** This is drafted issue/PR text for
> [`DavidKoleczek/amplifier-smart-tools-spec`](https://github.com/DavidKoleczek/amplifier-smart-tools-spec).
> Opening the issue/PR is an owner decision and is deliberately **not** done here.
> Nothing in this file pushes, opens, or notifies anything.

---

## Title

Add the first conformance kit: turn the spec's must/must-not prose into executable checks

## Summary

The spec today is prose. There is no way for a tool author -- or a future
registry -- to *mechanically* answer "is this a conforming smart tool?" This
offer contributes a small, dependency-free kit that operationalizes the
normative sentences in `structure.md`, `manifest.md`, and `invocation.md` into
13 executable checks, with fixtures that prove each check discriminates a
conforming tool from a violating one.

It is generic over any smart tool (it ships its own fixtures and knows nothing
about any particular tool), stdlib-only, and honest about what it cannot
evaluate: a rule it cannot check is reported `SKIP` with a reason, never a
fabricated `PASS`.

## Why this shape

- **stdlib only, no network.** `run.py` runs under any Python 3.11+ with no
  install step. A conformance gate that itself needs a dependency tree is a
  weaker gate.
- **JSON verdict on stdout, human summary on stderr, exit 0/1.** The same
  "structured result a caller can act on" the spec asks of smart tools
  (`invocation.md` -- "a result a caller can act on without parsing prose").
- **Honest SKIP.** Determinism and provider concerns are the caller's; a kit
  that guesses PASS when it could not actually observe the behavior would be
  making the same mistake the spec warns tools against ("a capability never
  silently returns the portion that worked").

## Rule -> spec-sentence mapping

Every check names the sentence it operationalizes. `M` = manifest (static file
inspection, language-agnostic). `R` = runtime (invokes the tool; SKIPs honestly
when no invocation recipe is discoverable).

| # | Rule id | Spec source | Sentence operationalized |
|---|---------|-------------|--------------------------|
| M1 | `manifest-present` | `manifest.md` | "`SMART_TOOL.md`, in the tool's own source, beside the code that reads it." |
| M2 | `manifest-frontmatter-parses` | `manifest.md` | "YAML frontmatter, then a Markdown body." |
| M3 | `manifest-fields-closed` | `manifest.md` | "Fields not listed here are not part of the manifest." |
| M4 | `manifest-required-fields` | `manifest.md` | The frontmatter fields (`smart_tool_format`, `name`, `version`, `description`, `use_cases`, `platforms`) each described as part of the manifest. |
| M5 | `manifest-name-format` | `manifest.md` | "`name` is lowercase alphanumeric and hyphens." |
| M6 | `manifest-version-matches-package` | `manifest.md` | "`version` ... matches the version in the package definition." |
| M7 | `manifest-requires-shape` | `manifest.md` | "Each entry carries `name`, `purpose`, and `install`, and may carry `optional`. `install` is a reference to documentation ... never a command." |
| M8 | `manifest-single-per-root` | `manifest.md` | "Exactly one manifest per distribution. A second `SMART_TOOL.md` beneath a distribution root is *incorrect*." |
| R1 | `loads-without-provider` | `structure.md` | "the straight code paths run with no model provider configured ... the tool must not refuse to load without them." |
| R2 | `help-discloses-model-backed` | `invocation.md` | "`--help` ... which of them are model-backed." |
| R3 | `deterministic-capability-runs` | `structure.md` | "A caller that only wants the deterministic capabilities never has to supply model credentials." |
| R4 | `failure-names-remedy` | `invocation.md` / `README.md` | "Failures are loud and they name the remedy ... a bare stack trace has handed back a problem its caller cannot resolve." |
| R5 | `no-hang-stdin-closed` | `README.md` design principle 3 (non-interactive agent caller) | A run with stdin closed completes; it does not block for interactive input. |

## Operationalizations worth reviewing

A few checks turn prose into a concrete, checkable convention. These are the
places where the spec is intentionally loose and the kit makes a choice; they
are the parts most worth the maintainer's eye, and are candidates for the spec
to bless (or adjust) explicitly:

1. **R2, "which capabilities are model-backed".** The spec mandates the
   disclosure but not its wording. The kit requires a case-insensitive
   `model-backed` marker in `--help` output. If the spec wants a stronger machine
   contract here (e.g. a `--help --json` capability listing with a
   `model_backed: true` field), the kit can target that instead.

2. **R4, "structured error".** The kit requires a bad invocation to exit non-zero
   and emit a JSON document on stdout, and to *not* print a Python `Traceback`.
   The spec says "structured" and "names the remedy" without fixing a schema; a
   blessed error-envelope shape (e.g. `{"error": {"code", "message", "remedy"}}`)
   would let the kit check the remedy field directly.

3. **M6, version match.** Checked against `pyproject.toml [project].version` and
   `package.json version`. A statically-unresolvable (dynamic) version is a
   `SKIP`, not a `FAIL`.

4. **R5, no-hang.** Not stated verbatim in the current spec text; derived from
   "the caller is usually an agent" (non-interactive). Offered as a candidate
   normative sentence for `invocation.md`.

## Runtime-check invocation model

The runtime rules need to run the tool. The kit discovers how in a
language-agnostic way: an optional `smart-tool-conformance.json` hint
(`cli_argv` / `deterministic_smoke` / `bad_invocation`) at the distribution
root, or, for Python tools, a `[project.scripts]` entry driven through `uv run`.
With neither present the runtime rules SKIP. If the spec later standardizes a
self-description surface, that becomes the natural discovery mechanism and the
hint can retire.

## What's in the kit

```
conformance/
  run.py                     # the kit -- stdlib only, uv-runnable
  README.md                  # how to run it against any smart tool
  UPSTREAM-OFFER.md          # this file
  fixtures/
    sample-good/             # a minimal conforming smart tool
    sample-bad-*/            # one defect each; every rule has a negative fixture
  tests/                     # parser unit tests + end-to-end discrimination
```

Evidence of discrimination: `sample-good` passes all 13 rules; each
`sample-bad-*` fixture fails with its rule named. `uv run --with pytest pytest -q`
is green.

## Offer

Happy to open this as a PR against a `conformance/` directory in the spec repo,
or as an issue for discussion first -- whichever the maintainer prefers. It is
intentionally small and additive; it changes none of the existing spec text and
can be adopted incrementally (the manifest rules alone are useful on day one).
