import pytest
from pydantic import ValidationError

from atomic_forge.models import ApiSpec, AtomicTask, AtomicTaskBatch, TestTriad


def _triad():
    return TestTriad(positive="p", negative="n", negative_to_positive="r")


def test_dev_task_requires_test_triad():
    with pytest.raises(ValidationError):
        AtomicTask(name="t", task_type="dev", action="create", file_path="a.py", description="d")


def test_dev_delete_task_does_not_require_test_triad():
    t = AtomicTask(name="t", task_type="dev", action="delete", file_path="a.py", description="d")
    assert t.test_triad is None


def test_api_layer_requires_api_spec():
    with pytest.raises(ValidationError):
        AtomicTask(name="t", task_type="dev", action="create", file_path="a.py",
                   description="d", layer="API", test_triad=_triad())


def test_valid_task_round_trips():
    t = AtomicTask(name="t", task_type="dev", action="create", file_path="a.py",
                   description="d", test_triad=_triad(),
                   api_spec=ApiSpec(endpoint="GET /x", request_schema="{}", response_schema="{}"))
    assert t.id  # auto-assigned
    dumped = t.model_dump()
    again = AtomicTask.model_validate(dumped)
    assert again.name == "t"


def test_invalid_task_type_rejected():
    with pytest.raises(ValidationError):
        AtomicTask(name="t", task_type="bogus", action="create", file_path="a.py",
                   description="d", test_triad=_triad())


def test_invalid_action_rejected():
    with pytest.raises(ValidationError):
        AtomicTask(name="t", task_type="dev", action="bogus", file_path="a.py",
                   description="d", test_triad=_triad())


def test_batch_helpers():
    dev = AtomicTask(name="d", task_type="dev", action="create", file_path="a.py",
                     description="x", test_triad=_triad())
    qa = AtomicTask(name="q", task_type="qa", action="create", file_path="tests/a.py", description="x")
    batch = AtomicTaskBatch(tasks=[dev, qa])
    assert batch.dev_tasks() == [dev]
    assert batch.qa_tasks() == [qa]
    assert batch.by_path()["a.py"] is dev
