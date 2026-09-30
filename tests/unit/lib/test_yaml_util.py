# SPDX-FileCopyrightText: 2025 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Tests for the centralised YAML I/O wrapper (ruamel.yaml round-trip)."""

from __future__ import annotations

import re

import pytest

from terok.lib.util.yaml import (
    YAMLError,
    append_entry,
    append_section,
    dump,
    dump_like,
    edit_in_place,
    load,
    put_key,
    set_entries,
)


class TestLoad:
    """Tests for ``load``."""

    def test_plain_dict(self) -> None:
        assert load("key: value") == {"key": "value"}

    def test_returns_none_on_empty(self) -> None:
        assert load("") is None

    def test_returns_none_on_whitespace_only(self) -> None:
        assert load("   \n\n  ") is None

    def test_nested_structure(self) -> None:
        text = "parent:\n  child: 42\n  list:\n    - a\n    - b\n"
        result = load(text)
        assert result["parent"]["child"] == 42
        assert result["parent"]["list"] == ["a", "b"]

    def test_raises_yaml_error_on_malformed(self) -> None:
        with pytest.raises(YAMLError):
            load("{{invalid yaml::")

    def test_plain_list(self) -> None:
        result = load("- one\n- two\n- three\n")
        assert result == ["one", "two", "three"]

    def test_scalar_value(self) -> None:
        assert load("42") == 42

    def test_boolean_values(self) -> None:
        data = load("flag: true\nother: false\n")
        assert data["flag"] is True
        assert data["other"] is False

    def test_multiline_string(self) -> None:
        text = "desc: |\n  line one\n  line two\n"
        data = load(text)
        assert "line one" in data["desc"]
        assert "line two" in data["desc"]

    def test_unicode_content(self) -> None:
        data = load("name: Jíří\nemoji: 🚀\n")
        assert data["name"] == "Jíří"
        assert data["emoji"] == "🚀"


class TestDump:
    """Tests for ``dump``."""

    def test_plain_dict(self) -> None:
        result = dump({"key": "value"})
        assert "key: value" in result

    def test_key_order_preserved(self) -> None:
        data = {"zebra": 1, "alpha": 2, "middle": 3}
        result = dump(data)
        lines = [ln for ln in result.strip().splitlines() if ln.strip()]
        keys = [ln.split(":")[0] for ln in lines]
        assert keys == ["zebra", "alpha", "middle"]

    def test_default_flow_style(self) -> None:
        data = {"items": [1, 2, 3]}
        result = dump(data, default_flow_style=True)
        assert "{" in result or "[" in result

    def test_flow_style_preserves_quotes(self) -> None:
        """Flow-style emitter must preserve quotes like the default emitter."""
        data = load("key: 'quoted value'\n")
        result = dump(data, default_flow_style=True)
        assert "'quoted value'" in result or '"quoted value"' in result

    def test_dumps_none(self) -> None:
        result = dump(None)
        assert "null" in result

    def test_dumps_list(self) -> None:
        result = dump(["a", "b", "c"])
        assert "- a" in result
        assert "- b" in result

    def test_dumps_nested_dict(self) -> None:
        data = {"outer": {"inner": "value"}}
        result = dump(data)
        assert "outer:" in result
        assert "inner: value" in result

    def test_trailing_newline(self) -> None:
        """Dump output ends with a newline (standard YAML convention)."""
        result = dump({"key": "value"})
        assert result.endswith("\n")


