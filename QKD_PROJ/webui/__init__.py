"""The standalone web UI: a second client of the daemon's protocol 1.

Nothing here owns job truth. The ledger does. This package holds a
subscription, a projection of what came over it, and an HTTP surface.
"""
