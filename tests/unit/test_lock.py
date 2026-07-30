"""B5 — two passes must not both process the same record."""
import pytest
from lock import AlreadyRunning, single_instance


def test_second_holder_is_refused():
    with single_instance("pytest-atticus"):
        with pytest.raises(AlreadyRunning):
            with single_instance("pytest-atticus"):
                pass


def test_lock_is_released_on_exit():
    with single_instance("pytest-atticus"):
        pass
    with single_instance("pytest-atticus"):
        pass  # reacquiring proves release
