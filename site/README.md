# Website

This repository's promotional page uses the Amplifier Smart Tools family theme.
Product page content lives in `site.json`. The canonical theme is maintained in
`microsoft/amplifier-smart-tools/site/theme`; this repository carries a versioned
copy so a build does not depend on a moving remote theme or an online service.
Original vendored theme version: 0.1.0. Theme code is MIT licensed. The original
vendor revision was not recorded; the version label alone does not prove a source
commit. The canonical repository's `14b6957` checkout is an inspectable reference,
not evidence that this copy was vendored from that revision.

## Build and preview

From the repository root, using Python 3.12 or later:

```sh
python3 -m venv .work/site-venv
.work/site-venv/bin/python -m pip install -r site/requirements.txt
.work/site-venv/bin/python site/theme/build.py
python3 -m http.server 8000 --directory _site --bind 127.0.0.1
```

Open http://127.0.0.1:8000. Generated output belongs in `_site/` and is ignored.
The site is a static build; visitors need no Python runtime or account.

The `--family-owner robotdad` option points overview and catalog links at the
fork previews. Omit it for their Microsoft organization URLs. Individual tool
links retain their authors' owners. Keep the same setting across the family.
For a combined local preview, build each page with `--local --output` into a
shared preview directory named after its repository, then serve that directory.

## GitHub Pages

The Website workflow builds and tests relevant pull requests without deploying.
Manual runs validate by default, including runs on non-main branches. Publication
requires the Microsoft repository, `refs/heads/main`, and either a relevant push
or a manual run with **publish** checked. Actions are pinned to immutable upstream
commits; ordinary CI tests remain separate and unchanged.

Before uploading a production artifact, a bounded read-only Pages API probe checks
that `build_type` is `workflow`. A 404 visibly reports Pages not configured; a 403
reports permission unknown. Both skip publication without claiming deployment.
Other HTTP/network errors fail the probe. Pull requests, forks and validation-only
manual runs do not probe or upload production artifacts. No repository variable
is required. Enable Pages with GitHub Actions as the source and allow main in the
github-pages environment; a configured source alone is not publication proof.

Production navigation points to the Microsoft overview and catalog by default.
Set `family_owner` to robotdad only when intentionally previewing the forks.
All linked family sites must be published for cross-site navigation to resolve.

## Shared identity

Use the full family name, Amplifier Smart Tools, as one masthead and footer
identity. The overview explains the format and how to build a tool; discovery
and individual tool listings belong in the catalog.

Use plain punctuation, with middle dots permitted between Math, AI, Design, and
Engineering in the team name. Keep MADE and Microsoft Office of the CTO as text
attribution. Do not link to the internal team site. Preserve the shared navigation,
layout, typography, paper background, and gold details; a tool may set its own
accent, content, and image in `site.json`.

The image or diagram selected in `site.json` is copied from this repository at build time.
Their existing provenance remains there. Screenshots are labeled as screenshots;
concept artwork is labeled as illustration. Add demo video only when reviewed
footage is available. Do not use a fake player over a still image.

To update a vendored theme, run the canonical `site/sync_theme.py` with an explicit
target checkout, review the diff, and build again. It updates only `site/theme/`.
Page content stays in the owning repository. The family link registry holds only
navigation identity; the catalog's tool inventory remains `tools/*/source.json`.

## Motion assets

The shared title mark uses an eight-second looping GIF with a static PNG fallback.
Reduced-motion
preferences select the still image by default. Shared media lives in
`site/theme/assets/` and is included by the theme sync script. The original briefs
and generation provenance live in `amplifier-smart-tools/site/artwork/`.

The footer's keyboard-operable pause/play button reuses the existing theme handler.
Reduced-motion visitors start on the still image. Without JavaScript the mark
stays still and content, links and selectable install commands remain usable.

## Local adaptations and checks

This is a locally adapted copy, not an unchanged upstream theme. In addition to
the original optional `--config` and family URL subpaths, local adaptations add
optional setup/capture guidance links, copy the complete existing theme `LICENSE`
into generated output, and link that notice visibly in the footer alongside the
motion control. License byte equality proves inclusion, not legal clearance.
The upstream notice itself is unchanged.

The page describes the CLI/library workflow, not a live terminal dashboard or a
production-health monitor. Model setup follows the
[tagged SDK installation guide](../docs/installing-amplifier-agent.md); sensitive
capture/forwarding guidance remains in the shipped
[SMART_TOOL.md](../src/tmux_fleet/SMART_TOOL.md).

Inside an isolated development container, run:

```sh
uv sync --locked --extra dev --extra mcp
uv run --locked --extra dev --extra mcp pytest tests/test_website.py tests/test_help.py -q
python site/theme/build.py
uv run --locked --extra dev --extra mcp playwright install chromium
uv run --locked --extra dev --extra mcp python tests/test_website.py --evidence-dir evidence/website-rendered.out.txt
```

The deterministic tests cover generated text/assets, relative paths, exact license
bytes, immutable action references, the deployment guard matrix and Pages probe
branches. The direct browser check requires Playwright/Chromium (no skip path):
1440px desktop and 390px mobile, copy, keyboard pause/play, reduced motion,
no-JavaScript fallback, loaded images, overflow and JavaScript errors. Screenshots
are local ignored evidence, not shipped artifacts. Review them for visible clipping.

After merge, open **Settings > Pages** and select **GitHub Actions** as the source.
If a build skipped publication before Pages was enabled, run the Website workflow
on **main** with **publish** checked after configuration. Later eligible website
changes on main publish when the readiness probe succeeds. Confirm the resulting
deployment and public page separately; a successful static build is not that proof.
