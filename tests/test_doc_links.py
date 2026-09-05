"""The documentation link checker, checked.

A linter nobody trusts is worse than no linter, so these tests do two things:
run the real check over the real repo docs (so a broken link fails the build),
and sabotage synthetic docs to prove the checker can actually fail. The second
half matters more — the first version of this check reported PASS on a link that
GitHub could not resolve.
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import doc_links  # noqa: E402


# ------------------------------------------------------- the real repo docs
def test_every_link_in_the_repo_docs_resolves():
    """The point of the module: README <-> STABILITY.md <-> DASHBOARD.md."""
    problems = doc_links.check()
    assert problems == [], 'broken documentation links:\n  ' + '\n  '.join(problems)


def test_the_check_actually_reaches_every_markdown_file():
    """Discovery, not a hand-written list.

    The first version enumerated four filenames and silently ignored
    examples/dashboard/README.md. If a doc is added and this list is not, the
    checker goes quiet instead of failing — so assert discovery covers the ones
    that must be there.
    """
    files = set(doc_links.markdown_files())
    for required in ('README.md', 'STABILITY.md', 'DASHBOARD.md', 'CHANGELOG.md',
                     'examples/dashboard/README.md'):
        assert required in files, f'{required} not discovered'


def test_it_examines_a_meaningful_number_of_links():
    """Guard against a regex that quietly stops matching.

    A checker that finds zero links passes every repo forever. This pins the
    order of magnitude, not an exact count, so adding docs does not break it.
    """
    total = 0
    for doc in doc_links.markdown_files():
        total += len(list(doc_links.links(os.path.join(doc_links.REPO, doc))))
    assert total >= 20, f'only {total} links found; the link regex is broken'


# ------------------------------------------------------------------ slugify
@pytest.mark.parametrize('heading,expected', [
    # The bug that started this: GitHub strips the dot, so a hand-written
    # #writing-config.yaml-safely anchor is dead.
    ('Writing config.yaml safely', 'writing-configyaml-safely'),
    ('Building a dashboard on this', 'building-a-dashboard-on-this'),
    # Anchors are built from RENDERED text: backticks vanish, they are not escaped.
    ('Reading `state` when you want `ok`', 'reading-state-when-you-want-ok'),
    ('How this differs from a proxy or router',
     'how-this-differs-from-a-proxy-or-router'),
    ('The quota wall a probe cannot see', 'the-quota-wall-a-probe-cannot-see'),
    ('Two chains, two pickers', 'two-chains-two-pickers'),
    ('**bold** and _em_', 'bold-and-em'),
    ('[a link](http://x)', 'a-link'),
    ('trailing spaces   ', 'trailing-spaces'),
    ('CAPS Become lower', 'caps-become-lower'),
])
def test_slugify_matches_github(heading, expected):
    assert doc_links.slugify(heading) == expected


def test_duplicate_headings_get_numbered_slugs():
    """GitHub appends -1, -2 to repeated headings; a link to the second is valid."""
    doc = 'A\n\n## Same\n\ntext\n\n## Same\n\ntext\n\n## Same\n'
    import tempfile
    with tempfile.NamedTemporaryFile('w', suffix='.md', delete=False) as f:
        f.write(doc)
        path = f.name
    try:
        slugs = doc_links.headings(path)
        assert slugs == {'same', 'same-1', 'same-2'}
    finally:
        os.unlink(path)


# ---------------------------------------------------------------- sabotage
def _repo(tmp_path, files):
    for name, text in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding='utf-8')
    return str(tmp_path)


def test_dead_anchor_is_reported(tmp_path):
    root = _repo(tmp_path, {
        'README.md': '# T\n\nsee [there](#no-such-heading)\n\n## Real heading\n',
    })
    problems = doc_links.check(root)
    assert len(problems) == 1
    assert 'dead anchor #no-such-heading' in problems[0]


def test_dead_cross_file_anchor_is_reported(tmp_path):
    """The README -> STABILITY.md direction, which is what Bos asked about."""
    root = _repo(tmp_path, {
        'README.md': '# T\n\n[evidence](STABILITY.md#behaviours-the-tests-pin)\n',
        'STABILITY.md': '# S\n\n## Something else\n',
    })
    problems = doc_links.check(root)
    assert len(problems) == 1
    assert 'dead anchor #behaviours-the-tests-pin in STABILITY.md' in problems[0]


def test_missing_file_link_is_reported(tmp_path):
    root = _repo(tmp_path, {'README.md': '# T\n\n[gone](DASHBOARD.md)\n'})
    problems = doc_links.check(root)
    assert len(problems) == 1
    assert 'link target does not exist: DASHBOARD.md' in problems[0]


def test_the_dot_stripping_trap_is_caught(tmp_path):
    """`#writing-config.yaml-safely` shipped once and rendered as a dead link."""
    root = _repo(tmp_path, {
        'README.md': ('# T\n\n[safe writes](#writing-config.yaml-safely)\n\n'
                      '## Writing config.yaml safely\n'),
    })
    problems = doc_links.check(root)
    assert len(problems) == 1
    assert 'dead anchor' in problems[0]
    # And the hint should point at the anchor that does work.
    assert 'writing-configyaml-safely' in problems[0]


def test_a_heading_inside_a_code_fence_is_not_an_anchor(tmp_path):
    """The false-PASS bug, and the reason fences are tracked rather than ignored.

    README.md really does contain `# on a timer, every 5 minutes` inside a bash
    block. Indexing that as a heading invents an anchor GitHub will not honour,
    so a link to it must be reported dead — a naive line-based checker calls it
    fine.
    """
    root = _repo(tmp_path, {
        'README.md': (
            '# T\n\n'
            '[timer](#on-a-timer-every-5-minutes)\n\n'
            '```bash\n'
            '# on a timer, every 5 minutes\n'
            'hermes-aux-autoheal --apply\n'
            '```\n'
        ),
    })
    problems = doc_links.check(root)
    assert len(problems) == 1, 'a shell comment was indexed as a heading'
    assert 'dead anchor #on-a-timer-every-5-minutes' in problems[0]


def test_links_inside_a_code_fence_are_not_checked(tmp_path):
    """Example markdown in a fenced block documents syntax; it is not a live link."""
    root = _repo(tmp_path, {
        'README.md': (
            '# T\n\nHow to link:\n\n'
            '```markdown\n[see](NOT_A_REAL_FILE.md#nope)\n```\n'
        ),
    })
    assert doc_links.check(root) == []


def test_tilde_fences_are_tracked_too(tmp_path):
    root = _repo(tmp_path, {
        'README.md': '# T\n\n~~~\n[x](MISSING.md)\n~~~\n',
    })
    assert doc_links.check(root) == []


def test_a_fence_is_only_closed_by_its_own_marker(tmp_path):
    """``` must not close a ~~~ block, or everything after it is mis-parsed."""
    root = _repo(tmp_path, {
        'README.md': '# T\n\n~~~\n```\n[x](MISSING.md)\n~~~\n\n[y](ALSO_MISSING.md)\n',
    })
    problems = doc_links.check(root)
    assert len(problems) == 1, problems
    assert 'ALSO_MISSING.md' in problems[0]


