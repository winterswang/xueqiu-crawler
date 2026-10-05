from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import opencli_extractor


def test_user_articles_preflight_does_not_visit_xueqiu(monkeypatch):
    def fail_if_site_call(*_args, **_kwargs):
        raise AssertionError("preflight must not call _run")

    def fake_help(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="Usage: opencli xueqiu user-articles",
            stderr="",
        )

    monkeypatch.setattr(opencli_extractor, "_run", fail_if_site_call)
    monkeypatch.setattr(opencli_extractor.subprocess, "run", fake_help)

    assert opencli_extractor.is_user_articles_available() is True


def test_opencli_run_uses_shared_rate_limiter(monkeypatch):
    slots = []

    def fake_slot(_source, _args):
        slot = {"enabled": True, "waited_seconds": 0.0, "reserved_seconds": 2.0}
        slots.append(slot)
        return slot

    def fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout="[]", stderr=""
        )

    monkeypatch.setattr(opencli_extractor, "acquire_opencli_slot", fake_slot)
    monkeypatch.setattr(opencli_extractor.subprocess, "run", fake_run)
    monkeypatch.setattr(
        opencli_extractor,
        "record_opencli_call",
        lambda *args, **kwargs: None,
    )

    result = opencli_extractor._run("xueqiu", "whoami")

    assert result.returncode == 0
    assert len(slots) == 1
