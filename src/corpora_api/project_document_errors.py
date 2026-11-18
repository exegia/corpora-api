"""Errors that indicate a temporary document-verification service failure."""


class DocumentVerificationUnavailable(Exception):
    """A bounded document verifier could not complete."""