class TestRoundTrip:
    """Tests for comment preservation across load → modify → dump."""

    def test_comments_preserved(self) -> None:
        text = "# top comment\nkey: value  # inline\nother: 42\n"
        data = load(text)
        data["other"] = 99
        result = dump(data)
        assert "# top comment" in result
        assert "# inline" in result
        assert "other: 99" in result

    def test_key_order_from_source(self) -> None:
        text = "zebra: 1\nalpha: 2\n"
        data = load(text)
        result = dump(data)
        lines = [ln for ln in result.strip().splitlines() if ln.strip()]
        keys = [ln.split(":")[0] for ln in lines]
        assert keys == ["zebra", "alpha"]

    def test_dict_isinstance(self) -> None:
        """CommentedMap is a dict subclass — isinstance works."""
        data = load("key: value")
        assert isinstance(data, dict)

    def test_dict_operations(self) -> None:
        """CommentedMap supports standard dict operations."""
        data = load("key: value")
        assert data.get("key") == "value"
        assert data.get("missing", "default") == "default"
        data.setdefault("new", 123)
        assert data["new"] == 123

    def test_quoted_strings_preserved(self) -> None:
        """Quoted strings survive a round-trip without losing their quotes."""
        text = "single: 'hello'\ndouble: \"world\"\n"
        data = load(text)
        result = dump(data)
        assert "'hello'" in result
        assert '"world"' in result

    def test_add_key_preserves_existing(self) -> None:
        """Adding a new key does not disturb existing comments or order."""
        text = "# config\nalpha: 1\nbeta: 2  # important\n"
        data = load(text)
        data["gamma"] = 3
        result = dump(data)
        assert "# config" in result
        assert "# important" in result
        assert "gamma: 3" in result
        # Original key order maintained
        lines = [ln.split(":")[0].strip() for ln in result.splitlines() if ":" in ln]
        assert lines.index("alpha") < lines.index("beta") < lines.index("gamma")

    def test_remove_key_preserves_rest(self) -> None:
        """Removing a key preserves comments on remaining keys."""
        text = "keep: 1  # stay\nremove: 2\nother: 3  # also stay\n"
        data = load(text)
        del data["remove"]
        result = dump(data)
        assert "# stay" in result
        assert "# also stay" in result
        assert re.search(r"(?m)^\s*remove\s*:", result) is None

    def test_or_empty_fallback_returns_plain_dict(self) -> None:
        """``load("") or {}`` returns a plain dict (the common fallback pattern)."""
        result = load("") or {}
        assert result == {}
        assert isinstance(result, dict)

    def test_multiple_round_trips_stable(self) -> None:
        """Data survives multiple load → dump cycles without drift."""
        text = "# header\nname: test  # project name\nversion: 1\n"
        data = load(text)
        for _ in range(5):
            text = dump(data)
            data = load(text)
        assert "# header" in text
        assert "# project name" in text
        assert "name: test" in text


class TestThreadSafety:
    """Concurrent ``load`` / ``dump`` from worker threads must not race.

    Regression: a shared module-level ``YAML()`` instance carries
    composer/parser/scanner state, so concurrent calls scrambled its event
    stream and surfaced as ``'MappingEndEvent' object has no attribute
    'anchor'`` deep inside the Textual TUI's shield-state worker.
    """

    SAMPLE = (
        "# top comment\n"
        "name: terok\n"
        "git:\n"
        "  remote: origin\n"
        "  branch: master  # default\n"
        "tasks:\n"
        "  - id: 1\n"
        "    title: 'first'\n"
        "  - id: 2\n"
        '    title: "second"\n'
        "nested:\n"
        "  a:\n"
        "    b:\n"
        "      c: 42\n"
    )

    def test_concurrent_load(self) -> None:
        from concurrent.futures import ThreadPoolExecutor

        def work() -> int:
            for _ in range(50):
                data = load(self.SAMPLE)
                assert data["name"] == "terok"
                assert data["nested"]["a"]["b"]["c"] == 42
            return 0

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(work) for _ in range(8)]
            for f in futures:
                f.result()  # re-raises any exception from a worker

    def test_concurrent_load_and_dump(self) -> None:
        from concurrent.futures import ThreadPoolExecutor

        def loader() -> None:
            for _ in range(50):
                load(self.SAMPLE)

        def dumper() -> None:
            data = load(self.SAMPLE)
            for _ in range(50):
                dump(data)

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(loader if i % 2 else dumper) for i in range(8)]
            for f in futures:
                f.result()


