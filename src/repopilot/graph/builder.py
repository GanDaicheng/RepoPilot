"""LangGraph assembly and stable checkpoint configuration."""

from __future__ import annotations

from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from repopilot.graph.nodes import GraphDependencies, GraphNodes
from repopilot.graph.routing import (
    route_after_apply,
    route_after_capture,
    route_after_failure,
    route_after_plan,
    route_after_proposal,
    route_after_retry,
    route_after_review,
    route_after_summary,
    route_after_tests,
    route_after_validate,
    route_after_worktree,
)
from repopilot.graph.state import RepoPilotState


def graph_config(thread_id: str) -> RunnableConfig:
    normalized = str(UUID(thread_id))
    return {"configurable": {"thread_id": normalized}}


def build_graph(
    deps: GraphDependencies,
    checkpointer: BaseCheckpointSaver,
) -> CompiledStateGraph:
    nodes = GraphNodes(deps)
    graph = StateGraph(RepoPilotState)
    for name in (
        "validate_request", "capture_base_commit", "create_worktree",
        "summarize_repository", "plan_change", "propose_patch", "propose_fix",
        "apply_patch", "run_tests", "inspect_failure", "review_change",
        "schedule_retry", "awaiting_approval", "success_report", "failed_report",
        "cancelled_report",
    ):
        graph.add_node(name, getattr(nodes, name))
    graph.add_edge(START, "validate_request")
    graph.add_conditional_edges("validate_request", route_after_validate)
    graph.add_conditional_edges("capture_base_commit", route_after_capture)
    graph.add_conditional_edges("create_worktree", route_after_worktree)
    graph.add_conditional_edges("summarize_repository", route_after_summary)
    graph.add_conditional_edges("plan_change", route_after_plan)
    graph.add_conditional_edges("propose_patch", route_after_proposal)
    graph.add_conditional_edges("propose_fix", route_after_proposal)
    graph.add_conditional_edges("apply_patch", route_after_apply)
    graph.add_conditional_edges("run_tests", route_after_tests)
    graph.add_conditional_edges("inspect_failure", route_after_failure)
    graph.add_conditional_edges("review_change", route_after_review)
    graph.add_conditional_edges("schedule_retry", route_after_retry)
    for terminal in (
        "awaiting_approval", "success_report", "failed_report", "cancelled_report"
    ):
        graph.add_edge(terminal, END)
    return graph.compile(checkpointer=checkpointer)
