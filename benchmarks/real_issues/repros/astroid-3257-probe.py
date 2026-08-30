"""Repro probe for pylint-dev/astroid#3257.

An invalid NamedTuple field (`a.b: str`, an attribute target instead of a
plain name) crashes the namedtuple brain's explicit inference with
AttributeError instead of a clean astroid error: infer_typing_namedtuple_class
accesses `annassign.target.name` assuming every AnnAssign target is a
Name node, but here it's an AssignAttr (`a.b`), which has no `.name`
(only `.attrname`).

Exit 0 once fixed (inference completes, or raises a clean astroid error);
non-zero while the bug is present (raw AttributeError leaks out).
"""
import sys

import astroid


def main() -> int:
    code = (
        "from typing import NamedTuple\n\n"
        "class C(NamedTuple):\n"
        "    a.b: str\n\n"
        "C()\n"
    )
    module = astroid.parse(code)
    call_expr = module.body[-1].value  # the `C()` Call node
    try:
        list(call_expr.infer())
    except AttributeError as e:
        print(f"BUG PRESENT: raw AttributeError leaked out: {e}")
        return 1
    except astroid.exceptions.AstroidError as e:
        str(e)
        print(f"OK: clean AstroidError, no internal crash: {e}")
        return 0
    print("OK: inference completed without error")
    return 0


if __name__ == "__main__":
    sys.exit(main())
