"""Code shared by more than one ops-server service.

Anything in here is imported by the dashboard, the webhook server and the
provisioning library alike, so it must depend on nothing but the standard
library — `shared` is the bottom of the import graph, not a second utils bin.
"""
