"""Self-healing REST API for SetuHaul Logistics.

Endpoints (all self-healing, observable, controllable, remediable):

  GET  /health                              — agent + service health
  GET  /monitor/status                      — latest monitor scan results
  GET  /monitor/targets                     — monitored targets (deployment, pods, service)
  POST /heal/actions                        — list pending/recent heal actions
  POST /heal/execute                        — trigger a scan+heal cycle manually
  GET  /heal/history                        — full heal action history
  POST /pr/create                           — manually create a PR for a given action
  GET  /pr/list                             — list created PRs (from history)
  GET  /config/rules                        — guardrail + agent configuration
  POST /webhooks/github                     — GitHub webhook receiver (PR comments, etc.)

Designed to be mounted into the existing SetuHaul service (service.py) or run standalone.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any
from urllib.parse import urlparse, parse_qs

from self_healing_agent import (
    SelfHealingAgent, FailureEvent, HealStatus, HealAction,
    Guardrails, Executor, PRCreator, Monitor, Healer,
    SELF_HEALING_LLM_ENABLED, SELF_HEALING_POLL_INTERVAL,
    SELF_HEALING_GITHUB_TOKEN, SELF_HEALING_GITHUB_REPO,
    SELF_HEALING_K8S_NAMESPACE, SELF_HEALING_DEPLOYMENT_NAME,
    SELF_HEALING_SERVICE_URL, ALLOWED_KUBECTL_VERBS, DESTRUCTIVE_VERBS,
)

# Single agent instance (module-level singleton)
_agent_instance: SelfHealingAgent | None = None


def _agent() -> SelfHealingAgent:
    global _agent_instance
    if _agent_instance is None:
        _agent_instance = SelfHealingAgent()
    return _agent_instance


def _json_body(handler) -> dict[str, Any]:
    """Read and parse JSON body from the HTTP handler."""
    raw = handler.rfile.read(int(handler.headers.get("Content-Length") or 0))
    if not raw:
        return {}
    return json.loads(raw)


def _send_json(handler, status: int, payload: dict[str, Any]) -> None:
    data = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


# ---------------------------------------------------------------------------
# Endpoint handlers — each receives (service, match_groups, body, query)
# ---------------------------------------------------------------------------


def sh_health(svc, groups, body, query):
    """GET /health — agent and service health."""
    a = _agent()
    svc_health = None
    try:
        status, svc_health = svc.health()
    except Exception as exc:  # service may not have health() in all contexts
        svc_health = {"error": str(exc)}

    return 200, {
        "agent": "self-healing",
        "status": "ok",
        "llm_enabled": SELF_HEALING_LLM_ENABLED,
        "service_health": svc_health,
        "config": a.get_config(),
    }


def sh_monitor_status(svc, groups, body, query):
    """GET /monitor/status — latest monitor scan results."""
    a = _agent()
    monitor = a.monitor
    events = monitor.scan()  # fresh scan
    return 200, {
        "scan_timestamp": events[0].timestamp if events else None,
        "events": [
            {
                "timestamp": e.timestamp,
                "source": e.source,
                "target": e.target,
                "severity": e.severity,
                "message": e.message,
                "details": e.details,
            }
            for e in events
        ],
        "event_count": len(events),
    }


def sh_monitor_targets(svc, groups, body, query):
    """GET /monitor/targets — monitored targets."""
    a = _agent()
    return 200, {
        "service_url": SELF_HEALING_SERVICE_URL,
        "k8s_namespace": SELF_HEALING_K8S_NAMESPACE,
        "deployment": SELF_HEALING_DEPLOYMENT_NAME,
        "targets": [
            {"type": "service", "url": f"{SELF_HEALING_SERVICE_URL}/health"},
            {"type": "service", "url": f"{SELF_HEALING_SERVICE_URL}/deployment/status"},
            {"type": "k8s", "target": f"deployment/{SELF_HEALING_DEPLOYMENT_NAME}",
             "namespace": SELF_HEALING_K8S_NAMESPACE},
            {"type": "k8s", "target": "pods", "namespace": SELF_HEALING_K8S_NAMESPACE,
             "label": f"app={SELF_HEALING_DEPLOYMENT_NAME}"},
        ],
    }


def sh_heal_actions(svc, groups, body, query):
    """POST /heal/actions — list heal actions (optionally filter by status)."""
    a = _agent()
    status_filter = body.get("status") if isinstance(body, dict) else None
    actions = a.history
    if status_filter:
        actions = [a_ for a_ in actions if a_.status.value == status_filter]
    return 200, {
        "actions": [
            {
                "id": act.id,
                "event_id": act.event_id,
                "status": act.status.value,
                "proposed_by": act.proposed_by,
                "action_type": act.action_type,
                "description": act.description,
                "kubectl_command": act.kubectl_command,
                "pr_title": act.pr_title,
                "pr_number": act.pr_number,
                "created_at": act.created_at,
                "executed_at": act.executed_at,
                "error": act.error,
            }
            for act in actions
        ],
        "count": len(actions),
    }


def sh_heal_execute(svc, groups, body, query):
    """POST /heal/execute — trigger a manual scan+heal cycle."""
    a = _agent()
    actions = a.scan_and_heal()
    return 200, {
        "triggered_at": actions[0].created_at if actions else None,
        "actions_executed": len(actions),
        "actions": [
            {
                "id": act.id,
                "status": act.status.value,
                "action_type": act.action_type,
                "description": act.description,
                "pr_number": act.pr_number,
                "error": act.error,
            }
            for act in actions
        ],
    }


def sh_heal_history(svc, groups, body, query):
    """GET /heal/history — full heal action history."""
    a = _agent()
    limit = int(query.get("limit", "50"))
    actions = a.history[-limit:]
    return 200, {
        "actions": [
            {
                "id": act.id,
                "event_id": act.event_id,
                "status": act.status.value,
                "proposed_by": act.proposed_by,
                "action_type": act.action_type,
                "description": act.description,
                "kubectl_command": act.kubectl_command,
                "pr_title": act.pr_title,
                "pr_number": act.pr_number,
                "created_at": act.created_at,
                "executed_at": act.executed_at,
                "error": act.error,
            }
            for act in actions
        ],
        "count": len(actions),
        "total": len(a.history),
    }


def sh_pr_create(svc, groups, body, query):
    """POST /pr/create — manually create a PR for a given action or free-text."""
    a = _agent()
    title = body.get("title")
    body_text = body.get("body")
    action_id = body.get("action_id")

    if action_id:
        act = next((x for x in a.history if x.id == action_id), None)
        if not act:
            return 404, {"error": f"action {action_id} not found"}
        title = act.pr_title or f"Self-heal remediation: {act.description}"
        body_text = act.pr_body or act.description
        success, pr_number, error = a.pr_creator.create_pr(act)
        if success:
            act.status = HealStatus.PR_CREATED
            act.pr_number = pr_number
            return 200, {
                "success": True, "pr_number": pr_number, "action_id": action_id,
            }
        return 400, {"error": error or "PR creation failed"}

    if not title or not body_text:
        return 400, {"error": "title and body are required (or provide action_id)"}

    # Create a synthetic action for manual PR creation
    act = HealAction(
        id=f"manual-{int(time.time() * 1000)}",
        event_id="manual",
        status=HealStatus.PENDING,
        proposed_by="manual",
        action_type="pr_only",
        description=body_text,
        pr_title=title,
        pr_body=body_text,
    )
    success, pr_number, error = a.pr_creator.create_pr(act)
    if success:
        act.status = HealStatus.PR_CREATED
        act.pr_number = pr_number
        a.history.append(act)
        return 200, {"success": True, "pr_number": pr_number, "action_id": act.id}
    return 400, {"error": error or "PR creation failed"}


def sh_pr_list(svc, groups, body, query):
    """GET /pr/list — list PRs created by the agent."""
    a = _agent()
    prs = [
        {
            "action_id": act.id,
            "pr_number": act.pr_number,
            "pr_title": act.pr_title,
            "status": act.status.value,
            "created_at": act.created_at,
            "action_type": act.action_type,
        }
        for act in a.history
        if act.pr_number is not None
    ]
    return 200, {"pull_requests": prs, "count": len(prs)}


def sh_config_rules(svc, groups, body, query):
    """GET /config/rules — guardrail + agent configuration."""
    a = _agent()
    return 200, a.get_config()


def sh_webhooks_github(svc, groups, body, query):
    """POST /webhooks/github — GitHub webhook receiver."""
    event_type = body.get("event_type", "unknown") if isinstance(body, dict) else "unknown"
    return 200, {
        "received": True,
        "event": event_type,
        "note": "GitHub webhook endpoint ready; configure in GitHub repo settings → Webhooks. "
                "Pass {\"event_type\": \"...\"} in the body for testing.",
    }


# ---------------------------------------------------------------------------
# Route table — merge into service.py ROUTES
# ---------------------------------------------------------------------------

SELF_HEALING_ROUTES = [
    ("GET",  re.compile(r"/self-healing/health"), sh_health),
    ("GET",  re.compile(r"/self-healing/monitor/status"), sh_monitor_status),
    ("GET",  re.compile(r"/self-healing/monitor/targets"), sh_monitor_targets),
    ("POST", re.compile(r"/self-healing/heal/actions"), sh_heal_actions),
    ("POST", re.compile(r"/self-healing/heal/execute"), sh_heal_execute),
    ("GET",  re.compile(r"/self-healing/heal/history"), sh_heal_history),
    ("POST", re.compile(r"/self-healing/pr/create"), sh_pr_create),
    ("GET",  re.compile(r"/self-healing/pr/list"), sh_pr_list),
    ("GET",  re.compile(r"/self-healing/config/rules"), sh_config_rules),
    ("POST", re.compile(r"/self-healing/webhooks/github"), sh_webhooks_github),
]



