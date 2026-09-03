"""The Product Entry web UI.

A React single-page app lives in ``frontend/`` and builds to ``web/app/``; this package
does nothing but serve those static files at ``/ui``. All of the UI's behaviour -- the
form, the eight distinct listing states, the per-retailer comparison -- is in the frontend
and its Vitest suite. The page talks to ``/api/v1`` like any other client and carries no
server-side logic, so the page and the API cannot drift apart.
"""
