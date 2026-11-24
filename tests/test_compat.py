from aiograpi import extractors
from aiograpi.mixins import story as story_mixin

from aiograpi_rest import compat


def _web_story(locations):
    return {
        "pk": 3100,
        "id": "3100_1",
        "code": "abc",
        "taken_at": 1795000000,
        "media_type": 1,
        "user": {"pk": "1", "username": "ana"},
        "image_versions2": {"candidates": [{"url": "https://example.com/t.jpg", "width": 10, "height": 10}]},
        "story_locations": locations,
    }


def test_web_story_with_nameless_location_is_extracted():
    compat.apply()
    payload = _web_story([
        {"location": {"pk": 2167925883460980}, "x": 0.5, "y": 0.4},
        {"location": {"pk": 213385402, "name": "Sao Paulo"}, "x": 0.5, "y": 0.6},
    ])

    story = story_mixin.extract_story_web(payload)

    assert [loc.location.pk for loc in story.locations] == [2167925883460980, 213385402]
    assert [loc.location.name for loc in story.locations] == ["", "Sao Paulo"]
    assert payload["story_locations"][0]["location"] == {"pk": 2167925883460980}


def test_apply_is_idempotent_and_patches_both_references():
    compat.apply()
    compat.apply()
    assert extractors.extract_story_web is compat.extract_story_web
    assert story_mixin.extract_story_web is compat.extract_story_web
