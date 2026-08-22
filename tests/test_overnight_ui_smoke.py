# tests/test_overnight_ui_smoke.py — Home + Generate render the overnight
# surfaces without exceptions, with and without data/optimizer/ present.
#
# Pattern: AppTest boots the REAL entrypoint (code/app.py) so st.navigation
# registers every page and st.page_link resolves (running a page script
# directly dies in page_link with KeyError: 'url_pathname'), then
# switch_page() onto the page under test. data_dir is seeded into
# session_state BEFORE the first run — app.py only sets it when absent.

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).parent / "fixtures" / "optimizer"
PAGES = ["pages/home.py", "pages/generate.py"]


def _boot(page: str, data_dir: Path) -> AppTest:
    at = AppTest.from_file(str(ROOT / "code" / "app.py"), default_timeout=180)
    at.session_state["data_dir"] = str(data_dir)
    at.switch_page(page)
    at.run()
    return at


def _texts(at: AppTest) -> str:
    parts = [str(getattr(el, "value", "")) for el in at.markdown]
    parts += [str(getattr(el, "value", "")) for el in at.caption]
    parts += [str(getattr(el, "body", "")) for el in at.subheader]
    return "\n".join(parts)


@pytest.mark.parametrize("page", PAGES)
def test_pages_render_without_optimizer_dir(page, tmp_path):
    at = _boot(page, tmp_path)
    assert not at.exception


@pytest.mark.parametrize("page", PAGES)
def test_pages_render_with_overnight_fixture(page, tmp_path):
    shutil.copytree(FIXTURE, tmp_path / "optimizer")
    at = _boot(page, tmp_path)
    assert not at.exception


def test_home_chip_not_set_without_fixture(tmp_path):
    at = _boot("pages/home.py", tmp_path)
    text = _texts(at)
    assert "Overnight optimizer" in text
    assert "no overnight generation yet" in text


def test_home_chip_reads_the_fixture(tmp_path):
    shutil.copytree(FIXTURE, tmp_path / "optimizer")
    at = _boot("pages/home.py", tmp_path)
    text = _texts(at)
    # fixture created 2026-08-19 → stale by wall clock; the stats still show
    assert "7 runs" in text
    assert "best 84.2" in text
    assert "+12.8 vs board" in text
    assert "Overnight brief" in text  # brief.md expander content


def test_generate_section_absent_without_fixture(tmp_path):
    at = _boot("pages/generate.py", tmp_path)
    assert "Overnight results" not in _texts(at)


def test_generate_section_renders_the_leaderboard(tmp_path):
    shutil.copytree(FIXTURE, tmp_path / "optimizer")
    at = _boot("pages/generate.py", tmp_path)
    text = _texts(at)
    assert "Overnight results" in text
    assert "Noise floor" in text
    assert "71.4" in text  # board baseline delta line
    assert "same generation" in text  # the delta names its frame
    assert "overnight-20260819-best" in text  # published caption → Compare
    assert "overnight-20260819-runner" in text
    frames = [df for df in at.dataframe]
    assert any("Composite" in df.value.columns for df in frames)
    # same-generation delta column: candidate minus THIS board's baseline
    lb = next(df.value for df in frames if "Δ vs board" in df.value.columns)
    assert round(float(lb["Δ vs board"].iloc[0]), 2) == 12.8  # 84.2 - 71.4


def test_generate_says_tie_when_all_deltas_sit_inside_noise(tmp_path):
    """The noise-aware caption: when every candidate's same-generation delta
    is inside ±spread, the ranking is a tie and the section says so."""
    shutil.copytree(FIXTURE, tmp_path / "optimizer")
    lb_path = (tmp_path / "optimizer" / "20260819-0230-9f3a7c21"
               / "leaderboard.json")
    board = json.loads(lb_path.read_text(encoding="utf-8"))
    board["noise_floor"]["spread_composite"] = 20.0  # swallows every delta
    lb_path.write_text(json.dumps(board), encoding="utf-8")
    at = _boot("pages/generate.py", tmp_path)
    text = _texts(at)
    assert "treat the ranking as a tie" in text
    assert "±20.00 noise floor" in text


# -- an interrupted night (state.json never reached `published`) --------------

def _interrupted_night(tmp_path: Path) -> str:
    shutil.copytree(FIXTURE, tmp_path / "optimizer")
    gen = "20260820-1900-5d421b01"   # newer than the fixture's generation
    gdir = tmp_path / "optimizer" / gen
    gdir.mkdir()
    shutil.copy(FIXTURE / "20260819-0230-9f3a7c21" / "leaderboard.json",
                gdir / "leaderboard.json")
    (gdir / "state.json").write_text(json.dumps(
        {"generation": gen, "phase": "arms_done",
         "updated": "2026-08-21T00:11:26", "arms_done": 6,
         "arms_planned": 6}), encoding="utf-8")
    return gen


def _warnings(at: AppTest) -> str:
    return "\n".join(str(getattr(el, "value", "")) for el in at.warning)


def test_home_chip_names_an_interrupted_night(tmp_path):
    gen = _interrupted_night(tmp_path)
    at = _boot("pages/home.py", tmp_path)
    assert not at.exception
    text = _texts(at)
    assert f"night {gen} interrupted in phase arms_done" in text
    assert "ATTENTION" in text
    assert f"--resume {gen}" in _warnings(at)


def test_generate_section_names_an_interrupted_night(tmp_path):
    gen = _interrupted_night(tmp_path)
    at = _boot("pages/generate.py", tmp_path)
    assert not at.exception
    assert "Overnight results" in _texts(at)
    assert f"--resume {gen}" in _warnings(at)
