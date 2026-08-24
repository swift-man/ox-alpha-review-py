from pathlib import Path

from reviewbot_common.domain import ReviewPathFilter
from reviewbot_common.infrastructure.reviewbot_config import (
    _CONFIG_MAX_BYTES,
    load_review_path_filter,
)


def test_directory_pattern_with_trailing_slash_matches_children() -> None:
    path_filter = ReviewPathFilter(exclude=("docs/",))

    assert not path_filter.allows("docs/guide/getting-started.md")
    assert path_filter.allows("src/docs/guide.md")


def test_always_review_directory_pattern_overrides_exclude() -> None:
    path_filter = ReviewPathFilter(
        exclude=("docs/",),
        always_review=("docs/important/",),
    )

    assert path_filter.allows("docs/important/readme.md")


def test_large_reviewbot_config_is_ignored(tmp_path: Path) -> None:
    config = tmp_path / ".reviewbot.yml"
    config.write_text("x" * (_CONFIG_MAX_BYTES + 1), encoding="utf-8")

    path_filter = load_review_path_filter(tmp_path)

    assert path_filter == ReviewPathFilter.allow_all()
