import subprocess
import sys
from pathlib import Path

from splitguard.cli import main

ROOT = Path(__file__).resolve().parents[1]


def _case(name: str) -> list[str]:
    base = ROOT / "fixtures" / name
    return [
        "--dataset",
        str(base / "dataset.csv"),
        "--splits",
        str(base / "splits.csv"),
        "--config",
        str(base / "config.json"),
    ]


def test_version_command() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "splitguard", "--version"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "splitguard 1.0.0"


def test_audit_exit_codes_and_output_files(tmp_path, capsys) -> None:
    clean_out = tmp_path / "clean"
    assert main(["audit", *_case("clean"), "--out", str(clean_out)]) == 0
    stdout = capsys.readouterr().out
    assert "run_id:" in stdout and "conclusion: pass" in stdout
    for name in ("report.json", "report.md", "report.html", "run_manifest.json", "ledger.sqlite"):
        assert (clean_out / name).is_file()

    leaky_out = tmp_path / "leaky"
    assert main(["audit", *_case("leaky"), "--out", str(leaky_out)]) == 2
    assert "conclusion: block" in capsys.readouterr().out

    never_out = tmp_path / "never"
    assert (
        main(
            [
                "audit",
                *_case("leaky"),
                "--out",
                str(never_out),
                "--fail-on",
                "never",
            ]
        )
        == 0
    )


def test_cli_input_error_is_nonzero(tmp_path, capsys) -> None:
    code = main(
        [
            "audit",
            "--dataset",
            str(tmp_path / "missing.csv"),
            "--splits",
            str(tmp_path / "splits.csv"),
            "--config",
            str(tmp_path / "config.json"),
            "--out",
            str(tmp_path / "out"),
        ]
    )
    assert code == 1
    assert "CONFIG_NOT_FOUND" in capsys.readouterr().err


def test_demo_runs_both_fixtures_and_exits_zero(tmp_path, capsys) -> None:
    workdir = tmp_path / "work"
    assert main(["demo", "--workdir", str(workdir)]) == 0
    output = capsys.readouterr().out
    assert "[clean]" in output and "[leaky]" in output
    assert (workdir / "runs" / "clean" / "report.json").is_file()
    assert (workdir / "runs" / "leaky" / "report.json").is_file()
