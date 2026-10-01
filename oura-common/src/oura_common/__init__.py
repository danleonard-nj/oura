"""Shared Oura integration code: config, infrastructure, clients, models,
repositories and services common to every service that talks to the Oura API
on a user's behalf (oura-mcp, oura-sync).

This is a plain shared source tree, not an installable package. Each service
reaches it via a `sys.path` bootstrap in its own `__init__.py` -- see any
service's `src/<pkg>/__init__.py` for the two-line snippet.
"""
