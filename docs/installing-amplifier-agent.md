# Installing amplifier-agent

The model-backed verbs — `triage` and `interpret` — execute through
**amplifier-agent**, the AI substrate. It is optional: every deterministic verb
(`socket`, `sessions`, `attention`, `read`, `send`, `create`, `doctor`,
`exit-code`) works without it. When it is absent, the smart verbs fail loudly
naming this remedy — they never silently degrade to a deterministic guess.

amplifier-agent is not on PyPI. Install it as a tool:

```
uv tool install git+https://github.com/microsoft/amplifier-agent
```

Then configure a provider for it (amplifier-agent owns provider credentials —
this tool never does):

```
amplifier-agent providers      # see how credentials resolve
amplifier-agent auth           # manage persistent provider credentials
```

Once configured, every smart tool on the machine shares the same substrate.

Verify with `amplifier-agent doctor`, then run `tmux-fleet --help` — the
model-backed verbs are marked there — and try `tmux-fleet triage`.

> **Provider SDK gotcha:** `uv tool install` pulls `amplifier-core` but **not**
> provider SDKs — add your provider, e.g. `--with anthropic`, or the model-backed
> verbs will fail with `No module named 'anthropic'`:
> `uv tool install --with anthropic git+https://github.com/microsoft/amplifier-agent`
