import textwrap
from pathlib import Path

import pytest


@pytest.fixture
def make_pr(tmp_path):
    """make_pr({"pkg/m.py": src}, {"pkg/m.py": src2}) -> (base_dir, head_dir)."""

    def _make(base: dict[str, str], head: dict[str, str]):
        roots = []
        for side, files in (("base", base), ("head", head)):
            root = tmp_path / side
            for rel, src in files.items():
                p = root / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(textwrap.dedent(src))
            roots.append(root)
        return roots[0], roots[1]

    return _make


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent
