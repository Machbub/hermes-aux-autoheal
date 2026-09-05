"""Every documentation link must resolve — checked, not eyeballed.

A dead intra-document anchor is invisible: the page loads, the link scrolls
nowhere, and nothing in a test suite notices. This module is a linter for the
repo's own markdown, run as part of the test suite so the check cannot quietly
stop happening.

Three failure modes, each a real one:

* **Anchor slugs are not headings.** GitHub lowercases, strips punctuation
  (INCLUDING dots) and joins words with hyphens, so ``## Writing config.yaml
  safely`` is reachable at ``#writing-configyaml-safely`` and NOT at
  ``#writing-config.yaml-safely``. That exact link shipped broken once.
* **Fenced code blocks contain `#` lines.** ``# on a timer, every 5 minutes``
  inside a bash block is a comment, not a heading. Indexing it invents an anchor
  that GitHub will not honour — which makes a checker report PASS for a link that
  is actually dead. Fences must be tracked, so this is not a regex-per-line job.
* **Relative file links rot.** ``[STABILITY.md](STABILITY.md)`` and
  ``examples/dashboard/`` are paths in a repo that gets reorganised.

Scope is every tracked ``.md`` file, discovered rather than listed: a doc added
next month is covered without editing this file.
"""

import difflib
import os
import re

#: Repo root, two levels up from tests/.
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_FENCE = re.compile(r'^\s*(```|~~~)')
_HEADING = re.compile(r'^(#{1,6})\s+(.+?)\s*$')
_INLINE_LINK = re.compile(r'\[(?:[^\]\\]|\\.)*\]\(([^)\s]+)(?:\s+"[^"]*")?\)')
_REF_DEF = re.compile(r'^\s{0,3}\[[^\]]+\]:\s*(\S+)')


def slugify(heading):
    """GitHub's heading-to-anchor transform, as far as this repo needs it.

    Inline markup is stripped before slugging because the anchor is built from
    the rendered TEXT: ``## Reading `state` vs `ok``` becomes
    ``#reading-state-vs-ok``, with the backticks gone rather than escaped.
    """
    text = heading.strip()
    text = re.sub(r'<[^>]+>', '', text)                    # inline HTML
    text = re.sub(r'!\[([^\]]*)\]\([^)]*\)', r'\1', text)  # images -> alt
    text = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', text)   # links -> text
    text = text.replace('`', '')
    text = re.sub(r'[*_~]', '', text)                      # emphasis markers
    text = text.lower()
    # GitHub drops every character that is not a word char, space or hyphen —
    # dots included, which is the trap this function exists for.
    text = re.sub(r'[^\w\s-]', '', text, flags=re.UNICODE)
    return re.sub(r'\s+', '-', text.strip())


def markdown_files(root=REPO):
    """Every ``.md`` file in the repo, excluding vendored/build directories.

    Discovered, not enumerated: a doc added later is checked automatically. That
    matters more than it sounds — the first version of this check listed four
    filenames by hand and silently ignored ``examples/dashboard/README.md``.
    """
    skip = {'.git', '.venv', 'venv', 'node_modules', '__pycache__',
            'build', 'dist', '.pytest_cache'}
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in skip and not d.endswith('.egg-info')]
        for name in sorted(filenames):
            if name.endswith('.md'):
                out.append(os.path.relpath(os.path.join(dirpath, name), root))
    return sorted(out)


def _strip_fences(lines):
    """Yield ``(lineno, text, in_fence)`` with fenced-block state tracked.

    Tracked rather than regex-skipped: a fence opened by ``~~~`` must not be
    closed by ``` and vice versa, and shell comments inside a block look exactly
    like headings.
    """
    fence = None
    for lineno, raw in enumerate(lines, 1):
        m = _FENCE.match(raw)
        if m:
            marker = m.group(1)
            if fence is None:
                fence = marker
            elif marker == fence:
                fence = None
            yield lineno, raw, True
            continue
        yield lineno, raw, fence is not None


def headings(path):
    """Slugs of every real heading in one markdown file.

    GitHub disambiguates repeated headings with ``-1``, ``-2``, ... suffixes, so
    those are generated too; otherwise a legitimate link to the second
    occurrence reads as dead.
    """
    with open(path, encoding='utf-8') as f:
        lines = f.readlines()
    seen, slugs = {}, set()
    for _lineno, raw, in_fence in _strip_fences(lines):
        if in_fence:
            continue
        m = _HEADING.match(raw)
        if not m:
            continue
        base = slugify(m.group(2))
        if not base:
            continue
        count = seen.get(base, 0)
        slugs.add(base if count == 0 else f'{base}-{count}')
        seen[base] = count + 1
    return slugs


def links(path):
    """Yield ``(lineno, target)`` for every link target in one file.

    Includes reference-style definitions (``[label]: target``), which are easy to
    forget and just as capable of pointing at a heading that no longer exists.
    """
    with open(path, encoding='utf-8') as f:
        lines = f.readlines()
    for lineno, raw, in_fence in _strip_fences(lines):
        if in_fence:
            continue
        for target in _INLINE_LINK.findall(raw):
            yield lineno, target
        m = _REF_DEF.match(raw)
        if m:
            yield lineno, m.group(1)


def check(root=REPO):
    """Return a list of human-readable problems. Empty means everything resolves."""
    files = markdown_files(root)
    index = {f: headings(os.path.join(root, f)) for f in files}
    problems = []

    for doc in files:
        doc_dir = os.path.dirname(doc)
        for lineno, target in links(os.path.join(root, doc)):
            if target.startswith(('http://', 'https://', 'mailto:', '#!')):
                continue
            file_part, _, anchor = target.partition('#')

            if file_part:
                # Relative to the linking document, not the repo root.
                rel = os.path.normpath(os.path.join(doc_dir, file_part))
                abs_path = os.path.join(root, rel)
                if not os.path.exists(abs_path):
                    problems.append(
                        f'{doc}:{lineno}: link target does not exist: {file_part}')
                    continue
                target_doc = rel.replace(os.sep, '/')
            else:
                target_doc = doc

            if not anchor:
                continue
            if target_doc not in index:
                # A link into a non-markdown file's anchor is not ours to judge.
                continue
            if anchor not in index[target_doc]:
                # difflib, not a substring match on the first token: an anchor
                # starting with "a-" matched half the document and buried the
                # real suggestion. The dot trap must surface
                # #writing-configyaml-safely as the fix, so the hint has to be
                # ordered by similarity.
                near = difflib.get_close_matches(
                    anchor, sorted(index[target_doc]), n=3, cutoff=0.6)
                hint = f' (did you mean: {", ".join(near)})' if near else ''
                problems.append(
                    f'{doc}:{lineno}: dead anchor #{anchor} in {target_doc}{hint}')
    return problems


def main():
    problems = check()
    files = markdown_files()
    total_headings = sum(len(headings(os.path.join(REPO, f))) for f in files)
    print(f'{len(files)} markdown files, {total_headings} headings indexed')
    for f in files:
        print(f'  {f}')
    print()
    if problems:
        print(f'{len(problems)} BROKEN:')
        for p in problems:
            print('  ' + p)
        return 1
    print('every relative link and anchor resolves')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
