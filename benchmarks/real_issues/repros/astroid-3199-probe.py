"""Repro probe for pylint-dev/astroid#3199.

`namedtuple('{0}', '')` crashes with a nested IndexError instead of
astroid's own UseInferenceDefault/AstroidError: brain_namedtuple_enum's
explicit inference wraps the real ValueError's text into
UseInferenceDefault("ValueError: " + str(exc)), but AstroidError.__str__
blindly calls self.message.format(**vars(self)) — and the original
ValueError's message ("Type names and field names must be valid
identifiers: '{0}'") itself contains a literal '{0}', which .format()
then tries to interpret as a real placeholder with no positional args.

Exit 0 once fixed (inference completes, or raises a clean astroid error
without the nested IndexError); non-zero while the bug is present.
"""
import sys

import astroid


def main() -> int:
    code = "from collections import namedtuple\na = namedtuple('{0}', '')\n"
    module = astroid.parse(code)
    assign = module.body[1]  # [0] is the `from collections import namedtuple` statement
    try:
        list(assign.value.infer())
    except astroid.exceptions.AstroidError as e:
        # A clean astroid-level error is the FIXED behavior — inference
        # is allowed to fail on invalid input, it just must not crash
        # with an internal IndexError while doing so.
        str(e)  # this is exactly where the bug manifests: __str__ crashing
        print(f"OK: clean AstroidError, no internal crash: {e}")
        return 0
    except IndexError as e:
        print(f"BUG PRESENT: raw IndexError leaked out: {e}")
        return 1
    print("OK: inference completed without error")
    return 0


if __name__ == "__main__":
    sys.exit(main())
