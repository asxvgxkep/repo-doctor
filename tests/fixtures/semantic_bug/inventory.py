"""Small project with a semantic boundary bug for mocked AI analysis."""


def can_fulfill(stock: int, requested: int) -> bool:
    """Return whether the available stock can fulfill an order."""
    return requested < stock
