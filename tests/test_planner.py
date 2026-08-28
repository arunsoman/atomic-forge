import pytest

from atomic_forge.models import AtomicTask, AtomicTaskBatch, TestTriad
from atomic_forge.planner import topo_layers, topo_order


def _t(name, file_path, deps=None, action="create"):
    return AtomicTask(
        name=name, task_type="dev", action=action, file_path=file_path, description="d",
        dependencies=deps or [],
        test_triad=None if action == "delete" else TestTriad(positive="p", negative="n", negative_to_positive="r"),
    )


def test_simple_chain_order():
    a = _t("a", "a.py")
    b = _t("b", "b.py", deps=["a.py"])
    c = _t("c", "c.py", deps=["b.py"])
    batch = AtomicTaskBatch(tasks=[c, a, b])
    order = [t.name for t in topo_order(batch)]
    assert order.index("a") < order.index("b") < order.index("c")


def test_cycle_raises():
    a = _t("a", "a.py", deps=["b.py"])
    b = _t("b", "b.py", deps=["a.py"])
    batch = AtomicTaskBatch(tasks=[a, b])
    with pytest.raises(ValueError, match="cycle"):
        topo_order(batch)


def test_same_file_path_chained_in_declaration_order():
    a = _t("a1", "shared.py")
    b = _t("a2", "shared.py")
    batch = AtomicTaskBatch(tasks=[a, b])
    order = [t.name for t in topo_order(batch)]
    assert order == ["a1", "a2"]


def test_layers_group_independent_tasks():
    a = _t("a", "a.py")
    b = _t("b", "b.py")
    c = _t("c", "c.py", deps=["a.py", "b.py"])
    batch = AtomicTaskBatch(tasks=[a, b, c])
    layers = topo_layers(batch)
    assert {t.name for t in layers[0]} == {"a", "b"}
    assert [t.name for t in layers[1]] == ["c"]


def test_external_dependency_ignored():
    a = _t("a", "a.py", deps=["not_in_batch.py"])
    batch = AtomicTaskBatch(tasks=[a])
    order = topo_order(batch)
    assert len(order) == 1
