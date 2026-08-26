# VISION

The desired end state this repo converges toward, written as though already true.
Never edited to record what shipped — status lives in the issue queue. Amendments
carry evidence and land in the dated changelog below. Governance: amend here
first → file work items against the amendment → execute.

## What this is

A **smart tool** for tmux fleets: one library, one thin `tmux-fleet` CLI, that
tells you what is happening across every tmux session on a machine — which are
parked at a prompt, which finished and how, which need a human — and, only under
explicit per-invocation confirmation, types into one or starts one. Its
deterministic paths run with nothing configured. Its smart paths bring judgment:
triage across a whole fleet, interpretation of what a session's output means.

This repo steers against the governing spec at
[amplifier-smart-tools-spec](https://github.com/DavidKoleczek/amplifier-smart-tools-spec)
and aims to be its first reference implementation. Where the spec is silent we
choose and record; where our experience contradicts it, we feed evidence
upstream rather than fork the shape.

## Principles

### 1. Library first, wrapper thin

Every capability lives in the library; the CLI is an adapter. No capability
exists only in a wrapper. Any agent that can run a shell command can use the
whole tool; any Python host can import it and skip the subprocess.

### 2. Mechanism below, judgment here

tmux mechanism — sockets, spawning, capture, input — belongs to
[tmux-kit](https://github.com/bkrabach/tmux-kit), consumed as a pinned PyPI
dependency, never vendored. This tool owns what tmux-kit correctly refuses to:
harness identification, fleet triage, "did it succeed" interpretation —
application judgment, not tmux mechanism. Improvements to mechanism flow
upstream as PRs.

### 3. amplifier-agent is the AI substrate — contractually

Model-backed capabilities execute through **amplifier-agent** (isolated
subprocess turns via its maintained SDK). The tool never carries provider
credentials, never links a provider SDK, never re-implements an agent loop.
Configure amplifier-agent once; every smart tool on the machine shares it.
Deterministic paths load and run with amplifier-agent entirely absent.

### 4. The smart path never lies about itself

A smart verb with no agent configured fails saying exactly that — it never
silently degrades to a deterministic answer. `--help` names which capabilities
are model-backed. Context fed to smart paths is assembled mechanically by code,
never by an agent's own summarizing. Results are structured; partial results
are failures unless the capability documents otherwise.

### 5. The fleet is someone's live work

Reads are safe by construction. The write verbs (`send`, `create`) refuse
without an explicit per-invocation confirmation; every attempt — refused or
delivered — lands in an append-only audit log. There is no verb that kills or
renames a session, and there will not be one. Every tmux invocation names its
socket explicitly; ambient `$TMUX`/`TMUX_TMPDIR` are ignored and reported, never
inherited.

### 6. Failures name the remedy

The caller is usually an agent. A failure that names what went wrong and how to
fix it is a working feature; an empty result or bare stack trace is a defect.

## What this repo deliberately resists

- **Provider credentials or SDKs in-tool** — that is amplifier-agent's job.
- **Kill/rename/undo verbs** — this tool observes a fleet it did not create.
- **Vendoring tmux-kit** — mechanism improvements go upstream.
- **Prose answers from smart verbs** — structured output or it did not happen.
- **Capability that exists only in the CLI** — the library is the product.

## Changelog

- **2026-08-26** — Initial vision. Encodes the negotiated decisions: triage +
  interpret (+ harness classification) as the smart capabilities;
  amplifier-agent as the contractually required AI substrate; `tmux-fleet` as
  the tool name; first-reference-implementation posture toward the upstream
  spec, including contributing its first conformance kit.
