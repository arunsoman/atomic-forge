#!/usr/bin/env python3
"""F1 pre-flight probe for pylint-dev/astroid#769.

Issue (2021, still open): "Unable to infer through a class constructor
even with type hints" — inference through an attribute assigned from a
constructor ARGUMENT returns Uninferable. Validated 2026-08-31 against
astroid main @ 4.4.0.dev0:
  v1 self._test = Test()  # type: Test  -> infers `test`  (fixed since)
  v2 self._test = test   # type: Test   -> [Uninferable] (bug present)

Contract: exit 0 = issue fixed (both variants infer);
exit != 0 = bug present (probe contract of `atomic-forge fix --repro`).
"""
import astroid

CASES = {
    "v1-created-in-ctor": "self._test = Test()  # type: Test",
    "v2-arg-with-annotation": "self._test = test  # type: Test",  # the live bug
}


def one_case(assign: str) -> dict:
    code = f"""
class Test:
    def test():
        return
class TestParent:
    def __init__(self, test: Test):
        {assign}

    def test_class(self):
        self._test.test() #@

test_parent = TestParent(Test())
"""
    node = astroid.extract_node(code)
    res = list(node.func.infer())
    names = [getattr(r, "name", r) for r in res]
    return {"assignment": assign, "inferred": names,
            "bug" if any(r is astroid.Uninferable for r in res) else "ok": True}


def main() -> int:
    results = {k: one_case(v) for k, v in CASES.items()}
    for k, r in results.items():
        state = "BUG (Uninferable)" if "bug" in r else f"ok {r['inferred']}"
        print(f"[probe astroid#769] {k}: {state}")
    still_buggy = [k for k, r in results.items() if "bug" in r]
    if still_buggy:
        print(f"[probe astroid#769] exit 1 — bug present on: {', '.join(still_buggy)}")
        return 1
    print("[probe astroid#769] exit 0 — both variants infer; issue appears fixed on HEAD")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())