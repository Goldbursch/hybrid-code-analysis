#!/usr/bin/env python3
"""
Hybrid Code Analysis Script

Retrieves the code diff for a pull request or push event, fetches static
analysis findings from SonarQube/SonarCloud, and sends both to the OpenAI
API for a comprehensive code review.  The feedback is posted as a PR comment
(for pull_request events) and always saved as a GitHub Actions artifact.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone

import requests
from openai import OpenAI

# Maximum number of characters of diff content sent to OpenAI.
MAX_DIFF_LENGTH = 15_000

# Maximum number of SonarQube issues included in the prompt.
MAX_SONAR_ISSUES = 100

# Maximum tokens the model may use for its response.
MAX_RESPONSE_TOKENS = 2048

# Null SHA used by GitHub to indicate an initial push with no previous commit.
NULL_SHA = "0" * 40

# OpenAI model used for the code review.
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")


# ---------------------------------------------------------------------------
# Diff helpers
# ---------------------------------------------------------------------------

def _run_git_diff(*args: str) -> str:
    """Run `git diff <args>` and return stdout."""
    result = subprocess.run(
        ["git", "diff"] + list(args),
        capture_output=True,
        text=True,
    )
    return result.stdout


def get_diff() -> str:
    """Return the relevant diff depending on the GitHub event type."""
    event_name = os.environ.get("GITHUB_EVENT_NAME", "push")

    if event_name == "pull_request":
        base_sha = os.environ.get("GITHUB_BASE_SHA", "")
        head_sha = os.environ.get("GITHUB_SHA", "HEAD")
        if base_sha:
            return _run_git_diff(base_sha, head_sha)
        return _run_git_diff("HEAD~1", "HEAD")

    # push event
    before_sha = os.environ.get("GITHUB_BEFORE_SHA", "")
    head_sha = os.environ.get("GITHUB_SHA", "HEAD")

    if not before_sha or before_sha == NULL_SHA:
        return _run_git_diff("HEAD~1", "HEAD")

    return _run_git_diff(before_sha, head_sha)


# ---------------------------------------------------------------------------
# SonarQube helpers
# ---------------------------------------------------------------------------

def get_sonarqube_issues(project_key: str, sonar_token: str, host_url: str) -> list:
    """Fetch open issues from SonarQube/SonarCloud for *project_key*."""
    url = f"{host_url.rstrip('/')}/api/issues/search"
    headers = {"Authorization": f"Bearer {sonar_token}"}
    params = {
        "projectKeys": project_key,
        "resolved": "false",
        "ps": 100,
        "p": 1,
    }

    all_issues: list = []
    while True:
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        if resp.status_code != 200:
            print(
                f"⚠️  SonarQube API returned HTTP {resp.status_code}: "
                f"{resp.text[:200]}"
            )
            break
        data = resp.json()
        page_issues = data.get("issues", [])
        all_issues.extend(page_issues)

        total = data.get("paging", {}).get("total", 0)
        if len(all_issues) >= total or len(all_issues) >= MAX_SONAR_ISSUES:
            break
        params["p"] += 1

    return all_issues[:MAX_SONAR_ISSUES]


def format_sonarqube_issues(issues: list) -> str:
    """Format *issues* into a concise, LLM-readable string."""
    if not issues:
        return "No issues reported by SonarQube."

    lines = [f"SonarQube reported {len(issues)} open issue(s):\n"]
    for idx, issue in enumerate(issues, 1):
        severity = issue.get("severity", "UNKNOWN")
        rule = issue.get("rule", "unknown")
        message = issue.get("message", "")
        component = issue.get("component", "")
        line = issue.get("line", "N/A")
        issue_type = issue.get("type", "")
        lines.append(
            f"{idx}. [{severity}] [{issue_type}] {component}:{line} – "
            f"{message} (rule: {rule})"
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# OpenAI analysis
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are an expert software engineer performing a code review. \
Analyse the provided git diff and give clear, constructive feedback covering:

1. **Code quality & best practices** – naming, structure, design patterns.
2. **Bugs & correctness** – logic errors, edge cases, incorrect assumptions.
3. **Security** – injection risks, secrets, unsafe operations, dependency issues.
4. **Performance** – unnecessary allocations, algorithmic complexity, I/O.
5. **Readability & maintainability** – clarity, documentation, test coverage.
6. **Actionable suggestions** – concrete improvements with brief rationale.

Be specific: reference file names and line numbers where possible. \
If the diff is clean and well-written, say so explicitly."""


def analyze_with_openai(diff: str, sonar_issues_text: str) -> str:
    """Send *diff* and *sonar_issues_text* to OpenAI and return the review."""
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

    user_message = (
        "Please review the following code changes:\n\n"
        "```diff\n"
        f"{diff}\n"
        "```\n\n"
        "## SonarQube Static Analysis Results\n\n"
        f"{sonar_issues_text}\n\n"
        "Provide a comprehensive code review that incorporates both the code "
        "changes above and the SonarQube static analysis findings. Where "
        "SonarQube has flagged an issue that is also visible in the diff, "
        "highlight it explicitly. Include specific feedback and suggestions."
    )

    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        temperature=0.3,
        max_tokens=MAX_RESPONSE_TOKENS,
    )

    return response.choices[0].message.content


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------

