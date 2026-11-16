"""Pure helpers in the map pipeline — no network, no tokens."""
import pytest

from backend.pipeline import _primary_genre


@pytest.mark.parametrize("tags,expected", [
    (["death metal", "doom"], "metal"),
    (["uk drill", "grime"], "hip-hop"),
    (["deep house", "techno"], "electronic"),
    (["shoegaze"], "rock"),
    (["bedroom pop"], "pop"),
    (["bebop"], "jazz"),
    (["baroque"], "classical"),
    (["bluegrass"], "folk"),
    (["reggaeton"], "latin"),
    (["bollywood"], "world"),
])
def test_known_tags_map_to_their_bucket(tags, expected):
    assert _primary_genre(tags) == expected


def test_no_tags_is_other():
    assert _primary_genre([]) == "other"


def test_first_match_wins():
    """Bucket order matters: 'metal' is checked before 'rock'."""
    assert _primary_genre(["metal", "rock"]) == "metal"


@pytest.mark.parametrize("tags,bucket,why", [
    (["soul jazz"], "hip-hop", "'soul' is a hip-hop keyword and that bucket is checked first"),
    (["k-pop"], "pop", "'pop' matches before the world bucket is reached"),
])
def test_known_overlaps_are_pinned_not_fixed(tags, bucket, why):
    """Documented quirks of the first-match-wins ordering.

    Changing the order would move existing tracks on everyone's map, so these
    pin current behaviour rather than asserting what is ideal.
    """
    assert _primary_genre(tags) == bucket, why


def test_unknown_short_tag_is_kept_as_a_label():
    assert _primary_genre(["sea shanty"]) == "sea shanty"


def test_unknown_long_tag_falls_back_to_other():
    assert _primary_genre(["a very long genre name that nobody would ever use"]) == "other"
