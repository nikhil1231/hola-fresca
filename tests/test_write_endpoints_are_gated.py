"""Every API write is answerable for who is asking.

Written as an audit over the routers rather than as a case per endpoint, because
the failure this catches is *forgetting* — and a test you have to remember to add
does not catch forgetting. It reads every POST/PUT/PATCH/DELETE in app/api and
insists each one resolves a user, transitively through its dependencies.

It was written after finding that every mapping write — approving a mapping,
bulk-approving, adding and deleting manual products, setting aliases — had no
gate at all, while the README said they were admin-only and the app had just
been given a UI that hid the tab. Hiding the tab is not the protection.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

API_DIR = pathlib.Path(__file__).resolve().parents[1] / "app" / "api"
WRITE_DECORATORS = {"post", "put", "patch", "delete"}

#: Dependencies that establish who is asking. ``require_admin`` and
#: ``get_active_retailer`` both depend on ``get_current_user`` themselves, and
#: ``get_cart_adapter``/``get_ocado_client`` are resolved through the endpoint's
#: own user lookup, so they are listed as the transitive gates they are.
USER_GATES = {"require_admin", "get_current_user", "get_active_retailer", "get_ocado_client"}

#: Writes that deliberately answer anyone. Empty, and the burden is on anything
#: that wants to join it: a write nobody is answerable for is how a household
#: app becomes a shared mutable blob.
UNGATED_BY_DESIGN: set[tuple[str, str]] = set()


def _writes():
    for path in sorted(API_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not (
                    isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    and decorator.func.attr in WRITE_DECORATORS
                ):
                    continue
                route = (
                    ast.literal_eval(decorator.args[0]) if decorator.args else ""
                )
                yield path.name, decorator.func.attr.upper(), route, ast.unparse(node.args)


def test_there_are_writes_to_audit():
    """Guards the audit itself: a parser that finds nothing passes vacuously."""
    assert len(list(_writes())) > 20


@pytest.mark.parametrize(
    "module, method, route, signature",
    [pytest.param(*w, id=f"{w[0]}:{w[1]}:{w[2]}") for w in _writes()],
)
def test_every_write_resolves_who_is_asking(module, method, route, signature):
    if (module, route) in UNGATED_BY_DESIGN:
        pytest.skip("ungated by design")
    assert any(gate in signature for gate in USER_GATES), (
        f"{method} {route} in {module} changes something without establishing "
        f"who is asking. Add require_admin for shared catalogue data, or "
        f"get_current_user for someone's own."
    )


CATALOGUE_WRITES = {"mapping.py"}


@pytest.mark.parametrize(
    "module, method, route, signature",
    [pytest.param(*w, id=f"{w[0]}:{w[1]}:{w[2]}") for w in _writes()],
)
def test_catalogue_writes_are_admin_only(module, method, route, signature):
    """The shared half of the database needs more than a signed-in user.

    Mappings, aliases and manual products are read by everybody's basket, so
    approving one changes what other people buy and what their week costs.
    """
    if module not in CATALOGUE_WRITES:
        pytest.skip("not a catalogue router")
    assert "require_admin" in signature, (
        f"{method} {route} edits the shared catalogue and must be admin-only"
    )
