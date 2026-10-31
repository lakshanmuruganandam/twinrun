from twinrun.astdiff import diff_trees, import_target

SRC = '''
"""Module doc."""
LIMIT = 10

def f(x: int) -> int:
    """Doc."""
    return x + LIMIT  # add

def g(x: int) -> int:
    return x * 2

class K:
    @staticmethod
    def s(x: int) -> int:
        return x

    def m(self):
        return 1
'''


def names(diff):
    return sorted((c.qualname, c.reason) for c in diff.candidates)


def test_formatting_comments_docstrings_are_not_changes(make_pr):
    reformatted = '''
"""A different module docstring."""
LIMIT = (10)

def f(x: int)  ->  int:
    # a new comment
    return (x +
            LIMIT)

def g(x: int) -> int:
    """New docstring."""
    return x*2

class K:
    @staticmethod
    def s(x: int) -> int:
        return x

    def m(self):
        return 1
'''
    base, head = make_pr({"m.py": SRC}, {"m.py": reformatted})
    d = diff_trees(base, head)
    assert d.candidates == []
    assert d.files_changed == ["m.py"]


def test_body_change_is_a_candidate(make_pr):
    base, head = make_pr({"m.py": SRC}, {"m.py": SRC.replace("x * 2", "x * 3")})
    assert names(diff_trees(base, head)) == [("g", "body changed")]


def test_module_constant_change_marks_every_function(make_pr):
    base, head = make_pr({"m.py": SRC}, {"m.py": SRC.replace("LIMIT = 10", "LIMIT = 11")})
    assert {q for q, r in names(diff_trees(base, head))} == {"f", "g", "K.s", "K.m"}
    assert all(r == "module context changed" for _, r in names(diff_trees(base, head)))


def test_methods_get_their_kind(make_pr):
    head = SRC.replace("return x\n", "return -x\n").replace("return 1", "return 2")
    base, head = make_pr({"m.py": SRC}, {"m.py": head})
    kinds = {c.qualname: c.kind for c in diff_trees(base, head).candidates}
    assert kinds == {"K.s": "staticmethod", "K.m": "method"}


def test_added_removed_and_tests_ignored(make_pr):
    base, head = make_pr(
        {"m.py": SRC, "tests/test_m.py": "def test_a():\n    assert 1\n"},
        {"m.py": SRC.replace("def g(", "def h("), "tests/test_m.py": "def test_a():\n    assert 2\n"},
    )
    d = diff_trees(base, head)
    assert "m.py::h" in d.added and "m.py::g" in d.removed
    assert all("tests/" not in c.relpath for c in d.candidates)


def test_syntax_error_is_reported_not_raised(make_pr):
    base, head = make_pr({"m.py": SRC}, {"m.py": SRC + "\ndef broken(:\n"})
    d = diff_trees(base, head)
    assert d.parse_errors and "m.py" in d.parse_errors[0]


def test_import_target_layouts(make_pr):
    base, _ = make_pr({"src/pkg/__init__.py": "", "src/pkg/sub/__init__.py": "", "src/pkg/sub/mod.py": "",
                       "flat.py": ""}, {})
    root, mod = import_target(base, "src/pkg/sub/mod.py")
    assert mod == "pkg.sub.mod" and root.endswith("src")
    root, mod = import_target(base, "flat.py")
    assert mod == "flat"
