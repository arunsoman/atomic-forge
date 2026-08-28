"""
Dependency-ordered execution planning (Kahn topological sort).

Generation must happen in dependency order so that when the LLM writes file B,
the contents of its dependency file A already exist on disk and can be inlined
into the prompt as ground truth — this is what turns "zero imagination" from a
schema slogan into an actual mechanism.
"""
from __future__ import annotations

from typing import Dict, List

from .models import AtomicTask, AtomicTaskBatch


def topo_order(batch: AtomicTaskBatch) -> List[AtomicTask]:
    """Return tasks in an order where every dependency is generated first.

    Dependencies that point outside the batch are ignored (assumed pre-existing
    on disk). Cycles raise — a cyclic task graph is a planning-layer bug.

    Graph nodes are task INDICES, not file_path: two tasks can legitimately
    target the same file_path (e.g. two independently-planned stories both
    touch one shared file) — a path-keyed graph would silently collapse
    them. Tasks sharing a file_path are chained in declaration order instead
    (each must finish before the next writes the same file); a declared
    dependency on that path depends on the LAST task in that chain (its
    final on-disk state).
    """
    tasks = batch.tasks
    n = len(tasks)
    indegree: List[int] = [0] * n
    edges: Dict[int, List[int]] = {i: [] for i in range(n)}

    by_path: Dict[str, List[int]] = {}
    for i, t in enumerate(tasks):
        by_path.setdefault(t.file_path, []).append(i)

    for indices in by_path.values():
        for a, b in zip(indices, indices[1:]):
            edges[a].append(b)
            indegree[b] += 1

    for i, t in enumerate(tasks):
        for dep in t.dependencies:
            if dep in by_path:
                dep_idx = by_path[dep][-1]
                if dep_idx != i:
                    edges[dep_idx].append(i)
                    indegree[i] += 1

    frontier = sorted(i for i in range(n) if indegree[i] == 0)
    ordered_idx: List[int] = []

    while frontier:
        i = frontier.pop(0)
        ordered_idx.append(i)
        for dependent in edges[i]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                frontier.append(dependent)
        frontier.sort()

    if len(ordered_idx) != n:
        remaining = [tasks[i].file_path for i in range(n) if indegree[i] > 0]
        raise ValueError(f"Dependency cycle detected among tasks: {remaining}")
    return [tasks[i] for i in ordered_idx]


def topo_layers(batch: AtomicTaskBatch) -> List[List[AtomicTask]]:
    """Same dependency graph as `topo_order`, but grouped into waves: every
    task in a layer has no dependency (direct or via a shared file_path
    chain) on any OTHER task in that same layer, so a caller can safely
    generate an entire layer's tasks concurrently and only needs to
    serialize between layers, not within one."""
    tasks = batch.tasks
    n = len(tasks)
    indegree: List[int] = [0] * n
    edges: Dict[int, List[int]] = {i: [] for i in range(n)}

    by_path: Dict[str, List[int]] = {}
    for i, t in enumerate(tasks):
        by_path.setdefault(t.file_path, []).append(i)

    for indices in by_path.values():
        for a, b in zip(indices, indices[1:]):
            edges[a].append(b)
            indegree[b] += 1

    for i, t in enumerate(tasks):
        for dep in t.dependencies:
            if dep in by_path:
                dep_idx = by_path[dep][-1]
                if dep_idx != i:
                    edges[dep_idx].append(i)
                    indegree[i] += 1

    frontier = sorted(i for i in range(n) if indegree[i] == 0)
    layers_idx: List[List[int]] = []
    seen = 0

    while frontier:
        layers_idx.append(frontier)
        seen += len(frontier)
        next_frontier: List[int] = []
        for i in frontier:
            for dependent in edges[i]:
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    next_frontier.append(dependent)
        frontier = sorted(next_frontier)

    if seen != n:
        remaining = [tasks[i].file_path for i in range(n) if indegree[i] > 0]
        raise ValueError(f"Dependency cycle detected among tasks: {remaining}")
    return [[tasks[i] for i in layer] for layer in layers_idx]


def read_dependency_context(project_dir, task: AtomicTask, max_chars_per_file: int = 8000) -> str:
    """Inline current on-disk contents of dependency files into a prompt block."""
    from pathlib import Path

    from .sandbox import truncate

    blocks: List[str] = []
    for dep in task.dependencies:
        p = Path(project_dir) / dep
        content = None
        try:
            if p.is_file():
                content = p.read_text(errors="replace")
        except OSError:
            content = None
        if content is not None:
            blocks.append(f"### {dep} (current on-disk content — treat as ground truth)\n"
                          f"```\n{truncate(content, max_chars_per_file)}\n```")
        else:
            blocks.append(f"### {dep}\n[declared dependency — not yet on disk; code against its "
                          f"declared contract only]")
    return "\n\n".join(blocks) if blocks else "(no dependencies)"
