"""Command line:  python -m cem_bot <command> [options]

  demo            start the mock portal + mock CEM and run the bot on them
  run             process tickets (use --dry-run first!)
  check-mapping   validate Mapping.xlsx and show the rules per category
"""
from __future__ import annotations

import argparse
import sys

from .config import ConfigError, load_config
from .mapping import MappingError, load_mapping


def _cmd_run(a) -> int:
    cfg = load_config(profile=a.profile)
    if cfg.is_real and not a.dry_run and not a.yes:
        print(f"\n  LIVE RUN against {cfg.portal_base} and {cfg.cem_base}")
        print("  Tickets will be closed and CEM users changed.")
        if input("  Type YES to continue: ").strip() != "YES":
            print("Cancelled.")
            return 1
    from .runner import Runner   # imported late so check-mapping works without Playwright
    types = [x.strip().upper() for x in a.types.split(",")] if a.types else None
    r = Runner(cfg, dry_run=a.dry_run, max_tickets=a.max, types=types,
               reassign=False if a.no_reassign else None,
               headless=True if a.headless else (False if a.headed else None),
               slow_mo=a.slowmo, verbose=a.verbose).run()
    return 1 if r.counts().get("ERROR") else 0


def _cmd_demo(a) -> int:
    """Mock systems + bot in one command (no second window needed)."""
    from urllib.parse import urlparse

    cfg = load_config(profile="mock")   # also loads .env, which the mock uses as valid logins
    from mock import state
    from mock.server import MockServers

    from .runner import Runner
    p, c = urlparse(cfg.portal_base), urlparse(cfg.cem_base)
    try:
        servers = MockServers(p.hostname, p.port, c.port).start()
    except OSError as e:
        print(f"ERROR: cannot start the mock systems on ports {p.port}/{c.port} ({e}). "
              "Is 'python -m mock.server' already running? Then use: python -m cem_bot run --profile mock")
        return 2
    state.reset()
    print(f"Mock portal {cfg.portal_base} | mock CEM {cfg.cem_base} (fake data, reset)")
    try:
        r = Runner(cfg, dry_run=a.dry_run, headless=True if a.headless else None,
                   slow_mo=a.slowmo, verbose=a.verbose).run()
    finally:
        servers.stop()
    return 0 if r.counts().get("ERROR", 0) <= 1 else 1   # one ticket fails on purpose


def _cmd_check_mapping(a) -> int:
    cfg = load_config(need_credentials=False)
    path = a.file or cfg.path(cfg.settings["run"]["mapping_file"])
    m = load_mapping(path, cfg.rules["categories"])
    print(f"Mapping file : {m.source}")
    print(f"Job codes    : {len(m.jobs)}  {m.summary()}\n")
    print("What each category gets in CEM (config/rules.yaml):")
    for cat, c in cfg.rules["categories"].items():
        print(f"  {cat:8} role={c.get('role') or '-':28} grants={', '.join(c['grants']):40} "
              f"clear old branches={c.get('clear_existing_branches', True)}")
    if cfg.rules.get("overrides"):
        print(f"  overrides: {cfg.rules['overrides']}")
    if m.warnings:
        print("\nWarnings:")
        for w in m.warnings:
            print("  - " + w)
    else:
        print("\nNo warnings.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m cem_bot", description="CEM provisioning bot")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="process the CEM tickets")
    r.add_argument("--profile", choices=["mock", "real"], help="override 'profile' in settings.yaml")
    r.add_argument("--dry-run", action="store_true", help="read tickets + look users up, change NOTHING")
    r.add_argument("--max", type=int, help="stop after N tickets")
    r.add_argument("--types", help="only these types, e.g. MODIFY,CREATE (MODIFY, REACTIVATE, CANCEL, CREATE)")
    r.add_argument("--no-reassign", action="store_true", help="skip the 'إعادة اسناد' step")
    r.add_argument("--headless", action="store_true", help="hide the browser")
    r.add_argument("--headed", action="store_true", help="show the browser")
    r.add_argument("--slowmo", type=int, help="ms delay between browser actions")
    r.add_argument("--yes", action="store_true", help="no confirmation prompt for live runs")
    r.add_argument("-v", "--verbose", action="store_true")
    r.set_defaults(func=_cmd_run)

    d = sub.add_parser("demo", help="run the bot on the built-in mock portal + mock CEM")
    d.add_argument("--dry-run", action="store_true")
    d.add_argument("--headless", action="store_true", help="hide the browser")
    d.add_argument("--slowmo", type=int, help="ms delay between browser actions")
    d.add_argument("-v", "--verbose", action="store_true")
    d.set_defaults(func=_cmd_demo)

    c = sub.add_parser("check-mapping", help="validate the mapping Excel file")
    c.add_argument("--file", help="path to another mapping file")
    c.set_defaults(func=_cmd_check_mapping)

    a = ap.parse_args(argv)
    try:
        return a.func(a)
    except (ConfigError, MappingError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
