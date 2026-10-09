# Configuring model-backed verbs

`triage` and `interpret` use the public `amplifier_agent` Python binding in the
tool's own process. The binding and its engine/provider dependencies install with
`tmux-fleet`; deterministic verbs do not import or initialize them. The
`anthropic` and `openai` extras remain compatible installation aliases.
The binding is pinned to stable Agent **v0.22.0** (`packages/python`); the
development lock records its complete tested dependency graph.

Choose credentials, provider and model explicitly, for example:

```sh
export ANTHROPIC_API_KEY=...
export TMUX_FLEET_PROVIDER=anthropic
export TMUX_FLEET_MODEL=<an-exact-model-your-account-can-use>
```

`TMUX_FLEET_PROVIDER` is optional: public provider discovery selects an installed,
credential-configured Anthropic, OpenAI, Gemini or Azure OpenAI first, then another
eligible provider. Credential-free local providers require an explicitly configured
endpoint for automatic selection. Discovery is advisory, not proof of authentication.
An unknown pin names registered IDs; a missing SDK refuses before model/credential
checks. A known installed pin reaches public construction even when discovery says
credentials are missing, allowing provider-owned OAuth or Azure identity validation.
There is no fallback after a refusal.

`TMUX_FLEET_MODEL` (or `AMPLIFIER_AGENT_MODEL`) is required for **every** provider,
including Anthropic. The tool variable retains precedence over the ambient model.
Both the selected provider and model are passed explicitly and checked against
public `Session.info` before any turn. The Agent's reasoning-effort configuration
is inherited; no tool-specific effort override is introduced.

API keys come from the provider's environment variables, including
`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`/`GEMINI_API_KEY`, and
`AZURE_OPENAI_API_KEY`. Copilot accepts its native token variables. For ChatGPT,
the public Agent uses its provider-owned `~/.amplifier/openai-chatgpt-oauth.json`
cache. This tool does not log in, migrate credentials or store its own copy.
Agent 0.22 rejects legacy `AMPLIFIER_AGENT_HOME`; that named refusal is preserved.
An old Agent credential-home override is not silently ignored or rewritten.

Every smart invocation gets a fresh ephemeral session with separate working and
session directories under a tool-owned temporary root. The compatible library
`cwd` argument grants no access to the caller's directory. There are no tools,
skills or MCP servers, and approvals are denied.

Timeout and caller cancellation request cancellation through the public Turn
handle and drain its terminal result before cleanup; repeated cancellation cannot
orphan that drain. Successful timeout cleanup remains automatic. Session cleanup
is attempted before owner cleanup, and the owner is attempted even if the Session
close fails. The temporary root is removed only after both closes succeed;
successful repeated close is a no-op. A failed close retains the root and owner
reference. Startup refusals and process-control exceptions keep their original
meaning even if cleanup fails; diagnostics name retained paths on stderr and in
exception notes. A failed or timed-out turn is never automatically retried.
New invocations are new model work and can spend tokens.

## Refusals

| Missing precondition | Remedy |
| --- | --- |
| Public binding `amplifier_agent` | Reinstall the tool and its declared dependencies. |
| Provider SDK | Repair the declared Agent dependencies or install the corresponding compatible extra. |
| Unknown provider pin | Choose one of the registered provider IDs named in the refusal. |
| Provider credentials | Configure the chosen provider's credentials. |
| Exact model | Set `TMUX_FLEET_MODEL` or `AMPLIFIER_AGENT_MODEL`. |
| Agent configuration compatibility | Follow the public Agent's named remedy; no host settings are changed automatically. |

Public Agent errors retain their code, category, message, remedy and cause. This
tool does not maintain a separate credential-variable table or guess an
Anthropic-key remedy for a different provider's refusal.

`tmux-fleet --help` describes all verbs. Only `triage` and `interpret` use a model;
all other verbs retain their existing confirmation and audit requirements.
