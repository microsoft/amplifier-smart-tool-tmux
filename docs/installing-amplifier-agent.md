# Configuring model-backed verbs

`triage` and `interpret` use the public `amplifier_agent` Python binding in the
tool's own process. The binding and its engine/provider dependencies install with
`tmux-fleet`; deterministic verbs do not import or initialize them. The
`anthropic` and `openai` extras remain compatible installation aliases.

Choose credentials, provider and model explicitly, for example:

```sh
export ANTHROPIC_API_KEY=...
export TMUX_FLEET_PROVIDER=anthropic
export TMUX_FLEET_MODEL=<an-exact-model-your-account-can-use>
```

`TMUX_FLEET_PROVIDER` is optional: otherwise credential presence selects Anthropic,
OpenAI, Gemini or Azure OpenAI first, then another configured provider. Presence
is only a selection hint; the Agent validates access. A pinned unavailable provider
refuses without fallback. `TMUX_FLEET_MODEL` (or `AMPLIFIER_AGENT_MODEL`) is required;
a provider choice never substitutes a model from another provider.

API keys come from the provider's environment variables, including
`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`/`GEMINI_API_KEY`, and
`AZURE_OPENAI_API_KEY`. Copilot accepts its native token variables. For ChatGPT,
the public Agent uses its provider-owned `~/.amplifier/openai-chatgpt-oauth.json`
cache. This tool does not log in, migrate credentials or store its own copy.
Agent 0.20 rejects legacy `AMPLIFIER_AGENT_HOME`; that named refusal is preserved.
An old Agent credential-home override is not silently ignored or rewritten.

Every smart invocation gets a fresh ephemeral session and temporary storage,
with no tools, skills or MCP servers and approvals denied. The timeout cancels
and joins the owned turn. A failed or timed-out turn is never automatically
retried. New invocations are new model work and can spend tokens.

## Refusals

| Missing precondition | Remedy |
| --- | --- |
| Public binding `amplifier_agent` | Reinstall the tool and its declared dependencies. |
| Provider SDK | Repair the declared Agent dependencies or install the corresponding compatible extra. |
| Provider credentials | Configure the chosen provider's credentials. |
| Exact model | Set `TMUX_FLEET_MODEL` or `AMPLIFIER_AGENT_MODEL`. |
| Agent configuration compatibility | Follow the public Agent's named remedy; no host settings are changed automatically. |

`tmux-fleet --help` describes all verbs. Only `triage` and `interpret` use a model;
all other verbs retain their existing confirmation and audit requirements.