class TestYAMLError:
    """Tests for ``YAMLError`` re-export."""

    def test_yaml_error_is_exception(self) -> None:
        assert issubclass(YAMLError, Exception)

    def test_yaml_error_catchable(self) -> None:
        """YAMLError can be caught in except clauses."""
        with pytest.raises(YAMLError):
            load(":\n  :\n    - :\n  bad: [")

    def test_yaml_error_on_duplicate_keys(self) -> None:
        """ruamel.yaml round-trip mode raises on duplicate keys by default."""
        with pytest.raises(YAMLError):
            load("key: 1\nkey: 2\n")


class TestInPlaceEdits:
    """``dump_like`` plus the entry helpers: an edit changes its entry and nothing else."""

    @pytest.mark.parametrize(
        "source",
        [
            pytest.param("a:\n  list:\n    - x\n    - y\n  key: v\n", id="dash-indented"),
            pytest.param("a:\n  list:\n  - x\n  - y\n  key: v\n", id="dash-flush"),
            pytest.param("a:\n    list:\n        - x\n    key: v\n", id="four-space"),
            pytest.param("# head\n---\na:\n  key: v\n", id="document-start"),
            pytest.param(
                "a:\n  long: " + "word " * 30 + "end\n  flow: [x, y]  # kept\n", id="long-line"
            ),
            pytest.param(
                "a:\n  snippet: |\n    RUN x\n    - not: structure\n  list:\n    - x\n",
                id="block-scalar",
            ),
        ],
    )
    def test_dump_like_reproduces_an_unedited_source(self, source: str) -> None:
        """Indentation, long lines, ``---`` and comments come back byte for byte."""
        assert dump_like(load(source), source) == source

    def test_plain_dump_would_have_reindented(self) -> None:
        """The reason ``dump_like`` exists: ``dump`` normalises block sequences."""
        source = "a:\n  list:\n    - x\n"
        assert dump(load(source)) != source

    def test_append_entry_lands_above_trailing_comment_lines(self) -> None:
        source = "a:\n  list:\n    - x   # first\n\n# about b\nb: 1\n"
        data = load(source)
        append_entry(data["a"]["list"], "y")
        assert dump_like(data, source) == (
            "a:\n  list:\n    - x   # first\n    - y\n\n# about b\nb: 1\n"
        )

    def test_append_entry_keeps_a_flow_list_flow(self) -> None:
        source = "a:\n  flow: []  # none yet\n"
        data = load(source)
        append_entry(data["a"]["flow"], "x")
        assert dump_like(data, source).startswith("a:\n  flow: [x]")

    def test_append_entry_after_a_mapping_item(self) -> None:
        source = "list:\n  - host: a\n    why: b\n# tail\n"
        data = load(source)
        append_entry(data["list"], {"host": "c", "why": "d"})
        assert dump_like(data, source) == (
            "list:\n  - host: a\n    why: b\n  - host: c\n    why: d\n# tail\n"
        )

    def test_put_key_updates_an_existing_key_where_it_is(self) -> None:
        source = "a:\n  list:   # later\n  b: 1  # eol\n"
        data = load(source)
        put_key(data["a"], "list", ["x"])
        assert dump_like(data, source) == "a:\n  list:   # later\n    - x\n  b: 1  # eol\n"

    def test_put_key_adds_a_new_key_after_the_last_one(self) -> None:
        source = "a:\n  b: 1\n\n# commented-out c:\n"
        data = load(source)
        put_key(data["a"], "list", ["x"])
        assert dump_like(data, source) == "a:\n  b: 1\n  list:\n    - x\n\n# commented-out c:\n"

    def test_put_key_can_separate_a_new_section(self) -> None:
        source = "a:\n  b: 1\n"
        data = load(source)
        put_key(data, "c", {"d": 2}, blank_line_before=True)
        assert dump_like(data, source) == "a:\n  b: 1\n\nc:\n  d: 2\n"

    def test_set_entries_refills_a_block_list_keeping_its_surroundings(self) -> None:
        source = "a:\n  list:   # chosen\n    - x\n    - y\n\n# about b\nb: 1\n"
        data = load(source)
        set_entries(data["a"], "list", ["p", "q", "r"])
        assert dump_like(data, source) == (
            "a:\n  list:   # chosen\n    - p\n    - q\n    - r\n\n# about b\nb: 1\n"
        )

    def test_set_entries_empties_a_block_list_to_flow(self) -> None:
        source = "a:\n  list:\n    - x\n# about b\nb: 1\n"
        data = load(source)
        set_entries(data["a"], "list", [])
        assert dump_like(data, source) == "a:\n  list: []\n# about b\nb: 1\n"