def test_reference_style_link_definitions_are_checked(tmp_path):
    root = _repo(tmp_path, {
        'README.md': '# T\n\nsee [ev][1]\n\n[1]: STABILITY.md#gone\n',
        'STABILITY.md': '# S\n\n## Present\n',
    })
    problems = doc_links.check(root)
    assert len(problems) == 1
    assert 'dead anchor #gone' in problems[0]


def test_relative_links_resolve_against_the_linking_document(tmp_path):
    """examples/dashboard/README.md links UP to ../../DASHBOARD.md."""
    root = _repo(tmp_path, {
        'DASHBOARD.md': '# D\n\n## The contract\n',
        'examples/dashboard/README.md':
            '# E\n\n[contract](../../DASHBOARD.md#the-contract)\n',
    })
    assert doc_links.check(root) == []


def test_a_wrong_relative_depth_is_reported(tmp_path):
    root = _repo(tmp_path, {
        'DASHBOARD.md': '# D\n',
        'examples/dashboard/README.md': '# E\n\n[contract](../DASHBOARD.md)\n',
    })
    problems = doc_links.check(root)
    assert len(problems) == 1
    assert 'does not exist' in problems[0]


def test_external_links_are_left_alone(tmp_path):
    """No network in the test suite, so http(s) targets are out of scope."""
    root = _repo(tmp_path, {
        'README.md': ('# T\n\n[gh](https://github.com/x/y#readme)\n'
                      '[mail](mailto:a@b.c)\n'),
    })
    assert doc_links.check(root) == []