def post_pr_comment(feedback: str, pr_number: int, sonar_project_key: str) -> None:
    """Post *feedback* as a comment on the pull request."""
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")

    if not token or not repo:
        print("GITHUB_TOKEN or GITHUB_REPOSITORY not set – skipping PR comment.")
        return

    sonar_note = (
        f" · SonarQube project: `{sonar_project_key}`" if sonar_project_key else ""
    )

    body = (
        "## 🤖 Hybrid Code Review (LLM + SonarQube)\n\n"
        f"{feedback}\n\n"
        "---\n"
        f"*Generated by [{OPENAI_MODEL}](https://platform.openai.com/docs/models) "
        "with SonarQube static analysis"
        f"{sonar_note} "
        "via the [hybrid-code-analysis](https://github.com/Goldbursch/hybrid-code-analysis) workflow.*"
    )

    url = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }
    resp = requests.post(url, headers=headers, json={"body": body}, timeout=30)

    if resp.status_code == 201:
        print(f"✅  Comment posted to PR #{pr_number}.")
    else:
        print(
            f"⚠️  Failed to post PR comment "
            f"(HTTP {resp.status_code}): {resp.text[:200]}"
        )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def save_feedback(
    feedback: str,
    event_name: str,
    sha: str,
    sonar_issues_text: str,
) -> str:
    """Save *feedback* (and SonarQube summary) to ``feedback/``."""
    os.makedirs("feedback", exist_ok=True)
    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y%m%dT%H%M%SZ")
    filename = f"feedback/{event_name}_{sha[:8]}_{timestamp}.md"

    with open(filename, "w", encoding="utf-8") as fh:
        fh.write("# Hybrid Code Review Feedback\n\n")
        fh.write("| Field | Value |\n|---|---|\n")
        fh.write(f"| **Event** | `{event_name}` |\n")
        fh.write(f"| **Commit** | `{sha}` |\n")
        fh.write(f"| **Model** | `{OPENAI_MODEL}` |\n")
        fh.write(f"| **Timestamp** | {now.isoformat()} |\n\n")
        fh.write("---\n\n")
        fh.write("## SonarQube Findings\n\n")
        fh.write(sonar_issues_text)
        fh.write("\n\n---\n\n")
        fh.write("## LLM Review\n\n")
        fh.write(feedback)
        fh.write("\n")

    print(f"💾  Feedback saved to `{filename}`.")
    return filename


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    event_name = os.environ.get("GITHUB_EVENT_NAME", "push")
    github_sha = os.environ.get("GITHUB_SHA", "unknown")
    event_path = os.environ.get("GITHUB_EVENT_PATH", "")

    # Parse the GitHub event payload.
    event_data: dict = {}
    if event_path and os.path.exists(event_path):
        with open(event_path, encoding="utf-8") as fh:
            event_data = json.load(fh)

    print(f"ℹ️  Event: {event_name}  |  SHA: {github_sha[:8]}")

    # ------------------------------------------------------------------
    # Obtain the diff
    # ------------------------------------------------------------------
    print("🔍  Fetching diff …")
    diff = get_diff()

    if not diff.strip():
        print("ℹ️  No diff detected – nothing to review.")
        sys.exit(0)

    if len(diff) > MAX_DIFF_LENGTH:
        print(
            f"⚠️  Diff is {len(diff):,} characters; "
            f"truncating to ~{MAX_DIFF_LENGTH:,} characters."
        )
        cutoff = diff.rfind("\n", 0, MAX_DIFF_LENGTH)
        if cutoff == -1:
            cutoff = MAX_DIFF_LENGTH
        diff = diff[:cutoff] + "\n\n[… diff truncated due to size …]"

    # ------------------------------------------------------------------
    # Fetch SonarQube issues
    # ------------------------------------------------------------------
    sonar_project_key = os.environ.get("SONAR_PROJECT_KEY", "")
    sonar_token = os.environ.get("SONAR_TOKEN", "")
    sonar_host_url = os.environ.get("SONAR_HOST_URL", "https://sonarcloud.io")

    sonar_issues_text = "SonarQube integration not configured."
    if sonar_project_key and sonar_token:
        print(
            f"📊  Fetching SonarQube issues for project '{sonar_project_key}' "
            f"from {sonar_host_url} …"
        )
        try:
            issues = get_sonarqube_issues(sonar_project_key, sonar_token, sonar_host_url)
            sonar_issues_text = format_sonarqube_issues(issues)
            print(f"📊  SonarQube: {len(issues)} issue(s) retrieved.")
        except requests.RequestException as exc:  # noqa: BLE001
            sonar_issues_text = f"Failed to retrieve SonarQube issues: {exc}"
            print(f"⚠️  {sonar_issues_text}")
    else:
        print(
            "⚠️  SONAR_PROJECT_KEY or SONAR_TOKEN not set – "
            "skipping SonarQube integration."
        )

    # ------------------------------------------------------------------
    # Call OpenAI
    # ------------------------------------------------------------------
    print(f"🤖  Sending diff + SonarQube findings to OpenAI ({OPENAI_MODEL}) …")
    feedback = analyze_with_openai(diff, sonar_issues_text)

    # ------------------------------------------------------------------
    # Save feedback
    # ------------------------------------------------------------------
    save_feedback(feedback, event_name, github_sha, sonar_issues_text)

    # ------------------------------------------------------------------
    # Post PR comment
    # ------------------------------------------------------------------
    if event_name == "pull_request":
        pr_number = event_data.get("pull_request", {}).get("number")
        if pr_number:
            post_pr_comment(feedback, pr_number, sonar_project_key)

    # ------------------------------------------------------------------
    # Print to workflow log
    # ------------------------------------------------------------------
    print("\n" + "=" * 60)
    print("Hybrid Code Review")
    print("=" * 60)
    print(sonar_issues_text)
    print("-" * 60)
    print(feedback)
    print("=" * 60)
    print("\n✅  Analysis complete.")


if __name__ == "__main__":
    main()
