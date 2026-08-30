"""Repro probe for pylint-dev/astroid#3258.

`class C[__slots__]: ...` (PEP 695 generic class syntax, where the type
parameter happens to be named `__slots__`) crashes ClassDef.slots() with
AttributeError instead of treating it as an ordinary (non-slotted) class:
_islots() walks the class's assigned `__slots__` value and calls
`.getattr(meth)` on it, but here that "value" resolves to a TypeVar node
(the generic type parameter object), which has no `.getattr` method.

Exit 0 once fixed (slots() returns cleanly, e.g. None/empty); non-zero
while the bug is present (raw AttributeError leaks out).
"""
import sys

import astroid


def main() -> int:
    code = "class C[__slots__]:\n    def __init__(self):\n        self.a = 1\n"
    module = astroid.parse(code)
    klass = module.body[0]
    try:
        klass.slots()
    except AttributeError as e:
        print(f"BUG PRESENT: raw AttributeError leaked out: {e}")
        return 1
    except astroid.exceptions.AstroidError as e:
        str(e)
        print(f"OK: clean AstroidError, no internal crash: {e}")
        return 0
    print("OK: slots() completed without error")
    return 0


if __name__ == "__main__":
    sys.exit(main())
