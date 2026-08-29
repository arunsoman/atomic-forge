from atomic_forge.patch import (
    apply_hunks,
    apply_search_replace,
    looks_like_search_replace,
    parse_hunks,
    Hunk,
)


def test_simple_replace():
    content = "line1\nline2\nline3\n"
    llm_out = "<<<<<<< SEARCH\nline2\n=======\nCHANGED\n>>>>>>> REPLACE"
    new, err = apply_search_replace(content, llm_out)
    assert err == ""
    assert new == "line1\nCHANGED\nline3\n"


def test_no_match_never_applies_at_position_zero():
    content = "line1\nline2\n"
    llm_out = "<<<<<<< SEARCH\nnope not here\n=======\nX\n>>>>>>> REPLACE"
    new, err = apply_search_replace(content, llm_out)
    assert new is None
    assert "NO_MATCH" in err or "not found" in err


def test_ambiguous_match_rejected():
    content = "dup\ndup\n"
    llm_out = "<<<<<<< SEARCH\ndup\n=======\nX\n>>>>>>> REPLACE"
    new, err = apply_search_replace(content, llm_out)
    assert new is None
    assert "matches more than once" in err or "AMBIGUOUS" in err


def test_empty_search_rejected():
    content = "line1\n"
    llm_out = "<<<<<<< SEARCH\n\n=======\nX\n>>>>>>> REPLACE"
    new, err = apply_search_replace(content, llm_out)
    assert new is None


def test_multiple_disjoint_hunks_apply_together():
    content = "a\nb\nc\nd\n"
    llm_out = (
        "<<<<<<< SEARCH\na\n=======\nA\n>>>>>>> REPLACE\n\n"
        "<<<<<<< SEARCH\nd\n=======\nD\n>>>>>>> REPLACE"
    )
    new, err = apply_search_replace(content, llm_out)
    assert err == ""
    assert new == "A\nb\nc\nD\n"


def test_overlapping_hunks_rejected_as_disjoint_conflict():
    content = "one two three\n"
    llm_out = (
        "<<<<<<< SEARCH\none two\n=======\nX\n>>>>>>> REPLACE\n\n"
        "<<<<<<< SEARCH\ntwo three\n=======\nY\n>>>>>>> REPLACE"
    )
    new, err = apply_search_replace(content, llm_out)
    assert new is None


def test_marker_length_tolerance():
    # 6 '<' instead of the "standard" 7 — a real observed model failure mode.
    content = "line1\n"
    llm_out = "<<<<<< SEARCH\nline1\n======\nCHANGED\n>>>>>> REPLACE"
    new, err = apply_search_replace(content, llm_out)
    assert new == "CHANGED\n"


def test_crlf_fallback():
    content = "line1\r\nline2\r\n"
    llm_out = "<<<<<<< SEARCH\nline2\n=======\nX\n>>>>>>> REPLACE"
    new, err = apply_search_replace(content, llm_out)
    assert new is not None
    assert "X" in new


def test_line_number_prefix_stripped_as_last_resort():
    content = "def foo():\n    pass\n"
    llm_out = "<<<<<<< SEARCH\n    2\tpass\n=======\n    return 1\n>>>>>>> REPLACE"
    new, err = apply_search_replace(content, llm_out)
    assert new == "def foo():\n    return 1\n"


def test_no_hunks_found():
    new, err = apply_search_replace("x", "no markers here")
    assert new is None
    assert "no SEARCH/REPLACE" in err


def test_nested_then_straddling_hunks_all_conflict():
    # Interval-containment + straddle: a container hunk, a hunk nested
    # inside it, and a hunk that straddles the container but starts after
    # the nested one ends. A pairwise-ADJACENT overlap check (zip with the
    # next span) compares only (container,nested) and (nested,straddler)
    # and, since the nested hunk ends before the straddler starts, never
    # compares (container,straddler) -> so it wrongly classifies the
    # straddler as clean and would apply it over the container's region,
    # corrupting the output. The running-max sweep-line catches all three.
    content = "abcdefghijklmn"
    hunks = [
        Hunk(0, "abcdefghijklmn", "CONTAINER"),
        Hunk(1, "cd", "NESTED"),
        Hunk(2, "hijklmn", "STRADDLE"),
    ]
    result = apply_hunks(content, hunks, allow_partial=True)
    assert not result.success, result.new_content
    assert result.new_content is None
    # all three are entangled -> every one is a disjoint-conflict reject
    assert {o.hunk.index for o in result.rejected} == {0, 1, 2}


def test_straddler_not_flagged_when_truly_disjoint():
    # Sanity for the sweep-line: a span that starts after the running max end
    # is clean. A=[0,6] (ends at 6), B=[2,4] (nested, ends 4), C=[7,14]
    # (starts at 7 > 6) -> C overlaps nothing.
    content = "abcdefghijklmn"
    hunks = [
        Hunk(0, "abcdef", "A"),
        Hunk(1, "cd", "B"),
        Hunk(2, "hijklmn", "C"),
    ]
    result = apply_hunks(content, hunks, allow_partial=True)
    # C is the only clean hunk -> applied alone; A and B conflict.
    assert result.success
    assert result.new_content is not None
    assert {o.hunk.index for o in result.applied} == {2}
    assert {o.hunk.index for o in result.rejected} == {0, 1}


def test_looks_like_search_replace():
    assert looks_like_search_replace("<<<<<<< SEARCH\nx\n=======\ny\n>>>>>>> REPLACE")
    assert not looks_like_search_replace("```python\nprint(1)\n```")


def test_parse_hunks_order():
    text = (
        "<<<<<<< SEARCH\na\n=======\nA\n>>>>>>> REPLACE\n"
        "<<<<<<< SEARCH\nb\n=======\nB\n>>>>>>> REPLACE"
    )
    hunks = parse_hunks(text)
    assert [h.search for h in hunks] == ["a", "b"]
    assert [h.index for h in hunks] == [0, 1]
