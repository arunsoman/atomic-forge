import astroid


def test_constructor_inference():
    # This reproduces the bug where astroid cannot infer through a class constructor
    # when the parameter is passed in (with type hint).
    code = """
class Test:
    def test():
        return
class TestParent:
    def __init__(self, test: Test):
        self._test = test  # type: Test

    def test_class(self):
        self._test.test() #@

test_parent = TestParent(Test())
"""
    func_node = astroid.extract_node(code)
    # func_node should be the Call node representing self._test.test()
    # Infer the function being called (the attribute access)
    for inferred in func_node.func.infer():
        # If any inferred is Uninferable, the bug is present
        assert inferred is not astroid.Uninferable, "Uninferable inferred during attribute access"