"""Standalone subprocess entry points.

Modules under this package are executed as top-level scripts by
``viseron`` (via ``python3 -u subprocess_workers/<name>.py``) to run
specific background jobs in isolated processes.

They deliberately do not import anything from the ``viseron`` package,
so the subprocess does not drag in opencv, scipy, matplotlib, or the
whole domain/component graph. That keeps each subprocess's resident
memory to roughly the size of the libraries it actually uses.
"""
