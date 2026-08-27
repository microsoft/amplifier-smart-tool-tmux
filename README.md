# tmux-fleet

A **smart tool** for tmux fleets: one library, one thin `tmux-fleet` CLI that
tells you what is happening across every tmux session on a machine — which are
parked at a prompt, which finished and how, which need a human — and, only
under explicit per-invocation confirmation, types into one or starts one.

Deterministic paths (`socket`, `sessions`, `attention`, `read`, `send`,
`create`, `doctor`, `exit-code`) run with nothing configured. Smart paths
(`triage`, `interpret`) bring model-backed judgment, executed through
[amplifier-agent](https://github.com/microsoft/amplifier-agent)'s engine library
**embedded in-process** — no subprocess, no PATH-resolved binary.

This repo is the first reference implementation of the
[amplifier smart-tools spec](https://github.com/microsoft/amplifier-smart-tools).
See `docs/VISION.md` and `contracts/cli.v1.md` for the governing design.

## Install

```
uv tool install git+<this repo>
```

The engine ships as a regular dependency — you get it automatically. To use the
model-backed verbs you additionally install the extra for your provider's SDK
and supply that provider's credentials from your environment (the tool stores
none):

```
uv tool install "git+<this repo>" --with anthropic   # or: pip install "tmux-fleet[anthropic]"
export ANTHROPIC_API_KEY=sk-ant-...
```

Extras available today: `anthropic`, `openai`. See
[`docs/installing-amplifier-agent.md`](docs/installing-amplifier-agent.md) for
provider configuration, the `TMUX_FLEET_PROVIDER` pin, and the refusal taxonomy.

## Use

```
tmux-fleet --help          # full, agent-facing listing (names the model-backed verbs)
tmux-fleet socket          # which socket am I reading, and on whose authority
tmux-fleet sessions        # every session on the resolved socket, with slivers
tmux-fleet attention       # triage ORDER: which sessions plausibly want a human
tmux-fleet read <name>     # one session's scrollback, with an honest completeness bound
tmux-fleet doctor          # preflight: tmux present, socket resolvable/writable
tmux-fleet exit-code <name># tmux-native exit status of a finished session
tmux-fleet triage          # [model-backed] fleet-wide: what needs attention and why
tmux-fleet interpret <name># [model-backed] what this session's state/output means
tmux-fleet send <name> --text 'echo hi' --submit --confirmed   # fenced write
tmux-fleet create <name> --confirmed                            # fenced create
```

Every response is one JSON document on stdout. Failures are a JSON error
envelope (`{"error": {"code", "message", "remedy"}}`) on stdout with a non-zero
exit; diagnostics go to stderr. The write verbs refuse without `--confirmed`.
There is deliberately no verb that kills or renames a session.

## Library

Every capability is in the library; the CLI is a thin adapter.

```python
import asyncio
from tmux_fleet import list_sessions, triage, manifest

asyncio.run(list_sessions())
manifest()  # structured manifest, read from the copy built into the tool
```

## Contributing

> [!NOTE]
> This project is not currently accepting external contributions, but we're actively working toward opening this up. We value community input and look forward to collaborating in the future. For now, feel free to fork and experiment!

Most contributions require you to agree to a
Contributor License Agreement (CLA) declaring that you have the right to, and actually do, grant us
the rights to use your contribution. For details, visit [Contributor License Agreements](https://cla.opensource.microsoft.com).

When you submit a pull request, a CLA bot will automatically determine whether you need to provide
a CLA and decorate the PR appropriately (e.g., status check, comment). Simply follow the instructions
provided by the bot. You will only need to do this once across all repos using our CLA.

This project has adopted the [Microsoft Open Source Code of Conduct](https://opensource.microsoft.com/codeofconduct/).
For more information see the [Code of Conduct FAQ](https://opensource.microsoft.com/codeofconduct/faq/) or
contact [opencode@microsoft.com](mailto:opencode@microsoft.com) with any additional questions or comments.

## Trademarks

This project may contain trademarks or logos for projects, products, or services. Authorized use of Microsoft
trademarks or logos is subject to and must follow
[Microsoft's Trademark & Brand Guidelines](https://www.microsoft.com/legal/intellectualproperty/trademarks/usage/general).
Use of Microsoft trademarks or logos in modified versions of this project must not cause confusion or imply Microsoft sponsorship.
Any use of third-party trademarks or logos are subject to those third-party's policies.
