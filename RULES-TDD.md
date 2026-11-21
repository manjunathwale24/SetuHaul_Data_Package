# RULE: Test-Driven Development (TDD)

## Always Use TDD for Any Fix

All fixes, features, and changes to the SetuHaul project must follow TDD: **write the failing test first, then write the code to make it pass.**

## TDD Workflow

### Step 1: Write a Failing Test (Red)

Before writing any code, write a test that demonstrates the bug or required behavior:

```python
def test_agent_creates_pr_for_integrity_conflict():
    """DevOps agent must create a PR when booking integrity conflicts exist."""
    agent = SelfHealingAgent(github_token="test_token", github_repo="owner/repo")
    event = {
        "source": "integrity",
        "severity": "high",
        "message": "Booking integrity conflicts detected",
        "details": {"conflicts": {"slots_with_two_live_claims": 2}},
    }
    result = agent.remediate(event, github={"token": "test_token", "repo": "owner/repo"})
    assert result["action"] == "create_pr"
    assert result["pr_created"] is True
    assert result["pr_number"] is not None
```

Run the test — it must **fail** because the code doesn't exist yet.

### Step 2: Write Minimal Code (Green)

Write the minimum code needed to make the test pass:

```python
def remediate(self, event, github):
    if event["source"] == "integrity" and not event["details"]["ok"]:
        return {"action": "create_pr", "success": True, "pr_created": True, "pr_number": 1}
    return {"action": "none", "success": False}
```

Run the test — it must **pass**.

### Step 3: Refactor

Clean up the code while keeping all tests passing:

```python
def remediate(self, event, github):
    if event["source"] == "integrity" and event["details"].get("conflicts"):
        return self._create_pr(event, github, "fix: resolve booking integrity conflicts")
    return {"action": "none", "success": False}
```

Run tests again — they must still **pass**.

## Rules

| Rule | Description |
|------|-------------|
| **Test First** | Never write implementation code before a failing test exists. |
| **One Assertion Per Test** | Each test verifies exactly one behavior. |
| **Tests Are Evidence** | Tests serve as documentation of expected behavior. |
| **No Dummy Code** | Never write code that doesn't have a corresponding test. |
| **Red-Green-Refactor** | Always follow the cycle: Red (failing test) → Green (passing code) → Refactor (clean code). |
| **Fixes Require Tests** | Every bug fix must include a regression test that would have caught the bug. |
| **PRs Require Tests** | Every pull request must include new or modified tests. |

## What Counts as a Fix

Any change to:
- `lambda_function.py`
- `service.py`
- `self_healing_agent.py`
- `self_healing_api.py`
- Any HTML/JS dashboard
- `vercel.json`
- AWS infrastructure (CloudFormation, Terraform, CDK)

…must include corresponding tests.

## Running Tests

### Python (Backend + Agent)
```bash
cd /Users/manjunath/setu/SetuHaul_Data_Package
source .venv/bin/activate
python -m unittest discover tests/
```

### Playwright (UI)
```bash
cd /Users/manjunath/setu
npx playwright test tests/e2e/ui.spec.ts
```

### DevOps Agent
```bash
cd /Users/manjunath/setu/devops_agent
source /Users/manjunath/setu/SetuHaul_Data_Package/.venv/bin/activate
python -m unittest discover tests/  # once tests exist
```

## Enforcement

- No code change is merged without a corresponding test.
- No PR is created without test coverage.
- If a bug is reported, the first step is writing a test that reproduces it, then fixing the bug.
- Code reviews must verify that tests exist and pass.
