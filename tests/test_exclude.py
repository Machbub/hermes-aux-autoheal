"""Tests for the never-probe exclude list."""
import json

import pytest

from hermes_aux_autoheal import exclude


def test_parse_accepts_dict_with_entries():
    data = {"version": 1, "entries": [
        {"provider": "VendorA", "model": "chat-v4", "task": "vision"},
        {"provider": "VendorB", "model": "chat-v4"},
    ]}
    out = exclude.parse(data)
    assert len(out) == 2
    assert out[0] == {"provider": "VendorA", "model": "chat-v4", "task": "vision"}
    assert out[1]["task"] == '*'          # absent task means every task


def test_parse_accepts_bare_list_and_slash_strings():
    out = exclude.parse([
        {"provider": "A", "model": "m1"},
        "B/m2",
        {"provider": "C"},                 # missing model -> skipped
        "no-slash-here",                   # not a pair -> skipped
        42,                                # not a dict/str -> skipped
    ])
    assert len(out) == 2
    assert out[1] == {"provider": "B", "model": "m2", "task": '*'}


def test_parse_unknown_shapes_yield_empty():
    assert exclude.parse(None) == []
    assert exclude.parse("garbage") == []
    assert exclude.parse({"entries": "not-a-list"}) == []


def test_load_missing_or_broken_file_means_nothing_blocked(tmp_path):
    assert exclude.load(str(tmp_path / 'nope.json')) == []
    bad = tmp_path / 'bad.json'
    bad.write_text('{not json')
    assert exclude.load(str(bad)) == []


def test_load_real_file(tmp_path):
    f = tmp_path / 'block.json'
    f.write_text(json.dumps({"entries": [{"provider": "X", "model": "y"}]}))
    assert exclude.load(str(f)) == [{"provider": "X", "model": "y", "task": '*'}]


def test_is_excluded_matching_is_case_insensitive():
    entries = [{"provider": "VendorA", "model": "chat-v4", "task": '*'}]
    assert exclude.is_excluded("vendorA", "CHAT-v4", entries)
    assert exclude.is_excluded("VENDORA", "chat-v4", entries, task='vision')
    assert not exclude.is_excluded("VendorB", "chat-v4", entries)
    assert not exclude.is_excluded("VendorA", "chat-v5", entries)


def test_is_excluded_task_scoping():
    entries = [{"provider": "X", "model": "text-only", "task": "vision"}]
    assert exclude.is_excluded("X", "text-only", entries, task='vision')
    assert not exclude.is_excluded("X", "text-only", entries, task='compression')
    # wildcard entry blocks every task
    assert exclude.is_excluded("X", "text-only",
                               [{"provider": "X", "model": "text-only",
                                 "task": "*"}], task='compression')


def test_split_keeps_order_and_reports_reason():
    cands = [
        {'provider': 'VendorA', 'model': 'chat-v4', 'base_url': 'x'},
        {'provider': 'VendorB', 'model': 'chat-v4', 'base_url': 'y'},
        {'provider': 'VendorC', 'model': 'chat-v4', 'base_url': 'z'},
    ]
    entries = [{"provider": "vendorB", "model": "chat-v4", "task": '*'}]
    allowed, excluded = exclude.split(cands, entries, task='vision')
    assert [c['provider'] for c in allowed] == ['VendorA', 'VendorC']
    assert len(excluded) == 1
    cand, reason = excluded[0]
    assert cand['provider'] == 'VendorB'
    assert 'never probed' in reason


def test_split_with_empty_entries_is_a_passthrough():
    cands = [{'provider': 'A', 'model': 'm'}]
    allowed, excluded = exclude.split(cands, [], task='vision')
    assert allowed == cands and excluded == []


def test_parse_pairs_cli():
    out = exclude.parse_pairs(['VendorA/chat-v4', 'B/m2', 'junk'])
    assert len(out) == 2
    assert out[0] == {"provider": "VendorA", "model": "chat-v4", "task": '*'}
