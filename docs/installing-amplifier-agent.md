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

Public `list_providers()` discovery checks SDK availability and credentials
before constructing an agent. A missing SDK still refuses with an install hint.

## 2. Provide credentials in the environment

**This tool never stores provider credentials.** They arrive from your
environment at runtime, exactly like any other credential your shell already
holds. Export the key for your provider before running a smart verb:

```
export ANTHROPIC_API_KEY=sk-ant-...     # Anthropic
export OPENAI_API_KEY=sk-...            # OpenAI
export GOOGLE_API_KEY=...               # Gemini
```

The engine auto-selects a provider from whichever credentials resolve. To pin
one explicitly (for example when several are configured), set
`TMUX_FLEET_PROVIDER`:

```
export TMUX_FLEET_PROVIDER=anthropic
```

If a pinned provider has no resolvable credentials, the smart verbs fail loudly
naming that provider and the environment variable to set — never a silent
fallback.

## Try it

For Anthropic, the upstream configured/default model remains available without
`TMUX_FLEET_MODEL`. Agent v0.22.0 documents Anthropic and `claude-sonnet-5-5` as
its defaults in [configuration](https://github.com/microsoft/amplifier-agent/blob/v0.22.0/docs/configuration.md).
For every non-default provider, `TMUX_FLEET_MODEL` is required and must name a
model compatible with that provider. Agent does not automatically match its
ambient/default model to a changed provider, so the tool refuses rather than
guessing or silently running Anthropic. This is a migration requirement:

```sh
export TMUX_FLEET_PROVIDER=openai
export OPENAI_API_KEY='your-key'
export TMUX_FLEET_MODEL=gpt-5  # use an exact model available to your account
export TMUX_FLEET_REASONING_EFFORT=low
tmux-fleet triage
```

An explicit `TMUX_FLEET_MODEL` overrides `AMPLIFIER_AGENT_MODEL` and the Agent
config/default model; the tool does not maintain a provider/model table. Set
`TMUX_FLEET_REASONING_EFFORT` to pin effort; when absent Agent's configured effort
remains in effect. Local
credential-free providers must be explicitly pinned or have their endpoint
configured (`OLLAMA_HOST` or `VLLM_BASE_URL`); discovery alone is not evidence
that a local server is running.

Each invocation creates separate temporary working and session directories and
an ephemeral session. Tools, skills and MCP servers are empty and approvals
deny: judgment consumes only the context assembled by this tool. On timeout,
cancellation is requested and the terminal result drained before close and
directory cleanup. Failed or cancelled turns are never replayed automatically.

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
| No provider is configured (no credentials resolve, none pinned) | Export a provider key (e.g. `ANTHROPIC_API_KEY`) or pin `TMUX_FLEET_PROVIDER`. |
| A pinned provider has no credentials in the environment | Export that provider's key, or unset `TMUX_FLEET_PROVIDER`. |
