"""Repro probe for pylint-dev/astroid#3259.

`def f(x, *y, y: tuple[x]):` crashes with IndexError instead of a clean
astroid inference result/error: inferring the annotation `tuple[x]`'s
slice reaches _arguments_infer_argname -> default_value(name), which
indexes self.defaults[idx] without checking idx is in range (the
parameter list here has a name collision between the `*y` vararg and the
`y:` keyword-only param, throwing off the positional-default index math).

Exit 0 once fixed (inference completes cleanly, or raises a clean astroid
error); non-zero while the bug is present (raw IndexError leaks out).
"""
import sys

import astroid


def main() -> int:
    code = "def f(x, *y, y: tuple[x]):\n    pass\n"
    module = astroid.parse(code)
    func = module.body[0]
    # Mirror pylint's own trigger: safe_infer(node.slice) on the Subscript
    # annotation `tuple[x]` of the keyword-only arg `y`.
    annotation = func.args.kwonlyargs_annotations[0]  # the `tuple[x]` Subscript
    try:
        list(annotation.slice.infer())
    except astroid.exceptions.AstroidError as e:
        str(e)
        print(f"OK: clean AstroidError, no internal crash: {e}")
        return 0
    except IndexError as e:
        print(f"BUG PRESENT: raw IndexError leaked out: {e}")
        return 1
    print("OK: inference completed without error")
    return 0


if __name__ == "__main__":
    sys.exit(main())
