"""Concurrent bands=true requests for one slate must run the PyMC sampler exactly once."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from fairline.api import services as svc


def test_concurrent_band_requests_share_one_sampler_run(monkeypatch):
    calls, running, overlap = [], [0], [False]
    guard = threading.Lock()

    def fake_sample(conn, config, pool, day, event_ids):
        with guard:
            running[0] += 1
            overlap[0] |= running[0] > 1
        calls.append((pool, day))
        time.sleep(0.2)
        with guard:
            running[0] -= 1
        return {"healthy": True, "rows": {}}

    monkeypatch.setattr(svc, "_sample_bands", fake_sample)
    monkeypatch.setattr(svc, "_band_cache", {})
    slates = [("ENG", "2025-01-04")] * 6 + [("ESP", "2025-01-04")] * 2
    with ThreadPoolExecutor(8) as ex:
        outs = list(ex.map(lambda k: svc._bands(None, None, k[0], k[1], []), slates))
    assert sorted(calls) == [("ENG", "2025-01-04"), ("ESP", "2025-01-04")]
    assert not overlap[0]  # never two samplers at once, even for different slates
    assert all(o["healthy"] for o in outs)
