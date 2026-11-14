"""Command line interface for SplitGuard."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from . import VERSION
from .engine import build_audit
from .models import InputError
from .report import summary_lines, write_run

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_GATE = 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="splitguard",
        description="离线 train/valid/test 切分泄漏核验与审计工具",
    )
    parser.add_argument(
        "--version", action="version", version=f"splitguard {VERSION}"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    demo = subparsers.add_parser("demo", help="运行仓库内合成样例演示")
    demo.add_argument("--workdir", default=".splitguard_work", help="工作目录")

    audit = subparsers.add_parser("audit", help="审计真实数据集和切分清单")
    audit.add_argument("--dataset", required=True, help="数据集 CSV 路径")
    audit.add_argument("--splits", required=True, help="切分清单 CSV 路径")
    audit.add_argument("--config", required=True, help="JSON 配置路径")
    audit.add_argument("--out", required=True, help="输出目录")
    audit.add_argument(
        "--fail-on",
        choices=("block", "warn", "never"),
        default="block",
        help="非零退出阈值，默认 block",
    )

    serve = subparsers.add_parser("serve", help="启动只读本地复核台")
    serve.add_argument("--workdir", default=".splitguard_work", help="工作目录")
    serve.add_argument("--host", default="127.0.0.1", help="仅允许 loopback 地址")
    serve.add_argument("--port", default=8765, type=int, help="监听端口")
    return parser


def _gate_exit(report: dict, fail_on: str) -> int:
    counts = report.get("summary", {}).get("finding_counts", {})
    if fail_on == "never":
        return EXIT_OK
    if fail_on == "warn" and (counts.get("block", 0) or counts.get("warn", 0)):
        return EXIT_GATE
    if fail_on == "block" and counts.get("block", 0):
        return EXIT_GATE
    return EXIT_OK


def _audit(args: argparse.Namespace) -> int:
    bundle = build_audit(args.dataset, args.splits, args.config)
    written = write_run(bundle.report, args.out)
    for line in summary_lines(written.report, written.out_dir):
        print(line)
    return _gate_exit(written.report, args.fail_on)


def _demo(args: argparse.Namespace) -> int:
    from .demo import run_demo

    return run_demo(Path(args.workdir))


def _serve(args: argparse.Namespace) -> int:
    from .server import serve

    serve(Path(args.workdir), args.host, args.port)
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "demo":
            return _demo(args)
        if args.command == "audit":
            return _audit(args)
        if args.command == "serve":
            return _serve(args)
        parser.print_help()
        return EXIT_ERROR
    except InputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except OSError as exc:
        print(f"error: [IO_ERROR] {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("stopped", file=sys.stderr)
        return EXIT_OK
