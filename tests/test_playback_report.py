import collections
from types import SimpleNamespace
from unittest import mock

from ironbark.engine import Health, OutputInfo, _Session
from ironbark.proof import Proof
from ironbark.tools.details import describe_events, describe_health, describe_proof

EXCLUSIVE = OutputInfo("DAC", 44100, None, True)
SHARED = OutputInfo("DAC", 44100, 48000, False)


def test_proven_bit_perfect_only_when_exclusive_untouched_and_all_checked():
    proof = Proof(sent=60.0, checked=60.0)
    text, tone = describe_proof(proof, EXCLUSIVE, 1.0, 1.0)
    assert text.startswith("✓ Bit-perfect, proven") and tone == "good"
    assert describe_proof(proof, SHARED, 1.0, 1.0)[1] == "plain"  # Windows mixes it next
    assert describe_proof(proof, EXCLUSIVE, 1.0, 1.0, lossy=True)[1] == "plain"


def test_a_volume_change_is_never_called_bit_perfect():
    proof = Proof(sent=60.0, checked=60.0, min_gain=0.8, changed_from=12.0)
    text, tone = describe_proof(proof, EXCLUSIVE, 0.8, 1.0)
    assert "volume" in text and "not bit-perfect" in text and "0:12" in text and tone == "warn"


def test_a_mismatch_is_reported_with_where():
    text, tone = describe_proof(Proof(sent=60, checked=60, mismatch_at=83.0), EXCLUSIVE, 1.0, 1.0)
    assert text.startswith("✗") and "1:23" in text and tone == "warn"


def test_health_wording():
    calm = Health(buffer_low=2.0, driver_low=0.1, longest_wait=0.1, usual_wait=0.093, slowest_read=0.001)
    assert describe_health(calm, 0)[1] == "good"
    assert describe_health(Health(buffer_low=0.3, near_misses=1), 0)[0].startswith("⚠ 1 near miss")
    assert describe_health(calm, 2)[0].startswith("⚠ 2 dropouts")


def _session():
    return SimpleNamespace(_events=collections.deque(maxlen=50), _track_name="a.flac", position=10.0)


def test_a_stutter_is_one_event_with_a_count():
    s = _session()
    with mock.patch("ironbark.engine.time.time", side_effect=[100.0, 100.5, 101.0, 109.0]):
        for _ in range(4):
            _Session._event(s, "dropout", "slow drive")
    assert [e.count for e in s._events] == [3, 1]  # the 4th came 8 s later: a new event
    assert "dropout ×3" in describe_events(list(s._events))[0]


def test_no_events_is_good_news():
    assert describe_events([])[1] == "good"
