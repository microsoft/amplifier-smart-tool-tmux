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

The engine talks to a model provider through that provider's Python SDK. To keep
the base install lean, provider SDKs are **optional extras**, not core
dependencies. Install the extra that matches the provider you intend to use:

```
uv add "tmux-fleet[anthropic]"      # Anthropic
uv add "tmux-fleet[openai]"         # OpenAI
```

or, into an existing environment:

```
pip install "tmux-fleet[anthropic]"
```

Without the matching extra, a smart verb refuses with a message naming the exact
extra to install (`tmux-fleet[<provider>]`).

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

Run `tmux-fleet --help` — the model-backed verbs are marked there — then:

```
tmux-fleet triage
```

## Refusal taxonomy

A smart verb invoked without a usable substrate names exactly which precondition
is missing:

| Precondition missing | What to do |
| --- | --- |
| The engine dependency (`amplifier_agent_lib`) is not importable | Reinstall the tool so `amplifier-agent` is present (`uv sync`). |
| The selected provider's SDK is not installed | Install the matching extra, e.g. `pip install "tmux-fleet[anthropic]"`. |
| No provider is configured (no credentials resolve, none pinned) | Export a provider key (e.g. `ANTHROPIC_API_KEY`) or pin `TMUX_FLEET_PROVIDER`. |
| A pinned provider has no credentials in the environment | Export that provider's key, or unset `TMUX_FLEET_PROVIDER`. |
