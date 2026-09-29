# AGENTS.md

Guidance for AI coding agents working on this repository.

## Project

ostack9s is a k9s style terminal dashboard for OpenStack, written in Python with
[Textual](https://textual.textualize.io/) on top of
[openstacksdk](https://docs.openstack.org/openstacksdk/). It replaces the day to day use
of Horizon: resource views with actions, quota panel, global overview across projects
and regions, network topology, privacy mode. It is released as standalone binaries
(PyInstaller), `.deb`/`.rpm` packages and a wheel.

## Commands

The project is managed with [uv](https://docs.astral.sh/uv/). Python 3.12.

```bash
uv sync                            # install dependencies (dev group included)
uv run ostack9s                    # run the TUI (reads clouds.yaml)
uv run black src tests             # format (line length 100)
uv run ruff check src tests        # lint
uv run ty check src                # type check
uv run pytest -q                   # tests, no network needed
packaging/build-binary.sh dist     # standalone binary in dist/ostack9s
```

Before declaring a change done, run black, ruff, ty and pytest: CI runs the same checks
(`.github/workflows/ci.yml`) and fails on any of them. If `pyproject.toml` changes,
refresh `uv.lock` with `uv lock`: CI uses `uv sync --locked`.

## Layout

| Path | Content |
|---|---|
| `src/ostack9s/cli.py` | argument parsing, `ostack9s import` subcommand, entry point |
| `src/ostack9s/cloud.py` | `CloudManager`: clouds.yaml entries, projects, regions, connections |
| `src/ostack9s/tokens.py` | on-disk Keystone token cache (`~/.cache/ostack9s/tokens`) |
| `src/ostack9s/overview.py` | quota and usage summary per context, progressive updates |
| `src/ostack9s/resources/` | declarative resource model (`base.py`) and resource kinds |
| `src/ostack9s/ui/app.py` | main Textual app: navigation, command bar, actions |
| `src/ostack9s/ui/widgets.py` | header, quota panel, describe pane, breadcrumbs |
| `src/ostack9s/ui/modals.py` | fuzzy select, confirm, form, text viewer |
| `src/ostack9s/ui/screens.py` | resource menu, topology, password prompt |
| `src/ostack9s/ui/overview_screen.py` | F1 overview with sorting |
| `src/ostack9s/topology.py` | network topology model, tree render, Mermaid/DOT export |
| `src/ostack9s/gpu.py` | GPU usage from flavor extra specs |
| `src/ostack9s/privacy.py` | privacy mode masking |
| `src/ostack9s/i18n.py`, `locales/` | `t()` translation layer and Italian catalog |
| `src/ostack9s/importer.py` | merge Horizon clouds.yaml downloads |
| `packaging/` | PyInstaller build script, nfpm config |
| `install.sh` | no sudo installer published with each release |

## Adding or changing resources

Resources are declarative: a `ResourceKind` in `src/ostack9s/resources/` lists the
items, defines columns, actions (`Action` with form `Field`s) and child navigation
(`Child`), then goes into the module's `KINDS` list. The UI is generic, so most features
need no UI change.

- Action keys must not clash with global keys (`q`, `a`, `y`, `m`, `d`, `:`, `/`, `?`,
  `[`, `]`, `ctrl+r`, `ctrl+o`, `ctrl+p`, `escape`, `enter`, `tab`, `f1`-`f5`) or with
  other keys of the same kind: `tests/test_resources.py` checks it.
- Conventions: `N` creates, `n` edits, `ctrl+d` deletes. Set `confirm=True` for
  disruptive operations and `destructive=True` for data loss.
- `run` functions receive `(conn, item, values)` and return a message; for actions with
  `needs_item=False` the item is the parent resource of the view (or `None`).

## Strings and translations

- Source strings, comments and docstrings are in English.
- Every string shown to the user goes through `t()` from `ostack9s.i18n`, with
  `str.format` placeholders: `t("Deleted {name}", name=x)`. Labels in resource
  definitions stay plain English and are translated at render time.
- Every new string needs an Italian translation in `src/ostack9s/locales/it.py`.
  `test_italian_catalog_is_complete` fails otherwise; placeholders must match.

## Textual pitfalls

These already caused bugs here:

- Do not name methods or attributes like Textual internals: `run_action`,
  `action_back`, `_render`, `loading`, `_auto_refresh` exist on `App`/`Widget`. Run
  `ty check`, it reports incompatible overrides.
- `App.query_one` queries the active screen: while a modal is open it does not see the
  main screen. In `OstdApp` use `self._q(...)`, which queries the main screen.
- Callbacks from workers can arrive after shutdown: guard with
  `if not self.screen_stack: return`.
- In Rich markup a literal `[` must be escaped as `\[` (see `_render_title`), and user
  data must go through `rich.markup.escape`.
- Blocking SDK calls run in `@work(thread=True)` workers or `asyncio.to_thread`, and
  results come back with `call_from_thread`. Never call the SDK from the event loop.

## OpenStack notes

- Per-region connections reuse the authenticated session
  (`cloud_region.from_session`): no new token per region.
- Application credentials are bound to one project. Entries with the same `auth_url`
  and user are grouped as projects of the same user (`CloudManager.targets`).
- API policies differ between clouds: a 403 must show an error in the view, never crash.
- The first call to a service can take seconds on the server side. Keep work
  progressive (see `summarize(on_update=...)`) and cache lists.
- Flavor `extra_specs` come with flavor and server lists (Nova microversion 2.61+): do
  not fetch them one by one.

## Safety

- Never commit credentials: `clouds.yaml`, `secure.yaml`, openrc files and `.env` are
  ignored, keep it that way. Do not print secrets in logs or output.
- Tests use fake data only: no real e-mails, IPs, project names or IDs. Use
  `example.com`, documentation or public DNS addresses, `foo`/`bar` names.
- Tests never touch a real cloud: use `FakeManager` and `MagicMock` connections as in
  `tests/test_app.py`.
- When testing against a real cloud, stay read-only unless the owner allows otherwise;
  write tests create only `ostack9s-test-*` resources and delete them at the end.

## Packaging and releases

- `packaging/build-binary.sh` lists the modules openstacksdk and keystoneauth load
  dynamically (auth plugins, dogpile cache backends, service data). If the binary
  fails with `No module named ...`, add the module there and rebuild; CI builds the
  binary on Linux and macOS to catch this.
- Release: bump the version in `pyproject.toml` and `src/ostack9s/__init__.py`, run
  `uv lock`, commit, then push a `vX.Y.Z` tag. The release workflow checks that tag
  and versions match.
- Pin GitHub Actions to tags that exist: some actions (e.g. `astral-sh/setup-uv`) do not
  publish moving major tags. actionlint does not detect this.

## Documentation

- `README.md` is user facing and in English; keep lines within 88 characters (tables,
  badges and long URLs excepted).
- Examples use placeholder names (`foo`, `bar`, `region-a`, `keystone.example.org`).
