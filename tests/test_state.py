"""Regression tests for src.state defensive corruption handling.

These guard against a class of catastrophic bugs: if posted.json is ever
silently treated as empty, the next run would re-post all 191 videos as
duplicates. The corruption path MUST raise loudly + back up the bad file.
"""
from __future__ import annotations
import json
import os

import pytest

from src import state


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """Point the state module at a temp directory for the test."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(state, "STATE_DIR", str(state_dir))
    monkeypatch.setattr(state, "POSTED_PATH", str(state_dir / "posted.json"))
    return state_dir


class TestLoadPosted:
    def test_missing_file_returns_empty(self, isolated_state):
        assert state.load_posted() == {}

    def test_valid_file_returns_data(self, isolated_state):
        path = isolated_state / "posted.json"
        path.write_text(json.dumps({"abc": {"name": "v.mp4"}}), encoding="utf-8")
        assert state.load_posted() == {"abc": {"name": "v.mp4"}}

    def test_corrupt_json_raises_and_backs_up(self, isolated_state):
        path = isolated_state / "posted.json"
        path.write_text("{ this is not valid json", encoding="utf-8")
        with pytest.raises(state.StateCorruptError):
            state.load_posted()
        # Original file is moved aside, not deleted.
        assert not path.exists()
        backups = list(isolated_state.glob("posted.json.corrupt-*"))
        assert len(backups) == 1
        # Backup contains the original bad content for manual recovery.
        assert "not valid json" in backups[0].read_text(encoding="utf-8")

    def test_wrong_type_raises_and_backs_up(self, isolated_state):
        path = isolated_state / "posted.json"
        # Valid JSON but list instead of object \u2014 shape corruption.
        path.write_text(json.dumps(["a", "b"]), encoding="utf-8")
        with pytest.raises(state.StateCorruptError):
            state.load_posted()
        assert not path.exists()
        assert len(list(isolated_state.glob("posted.json.corrupt-*"))) == 1


class TestSavePostedRoundTrip:
    def test_atomic_write(self, isolated_state):
        state.save_posted({"id1": {"name": "x.mp4"}})
        # No leftover .tmp after successful write.
        assert not (isolated_state / "posted.json.tmp").exists()
        loaded = state.load_posted()
        assert loaded == {"id1": {"name": "x.mp4"}}

    def test_mark_posted_round_trip(self, isolated_state):
        state.mark_posted(
            "fileXYZ", "vid.mp4", fb_post_id="123", fb_video_id="999",
            music_track={"id": "t1", "name": "Track"}, caption="hi",
        )
        loaded = state.load_posted()
        assert "fileXYZ" in loaded
        assert loaded["fileXYZ"]["fb_post_id"] == "123"
        assert loaded["fileXYZ"]["music_track"]["id"] == "t1"
        assert loaded["fileXYZ"]["caption"] == "hi"
        assert "posted_at" in loaded["fileXYZ"]
