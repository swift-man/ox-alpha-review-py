from reviewbot_common.infrastructure.diff_parser import parse_right_lines


def test_parse_right_lines_matches_standard_hunk_headers() -> None:
    patch = "@@ -1,2 +10,3 @@\n context\n-old\n+new\n"

    assert parse_right_lines(patch) == frozenset({10, 11})
