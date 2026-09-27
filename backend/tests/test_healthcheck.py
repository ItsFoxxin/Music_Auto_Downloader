from __future__ import annotations

import pytest

from foxden_music import healthcheck
from foxden_music.healthcheck import HealthcheckError, _require_non_root_runtime


@pytest.mark.parametrize(("uid", "gid"), [(0, 10001), (10001, 0)])
def test_runtime_wrapper_rejects_root_identity(monkeypatch, uid: int, gid: int) -> None:
    monkeypatch.setattr(healthcheck.os, "geteuid", lambda: uid, raising=False)
    monkeypatch.setattr(healthcheck.os, "getegid", lambda: gid, raising=False)
    with pytest.raises(HealthcheckError, match="effective"):
        _require_non_root_runtime()


def test_runtime_wrapper_accepts_non_root_identity(monkeypatch) -> None:
    monkeypatch.setattr(healthcheck.os, "geteuid", lambda: 10001, raising=False)
    monkeypatch.setattr(healthcheck.os, "getegid", lambda: 10001, raising=False)
    _require_non_root_runtime()
