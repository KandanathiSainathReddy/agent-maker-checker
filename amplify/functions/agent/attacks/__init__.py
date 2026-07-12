"""Executable scenario pack for the enforcement proxy — the one-click demo a
reviewer presses play on. See ``run.py`` and ``README.md`` in this package.

Deliberately flat-script style (no package-relative imports between the
sibling modules in this directory) so ``python run.py`` works unmodified no
matter what the invoking working directory is — Python always puts the
running script's own directory at the front of ``sys.path``.
"""
