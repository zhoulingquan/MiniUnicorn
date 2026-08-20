"""Execution services split out of ``AgentRunner``.

The LLM request path (:class:`ModelRequestExecutor`), the tool-execution
path (:class:`ToolExecutionCoordinator`) (both PR-5a), the context
governance path (:class:`ContextGovernanceService`) and the turn-recovery
path (:class:`TurnRecoveryPolicy`) (both PR-5b), and the planning /
reflection path (:class:`PlanningReflectionService`) (PR-5c) were
extracted from ``miniunicorn.agent.runner`` so the runner keeps only thin
delegation methods.  All services read the provider / policy collaborators
through their host runner so runtime state such as provider hot-switching
keeps working unchanged.
"""
