"""Server-rendered Product Entry pages.

A leaf package: ``api`` mounts it, and nothing in ``api`` or ``services`` imports from
here. The pages call the same services the API and CLI do, so the three cannot drift into
disagreeing about what a Product Entry is.
"""
