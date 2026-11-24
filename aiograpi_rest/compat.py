"""Temporary patches over released aiograpi, each to be removed once a release carries the fix.

- Web stories with a location sticker whose location has only a ``pk`` (no ``name``) fail
  ``Location`` validation and take the whole ``users_stories_gql`` batch down (500 on
  ``GET /story/users``). Upstream fix: subzeroid/aiograpi, "fix: accept web story location
  stickers without a name". Remove this patch when the pinned aiograpi includes it.
"""

from aiograpi import extractors
from aiograpi.mixins import story as story_mixin

_original_extract_story_web = extractors.extract_story_web


def _fill_missing_location_names(data):
    if not isinstance(data, dict):
        return data
    stickers = data.get("story_locations")
    if not isinstance(stickers, list) or not any(
        isinstance(s, dict) and isinstance(s.get("location"), dict) and s["location"].get("name") is None
        for s in stickers
    ):
        return data
    fixed = dict(data)
    fixed["story_locations"] = [
        {**s, "location": {**s["location"], "name": ""}}
        if isinstance(s, dict) and isinstance(s.get("location"), dict) and s["location"].get("name") is None
        else s
        for s in stickers
    ]
    return fixed


def extract_story_web(data):
    return _original_extract_story_web(_fill_missing_location_names(data))


def apply() -> None:
    """Install the patches (idempotent)."""
    if getattr(extractors.extract_story_web, "__aiograpi_rest_compat__", False):
        return
    extract_story_web.__aiograpi_rest_compat__ = True  # type: ignore[attr-defined]
    extractors.extract_story_web = extract_story_web
    # users_stories_gql calls the name imported into the story mixin module.
    story_mixin.extract_story_web = extract_story_web
