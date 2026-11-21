"""Self-healing agent for SetuHaul Logistics.

Hybrid architecture:
  * Deterministic Monitor — polls /health, /deployment/status, K8s resources,
    and detects failures.
  * LLM Analyzer (optional, feature-flagged) — receives structured failure events,
    proposes root cause + remediation plan, drafts PR patches.
  * Deterministic Guardrails — validate every proposed action against an
    allowlist before anything is executed.
  * PR Creator — opens draft PRs on GitHub with proposed fixes.

Execution is NEVER performed by the LLM. The LLM only advises; deterministic
code validates and executes.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

# Reuse the existing NVIDIA client pattern from nvidia_agents.py
from nvidia_agents import NVIDIAClientError, ask_agent, chat_text

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SELF_HEALING_LLM_ENABLED = os.environ.get("SELF_HEALING_LLM_ENABLED", "false").lower() == "true"
SELF_HEALING_POLL_INTERVAL = int(os.environ.get("SELF_HEALING_POLL_INTERVAL", "30"))
SELF_HEALING_GITHUB_TOKEN = os.environ.get("SELF_HEALING_GITHUB_TOKEN", "")
SELF_HEALING_GITHUB_REPO = os.environ.get("SELF_HEALING_GITHUB_REPO", "")
SELF_HEALING_K8S_NAMESPACE = os.environ.get("SELF_HEALING_K8S_NAMESPACE", "setuhaul")
SELF_HEALING_DEPLOYMENT_NAME = os.environ.get("SELF_HEALING_DEPLOYMENT_NAME", "setuhaul-service")
SELF_HEALING_SERVICE_URL = os.environ.get(
    "SELF_HEALING_SERVICE_URL", "http://localhost:8080"
)

# Guardrail allowlist — only these kubectl verbs/resources may be executed automatically
ALLOWED_KUBECTL_VERBS = {"get", "describe", "logs", "rollout", "scale", "restart"}
ALLOWED_KUBECTL_RESOURCES = {
    "pod", "pods", "deployment", "deployments", "service", "services",
    "pvc", "persistentvolumeclaim", "node", "nodes",
}
# Destructive operations require human approval (never auto-executed)
DESTRUCTIVE_VERBS = {"delete", "drain", "cordon", "taint"}

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class HealStatus(str, Enum):
    PENDING = "PENDING"
    ANALYZING = "ANALYZING"
    GUARDRAILS_PASSED = "GUARDRAILS_PASSED"
    GUARDRAILS_FAILED = "GUARDRAILS_FAILED"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"
    PR_CREATED = "PR_CREATED"


@dataclass
class FailureEvent:
    timestamp: str
    source: str  # "health", "k8s", "deployment", "webhook"
    target: str  # e.g. "deployment/setuhaul-service", "pod/setuhaul-xyz"
    severity: str  # "low", "medium", "high", "critical"
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class HealAction:
    id: str
    event_id: str
    status: HealStatus
    proposed_by: str  # "deterministic" or "llm"
    action_type: str  # "restart_deployment", "scale_deployment", "investigate", "pr_only"
    description: str
    kubectl_command: list[str] | None = None
    pr_title: str | None = None
    pr_body: str | None = None
    pr_number: int | None = None
    created_at: str = field(default_factory=_now_iso)
    executed_at: str | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Monitor — deterministic failure detection
# ---------------------------------------------------------------------------


class Monitor:
    """Polls health endpoints and Kubernetes resources for failures."""

    def __init__(
        self,
        service_url: str = SELF_HEALING_SERVICE_URL,
        namespace: str = SELF_HEALING_K8S_NAMESPACE,
        deployment: str = SELF_HEALING_DEPLOYMENT_NAME,
    ) -> None:
        self.service_url = service_url.rstrip("/")
        self.namespace = namespace
        self.deployment = deployment
        self.events: list[FailureEvent] = []

    def _http_get_json(self, path: str, timeout: float = 5.0) -> dict[str, Any] | None:
        try:
            req = Request(f"{self.service_url}{path}", method="GET")
            with urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (HTTPError, URLError, OSError, json.JSONDecodeError, TimeoutError):
            return None

    def check_health(self) -> list[FailureEvent]:
        """Check /health and /deployment/status for unhealthy conditions."""
        events: list[FailureEvent] = []
        health = self._http_get_json("/health")
        if health is None:
            events.append(FailureEvent(
                timestamp=_now_iso(), source="health", target="service",
                severity="critical", message="Service /health endpoint unreachable",
                details={"url": f"{self.service_url}/health"},
            ))
            return events

        if health.get("status") != "ok":
            events.append(FailureEvent(
                timestamp=_now_iso(), source="health", target="service",
                severity="high", message=f"Service health reports status={health.get('status')}",
                details=health,
            ))

        dep_status = self._http_get_json("/deployment/status")
        if dep_status is None:
            events.append(FailureEvent(
                timestamp=_now_iso(), source="deployment", target="deployment/status",
                severity="medium", message="/deployment/status endpoint unreachable",
                details={"url": f"{self.service_url}/deployment/status"},
            ))
        else:
            if dep_status.get("rollout_status") == "NOT_OBSERVED":
                events.append(FailureEvent(
                    timestamp=_now_iso(), source="deployment", target=dep_status.get("target_cluster", "unknown"),
                    severity="medium", message="Cluster rollout state unobserved",
                    details=dep_status,
                ))
            if dep_status.get("cluster_api_status") == "NOT_CONNECTED":
                events.append(FailureEvent(
                    timestamp=_now_iso(), source="deployment", target=dep_status.get("target_cluster", "unknown"),
                    severity="high", message="Kubernetes API not connected",
                    details=dep_status,
                ))

        return events

    def check_k8s(self) -> list[FailureEvent]:
        """Check Kubernetes deployment rollout status and pod health."""
        events: list[FailureEvent] = []

        # Check deployment rollout status
        rc, out = self._kubectl(["rollout", "status", f"deployment/{self.deployment}",
                                 f"-n {self.namespace}", "--timeout=10s"], check=False)
        if rc != 0:
            events.append(FailureEvent(
                timestamp=_now_iso(), source="k8s", target=f"deployment/{self.deployment}",
                severity="high", message=f"Deployment rollout check failed: {out.strip()}",
                details={"command": ["rollout", "status", f"deployment/{self.deployment}"]},
            ))
            return events

        # Check pod restarts and status
        rc, out = self._kubectl(["get", "pods", "-n", self.namespace,
                                 "-l", f"app={self.deployment}",
                                 "-o", "json"], check=False)
        if rc != 0:
            events.append(FailureEvent(
                timestamp=_now_iso(), source="k8s", target=f"pods/{self.namespace}",
                severity="medium", message="Could not list pods",
                details={"error": out.strip()},
            ))
            return events

        try:
            pod_list = json.loads(out)
        except json.JSONDecodeError:
            return events

        for pod in pod_list.get("items", []):
            pod_name = pod["metadata"]["name"]
            status = pod.get("status", {})
            container_statuses = status.get("containerStatuses", [])

            for cs in container_statuses:
                restart_count = cs.get("restartCount", 0)
                if restart_count > 5:
                    events.append(FailureEvent(
                        timestamp=_now_iso(), source="k8s", target=f"pod/{pod_name}",
                        severity="high", message=f"Container restart count {restart_count} > 5",
                        details={"pod": pod_name, "restart_count": restart_count,
                                 "last_state": cs.get("lastState", {})},
                    ))

                # Check for crash loops
                state = cs.get("state", {})
                if "waiting" in state:
                    reason = state["waiting"].get("reason", "")
                    if reason in {"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull"}:
                        events.append(FailureEvent(
                            timestamp=_now_iso(), source="k8s", target=f"pod/{pod_name}",
                            severity="critical", message=f"Container {reason}",
                            details={"pod": pod_name, "reason": reason,
                                     "message": state["waiting"].get("message", "")},
                        ))

                # Check if not running
                if not cs.get("running"):
                    events.append(FailureEvent(
                        timestamp=_now_iso(), source="k8s", target=f"pod/{pod_name}",
                        severity="medium", message="Container not running",
                        details={"pod": pod_name, "state": state},
                    ))

        return events

    def _kubectl(self, args: list[str], *, check: bool = True, timeout: float = 30.0) -> tuple[int, str]:
        cmd = ["kubectl"] + args
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout, check=check
            )
            return result.returncode, result.stdout
        except subprocess.CalledProcessError as exc:
            return exc.returncode, exc.stdout or ""
        except (subprocess.TimeoutExpired, OSError) as exc:
            return -1, str(exc)

    def scan(self) -> list[FailureEvent]:
        """Run all monitoring checks and return new failure events."""
        events: list[FailureEvent] = []
        events.extend(self.check_health())
        events.extend(self.check_k8s())
        self.events.extend(events)
        return events


# ---------------------------------------------------------------------------
# Healer — LLM analyzer for root cause + remediation planning
# ---------------------------------------------------------------------------


class Healer:
    """Analyzes failure events and proposes remediation actions.

    Uses an LLM (when enabled) for analysis and patch drafting. Deterministic
    fallback rules apply when LLM is disabled or unavailable.
    """

    def __init__(self, llm_enabled: bool = SELF_HEALING_LLM_ENABLED) -> None:
        self.llm_enabled = llm_enabled

    def analyze(self, event: FailureEvent) -> HealAction:
        action_id = f"heal-{int(time.time() * 1000)}"

        if self.llm_enabled:
            try:
                return self._llm_analyze(action_id, event)
            except (NVIDIAClientError, Exception) as exc:
                # Fall through to deterministic analysis on LLM failure
                pass

        return self._deterministic_analyze(action_id, event)

    def _llm_analyze(self, action_id: str, event: FailureEvent) -> HealAction:
        scenario = {
            "event": asdict(event),
            "task": "Analyze this SetuHaul service failure and propose remediation.",
            "constraints": (
                "Only propose actions from this allowlist: restart_deployment, scale_deployment, "
                "investigate, pr_only. Never propose delete/drain/cordon on production resources. "
                "Return a JSON object with: action_type, description, kubectl_command (or null), "
                "pr_title, pr_body."
            ),
        }

        system_prompt = (
            "You are a Site Reliability Engineer for SetuHaul Logistics, a shipment scheduling "
            "service running on EKS. Analyze the failure event and propose a safe remediation "
            "action. The kubectl_command must only use verbs from: get, describe, logs, rollout, "
            "scale, restart. Resources: pod, pods, deployment, deployments, service, services, "
            "pvc, persistentvolumeclaim, node, nodes. Never propose destructive operations "
            "(delete, drain, cordon, taint). If the fix requires code changes, set action_type "
            "to 'pr_only' and draft a PR title/body. Return exactly one JSON object."
        )

        response = ask_agent.__wrapped__ if hasattr(ask_agent, "__wrapped__") else ask_agent
        # Use chat_text for free-form JSON generation since we need a custom schema
        result = chat_text(
            system=system_prompt,
            user=json.dumps(scenario, ensure_ascii=True),
            timeout=60,
        )

        parsed = json.loads(result)
        return HealAction(
            id=action_id,
            event_id=event.timestamp,
            status=HealStatus.ANALYZING,
            proposed_by="llm",
            action_type=parsed.get("action_type", "investigate"),
            description=parsed.get("description", ""),
            kubectl_command=parsed.get("kubectl_command"),
            pr_title=parsed.get("pr_title"),
            pr_body=parsed.get("pr_body"),
        )

    def _deterministic_analyze(self, action_id: str, event: FailureEvent) -> HealAction:
        """Rule-based analysis when LLM is disabled."""
        source = event.source
        severity = event.severity
        message = event.message.lower()
        target = event.target or "unknown"

        if source == "health" and "unreachable" in message:
            return HealAction(
                id=action_id, event_id=event.timestamp, status=HealStatus.ANALYZING,
                proposed_by="deterministic", action_type="restart_deployment",
                description="Service health endpoint unreachable; restart deployment to recover",
                kubectl_command=["rollout", "restart", f"deployment/{self._deployment_from_event(event)}",
                                 f"-n {SELF_HEALING_K8S_NAMESPACE}"],
            )

        if source == "k8s" and "crashloopbackoff" in message:
            pod_name = target.split("/")[-1] if "/" in target else target
            return HealAction(
                id=action_id, event_id=event.timestamp, status=HealStatus.ANALYZING,
                proposed_by="deterministic", action_type="investigate",
                description="Pod in CrashLoopBackOff; inspect logs before restarting",
                kubectl_command=["logs", f"pod/{pod_name}",
                                 f"-n {SELF_HEALING_K8S_NAMESPACE}", "--tail=50"],
            )

        if source == "deployment" and "not connected" in message:
            return HealAction(
                id=action_id, event_id=event.timestamp, status=HealStatus.ANALYZING,
                proposed_by="deterministic", action_type="pr_only",
                description="Kubernetes API not connected; create PR to review cluster connectivity",
                pr_title="fix: investigate EKS cluster API connectivity for setuhaul-service",
                pr_body=f"Failure detected: {event.message}\n\nDetails: {json.dumps(event.details, indent=2)}",
            )

        if severity == "critical":
            return HealAction(
                id=action_id, event_id=event.timestamp, status=HealStatus.ANALYZING,
                proposed_by="deterministic", action_type="pr_only",
                description=f"Critical failure requires manual review: {event.message}",
                pr_title=f"fix: address critical failure in {target}",
                pr_body=f"Failure: {event.message}\n\nSource: {source}\nDetails: {json.dumps(event.details, indent=2)}",
            )

        return HealAction(
            id=action_id, event_id=event.timestamp, status=HealStatus.ANALYZING,
            proposed_by="deterministic", action_type="investigate",
            description=f"No automated remediation for {source}/{severity}; flagged for investigation",
            kubectl_command=["describe", target, f"-n {SELF_HEALING_K8S_NAMESPACE}"],
        )

    def _deployment_from_event(self, event: FailureEvent) -> str:
        target = event.target or ""
        if target.startswith("deployment/"):
            return target.split("/", 1)[1]
        return SELF_HEALING_DEPLOYMENT_NAME


# ---------------------------------------------------------------------------
# Guardrails — deterministic validation of proposed actions
# ---------------------------------------------------------------------------


class Guardrails:
    """Validate proposed heal actions before execution. Never auto-executes destructive ops."""

    @staticmethod
    def validate(action: HealAction) -> tuple[bool, str | None]:
        """Return (is_valid, reason_if_invalid)."""

        # LLM-proposed actions always need guardrail validation
        if action.action_type == "pr_only":
            # PR-only actions are always safe (no cluster mutation)
            return True, None

        if action.action_type == "investigate":
            # Read-only kubectl commands are safe
            if action.kubectl_command:
                ok, reason = Guardrails._validate_kubectl(action.kubectl_command)
                if not ok:
                    return False, reason
            return True, None

        if action.action_type in {"restart_deployment", "scale_deployment"}:
            if not action.kubectl_command:
                return False, "restart/scale requires a kubectl command"
            ok, reason = Guardrails._validate_kubectl(action.kubectl_command)
            if not ok:
                return False, reason
            # Confirm the command targets our deployment
            cmd_str = " ".join(action.kubectl_command)
            if SELF_HEALING_DEPLOYMENT_NAME not in cmd_str:
                return False, f"command does not target {SELF_HEALING_DEPLOYMENT_NAME}"
            return True, None

        return False, f"unknown action_type: {action.action_type}"

    @staticmethod
    def _validate_kubectl(cmd: list[str]) -> tuple[bool, str | None]:
        if not cmd:
            return False, "empty kubectl command"
        verb = cmd[0].lower()
        if verb in DESTRUCTIVE_VERBS:
            return False, f"destructive verb '{verb}' is not allowed"
        if verb not in ALLOWED_KUBECTL_VERBS:
            return False, f"verb '{verb}' not in allowlist"
        # Check resources mentioned in command (handles both "deployment" and "deployment/name")
        for token in cmd:
            base = token.split("/", 1)[0].split("=", 1)[0]
            if base in ALLOWED_KUBECTL_RESOURCES:
                break
        else:
            return False, "no recognized Kubernetes resource in command"
        return True, None


# ---------------------------------------------------------------------------
# Executor — runs validated actions against the cluster
# ---------------------------------------------------------------------------


class Executor:
    """Executes validated heal actions against Kubernetes."""

    def __init__(self, namespace: str = SELF_HEALING_K8S_NAMESPACE) -> None:
        self.namespace = namespace

    def execute(self, action: HealAction) -> tuple[bool, str | None]:
        """Execute an action. Returns (success, error_message)."""
        if action.action_type == "pr_only":
            return True, None  # PR creation handled separately

        if action.action_type == "investigate":
            if action.kubectl_command:
                rc, out = self._kubectl(action.kubectl_command)
                if rc != 0:
                    return False, out.strip()
            return True, None

        if action.action_type in {"restart_deployment", "scale_deployment"}:
            if not action.kubectl_command:
                return False, "no kubectl command to execute"
            rc, out = self._kubectl(action.kubectl_command)
            if rc != 0:
                return False, out.strip()
            return True, None

        return False, f"unknown action_type: {action.action_type}"

    def _kubectl(self, args: list[str], timeout: float = 60.0) -> tuple[int, str]:
        cmd = ["kubectl"] + args
        try:
            result = subprocess.run(cmd, capture_output=True, text=True,
                                    timeout=timeout, check=False)
            return result.returncode, result.stdout
        except subprocess.TimeoutExpired:
            return -1, "kubectl command timed out"
        except OSError as exc:
            return -1, str(exc)


# ---------------------------------------------------------------------------
# PR Creator — GitHub integration
# ---------------------------------------------------------------------------


class PRCreator:
    """Opens draft PRs on GitHub for proposed fixes."""

    def __init__(self, token: str = SELF_HEALING_GITHUB_TOKEN,
                 repo: str = SELF_HEALING_GITHUB_REPO) -> None:
        self.token = token
        self.repo = repo  # "owner/name"

    def create_pr(self, action: HealAction) -> tuple[bool, int | None, str | None]:
        """Create a draft PR. Returns (success, pr_number, error)."""
        if not self.token or not self.repo:
            return False, None, "SELF_HEALING_GITHUB_TOKEN or SELF_HEALING_GITHUB_REPO not set"

        if not action.pr_title or not action.pr_body:
            return False, None, "PR title and body are required"

        # Get default branch
        rc, out = self._github_api("GET", f"/repos/{self.repo}")
        if rc != 0:
            return False, None, f"failed to fetch repo info: {out.strip()}"
        try:
            repo_info = json.loads(out)
            default_branch = repo_info.get("default_branch", "main")
        except json.JSONDecodeError:
            default_branch = "main"

        # Create a branch
        branch_name = f"self-heal/{action.id}"
        sha = self._get_branch_sha(default_branch)
        if sha is None:
            return False, None, f"could not resolve SHA for branch {default_branch}"

        self._github_api("POST", f"/repos/{self.repo}/git/refs",
                         body={"ref": f"refs/heads/{branch_name}", "sha": sha})

        # For PR-only actions, we can optionally add a fix file. For now, create
        # a PR that documents the issue and proposed remediation.
        rc, out = self._github_api("POST", f"/repos/{self.repo}/pulls", body={
            "title": action.pr_title,
            "body": action.pr_body,
            "head": branch_name,
            "base": default_branch,
            "draft": True,
        })
        if rc != 0:
            return False, None, f"PR creation failed: {out.strip()}"

        try:
            pr = json.loads(out)
            return True, pr.get("number"), None
        except json.JSONDecodeError:
            return False, None, "PR created but could not parse response"

    def _get_branch_sha(self, branch: str) -> str | None:
        rc, out = self._github_api("GET", f"/repos/{self.repo}/git/ref/heads/{branch}")
        if rc != 0:
            return None
        try:
            ref = json.loads(out)
            return ref.get("object", {}).get("sha")
        except json.JSONDecodeError:
            return None

    def _github_api(self, method: str, path: str,
                    body: dict[str, Any] | None = None) -> tuple[int, str]:
        url = f"https://api.github.com{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "setuhaul-self-healing-agent",
        }
        if data:
            headers["Content-Type"] = "application/json"

        req = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout=30) as resp:
                return resp.status, resp.read().decode("utf-8")
        except HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", errors="replace")
        except (URLError, OSError, TimeoutError) as exc:
            return -1, str(exc)


# ---------------------------------------------------------------------------
# SelfHealingAgent — orchestrates Monitor → Healer → Guardrails → Executor → PR
# ---------------------------------------------------------------------------


class SelfHealingAgent:
    """Orchestrates the full self-healing loop."""

    def __init__(self) -> None:
        self.monitor = Monitor()
        self.healer = Healer()
        self.guardrails = Guardrails()
        self.executor = Executor()
        self.pr_creator = PRCreator()
        self.history: list[HealAction] = []

    def scan_and_heal(self) -> list[HealAction]:
        """Run one monitoring cycle: detect failures, analyze, validate, execute/PR."""
        events = self.monitor.scan()
        actions: list[HealAction] = []

        for event in events:
            action = self.healer.analyze(event)

            # Guardrails validation
            ok, reason = self.guardrails.validate(action)
            if not ok:
                action.status = HealStatus.GUARDRAILS_FAILED
                action.error = reason
                self.history.append(action)
                actions.append(action)
                continue

            action.status = HealStatus.GUARDRAILS_PASSED

            # Execute (non-PR actions)
            if action.action_type != "pr_only":
                success, error = self.executor.execute(action)
                if success:
                    action.status = HealStatus.EXECUTED
                    action.executed_at = _now_iso()
                else:
                    action.status = HealStatus.FAILED
                    action.error = error

            # Create PR (for pr_only actions, or as follow-up for executed actions)
            should_pr = (
                action.action_type == "pr_only"
                or action.status in {HealStatus.FAILED, HealStatus.EXECUTED}
            )
            if should_pr and action.pr_title:
                pr_success, pr_number, pr_error = self.pr_creator.create_pr(action)
                if pr_success:
                    action.status = HealStatus.PR_CREATED
                    action.pr_number = pr_number
                else:
                    if action.status != HealStatus.PR_CREATED:
                        action.status = HealStatus.FAILED
                    action.error = (action.error or "") + (
                        f"; PR creation failed: {pr_error}" if pr_error else ""
                    )

            self.history.append(action)
            actions.append(action)

        return actions

    def get_status(self) -> dict[str, Any]:
        return {
            "agent": "self-healing",
            "llm_enabled": self.healer.llm_enabled,
            "total_events": len(self.monitor.events),
            "total_actions": len(self.history),
            "actions_by_status": {
                status.value: sum(1 for a in self.history if a.status == status)
                for status in HealStatus
            },
            "recent_actions": [asdict(a) for a in self.history[-10:]],
        }

    def get_config(self) -> dict[str, Any]:
        return {
            "llm_enabled": SELF_HEALING_LLM_ENABLED,
            "poll_interval_seconds": SELF_HEALING_POLL_INTERVAL,
            "github_configured": bool(SELF_HEALING_GITHUB_TOKEN and SELF_HEALING_GITHUB_REPO),
            "k8s_namespace": SELF_HEALING_K8S_NAMESPACE,
            "deployment_name": SELF_HEALING_DEPLOYMENT_NAME,
            "service_url": SELF_HEALING_SERVICE_URL,
            "allowed_kubectl_verbs": sorted(ALLOWED_KUBECTL_VERBS),
            "destructive_verbs_never_auto_executed": sorted(DESTRUCTIVE_VERBS),
        }
