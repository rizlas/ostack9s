"""Command line entry point."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from . import __version__, privacy, resources
from .cloud import DEFAULT_TIMEOUT, CloudManager
from .i18n import LANGUAGES, detect_language, set_language
from .tokens import TokenCache

DEFAULT_CLOUDS = Path("~/.config/openstack/clouds.yaml").expanduser()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ostack9s",
        description="Interactive OpenStack dashboard for the terminal (k9s style).",
        epilog="Run 'ostack9s import --help' to merge credential files downloaded from Horizon.",
    )
    parser.add_argument(
        "--os-cloud",
        default=os.environ.get("OS_CLOUD"),
        help="clouds.yaml entry to use (default: $OS_CLOUD, otherwise ask)",
    )
    parser.add_argument("--os-region", default=None, help="initial region")
    parser.add_argument(
        "--config",
        default=os.environ.get("OS_CLIENT_CONFIG_FILE"),
        help="path of clouds.yaml (default: openstacksdk standard lookup)",
    )
    parser.add_argument(
        "--view",
        default="compute.server",
        choices=sorted(k.key for k in resources.top_level()),
        metavar="VIEW",
        help="initial view (default: compute.server)",
    )
    parser.add_argument(
        "--refresh",
        type=float,
        default=30.0,
        help="seconds between automatic refreshes (0 = disabled)",
    )
    parser.add_argument(
        "--lang",
        choices=LANGUAGES,
        default=detect_language(),
        help="interface language (default: $OSTACK9S_LANG, otherwise en)",
    )
    parser.add_argument(
        "--privacy",
        action="store_true",
        default=os.environ.get("OSTACK9S_PRIVACY", "") not in ("", "0"),
        help="start in privacy mode: mask public IPs, IDs, e-mails, keys (Ctrl+P toggles)",
    )
    parser.add_argument(
        "--privacy-word",
        action="append",
        default=[w for w in os.environ.get("OSTACK9S_PRIVACY_WORDS", "").split(",") if w],
        metavar="WORD",
        help="extra word to hide in privacy mode (repeatable, or $OSTACK9S_PRIVACY_WORDS)",
    )
    parser.add_argument(
        "--no-token-cache",
        action="store_true",
        help="do not reuse Keystone tokens across runs (~/.cache/ostack9s/tokens)",
    )
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="API timeout (s)")
    parser.add_argument("--log", default=None, help="debug log file")
    parser.add_argument("--version", action="version", version=f"ostack9s {__version__}")
    return parser.parse_args(argv)


def parse_import_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ostack9s import",
        description=(
            "Merge clouds.yaml files downloaded from Horizon (one application credential "
            "per project) into one file, naming each entry after its project."
        ),
    )
    parser.add_argument("files", nargs="+", type=Path, help="downloaded clouds.yaml files")
    parser.add_argument(
        "--target",
        type=Path,
        default=DEFAULT_CLOUDS,
        help=f"file to update (default: {DEFAULT_CLOUDS}); a backup is written first",
    )
    parser.add_argument("--prefix", default="", help="prefix for the entry names, e.g. acme-")
    parser.add_argument("--replace", action="store_true", help="replace entries with the same name")
    parser.add_argument(
        "--dry-run", action="store_true", help="show what would change, write nothing"
    )
    return parser.parse_args(argv)


def run_import(argv: list[str]) -> int:
    from .importer import merge

    args = parse_import_args(argv)
    logging.basicConfig(level=logging.CRITICAL)
    report, backup = merge(
        args.files, args.target, prefix=args.prefix, replace=args.replace, dry_run=args.dry_run
    )
    for r in report:
        where = f"{r.source}:{r.entry}" if r.entry else str(r.source)
        target = f" -> {r.name} (project {r.project})" if r.name else ""
        print(f"{r.status:9} {where}{target}")
    if backup:
        print(f"backup: {backup}")
    if args.dry_run:
        print("dry run: nothing written")
    return 1 if any(r.status.startswith("error") for r in report) else 0


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["import"]:
        sys.exit(run_import(argv[1:]))
    args = parse_args(argv)
    set_language(args.lang)
    privacy.set_enabled(args.privacy)
    privacy.add_words(args.privacy_word)
    if args.log:
        logging.basicConfig(filename=args.log, level=logging.DEBUG)
    else:
        # openstacksdk writes warnings to stderr, which would garble the TUI.
        logging.basicConfig(level=logging.CRITICAL)

    from .ui.app import OstdApp

    manager = CloudManager(
        config_file=args.config,
        timeout=args.timeout,
        token_cache=TokenCache(enabled=not args.no_token_cache),
    )
    app = OstdApp(
        manager,
        cloud=args.os_cloud,
        region=args.os_region,
        kind=args.view,
        refresh=args.refresh,
    )
    app.run()


if __name__ == "__main__":
    main()
