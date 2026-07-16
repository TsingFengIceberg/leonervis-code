from __future__ import annotations

import io

from leonervis_code.cli.brand import BODY, HEAD, TAIL, display_path, render_banner, render_mark


def test_mark_has_five_rows_without_color() -> None:
    mark = render_mark(color=False)

    assert len(mark) == 5
    assert mark[0] == "█    █████  ███ "
    assert all("\x1b[" not in row for row in mark)


def test_colored_mark_resets_each_colored_character() -> None:
    mark = render_mark(color=True)

    assert "\x1b[38;2;166;90;24m█\x1b[0m" in mark[0]
    assert "\x1b[38;2;230;154;43m█\x1b[0m" in mark[0]
    assert "\x1b[38;2;255;224;154m█\x1b[0m" in mark[0]
    assert mark[0].endswith(" ")
    assert (TAIL, BODY, HEAD) == ((166, 90, 24), (230, 154, 43), (255, 224, 154))


def test_path_inside_home_uses_tilde(monkeypatch, tmp_path) -> None:
    home = tmp_path / "home"
    project = home / "Projects" / "leonervis-code"
    project.mkdir(parents=True)
    monkeypatch.setattr("leonervis_code.cli.brand.Path.home", lambda: home)

    assert display_path(project) == "~/Projects/leonervis-code"


def test_banner_has_version_status_and_display_path(tmp_path) -> None:
    banner = render_banner(version="0.1.0", cwd=tmp_path, color=False)

    assert "LEONERVIS CODE v0.1.0" in banner
    assert "Foundation 1B · bounded read_file tool loop" in banner
    assert str(tmp_path) in banner
    assert "\x1b[" not in banner


def test_color_is_disabled_for_noninteractive_output(monkeypatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)

    assert not __import__("leonervis_code.cli.brand", fromlist=["color_enabled"]).color_enabled(
        io.StringIO()
    )
