"""Smoke run of each recording in replay mode (print-only: no model, no network)."""

from pathlib import Path

import pytest

from agent import recorder, runner
from agent.scenes import SCENES

ROOT = Path(__file__).resolve().parent.parent
RECORDINGS = sorted((ROOT / "recordings").glob("*.jsonl")) + sorted((ROOT / "tests" / "fixtures").glob("*.jsonl"))


def test_scene_ids_and_keys_unique():
    assert len({s["id"] for s in SCENES}) == len(SCENES)
    assert len({s["key"] for s in SCENES}) == len(SCENES)


@pytest.mark.parametrize("path", RECORDINGS, ids=[p.name for p in RECORDINGS])
def test_recording_replays(path):
    lines = recorder.load(path)
    assert lines[0]["type"] == "meta"
    assert any(l["type"] == "turn" for l in lines)
    runner.main(["--replay", str(path), "--print-only", "--speed", "0"])
