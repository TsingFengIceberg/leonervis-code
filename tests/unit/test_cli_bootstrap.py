import pytest

from leonervis_code import __version__
from leonervis_code.cli.main import main


def test_package_version_is_declared() -> None:
    assert __version__ == "0.1.0"


def test_cli_without_runtime_exits_with_error(capsys) -> None:
    with pytest.raises(SystemExit) as error:
        main()

    assert error.value.code == 2
    captured = capsys.readouterr()
    assert "Harness runtime has not been implemented yet" in captured.err
