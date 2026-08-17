"""Actionable errors raised by the optional AI workflow."""


class AIError(Exception):
    """Base class for errors safe to display to a user."""


class AIConfigurationError(AIError):
    """Required provider configuration is absent or invalid."""


class ProviderError(AIError):
    """The configured provider could not complete a request."""


class ResponseValidationError(AIError):
    """Provider output did not match the trusted schema."""


class ContextSelectionError(AIError):
    """Safe repository context could not be selected."""


class PatchValidationError(AIError):
    """A proposed patch violated a safety constraint."""


class VerificationError(AIError):
    """A patch could not be verified safely."""


class RollbackError(AIError):
    """The original file could not be restored exactly."""
