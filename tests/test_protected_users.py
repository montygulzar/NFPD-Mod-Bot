"""The runtime protected list.

is_protected sits on the hot path of every moderation command and is
synchronous, so the stored list is mirrored in memory rather than queried.
These cover that the mirror is actually consulted, and that it adds to the
configured lists rather than replacing them.
"""
import pytest

import guards


@pytest.fixture(autouse=True)
def clean_runtime_list():
    guards.set_runtime_protected(set())
    yield
    guards.set_runtime_protected(set())


def test_a_runtime_protected_user_is_protected():
    assert not guards.is_protected(12345)
    guards.set_runtime_protected({12345})
    assert guards.is_protected(12345)


def test_unprotecting_takes_effect_immediately():
    guards.set_runtime_protected({12345})
    guards.set_runtime_protected(set())
    assert not guards.is_protected(12345)


def test_the_runtime_list_adds_to_the_configured_ones():
    """A user protected by the environment must stay protected whatever the
    runtime list says, so /unprotect cannot undo configuration."""
    from unittest.mock import patch
    with patch.object(guards, "PROTECTED_USER_IDS", {7}), \
         patch.object(guards, "OWNER_IDS", {1}):
        guards.set_runtime_protected(set())
        assert guards.is_protected(7)
        assert guards.is_protected(1)


def test_runtime_protection_reaches_the_moderation_guard():
    """The point of the feature: a protected user cannot be banned."""
    from unittest.mock import patch

    class Role:
        def __init__(self, p): self.position = p
        def __ge__(self, o): return self.position >= o.position

    class Guild:
        owner_id, id = 0, 500

    class Member:
        def __init__(self, i, pos):
            self.id, self.top_role, self.roles, self.guild = i, Role(pos), [], Guild()
        def __str__(self): return f"User{self.id}"

    actor, target = Member(100, 9), Member(12345, 1)
    with patch.object(guards.discord, "Member", Member):
        assert guards.refusal_reason(actor, target, 999) is None
        guards.set_runtime_protected({12345})
        refusal = guards.refusal_reason(actor, target, 999)
    assert refusal is not None and "protected" in refusal


def test_runtime_list_is_copied_not_aliased():
    """Handing back the live set would let a caller mutate protection state."""
    source = {1, 2}
    guards.set_runtime_protected(source)
    source.add(3)
    assert not guards.is_protected(3)
    guards.runtime_protected_ids().add(4)
    assert not guards.is_protected(4)