class TestEditInPlace:
    """``edit_in_place``: the edit's lines carried into the text, every other byte kept."""

    def test_an_unchanged_document_is_the_source_itself(self) -> None:
        source = "a:  # odd spacing kept\n  flag: True\n  none: ~\n"
        assert edit_in_place(source, load(source)) == source

    def test_only_the_changed_lines_are_carried(self) -> None:
        """A dump would write ``true`` and nothing for ``~``; those lines are not the edit's."""
        source = "a:\n  flag: True\n  none: ~\nb:\n  list:\n    - x\n"
        data = load(source)
        append_entry(data["b"]["list"], "y")
        assert edit_in_place(source, data, section="b") == source + "    - y\n"

    def test_the_section_keeps_its_own_indentation(self) -> None:
        """The file's first list is flush; the edited section's is indented — and stays so."""
        source = "a:\n  list:\n  - p\nb:\n  list:\n    - x\n"
        data = load(source)
        append_entry(data["b"]["list"], "y")
        assert edit_in_place(source, data, section="b") == source + "    - y\n"

    def test_a_new_list_in_a_section_follows_the_files_sequence_layout(self) -> None:
        """A section with no list of its own takes the file's dash style for a new one."""
        source = "a:\n  list:\n  - p\nb:\n  key: v\n"
        data = load(source)
        put_key(data["b"], "list", ["x"])
        assert edit_in_place(source, data, section="b") == source + "  list:\n  - x\n"

    def test_neighbours_spelled_their_own_way_are_kept_by_position(self) -> None:
        source = "a:\n  one: [ p ]\n  two: [ q ]\n  three: [ r ]\n"
        data = load(source)
        append_entry(data["a"]["two"], "s")
        assert edit_in_place(source, data, section="a") == (
            "a:\n  one: [ p ]\n  two: [q, s]\n  three: [ r ]\n"
        )

    def test_a_change_it_cannot_place_is_refused(self) -> None:
        """The neighbours are spelled differently and the lines no longer correspond."""
        source = "a:\n  one: [ p,\n         x ]\n  two: [ q ]\nb: 1\n"
        data = load(source)
        append_entry(data["a"]["one"], "s")
        with pytest.raises(ValueError, match="not in the file"):
            edit_in_place(source, data, section="a")

    def test_a_document_that_is_no_mapping_is_refused(self) -> None:
        with pytest.raises(ValueError, match="no mapping"):
            edit_in_place("- a\n- b\n", ["a", "b", "c"])

    def test_a_result_that_would_mean_something_else_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The last guard: whatever the placement did, the text must load as the edit."""
        import terok.lib.util.yaml as yaml_mod

        source = "a:\n  list:\n    - x\n"
        data = load(source)
        append_entry(data["a"]["list"], "y")
        monkeypatch.setattr(yaml_mod, "_splice", lambda *_args: source)  # placed nothing
        with pytest.raises(ValueError, match="would change what it means"):
            edit_in_place(source, data, section="a")

    def test_a_result_that_no_longer_loads_is_refused(self) -> None:
        """``b: null`` stays as written, and a block under it would break the file."""
        source = "a: 1\nb: null\n"
        data = load(source)
        put_key(data, "b", {"c": 2})
        with pytest.raises(ValueError, match="would change what it means"):
            edit_in_place(source, data, section="b")

    def test_a_list_spelled_over_several_lines_is_replaced_whole(self) -> None:
        """When the changed entry is all there is between its anchors, its own lines give way."""
        source = "a: [ x,\n     y ]\nb: 1\n"
        data = load(source)
        append_entry(data["a"], "z")
        assert edit_in_place(source, data) == "a: [x, y, z]\nb: 1\n"

    def test_a_new_key_after_an_anchor_leaves_a_respelled_neighbour_below(self) -> None:
        source = "a: 1\nb: [ x,\n     y ]\n"
        data = load(source)
        data.insert(1, "n", 5)
        assert edit_in_place(source, data) == "a: 1\nn: 5\nb: [ x,\n     y ]\n"

    def test_a_new_key_before_an_anchor_leaves_a_respelled_neighbour_above(self) -> None:
        source = "a: [ x,\n     y ]\nb: 1\n"
        data = load(source)
        data.insert(1, "n", 5)
        assert edit_in_place(source, data) == "a: [ x,\n     y ]\nn: 5\nb: 1\n"

    def test_a_new_first_key_opens_the_document(self) -> None:
        source = "a:\n  - x\nb: 2\n"
        data = load(source)
        data.insert(0, "z", 0)
        assert edit_in_place(source, data) == "z: 0\n" + source

    def test_a_section_the_file_lacks_takes_the_files_layout(self) -> None:
        source = "a: 1\n"
        data = load(source)
        put_key(data, "b", {"c": 1})
        assert edit_in_place(source, data, section="b") == "a: 1\nb:\n  c: 1\n"

    def test_a_key_after_a_block_scalar_goes_above_the_comment_that_follows_it(self) -> None:
        """A block scalar ends with its own newline, so every comment line after it trails."""
        source = "a:\n  s: |\n    hello\n  # tail\n"
        data = load(source)
        put_key(data["a"], "t", 1)
        assert edit_in_place(source, data, section="a") == (
            "a:\n  s: |\n    hello\n  t: 1\n# tail\n"
        )


class TestDumpLike:
    """``dump_like``: a dump laid out the way the text it came from is."""

    def test_a_document_start_without_a_newline_keeps_its_own_line(self) -> None:
        assert dump_like(load("---"), "---") == "---\nnull\n...\n"

    def test_a_dash_with_nothing_after_it_gives_way_to_the_default_layout(self) -> None:
        """No room for ``- `` is no layout to copy: the defaults lay the list out instead."""
        source = "a:\n  -\n    b: 1\n"
        assert dump_like(load(source), source) == "a:\n  - b: 1\n"


class TestAppendSection:
    """``append_section``: a new top-level section, and every other byte as it was."""

    def test_it_goes_above_the_files_closing_comments(self) -> None:
        source = "a:\n  flag: True\n\n# b:\n#   c: 2\n"
        assert append_section(source, "b", {"c": 3}) == (
            "a:\n  flag: True\n\nb:\n  c: 3\n\n# b:\n#   c: 2\n"
        )

    def test_a_commented_out_body_stays_with_its_key(self) -> None:
        source = "a:\n  # flag: true\n# about what follows\n"
        assert append_section(source, "b", {"c": 3}) == (
            "a:\n  # flag: true\n\nb:\n  c: 3\n# about what follows\n"
        )

    def test_it_takes_the_files_layout_and_line_ends(self) -> None:
        source = "a:\r\n  list:\r\n  - x\r\n...\r\n"
        assert append_section(source, "b", {"list": ["y"]}) == (
            "a:\r\n  list:\r\n  - x\r\n\r\nb:\r\n  list:\r\n  - y\r\n...\r\n"
        )

    def test_a_file_without_a_final_newline_keeps_it_that_way(self) -> None:
        assert append_section("a: 1", "b", 2) == "a: 1\n\nb: 2"

    @pytest.mark.parametrize("source", ["b: 1\n", "- a\n"], ids=["has-the-key", "not-a-mapping"])
    def test_it_refuses_what_it_cannot_add_to(self, source: str) -> None:
        with pytest.raises(ValueError):
            append_section(source, "b", 2)

    def test_a_result_that_no_longer_loads_is_refused(self) -> None:
        """A flow mapping takes no block section after it: the file would break."""
        with pytest.raises(ValueError, match="would change what the file means"):
            append_section("{a: 1}\n", "b", 2)
