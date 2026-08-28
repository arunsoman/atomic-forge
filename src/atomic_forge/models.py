"""
AtomicTask — the contract between a planning layer (yours) and forge's
generate/repair pipeline.

The whole "zero imagination" idea in this codebase cashes out to one rule:
an LLM never writes code against a description alone. It writes exactly one
file, against an exact, machine-validated contract (imports, signatures,
steps), with a required test_triad for anything real. `AtomicTask` is that
contract, enforced by `_enforce_contract` below at construction time, not
hoped for in a prompt.

You are expected to produce `AtomicTask`s yourself (from a PRD, an issue,
a spec, a human) and hand a batch of them to `forge.generate_agent`. Forge
does not care where they came from.
"""
from __future__ import annotations

import uuid
from typing import List, Optional

from pydantic import BaseModel, Field, model_validator


class GraphNode(BaseModel):
    """Minimal node identity: a stable id plus parent/child links, enough
    to track provenance if you're building a task graph on top of this.
    forge itself never reads parent_ids/child_ids — they're here purely so
    a caller can round-trip its own DAG through AtomicTask without a
    separate id scheme."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    parent_ids: List[str] = Field(default_factory=list)
    child_ids: List[str] = Field(default_factory=list)


class ApiSpec(BaseModel):
    """Contract between frontend and backend for one HTTP endpoint. Both
    the UI task and the backend task for the same route must carry an
    identical ApiSpec — that's what keeps them in sync without either
    having read the other's file."""

    endpoint: str = Field(description="e.g. POST /api/v1/import/parse-csv")
    request_schema: str = Field(description="JSON schema or exact TS/Pydantic interface of the request body")
    response_schema: str = Field(description="JSON schema or exact interface of the response body")
    error_codes: List[str] = Field(default_factory=list, description="e.g. ['400 Bad Request', '500 Parse Error']")


class TestTriad(BaseModel):
    """The three test cases required of every dev task: proof the happy
    path works, proof the failure path is handled, and proof recovery
    works. Not optional decoration — `generate_file_agentic`'s companion
    QA phase turns this directly into a real test file."""

    #: Not a pytest test class despite the name — silences pytest's
    #: collection warning for any module that imports this.
    __test__ = False

    positive: str = Field(description="Happy path. e.g. 'Valid CSV parses and returns 200 with a transaction array.'")
    negative: str = Field(description="Failure path. e.g. 'Malformed CSV returns 400 Bad Request.'")
    negative_to_positive: str = Field(description="Recovery path. e.g. 'Retry after fixing the file succeeds.'")


class AtomicTask(GraphNode):
    """One task = one file. The only unit forge's generator/repair loop
    accepts."""

    # Identity
    name: str
    userstory_id: str = ""
    parent_feature: str = ""

    # Classification
    task_type: str = Field(description="'dev' or 'qa'")
    layer: str = Field(default="Backend", description="free-form: 'Backend' | 'Frontend' | 'API' | 'Data' | ...")
    action: str = Field(description="'create' | 'modify' | 'delete'")
    file_path: str = Field(description="Path from project root, e.g. 'src/api/parse_csv.py'")

    # The contract
    description: str = Field(description="1-2 sentences: exactly what this file does.")
    exact_imports: List[str] = Field(default_factory=list, description="Exact import statements needed.")
    function_signatures: List[str] = Field(default_factory=list, description="Exact signatures this file must define.")
    step_by_step_implementation: List[str] = Field(default_factory=list, description="Numbered implementation steps.")
    dependencies: List[str] = Field(default_factory=list, description="Other file_paths this task depends on.")

    # API sync
    api_spec: Optional[ApiSpec] = Field(default=None, description="Required when layer == 'API'.")

    # Testing
    test_triad: Optional[TestTriad] = Field(default=None, description="Required for 'dev' tasks.")

    # Optional context a caller may set; forge renders these into the
    # generation/repair prompt when present, but never requires them.
    required_permission: Optional[str] = Field(
        default=None, description="An access-control token this file's functionality is gated on, if any.")
    preconditions: List[str] = Field(default_factory=list, description="Story-level state that must hold before.")
    postconditions: List[str] = Field(default_factory=list, description="Story-level state that must hold after.")
    origin_requirement: Optional[str] = Field(default=None, description="A compliance/capability tag this task fulfills, if any.")

    @model_validator(mode="after")
    def _enforce_contract(self) -> "AtomicTask":
        if self.task_type not in ("dev", "qa"):
            raise ValueError(f"{self.name}: task_type must be 'dev' or 'qa', got {self.task_type!r}")
        if self.action not in ("create", "modify", "delete"):
            raise ValueError(f"{self.name}: action must be create|modify|delete, got {self.action!r}")
        if self.task_type == "dev" and self.action != "delete" and self.test_triad is None:
            raise ValueError(f"{self.name}: 'dev' tasks must carry a test_triad (unless action == 'delete')")
        if self.layer == "API" and self.api_spec is None:
            raise ValueError(f"{self.name}: API-layer tasks must carry an api_spec")
        return self


class AtomicTaskBatch(BaseModel):
    """A set of tasks handed to the pipeline together — enough for
    dependency-ordered / concurrent generation (see `planner.topo_layers`)."""

    tasks: List[AtomicTask]

    def by_path(self) -> dict:
        return {t.file_path: t for t in self.tasks}

    def dev_tasks(self) -> List[AtomicTask]:
        return [t for t in self.tasks if t.task_type == "dev"]

    def qa_tasks(self) -> List[AtomicTask]:
        return [t for t in self.tasks if t.task_type == "qa"]
