from atomic_forge.codegraph import CodeGraph
from atomic_forge.symbols import SymbolIndex
from atomic_forge.stacks import detect_test_stack, is_test_file


# -------------------------------------------------- stack detection (R16a) ----

def test_detects_cmake_stack(tmp_path):
    (tmp_path / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.10)\nproject(foo C)\nenable_testing()\n"
        "add_executable(main main.c)\nadd_test(NAME main COMMAND main)\n"
    )
    stack = detect_test_stack(tmp_path)
    assert stack is not None
    assert "cmake" in stack.cmd
    assert "ctest" in stack.cmd
    assert stack.image == "gcc:14"


def test_cmake_configure_enables_build_testing(tmp_path):
    """`enable_testing()`/`add_test(` can sit inside an
    `if(BUILD_TESTING) ... endif()` guard — the standard CTest option
    (from `include(CTest)`) that some projects default OFF. The marker
    scan finds the text either way (same as before), but the actual
    configure step must turn the option ON or a guarded project silently
    builds with no tests at all despite `_cmake_declares_tests` saying
    yes."""
    (tmp_path / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.10)\nproject(foo C)\n"
        "include(CTest)\n"
        "if(BUILD_TESTING)\n"
        "  add_executable(main main.c)\n"
        "  add_test(NAME main COMMAND main)\n"
        "endif()\n"
    )
    stack = detect_test_stack(tmp_path)
    assert stack is not None
    assert "-DBUILD_TESTING=ON" in stack.cmd


def test_cmake_without_scannable_tests_is_untreated(tmp_path):
    """A CMake repo that never declares tests deterministically says so —
    'not testable yet' (existing RepoStack contract), not a guessed
    command that later dies with `No tests were found!!!`."""
    (tmp_path / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.10)\nproject(foo C)\n"
        "add_executable(main main.c)\n"
    )
    (tmp_path / "main.c").write_text("int main(void) { return 0; }\n")
    assert detect_test_stack(tmp_path) is None


def test_cmake_tests_declared_in_subdirectory_are_found(tmp_path):
    (tmp_path / "CMakeLists.txt").write_text("project(foo C)\nadd_subdirectory(tests)\n")
    sub = tmp_path / "tests"
    sub.mkdir()
    (sub / "CMakeLists.txt").write_text("add_test(NAME t COMMAND t)\n")
    stack = detect_test_stack(tmp_path)
    assert stack is not None
    assert "ctest" in stack.cmd


def test_detects_makefile_with_test_target(tmp_path):
    (tmp_path / "Makefile").write_text(
        "all: main.o\nmain: main.o\n\tcc -o main main.c\n\ntest: all\n\t./main\n"
    )
    stack = detect_test_stack(tmp_path)
    assert stack is not None
    assert stack.cmd == "make -j test"
    assert stack.image == "gcc:14"


def test_makefile_prefers_check_when_no_test_target(tmp_path):
    (tmp_path / "Makefile").write_text("check:\n\trun-tests.sh\n")
    stack = detect_test_stack(tmp_path)
    assert stack is not None
    assert stack.cmd == "make -j check"


def test_makefile_without_test_target_is_not_testable(tmp_path):
    (tmp_path / "Makefile").write_text("all:\n\tcc main.c\n")
    assert detect_test_stack(tmp_path) is None


def test_detects_autotools_stack(tmp_path):
    (tmp_path / "configure.ac").write_text("AC_INIT([foo], [1.0])\n")
    (tmp_path / "Makefile.am").write_text("bin_PROGRAMS = foo\n")
    stack = detect_test_stack(tmp_path)
    assert stack is not None
    assert "autoreconf" in stack.cmd
    assert "./configure" in stack.cmd
    assert "make check" in stack.cmd


def test_autotools_with_checked_in_configure_skips_autoreconf(tmp_path):
    (tmp_path / "configure").write_text("#!/bin/sh\n")
    stack = detect_test_stack(tmp_path)
    assert stack is not None
    assert "autoreconf" not in stack.cmd


