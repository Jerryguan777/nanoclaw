"""Placeholder test to verify pytest runs successfully."""


def test_import() -> None:
    """Verify the nanoclaw package can be imported."""
    import nanoclaw

    assert nanoclaw.__doc__ is not None
