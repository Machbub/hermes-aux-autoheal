"""Tests for the dashboard report helpers.

Each test names the failure mode it prevents, because most of these were real
bugs in a dashboard reading the cache by hand before the module existed.
"""
import json

import pytest

from hermes_aux_autoheal import report


# --------------------------------------------------------------------------
# parse_key: key length carries meaning
# --------------------------------------------------------------------------

def test_four_part_key_is_provider_task_url_model():
    got = report.parse_key('VendorA|vision|https://a.example/v1|chat-v4')
    assert got == {'provider': 'VendorA', 'task': 'vision',
                   'base_url': 'https://a.example/v1', 'model': 'chat-v4'}


def test_three_part_key_defaults_to_provider_scope():
    got = report.parse_key('VendorA|https://a.example/v1|chat-v4')
    assert got['provider'] == 'VendorA'
    assert got['task'] == ''
    assert got['model'] == 'chat-v4'


def test_three_part_key_reads_a_known_task_name_as_a_task():
    got = report.parse_key('vision|https://a.example/v1|chat-v4')
    assert got['task'] == 'vision'
    assert got['provider'] == ''


def test_a_provider_literally_named_like_a_task_can_be_forced():
    """`tasks=()` disables the task reading for installs with odd labels."""
    got = report.parse_key('vision|https://a.example/v1|chat-v4', tasks=())
    assert got['provider'] == 'vision'
    assert got['task'] == ''


def test_legacy_two_part_key_has_no_provider():
    got = report.parse_key('https://a.example/v1|chat-v4')
    assert got == {'provider': '', 'task': '',
                   'base_url': 'https://a.example/v1', 'model': 'chat-v4'}


def test_unknown_shapes_do_not_raise():
    assert report.parse_key('')['model'] == ''
    assert report.parse_key('lonely')['model'] == 'lonely'
    assert report.parse_key('a|b|c|d|e|f')['model'] == 'f'


# --------------------------------------------------------------------------
# classify: the categories are the API
# --------------------------------------------------------------------------

@pytest.mark.parametrize('err,expected', [
    ('HTTP 400 insufficient_user_quota', 'no_balance'),
    ('balance=0', 'no_balance'),
    ('HTTP 429 rate limit exceeded', 'rate_limit'),
    ('model quota is temporarily paused', 'rate_limit'),
    ('HTTP 404 model_not_found', 'model_gone'),
    ('HTTP 503 no available channel for model chat-v4', 'model_gone'),
    ('HTTP 401 unauthorized', 'auth'),
    ('HTTP 403 invalid api key', 'auth'),
    ('probe timed out after 45s', 'timeout'),
    ('HTTP 502 bad gateway', 'upstream'),
    ('HTTP 400 do not support image input', 'no_vision'),
    ('HTTP 400 something we have never seen', 'other'),
    ('', 'unknown'),
])
def test_classify_categories(err, expected):
    category, label, _hint = report.classify(err)
    assert category == expected
    assert label, 'every category needs a display label'


def test_a_request_id_containing_404_is_not_a_dead_model():
    """The bug this guard exists for: `'404' in text` matched a request id.

    Providers echo an id like `request id: 20260903121131601478965c955d568`.
    A bare digit search finds 404 inside it and files a quota error as a dead
    model — then offers a delete button for a model that was working.
    """
    err = ('HTTP 429 quota paused (request id: '
           '20260903404131601478965c955d568)')
    assert report.classify(err)[0] == 'rate_limit'


def test_vision_refusal_beats_the_generic_400():
    err = 'HTTP 400 invalid_request_error: this model do not support image input'
    assert report.classify(err)[0] == 'no_vision'


def test_only_actionable_categories_are_deletable():
    for category in ('no_balance', 'model_gone', 'no_vision'):
        assert category in report.DELETABLE
    # These heal themselves or need a different fix — hiding the row is wrong.
    for category in ('rate_limit', 'timeout', 'upstream', 'auth'):
        assert category not in report.DELETABLE


# --------------------------------------------------------------------------
# redact: an error string is untrusted upstream text
# --------------------------------------------------------------------------

@pytest.mark.parametrize('raw', [
    'auth failed for sk-abcdefghijklmnop1234',
    'Authorization: Bearer abcdefghijklmnopqrst',
    'api_key=abcdefghijklmnop',
    'token: ghp_abcdefghijklmnopqrstuvwxyz01',
])
def test_key_shaped_text_is_redacted(raw):
    out = report.redact(raw)
    assert '[redacted]' in out
    for secret in ('sk-abcdefghijklmnop1234', 'abcdefghijklmnopqrst',
                   'abcdefghijklmnop', 'ghp_abcdefghijklmnopqrstuvwxyz01'):
        assert secret not in out


