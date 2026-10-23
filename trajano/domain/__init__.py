"""Business rules.

Functions receive a Session, flush, and do not commit. The interface owns the
transaction and turns DomainError into a structured tool error.
"""
