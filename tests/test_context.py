"""Tests for context-window resolution.

No network: the ``/v1/models`` path is exercised against a stubbed urlopen.
"""
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hermes_aux_autoheal import context


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _stub_models(monkeypatch, payload, *, fail=False):
    context._MODELS_CACHE.clear()

    def fake_urlopen(req, timeout=None):
        if fail:
            raise OSError('connection refused')
        return _FakeResponse(json.dumps(payload).encode())

    monkeypatch.setattr(context.urllib.request, 'urlopen', fake_urlopen)


def _cand(model='m1'):
    return {'provider': 'A', 'model': model,
            'base_url': 'https://a.example/v1', 'api_key': 'k'}


def test_reads_context_length_from_listing(monkeypatch):
    _stub_models(monkeypatch, {'data': [
        {'id': 'm1', 'context_length': 128000},
    ]})
    lookup = context.make_lookup(hermes_path='/nonexistent')
    assert lookup(_cand()) == 128000


def test_accepts_alternate_field_names(monkeypatch):
    for field in ('max_context_length', 'context_window', 'max_model_len'):
        _stub_models(monkeypatch, {'data': [{'id': 'm1', field: 64000}]})
        lookup = context.make_lookup(hermes_path='/nonexistent')
        assert lookup(_cand()) == 64000, field


def test_unknown_model_is_zero_not_an_error(monkeypatch):
    _stub_models(monkeypatch, {'data': [{'id': 'other', 'context_length': 1}]})
    lookup = context.make_lookup(hermes_path='/nonexistent')
    assert lookup(_cand()) == 0


def test_endpoint_failure_degrades_to_zero(monkeypatch):
    _stub_models(monkeypatch, {}, fail=True)
    lookup = context.make_lookup(hermes_path='/nonexistent')
    assert lookup(_cand()) == 0, 'a dead listing endpoint must not raise'


def test_listing_is_fetched_once_per_base_url(monkeypatch):
    context._MODELS_CACHE.clear()
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        return _FakeResponse(json.dumps({'data': [
            {'id': 'm1', 'context_length': 1000},
            {'id': 'm2', 'context_length': 2000},
        ]}).encode())

    monkeypatch.setattr(context.urllib.request, 'urlopen', fake_urlopen)
    lookup = context.make_lookup(hermes_path='/nonexistent')
    assert lookup(_cand('m1')) == 1000
    assert lookup(_cand('m2')) == 2000
    assert len(calls) == 1, 'one listing call should serve every model'


def test_no_lookup_when_disabled(monkeypatch):
    def explode(*a, **k):
        raise AssertionError('should not be called')

    monkeypatch.setattr(context.urllib.request, 'urlopen', explode)
    lookup = context.make_lookup(hermes_path='/nonexistent',
                                 use_models_endpoint=False)
    assert lookup(_cand()) == 0


def test_hermes_metadata_wins_when_available(monkeypatch):
    _stub_models(monkeypatch, {'data': [{'id': 'm1', 'context_length': 1000}]})
    monkeypatch.setattr(context, '_hermes_lookup', lambda path=None: (lambda m: 999999))
    lookup = context.make_lookup()
    assert lookup(_cand()) == 999999, "Hermes' own table is authoritative"


def test_falls_back_when_hermes_returns_nothing(monkeypatch):
    _stub_models(monkeypatch, {'data': [{'id': 'm1', 'context_length': 4096}]})
    monkeypatch.setattr(context, '_hermes_lookup', lambda path=None: (lambda m: 0))
    lookup = context.make_lookup()
    assert lookup(_cand()) == 4096


def test_hermes_lookup_exception_is_survivable(monkeypatch):
    _stub_models(monkeypatch, {'data': [{'id': 'm1', 'context_length': 8192}]})

    def boom(m):
        raise RuntimeError('metadata table exploded')

    monkeypatch.setattr(context, '_hermes_lookup', lambda path=None: boom)
    lookup = context.make_lookup()
    assert lookup(_cand()) == 8192
