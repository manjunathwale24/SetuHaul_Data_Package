"""LangGraph ingress router for deterministic and safety-escalation message flows."""

from __future__ import annotations

import re
from typing import Any, Callable, TypedDict

from langgraph.graph import END, START, StateGraph


_SAFETY_EMERGENCY = re.compile(
    r"\b(?:emergency|accident|fire|injur(?:y|ed)|medical|brakes?\s+(?:failed|failure|not working)|"
    r"failed brakes|steering\s+(?:failed|failure|not working)|tyre burst|tire burst|fuel leak)\b",
    re.IGNORECASE,
)


def is_safety_emergency(text: str) -> bool:
    return bool(_SAFETY_EMERGENCY.search(text))


class MessageState(TypedDict, total=False):
    body: dict[str, Any]
    route: str
    response: dict[str, Any]


MessageHandler = Callable[[dict[str, Any]], dict[str, Any]]
DriverCheck = Callable[[str], bool]


def build_message_graph(
    normal_handler: MessageHandler,
    emergency_handler: MessageHandler,
    is_driver: DriverCheck,
):
    """Route driver safety reports separately; both paths retain deterministic service handling."""

    def safety_gate(state: MessageState) -> dict[str, str]:
        body = state["body"]
        sender = body["sender"]
        route = "emergency" if is_driver(sender) and is_safety_emergency(body["text"]) else "normal"
        return {"route": route}

    def normal_message(state: MessageState) -> dict[str, Any]:
        return {"response": normal_handler(state["body"])}

    def emergency_message(state: MessageState) -> dict[str, Any]:
        return {"response": emergency_handler(state["body"])}

    graph = StateGraph(MessageState)
    graph.add_node("safety_gate", safety_gate)
    graph.add_node("normal_message", normal_message)
    graph.add_node("emergency_message", emergency_message)
    graph.add_edge(START, "safety_gate")
    graph.add_conditional_edges(
        "safety_gate",
        lambda state: state["route"],
        {"normal": "normal_message", "emergency": "emergency_message"},
    )
    graph.add_edge("normal_message", END)
    graph.add_edge("emergency_message", END)
    return graph.compile()
