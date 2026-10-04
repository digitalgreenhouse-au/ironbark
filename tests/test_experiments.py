"""The Experimental switches: off by default, kept in settings, and a removed experiment can
never switch itself back on."""
import pytest

from ironbark import experiments
from ironbark.experiments import Experiment


@pytest.fixture
def trial(monkeypatch):
    listed = (Experiment("room_eq", "Room correction", "Evens out boomy notes.", "0.6.0"),)
    monkeypatch.setattr(experiments, "EXPERIMENTS", listed)
    return listed[0]


def test_off_by_default(trial):
    assert not experiments.enabled({}, trial.key)
    assert not experiments.enabled({"experiments": None}, trial.key)  # a fresh settings file
    assert experiments.switched_on({}) == []


def test_switching_on_is_saved_in_the_settings(trial):
    prefs = {}
    experiments.set_enabled(prefs, trial.key, True)
    assert prefs["experiments"] == {"room_eq": True}
    assert experiments.enabled(prefs, trial.key) and experiments.switched_on(prefs) == [trial]
    experiments.set_enabled(prefs, trial.key, False)
    assert not experiments.enabled(prefs, trial.key)


def test_a_removed_experiment_stays_off_and_is_tidied_away(trial):
    prefs = {"experiments": {"room_eq": True, "old_idea": True}}
    assert not experiments.enabled(prefs, "old_idea")  # rolled in or dropped since: ignored
    experiments.tidy(prefs)
    assert prefs["experiments"] == {"room_eq": True}
    with pytest.raises(KeyError):
        experiments.set_enabled(prefs, "old_idea", True)


def test_a_damaged_setting_is_treated_as_all_off(trial):
    prefs = {"experiments": "garbage"}
    assert not experiments.enabled(prefs, trial.key)
    experiments.tidy(prefs)
    assert prefs["experiments"] == {}


def test_the_shipped_list_is_well_formed():
    keys = [e.key for e in experiments.EXPERIMENTS]
    assert len(keys) == len(set(keys))
    for e in experiments.EXPERIMENTS:
        assert e.key.isidentifier() and e.title and e.description and e.since
