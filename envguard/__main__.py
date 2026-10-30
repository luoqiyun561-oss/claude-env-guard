"""Entry point: native window by default, console formats for automation.

The default is the tkinter window (envguard.gui) — the tool no longer starts a
loopback server or opens a browser on its own. `--web` still exists for the HTML
page, because that page is the only place the browser-only checks (WebRTC
candidates, the browser's own timezone) can run; it is an explicitly requested
mode, not the default path.
"""

import argparse
import sys
import traceback


def _force_utf8_stdout() -> None:
    """Make redirected output UTF-8 regardless of the console code page.

    On Windows Python encodes stdout with the ANSI code page (GBK on a zh-CN
    machine), so `--json > report.json` writes mojibake for every Chinese field
    and a consumer parsing it gets garbage — measured here. Reconfiguring to
    UTF-8 is safe in a console too: modern Windows Terminal renders it.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            continue


def _run_console(fmt: str, no_net: bool) -> int:
    from . import collector, report as report_mod

    if no_net:
        # The network readings degrade to unknown on their own; setting the flag
        # here keeps `--no-net` honest instead of fake-passing those checks.
        collector.NO_NET = True
    result = collector.collect()
    if fmt == "json":
        print(report_mod.to_json(result))
    elif fmt == "md":
        print(report_mod.to_markdown(result))
    else:
        print(report_mod.to_text(result))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ClaudeEnvGuard",
        description="检测本机环境里能被观测的 Claude 风控相关事实。只读，不改任何设置。",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出检测结果")
    parser.add_argument("--text", action="store_true", help="以纯文本输出检测结果")
    parser.add_argument("--md", action="store_true", help="以 Markdown 输出检测结果")
    parser.add_argument("--no-net", action="store_true", help="跳过联网探测（只查本机）")
    parser.add_argument(
        "--web",
        action="store_true",
        help="改为在本地服务上打开 HTML 版（唯一能测 WebRTC 的形态）",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8788,
        help="--web 的起始端口，占用时自动向后找（默认 8788）",
    )
    args = parser.parse_args(argv)

    try:
        if args.json or args.text or args.md:
            _force_utf8_stdout()
            fmt = "json" if args.json else "md" if args.md else "text"
            return _run_console(fmt, args.no_net)

        if args.web:
            from . import server

            server.DEFAULT_PORT = args.port
            _force_utf8_stdout()
            return server.serve(open_browser=True)

        from . import gui

        return gui.main()
    except Exception:
        traceback.print_exc()
        return 1
