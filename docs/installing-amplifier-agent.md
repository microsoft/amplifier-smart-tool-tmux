# Configuring a provider for the model-backed verbs

The model-backed verbs — `triage` and `interpret` — execute through
**amplifier-agent v0.22.0's supported public Python binding, embedded in-process**.
There is no CLI subprocess, PATH-resolved agent binary, or private-engine access.
The public `amplifier_agent` import is lazy: deterministic verbs and help never
load it or require provider configuration.

Every deterministic verb (`socket`, `sessions`, `attention`, `read`, `send`,
`create`, `doctor`, `exit-code`) works without an AI provider. Smart verbs require
a usable binding/provider install, caller-environment credential evidence, and
a matching model. Missing prerequisites refuse with a remedy, not a guess.

## Install with the tool

Use Python >=3.13 with `tmux` on PATH:

```bash
uv tool install git+https://github.com/microsoft/amplifier-smart-tool-tmux
```

The regular dependency is the released Python subproject, not the repository root:

```text
amplifier-agent @ git+https://github.com/microsoft/amplifier-agent@v0.22.0#subdirectory=packages/python
```

The SDK owns its engine and transitive provider pins; do not override them locally.
`tmux-fleet[anthropic]` and `tmux-fleet[openai]` remain **no-op compatibility aliases**.
They are not mandatory: their provider SDKs arrive transitively. A missing base
provider package normally means a broken install; reinstall the tool rather than
installing a supposed required tmux-fleet extra. Copilot has a separate optional
upstream SDK extra, as noted below.

## Caller-environment credentials and endpoints

**The tool never stores credentials or swaps process-global environment values.**
Credential eligibility is checked at the SDK boundary against the caller's
environment, not a secret store or saved login. Connection variable names below
come from the released SDK's [public provider documentation](https://github.com/microsoft/amplifier-agent/blob/v0.22.0/docs/providers.md).
That SDK also supports authentication sources this tool deliberately excludes.

Key examples (choose the provider you need; do not export all three):

```
export ANTHROPIC_API_KEY=sk-ant-...     # Anthropic
export OPENAI_API_KEY=sk-...            # OpenAI
export GOOGLE_API_KEY=...               # Gemini
```

| Provider | Caller environment evidence and requirements |
| --- | --- |
| `anthropic` | `ANTHROPIC_API_KEY`; optional `ANTHROPIC_BASE_URL` for a compatible Messages endpoint. |
| `openai` | `OPENAI_API_KEY`; optional `OPENAI_BASE_URL` for a compatible Responses endpoint. |
| `gemini` | `GOOGLE_API_KEY` or `GEMINI_API_KEY` (Google takes precedence); optional `GOOGLE_GEMINI_BASE_URL`. |
| `azure-openai` | `AZURE_OPENAI_ENDPOINT` **and** either `AZURE_OPENAI_API_KEY` or all three of `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`. The key takes precedence. Use the resource endpoint without `/openai/v1` and a deployment ID as the model. |
| `ollama` | Explicit `OLLAMA_HOST` or `OLLAMA_API_KEY` is environment evidence. The SDK considers localhost, `127.0.0.1`, and `::1` credential-free; a remote host requires `OLLAMA_API_KEY` in discovery. Its local default is `http://localhost:11434`. |
| `vllm` | Explicit `VLLM_BASE_URL` or `VLLM_API_KEY` is environment evidence; supply a key if the server requires it. Its local default is `http://localhost:8000/v1` (Responses API). |
| `chat-completions` | Explicit `CHAT_COMPLETIONS_BASE_URL` or `CHAT_COMPLETIONS_API_KEY` is environment evidence; supply a key if the OpenAI-compatible endpoint requires it. |
| `github-copilot` | First nonempty `COPILOT_AGENT_TOKEN`, `COPILOT_GITHUB_TOKEN`, `GH_TOKEN`, or `GITHUB_TOKEN`. Requires the upstream binding's optional `github-copilot` extra, not a tmux-fleet Anthropic/OpenAI extra. |

