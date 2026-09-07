"""The standalone script embeds a copy of the Containerfile; this proves the
two do not drift.

scripts/agentbox.py predates the package and is kept runnable on its own, so it
carries the image definition inline rather than reading the packaged resource.
Two copies of anything rot, and this one rots silently: a stale embedded copy
still builds, just not the image the package builds.
"""

import ast
import pathlib

import pytest

SCRIPT = pathlib.Path(__file__).parent.parent / "scripts" / "agentbox.py"
RESOURCE = (
    pathlib.Path(__file__).parent.parent
    / "src"
    / "agentbox"
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
    raise AssertionError("scripts/agentbox.py defines no CONTAINERFILE")


@pytest.mark.skipif(not SCRIPT.is_file(), reason="scripts/ is not in this tree")
def test_embedded_containerfile_matches_the_packaged_one():
    assert embedded_containerfile() == RESOURCE.read_text()


@pytest.mark.skipif(not SCRIPT.is_file(), reason="scripts/ is not in this tree")
def test_line_continuations_survived_the_embedding():
    """A plain triple-quoted literal would splice these away and corrupt the
    build; the constant has to be a raw string."""
    lines = embedded_containerfile().splitlines()
    assert sum(1 for line in lines if line.endswith("\\")) == 6