def test_redaction_leaves_ordinary_errors_readable():
    assert report.redact('HTTP 503 model_not_found') == 'HTTP 503 model_not_found'


# --------------------------------------------------------------------------
# problems / summarize
# --------------------------------------------------------------------------

def _cache(tmp_path, name, data):
    path = tmp_path / name
    path.write_text(json.dumps(data))
    return str(path)


def test_healthy_means_last_probe_ok_not_state_up():
    """The bug: `state == 'up'` marked a recovered model as broken.

    `state` needs promote_streak consecutive successes, so a model that just
    came back reads `ok=True, state='down'` for one tick. Reporting that as a
    problem is how a dashboard loses trust — the user tests the model by hand,
    watches it answer, and stops believing the page.
    """
    cache = {
        'VendorA|https://a.example/v1|chat-v4': {
            'ok': True, 'state': 'down', 'err': '', 'ts': 100},
    }
    rows, ok_count = report.problems(cache)
    assert rows == []
    assert ok_count == 1


def test_a_grace_window_row_is_flagged_not_hidden():
    cache = {
        'VendorA|https://a.example/v1|chat-v4': {
            'ok': False, 'state': 'up', 'err': 'HTTP 502 bad gateway',
            'fail_streak': 1, 'ts': 100},
    }
    rows, ok_count = report.problems(cache)
    assert ok_count == 0
    assert len(rows) == 1
    assert rows[0]['in_grace'] is True
    assert rows[0]['category'] == 'upstream'


def test_summarize_sorts_worst_first(tmp_path):
    comp = _cache(tmp_path, 'comp.json', {
        'VendorA|https://a.example/v1|slow': {
            'ok': False, 'state': 'down', 'err': 'timed out', 'ts': 10},
        'VendorB|https://b.example/v1|broke': {
            'ok': False, 'state': 'down', 'err': 'balance=0',
            'fail_streak': 3, 'ts': 20},
        'VendorC|https://c.example/v1|fine': {
            'ok': True, 'state': 'up', 'err': '', 'ts': 30},
    })
    out = report.summarize([('compression', comp)])
    assert [r['category'] for r in out['problems']] == ['no_balance', 'timeout']
    assert out['ok_count'] == 1
    assert out['total'] == 3
    assert out['last_probe_ts'] == 20      # newest among PROBLEM rows


def test_summarize_merges_two_tasks_and_keeps_them_labelled(tmp_path):
    comp = _cache(tmp_path, 'comp.json', {
        'VendorA|https://a.example/v1|chat-v4': {
            'ok': False, 'state': 'down', 'err': 'HTTP 401 unauthorized',
            'ts': 10},
    })
    vis = _cache(tmp_path, 'vis.json', {
        'VendorA|vision|https://a.example/v1|chat-v4': {
            'ok': False, 'state': 'down',
            'err': 'HTTP 400 do not support image input', 'ts': 11},
    })
    out = report.summarize([('compression', comp), ('vision', vis)])
    tasks = {(r['task'], r['category']) for r in out['problems']}
    assert tasks == {('compression', 'auth'), ('vision', 'no_vision')}


def test_a_missing_or_corrupt_cache_is_an_empty_report(tmp_path):
    bad = tmp_path / 'bad.json'
    bad.write_text('{not json')
    out = report.summarize([('compression', str(tmp_path / 'nope.json')),
                            ('vision', str(bad))])
    assert out == {'problems': [], 'ok_count': 0, 'total': 0,
                   'last_probe_ts': 0}


def test_non_dict_cache_entries_are_skipped():
    rows, ok = report.problems({'a|b|c': 'garbage', 'd|e|f': None})
    assert rows == [] and ok == 0


def test_error_text_in_a_row_is_redacted_and_bounded():
    cache = {
        'VendorA|https://a.example/v1|chat-v4': {
            'ok': False, 'state': 'down',
            'err': 'denied for sk-abcdefghijklmnop1234 ' + 'x' * 500,
            'ts': 1},
    }
    rows, _ = report.problems(cache)
    assert 'sk-abcdefghijklmnop1234' not in rows[0]['error']
    assert len(rows[0]['error']) <= 300
