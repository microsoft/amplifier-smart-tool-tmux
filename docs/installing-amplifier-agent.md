# Configuring a provider for the model-backed verbs

The model-backed verbs — `triage` and `interpret` — execute through
**amplifier-agent's engine, embedded in this tool's own process**. There is no
separate binary to install and nothing to put on your `PATH`: the engine ships
as a regular dependency of `tmux-fleet` and is imported in-process only when a
smart verb actually runs.

Every deterministic verb (`socket`, `sessions`, `attention`, `read`, `send`,
`create`, `doctor`, `exit-code`) works with none of the below. The smart verbs
need two things, and when either is missing they fail loudly naming exactly
which — they never silently degrade to a deterministic guess.

## 1. Install a provider SDK (an extra)

The pinned Agent v0.22.0 Python binding uses the public `amplifier_agent` API
and installs several provider SDKs transitively (including Anthropic and OpenAI).
The existing extra aliases remain supported:

```
uv add "tmux-fleet[anthropic]"      # Anthropic
uv add "tmux-fleet[openai]"         # OpenAI
```

or, into an existing environment:

```
pip install "tmux-fleet[anthropic]"
```

Public `list_providers()` discovery reports registered provider IDs, SDK
availability and advisory credential presence. An unknown pin refuses with the
registered IDs; an unavailable SDK refuses with an install hint before model or
credential checks.

## 2. Configure the provider connection outside the tool

**This tool never stores provider credentials.** Agent resolves the connection
from the caller's environment or the provider's existing login. For key-based
providers, export the key before running a smart verb:

```
export ANTHROPIC_API_KEY=sk-ant-...     # Anthropic
export OPENAI_API_KEY=sk-...            # OpenAI
export GOOGLE_API_KEY=...               # Gemini
```

For automatic selection, tmux-fleet uses public discovery's credential presence
with the preference Anthropic, OpenAI, Gemini, then Azure OpenAI, followed by
other eligible records in discovery order. To pin
one explicitly (for example when several are configured), set
`TMUX_FLEET_PROVIDER`:

```
export TMUX_FLEET_PROVIDER=anthropic
```

A registered, installed pin proceeds to real public Agent construction even
when discovery reports credentials missing: OAuth or Azure identity construction
can know more than that advisory record. Construction refusals preserve Agent's
public code, category, message and remedy (including provider-specific key or
login guidance). There is no silent fallback to another provider.

## Try it

For Anthropic, the upstream configured/default pair remains available without
`TMUX_FLEET_MODEL`. Agent v0.22.0 documents Anthropic and `claude-sonnet-5-5` as
its defaults in [configuration](https://github.com/microsoft/amplifier-agent/blob/v0.22.0/docs/configuration.md).
For every non-default provider, `TMUX_FLEET_MODEL` is required and must name a
model compatible with that provider. Agent does not automatically match its
ambient/default model to a changed provider, so the tool refuses rather than
guessing. This is a migration requirement:

```sh
export TMUX_FLEET_PROVIDER=openai
export OPENAI_API_KEY='your-key'
export TMUX_FLEET_MODEL=gpt-5  # use an exact model available to your account
export TMUX_FLEET_REASONING_EFFORT=low
tmux-fleet triage
```

An explicit `TMUX_FLEET_MODEL` overrides `AMPLIFIER_AGENT_MODEL` and the Agent
config/default model and pins both model and the tool-selected provider.
Without it, Agent resolves the ambient provider/model pair intact; tmux-fleet
does not read private configuration or reimplement its precedence.
The public ephemeral `session.info` must match the tool-selected provider and,
when explicit, the tool model. A mismatch refuses before any turn, naming the
observed pair and requested selection with a `TMUX_FLEET_MODEL` remedy.
If construction refuses before session info is available, the tool reports the
actual public construction error, not a purported observed mismatch; without a
tool model it also names the conditional explicit-model escape hatch.
The tool does not maintain a provider/model compatibility table. Set
`TMUX_FLEET_REASONING_EFFORT` to pin effort; when absent Agent's configured effort
remains in effect. Local
credential-free providers must be explicitly pinned or have their endpoint
configured (`OLLAMA_HOST` or `VLLM_BASE_URL`); discovery alone is not evidence
that a local server is running.

Successful construction and matching session info establish selection only:
they do **not** prove authentication, endpoint reachability, model compatibility,
deployment availability or account access to that model. No inference is
performed during this preflight.

Each invocation creates separate temporary working and session directories and
an ephemeral session. Tools, skills and MCP servers are empty and approvals
deny: judgment consumes only the context assembled by this tool. On timeout,
cancellation is requested and the terminal result drained before close and
directory cleanup. Session and agent close are both attempted; directories are
removed only after both succeed. Failed or cancelled turns are never replayed
automatically. A startup refusal also closes any constructed session and agent.
Failed startup cleanup retains the directories, reports the cleanup failure on
stderr and in exception notes, and preserves the original refusal or
process-control exception.

Run `tmux-fleet --help` — the model-backed verbs are marked there — then:

```
tmux-fleet triage
```

## Refusal taxonomy

A smart verb invoked without a usable substrate names exactly which precondition
is missing:

| Precondition missing | What to do |
| --- | --- |
| The public binding (`amplifier_agent`) is not importable | Reinstall the tool so `amplifier-agent` is present (`uv sync`). |
| The selected provider's SDK is not installed | Install the matching extra, e.g. `pip install "tmux-fleet[anthropic]"`. |
| No provider is eligible for automatic selection | Configure a provider connection, or pin a registered `TMUX_FLEET_PROVIDER` and set `TMUX_FLEET_MODEL`. |
| A pin is not registered | Use a provider ID from the public discovery listing named in the refusal. |
| Public construction rejects the provider connection | Follow the public Agent remedy; discovery credential presence is advisory, not authentication. |
| The public session pair differs from the requested selection | Set `TMUX_FLEET_MODEL` to a compatible model for the requested provider; no turn was submitted. |
