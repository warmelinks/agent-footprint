"""agent-footprint: the environmental footprint of your AI coding agents.

    agent-footprint serve [--port N]          browser dashboard
    agent-footprint watch [view] [--week]     live terminal view
    agent-footprint sync                      copy new usage into the history store and exit
    agent-footprint info                      versions, sources found, history, config path
    agent-footprint setup                     open the footprint whenever an agent session starts
    agent-footprint open                      open the watch view next to this terminal (used by the hooks)
    agent-footprint config                    create (if needed) and print the config file path
    agent-footprint update                    upgrade EcoLogits (new models, grid data)
"""
import argparse
import sys
from datetime import date

from . import config, model, periods, sources, updates
from .store import Store


def _period_arg(kind, value):
    if value in (None, "current", "today"):
        return "%s:%s" % (kind, "today" if kind == "day" else "current")
    view = "%s:%s" % (kind, value)
    periods.parse(view)  # validate
    return view


def cmd_watch(args):
    from .watch import watch
    view = args.view or "day:today"
    for kind in ("year", "month", "week", "day"):
        value = getattr(args, kind)
        if value is not False:
            view = _period_arg(kind, value)
    watch(view, once=args.once, color=not args.no_color)


def cmd_serve(args):
    from .server import serve
    serve(args.host, args.port, idle_exit=args.idle_exit)


def cmd_sync(args):
    store = Store()
    syncer = sources.Syncer(store, config.load_config())
    n = syncer.sync()
    st = store.stats()
    print("%d records updated; history holds %s responses in %s sessions (%.1f MB)" % (
        n, "{:,}".format(st["records"]), st["sessions"], st["bytes"] / 1e6))
    for notice in syncer.notices():
        print(notice)


def cmd_info(args):
    cfg = config.load_config()
    store = Store()
    syncer = sources.Syncer(store, cfg)
    syncer.sync()
    st = store.stats()
    print("agent-footprint %s, ecologits %s" % (updates.own_version(), model.ecologits_version()))
    print("config:  %s%s" % (config.user_path(), "" if config.mtime() else " (not created; using defaults)"))
    print("history: %s (%s responses, %s sessions, %.1f MB%s)" % (
        store.path, "{:,}".format(st["records"]), st["sessions"], st["bytes"] / 1e6,
        ", since %s" % date.fromtimestamp(st["since"]).isoformat() if st["since"] else ""))
    for s in syncer.sources:
        print("%-12s %s %s" % (sources.TOOL_LABEL[s.tool] + ":", "found  " if s.available() else "missing", s.root))
    for notice in syncer.notices():
        print(notice)
    for problem in cfg["problems"]:
        print("config problem: %s" % problem)
    from . import hooks
    print("session-start hooks (agent-footprint setup):")
    print("\n".join(hooks.status_lines()))
    if cfg["auto_update_check"]:
        notice = updates.check()
        print(notice or "Figures are up to date (EcoLogits %s)." % model.ecologits_version())


def cmd_setup(args):
    from . import hooks
    agents = [a.strip() for a in args.agents.split(",")] if args.agents else None
    bad = [a for a in agents or [] if a not in hooks.AGENTS]
    if bad:
        raise ValueError("unknown agent(s): %s (choose from %s)" % (", ".join(bad), ", ".join(hooks.AGENTS)))
    titles = {"on": True, "off": False}.get(args.titles)
    sys.exit(hooks.setup(agents, yes=args.yes, remove=args.remove, status=args.status,
                         interactive=sys.stdin.isatty(), titles=titles))


def cmd_open(args):
    from . import launch
    sys.exit(launch.run(args.agent, hook=args.hook, terminal=args.terminal))


def cmd_config(args):
    print(config.ensure_user_config())


def cmd_update(args):
    sys.exit(updates.update())


def main(argv=None):
    parser = argparse.ArgumentParser(prog="agent-footprint", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd")

    w = sub.add_parser("watch", help="live terminal view")
    w.add_argument("view", nargs="?", help="'latest', a session id, or a period id like month:2026-09 (default: today)")
    for kind, example in (("day", "2026-10-01"), ("week", "2026-W40"), ("month", "2026-09"), ("year", "2026")):
        w.add_argument("--" + kind, nargs="?", const=None, default=False, metavar=example,
                       help="show a whole %s (default: the current one)" % kind)
    w.add_argument("--once", action="store_true", help="print a snapshot and exit")
    w.add_argument("--no-color", action="store_true")
    w.set_defaults(func=cmd_watch)

    s = sub.add_parser("serve", help="browser dashboard")
    s.add_argument("--port", type=int, default=None)
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--idle-exit", type=int, metavar="SECONDS",
                   help="stop after this long without a browser connected (used when a hook starts it)")
    s.set_defaults(func=cmd_serve)

    sub.add_parser("sync", help="copy new usage into the history store and exit").set_defaults(func=cmd_sync)
    sub.add_parser("info", help="versions, sources, history and config").set_defaults(func=cmd_info)
    st = sub.add_parser("setup", help="open the footprint whenever an agent session starts (installs hooks)")
    st.add_argument("--agents", help="comma-separated: claude,codex,gemini,opencode (default: every agent found)")
    st.add_argument("--yes", "-y", action="store_true", help="don't ask")
    st.add_argument("--remove", action="store_true", help="remove the hooks again")
    st.add_argument("--status", action="store_true", help="show which agents have the hook")
    st.add_argument("--titles", choices=("on", "off"),
                    help="name sessions after the start of their first prompt (stored in the history; default off)")
    st.set_defaults(func=cmd_setup)

    op = sub.add_parser("open", help="open the watch view next to this terminal, or the dashboard")
    op.add_argument("--hook", action="store_true", help="run as an agent hook: read its input, print nothing")
    op.add_argument("--agent", help="which agent runs the hook")
    op.add_argument("--terminal", help="override the terminal detection (e.g. tmux, wezterm, none)")
    op.set_defaults(func=cmd_open)

    sub.add_parser("config", help="create and print the config file path").set_defaults(func=cmd_config)
    sub.add_parser("update", help="upgrade EcoLogits to get new models and grid data").set_defaults(func=cmd_update)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        args = parser.parse_args(["watch"] + (argv or sys.argv[1:]))
    try:
        args.func(args)
    except ValueError as e:
        parser.error(str(e))


if __name__ == "__main__":
    main()
