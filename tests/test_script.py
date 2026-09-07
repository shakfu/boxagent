"""The standalone script embeds a copy of the Containerfile; this proves the
two do not drift.

scripts/sanduk.py predates the package and is kept runnable on its own, so it
carries the image definition inline rather than reading the packaged resource.
Two copies of anything rot, and this one rots silently: a stale embedded copy
still builds, just not the image the package builds.
"""

import ast
import pathlib

import pytest

SCRIPT = pathlib.Path(__file__).parent.parent / "scripts" / "sanduk.py"
RESOURCE = (
    pathlib.Path(__file__).parent.parent
    / "src"
    / "sanduk"
    / "resources"
    / "Containerfile"
)


def embedded_containerfile():
    """Read the constant without executing the script."""
    tree = ast.parse(SCRIPT.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "CONTAINERFILE" for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("scripts/sanduk.py defines no CONTAINERFILE")


@pytest.mark.skipif(not SCRIPT.is_file(), reason="scripts/ is not in this tree")
def test_embedded_containerfile_matches_the_packaged_one():
    assert embedded_containerfile() == RESOURCE.read_text()


@pytest.mark.skipif(not SCRIPT.is_file(), reason="scripts/ is not in this tree")
def test_line_continuations_survived_the_embedding():
    """A plain triple-quoted literal would splice these away and corrupt the
    build; the constant has to be a raw string."""
    lines = embedded_containerfile().splitlines()
    assert sum(1 for line in lines if line.endswith("\\")) == 6


# The package raises AgentboxError where the script calls die(), routes output
# through util.note, and carries type annotations. Those four differences are by
# design and account for exactly these names; every other shared name must match.
ARCHITECTURAL = {
    "Handler.cfg",
    "_firewall_entries",
    "firewall_warning",
    "launch",
    "main",
    "parse_args",
    "start_proxy",
    "validate_key",
}

PACKAGE = pathlib.Path(__file__).parent.parent / "src" / "sanduk"


def _normalize(node):
    """Annotations and docstrings out, so only behaviour is compared."""
    for n in ast.walk(node):
        if isinstance(n, ast.FunctionDef):
            n.returns = None
            for a in n.args.posonlyargs + n.args.args + n.args.kwonlyargs:
                a.annotation = None
            for a in (n.args.vararg, n.args.kwarg):
                if a:
                    a.annotation = None
        if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Module)):
            n.body = [
                ast.Assign(targets=[b.target], value=b.value)
                if isinstance(b, ast.AnnAssign) and b.value is not None
                else b
                for b in n.body
            ]
            head = n.body[0] if n.body else None
            if (
                isinstance(head, ast.Expr)
                and isinstance(head.value, ast.Constant)
                and isinstance(head.value.value, str)
            ):
                n.body = n.body[1:] or [ast.Pass()]
    return ast.fix_missing_locations(node)


def definitions(path):
    """Every top-level function, method, and CONSTANT, as normalized source."""
    out = {}
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.FunctionDef):
            out[node.name] = ast.unparse(_normalize(node))
        elif isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef):
                    out[f"{node.name}.{sub.name}"] = ast.unparse(_normalize(sub))
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id.isupper():
                    out[t.id] = ast.unparse(node.value)
    return out


@pytest.mark.skipif(not SCRIPT.is_file(), reason="scripts/ is not in this tree")
def test_shared_logic_has_not_drifted():
    """The relay is the security boundary and exists in both copies. A fix
    applied to one and not the other is the failure this catches."""
    script = definitions(SCRIPT)
    package = {}
    for module in sorted(PACKAGE.glob("*.py")):
        package.update(definitions(module))
    shared = set(script) & set(package)
    drifted = {n for n in shared if script[n] != package[n]} - ARCHITECTURAL
    assert not drifted, f"script and package disagree on: {sorted(drifted)}"
    assert "Handler.relay" in shared, "the relay must be in both copies"
