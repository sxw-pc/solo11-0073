"""One-command synthetic demo over repository fixtures."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .engine import build_audit
from .models import InputError
from .report import summary_lines, write_run


@dataclass(frozen=True)
class FixtureSet:
    name: str
    dataset: Path
    splits: Path
    config: Path


def _fixture_root() -> Path:
    return Path(__file__).resolve().parent.parent / "fixtures"


def fixture_sets() -> list[FixtureSet]:
    root = _fixture_root()
    result = []
    for name in ("clean", "leaky"):
        directory = root / name
        paths = (directory / "dataset.csv", directory / "splits.csv", directory / "config.json")
        missing = [path.name for path in paths if not path.is_file()]
        if missing:
            raise InputError(
                "DEMO_FIXTURES_MISSING",
                f"fixture set {name} is incomplete (missing {', '.join(missing)})",
                path=directory,
            )
        result.append(FixtureSet(name, *paths))
    return result


def run_demo(workdir: Path) -> int:
    workdir = Path(workdir)
    runs_dir = workdir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    print(f"demo workdir: {workdir}")
    print("all fixtures are synthetic; no network access is performed")
    for fixture in fixture_sets():
        output_dir = runs_dir / fixture.name
        bundle = build_audit(fixture.dataset, fixture.splits, fixture.config)
        written = write_run(bundle.report, output_dir)
        print("")
        print(f"[{fixture.name}]")
        for line in summary_lines(written.report, written.out_dir):
            print(line)
    print("")
    print("demo complete; blocking findings in the leaky fixture do not set exit code 2")
    print(
        "review: python -m splitguard serve --workdir "
        f"{workdir.as_posix()} --host 127.0.0.1 --port 8765"
    )
    return 0
