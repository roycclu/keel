import pytest

from keel.cli import _build_parser


def test_top_level_help_lists_commands_and_examples() -> None:
    help_text = _build_parser().format_help()

    assert "keel run --max-steps 10 --dry-run" in help_text
    assert "keel workflow TASK_ID --watch" in help_text
    assert "Run `keel COMMAND --help`" in help_text


def test_command_help_exits_without_starting_application(capsys) -> None:
    with pytest.raises(SystemExit) as exit_info:
        _build_parser().parse_args(["run", "--help"])

    assert exit_info.value.code == 0
    output = capsys.readouterr().out
    assert "Advance actionable tasks through the workflow." in output
    assert "--max-steps MAX_STEPS" in output
    assert "--dry-run" in output


def test_discovery_tag_limit_is_bounded() -> None:
    parser = _build_parser()

    assert parser.parse_args(["discover"]).tags_per_page is None
    assert parser.parse_args(["discover", "--tags-per-page", "5"]).tags_per_page == 5
    with pytest.raises(SystemExit):
        parser.parse_args(["discover", "--tags-per-page", "11"])
