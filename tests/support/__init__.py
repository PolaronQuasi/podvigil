"""Shared fixtures for the tests. Test modules import from here, never from each other.

Importing this package or support.paths needs only the standard library: the container lane copies
parts of tests/ into network-less Linux containers next to the Pod code.
"""
