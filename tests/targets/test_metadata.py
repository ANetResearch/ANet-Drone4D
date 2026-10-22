import pytest

from awr.sim.targets.model import TargetError, merged_config, validate_config


def test_vehicle_metadata_is_editable_and_class_scoped():
    config = merged_config(
        {
            "class": "vehicle",
            "metadata": {"role": "delivery", "plate_text": "皖A 12345", "plate_region": "安徽", "vehicle_type": "van"},
        }
    )
    assert validate_config(config) == []
    for cls in ("person", "dog", "object"):
        with pytest.raises(TargetError):
            validate_config(merged_config({"class": cls, "metadata": {"plate_text": "ABC"}}))


@pytest.mark.parametrize(
    "patch",
    [
        {"plate_text": "ABC\n123"},
        {"plate_text": "a<script>"},
        {"vehicle_type": "spaceship"},
        {"url": "https://arbitrary.example"},
        {"role": "x" * 49},
    ],
)
def test_metadata_rejects_invalid_values(patch):
    with pytest.raises(TargetError):
        validate_config(merged_config({"class": "vehicle", "metadata": patch}))
