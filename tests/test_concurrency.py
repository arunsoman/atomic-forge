from atomic_forge.concurrency import AdaptiveConcurrencyLimiter


def test_ramps_up_on_success():
    lim = AdaptiveConcurrencyLimiter(start=1, ceiling=4)
    assert lim.limit == 1
    lim.record_success()
    assert lim.limit == 2
    lim.record_success()
    assert lim.limit == 3


def test_does_not_exceed_ceiling():
    lim = AdaptiveConcurrencyLimiter(start=3, ceiling=4)
    lim.record_success()
    lim.record_success()
    lim.record_success()
    assert lim.limit == 4


def test_steps_down_by_two_on_rate_limit():
    lim = AdaptiveConcurrencyLimiter(start=5, ceiling=16, floor=1)
    lim.record_rate_limited()
    assert lim.limit == 3
    assert lim.rate_limit_events == 1


def test_never_below_floor():
    lim = AdaptiveConcurrencyLimiter(start=2, ceiling=16, floor=1)
    lim.record_rate_limited()
    assert lim.limit == 1
    lim.record_rate_limited()
    assert lim.limit == 1


def test_acquire_release_tracks_active():
    lim = AdaptiveConcurrencyLimiter(start=2, ceiling=4)
    lim.acquire()
    assert lim.active == 1
    lim.release()
    assert lim.active == 0
