"""Sources a Free Research pull can read.

A source fetches and writes rows. It does not extract fields, score anything or
decide what a row means -- that is the brief's job, and keeping it out of here is
what lets the same rows be read again later by a different template.
"""
