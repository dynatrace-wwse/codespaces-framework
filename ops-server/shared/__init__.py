"""Code that BOTH the dashboard and the worker agent need.

The worker is a deliberately slim sparse checkout — it does not clone
``ops-server/dashboard`` at all. Anything the two sides must agree on therefore
cannot live in ``dashboard``, or the worker silently gets a fallback instead of
the shared answer. That happened: the capacity unit table was in ``dashboard``,
so one worker derived 20 slots and its identical twin derived 6.

``worker-agent/setup-worker.sh`` includes ``ops-server/shared/**`` in the sparse
pattern. Adding a module here is enough; adding one to ``dashboard`` and
importing it from the worker is not.

Keep this package free of third-party imports — the worker's virtualenv is
smaller than the dashboard's.
"""
