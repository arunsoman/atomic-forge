from atomic_forge.patch import apply_search_replace, looks_like_search_replace, parse_hunks


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
