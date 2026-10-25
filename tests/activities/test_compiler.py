import pytest

from awr.sim.activities.compiler import populate


@pytest.mark.parametrize(
    "options",
    [
        {"seed": True},
        {"seed": -1},
        {"people": -1},
        {"people": 0, "vehicles": 0},
        {"extras": []},
        {"extras": {"person": 1}},
        {"extras": {"dog": True}},
        {"vehicle_types": []},
        {"vehicle_types": ["flying-car"]},
    ],
)
def test_invalid_population_requests_fail_before_scene_access(options):
    with pytest.raises(ValueError):
        populate({}, **options)


def test_vehicle_selection_click_order_does_not_change_seeded_population():
    from lifelib import ring_population

    network = ring_population(1)
    network["spawn_nodes"] = {"vehicle": list(network["nodes"])}
    for edge in network["edges"].values():
        edge.update(profiles=["vehicle"], clearance_m=10)
    for facility in network["facilities"].values():
        facility.update(profiles=["vehicle"], kind="parking", indoor=False)
    first = populate(network, 81, 0, 2, vehicle_types=["sedan", "suv"])
    reordered = populate(network, 81, 0, 2, vehicle_types=["suv", "sedan"])
    assert first == reordered