def test_makefile_takes_priority_over_autotools_when_configure_present(tmp_path):
    # A checked-in configure with a Makefile that HAS a test target: the
    # Makefile path is the more direct, less speculative command.
    (tmp_path / "Makefile").write_text("test:\n\t./run_tests\n")
    stack = detect_test_stack(tmp_path)
    assert stack is not None
    assert stack.cmd == "make -j test"


def test_is_test_file_cpp():
    assert is_test_file("tests/CalcTest.cc")
    assert is_test_file("test/foo_test.cpp")
    assert is_test_file("src/calc_test.cxx")
    assert is_test_file("tests/calc_tests.cpp")
    assert is_test_file("tests/CalcTest.hpp")
    assert not is_test_file("src/calc.cpp")
    assert not is_test_file("src/calculator.c")


def test_c_and_node_stack_combine(tmp_path):
    (tmp_path / "package.json").write_text('{"scripts": {"test": "vitest run"}}')
    (tmp_path / "CMakeLists.txt").write_text("enable_testing()\nadd_test(NAME t COMMAND t)\n")
    stack = detect_test_stack(tmp_path)
    assert stack is not None
    assert "npm test" in stack.cmd and "ctest" in stack.cmd


# ------------------------------------------------ symbol parsing (R16a) ----

_CPP_FREE_FUNCTIONS = """\
#include <stdio.h>

/* a comment that mentions decoy_function(x) should not create a symbol */
static int helper(int x) {
    if (x > 0) {
        return x;   // calls inside control flow must not break parsing
    }
    return -x;
}

int add(int a, int b) {
    return helper(a) + b;
}
"""


def test_cpp_parse_free_function(tmp_path):
    (tmp_path / "calc.c").write_text(_CPP_FREE_FUNCTIONS)
    idx = SymbolIndex(tmp_path)
    idx.build()
    syms = {s.name: s for s in idx.symbols}
    assert {"helper", "add"} <= set(syms)
    assert syms["helper"].kind == "function"
    assert syms["helper"].line == 4  # the `if (x) {` line must NOT be a def


def test_cpp_parse_class_and_methods(tmp_path):
    (tmp_path / "calc.hpp").write_text(
        "class Calculator {\n"
        "public:\n"
        "    int add(int a, int b);\n"
        "    int sub(int a, int b);\n"
        "private:\n"
        "    int value_ = 0;\n"
        "};\n"
        "\n"
        "int Calculator::add(int a, int b) { return value_ + a; }\n"
    )
    idx = SymbolIndex(tmp_path)
    idx.build()
    syms = {s.name: s for s in idx.symbols}
    assert "Calculator" in syms and syms["Calculator"].kind == "class"
    assert "add" in syms and syms["add"].kind == "method"
    assert "::" not in syms["add"].signature  # signature stays a clean call shape


def test_cpp_parse_skips_control_statements_and_macros(tmp_path):
    (tmp_path / "weird.c").write_text(
        "#include <stdio.h>\n"
        "\n"
        "int main(void) {\n"
        "    if (1) { return helper(1); }\n"
        "    for (int i = 0; i < 3; i++) { helper(i); }\n"
        "    while (0) { continue; }\n"
        "    switch (1) { case 1: break; }\n"
        "    return 0;\n"
        "}\n"
        "#define CALL(X) (X)\n"
    )
    idx = SymbolIndex(tmp_path)
    idx.build()
    names = [s.name for s in idx.symbols]
    assert names == ["main"]


def test_cpp_graph_callers_and_callees(tmp_path):
    (tmp_path / "util.c").write_text(
        "int helper(int x) {\n    return x * 2;\n}\n"
    )
    (tmp_path / "main.c").write_text(
        "int helper(int x);\n\nint run(int a) {\n    return helper(a) + 1;\n}\n"
    )
    graph = CodeGraph(project_dir=tmp_path)
    graph.build()
    counts = graph.counts()
    assert counts["files"] == 2
    assert counts["symbols"] >= 2
    assert any(c["symbol"] == "run" for c in graph.callers("helper"))
    assert any(c["symbol"] == "helper" for c in graph.callees("run"))
    graph.close()