Azure's SDK can use managed identity, but **this tool does not permit it**: it is
not caller-environment-only authentication. Azure CLI and workload-identity logins
are not alternatives. Public discovery currently reports Azure credentials
`found` only for endpoint + API key; its `missing` status does not invalidate the
complete client-secret environment alternative documented above.

Saved Copilot/GitHub CLI login and ChatGPT OAuth login are **not credential
sources here**. The SDK's `openai-chatgpt` path uses a saved OAuth file, not
`OPENAI_API_KEY`; it is not an eligible environment-only selection.

For Ollama/vLLM/Chat Completions, `not_required` alone never creates an automatic
candidate. An explicit endpoint is caller-environment evidence, not proof of
reachability or authentication; a required key must still be present. Do not
infer that every remote endpoint is anonymous: Ollama discovery requires a
remote key, while vLLM/Chat Completions report `not_required` without a key and
the actual server's authentication requirements still apply.

## Select a provider and model

1. `TMUX_FLEET_PROVIDER` and `AMPLIFIER_AGENT_PROVIDER` are explicit pins.
   If both are set differently, refuse; matching pins are allowed.
2. With neither pin, tmux-fleet (not the SDK default resolver) prefers eligible
   caller-environment evidence in this order: `anthropic`, `openai`, `gemini`,
   `azure-openai`, then other eligible public discovery records in SDK order.
   A selected provider's broken install never silently falls through.
3. Use `AMPLIFIER_AGENT_MODEL` **exactly when set**. Absent it, only Anthropic
   gets `claude-sonnet-5-5`. Every other provider requires an explicit matching
   model/deployment ID; there is no first-model guess or maintained routing table.
4. Pass that provider/model pair explicitly to the SDK. SDK file configuration
   is **not a selector source** here, but normal SDK loading and validation still
   apply. An invalid or explicitly missing config file can still refuse.

For example, with an OpenAI key already exported:

```bash
export TMUX_FLEET_PROVIDER=openai
export AMPLIFIER_AGENT_MODEL='<model-id-your-account-can-use>'
tmux-fleet triage
tmux-fleet interpret work
```

Unset or align any conflicting `AMPLIFIER_AGENT_PROVIDER` first. `tmux-fleet doctor`
checks tmux/socket prerequisites, not model reachability or authentication.

## Authority, deadlines and capture

The adapter uses public `create_agent`, `AgentOptions`, `SessionOptions`,
`TurnInput` and `TextPart`. Each invocation runs one turn with explicit
`tools=[]`, `skills=[]`, `mcp_servers=[]`, `approvals="deny"` and
`additional_directories=[]`, and `SessionOptions(persistence="ephemeral")`.
There is **no public `agents=[]` field**. Fleet/scrollback context is mechanically
collected by tmux-fleet; the agent has no file, shell or delegation tools.

Owned temporary working and sessions directories hold the invocation's state,
but are not a sandbox. The process cwd is not changed; caller-owned cwd, when
supplied, is preserved and never deleted.

The default caller-owned turn deadline is **180 seconds**, separate from a fixed
**10-second TOTAL cleanup tail** shared across all cleanup steps. Timeout or
external cancellation must cancel/drain via the public session API, close the
session, close the agent, then delete only owned state after safe drain. Cancelling
only the coroutine awaiting `run()` does not stop the independent SDK turn.
If shutdown cannot finish within the tail, report failure honestly and retain
state still in use; never delete live state or hide the primary failure behind a
cleanup error. SDK diagnostics go to stderr so CLI stdout stays one JSON result.

The cleanup tail bounds the adapter's wait, not a force-stop of Python or an
arbitrary SDK/provider that ignores public close. Supported delayed-provider
shutdown is tested; an uncooperative SDK can still prevent its hosting event loop
from exiting. The library never adds a process-exit/kill workaround.
If construction fails without returning a session handle, public agent close
cannot certify cleanup of an unreturned runtime. The adapter closes what it owns,
reports that limitation and conservatively retains its local state.

