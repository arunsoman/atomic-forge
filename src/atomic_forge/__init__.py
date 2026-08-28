"""atomic-forge: an agentic generate -> test -> repair loop for LLM codegen.

Quickstart:

    from atomic_forge import AtomicTask, TestTriad, AtomicTaskBatch
    from atomic_forge.generate_agent import generate_batch_agentic
    from atomic_forge.qa import qa_phase
    from atomic_forge.repair_agent import repair_loop_agentic
    from atomic_forge.llm import default_llm
    from atomic_forge.tools import make_tools
    from atomic_forge.trajectory import Trajectory

See README.md for the full walkthrough.
"""
from .models import ApiSpec, AtomicTask, AtomicTaskBatch, TestTriad

__version__ = "0.1.0"

__all__ = ["AtomicTask", "AtomicTaskBatch", "ApiSpec", "TestTriad", "__version__"]