**Ephemeral sessions still write observation captures.** Safe local cleanup does
not promise no remote forwarding or provider retention. Deliberate SDK file
configuration under `context_intelligence.destinations` is honored; tmux-fleet
neither adds destinations nor edits global settings. Review the SDK's
[configuration/forwarding policy](https://github.com/microsoft/amplifier-agent/blob/v0.22.0/docs/configuration.md#context_intelligence)
before sending sensitive pane output. No public per-agent no-forwarding override
is claimed.

## Refusal remedies

| Missing or invalid prerequisite | Remedy |
| --- | --- |
| Public `amplifier_agent` or selected base provider package is not importable | Reinstall the tool with its regular tagged dependency; source checkouts use `uv sync --locked`. Do not patch private engine caches. |
| Unknown provider, conflicting pins, or no eligible environment evidence | Set a supported provider with its required environment credentials/endpoint; align or unset conflicting pins. |
| Selected provider lacks credentials or a matching model | Export the variables above and `AMPLIFIER_AGENT_MODEL` for non-Anthropic providers. Pinning alone supplies neither. |
| SDK config is invalid or an explicitly selected file is missing | Fix the file named by the SDK (`AMPLIFIER_AGENT_CONFIG`, otherwise `~/.amplifier-agent/config.json`); do not bypass its validation. |
| Provider/turn/output/shutdown fails | Follow the safe error remedy; only successful, error-free, nonempty SDK text can reach JSON-object parsing. Non-success terminal text is not a result; retained in-use state is not successful cleanup. |

## Verification lesson for maintainers

Run all installs, locks, builds, tests and this live proof **inside an isolated
container**, never the shared host. Do not mount host homes, credentials or live
tmux sockets; use synthetic credentials and owned HTTP/tmux resources for the
automated gate. Unit mocks alone do not prove the installed release works.

1. Keep the **installed public binding, factory, engine and provider adapter
   unmocked**. Script only local HTTP inference responses. Serve model metadata
   (`/v1/models`) and token counts (`/v1/messages/count_tokens`) without consuming
   completion ordinals; otherwise fixture sequencing can misdiagnose real SDK
   traffic. Assert installed v0.22.0, actual options and no advertised tools.
2. Exercise triage/interpret on `tmux_kit.isolated_tmux_server()`, including
   HTTP 401/503, partial EOF, delayed/hung providers, external cancellation,
   empty/malformed/non-object JSON and non-success terminal text. Check cleanup's
   total bound, remaining tasks/state, and preservation of caller paths. Every
   raw tmux command, including teardown, must name an owned `-L` or `-S` socket.
3. Check both the locked development install and a **fresh wheel install** with
   no editable checkout or preinstalled SDK masking resolution. Inspect wheel
   `Requires-Dist`, installed `direct_url.json` and versions: binding
   `packages/python` and engine `packages/engine`, both v0.22.0 at release commit
   `915edf9372312d4c8244df4b2b16b094cf455cff`. Let the SDK own transitive pins.
4. Keep the real-SDK CI gate required, not silently skipped; retain the full
   suite, Playwright/MCP App build and canonical conformance against the staged
   wheel. Record exact commands/results; an unrun gate is **NOT RUN**, not PASS.
5. After scripted/package proof, an authorized paid live check can run
   `uv run --locked python evidence/real_triage_proof.py` inside the container
   with deliberately supplied provider credentials/model. It samples Linux
   `/proc` descendants and checks `amplifier_agent` in `sys.modules`; it is
   supplementary evidence, not a replacement for lifecycle/package tests or
   proof that ephemeral capture was never forwarded. Never use a scratch
   `AMPLIFIER_AGENT_HOME` cache workaround.

The CLI contract remains deliberately un-frozen **DRAFT**; this migration does
not freeze it.
