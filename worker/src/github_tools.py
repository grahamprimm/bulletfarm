"""GitHub interaction layer.

Wraps PyGithub for draft PR creation, labeling, ready-for-review marking,
PR status checking, commenting, and cleanup.
Uses factory pattern — the token is injected, not read from env.
"""

from __future__ import annotations

import logging
import re
from typing import Any, TypedDict

logger = logging.getLogger(__name__)

LABEL_PREFIX: str = "bulletfarm"


class PRInfo(TypedDict):
    url: str
    number: int


class PRStatus(TypedDict):
    state: str
    merged: bool
    draft: bool


class CleanupResult(TypedDict):
    pr_closed: bool
    branch_deleted: bool


class RepoInfo(TypedDict):
    name: str
    default_branch: str
    description: str


class GitHubClient(TypedDict):
    create_draft_pr: Any
    get_repo_info: Any
    get_pr_status: Any
    mark_pr_ready: Any
    comment_on_pr: Any
    cleanup_pr: Any
    delete_branch: Any


def create_github_client(token: str) -> GitHubClient:
    """Factory: creates GitHub helper functions bound to a token."""
    from github import Github, GithubException

    client: Github = Github(token)

    def _ensure_labels(repo: Any, labels: list[dict[str, str]]) -> None:
        existing: set[str] = {label.name for label in repo.get_labels()}
        for label in labels:
            if label["name"] not in existing:
                try:
                    repo.create_label(
                        name=label["name"],
                        color=label["color"],
                        description=label.get("description", ""),
                    )
                    logger.info("Created label %s on %s", label["name"], repo.full_name)
                except GithubException:
                    pass

    def create_draft_pr(
        repo_name: str,
        head: str,
        base: str,
        title: str,
        body: str,
        task_id: str = "",
        agent_ref: str = "",
    ) -> PRInfo:
        repo = client.get_repo(repo_name)

        labels_to_create: list[dict[str, str]] = [
            {"name": f"{LABEL_PREFIX}/agent", "color": "0075ca", "description": "Created by BulletFarm agent"},
        ]
        if task_id:
            labels_to_create.append(
                {"name": f"{LABEL_PREFIX}/task:{task_id}", "color": "e4e669", "description": f"Task: {task_id}"}
            )
        if agent_ref:
            labels_to_create.append(
                {"name": f"{LABEL_PREFIX}/agent:{agent_ref}", "color": "7057ff", "description": f"Agent: {agent_ref}"}
            )
        _ensure_labels(repo, labels_to_create)

        pr = repo.create_pull(base=base, head=head, title=title, body=body, draft=True)

        label_names: list[str] = [l["name"] for l in labels_to_create]
        pr.set_labels(*label_names)
        logger.info("Created draft PR #%d on %s with labels %s", pr.number, repo_name, label_names)

        return {"url": pr.html_url, "number": pr.number}

    def get_repo_info(repo_name: str) -> RepoInfo:
        repo = client.get_repo(repo_name)
        return {
            "name": repo.full_name,
            "default_branch": repo.default_branch,
            "description": repo.description or "",
        }

    def get_pr_status(pr_url: str) -> PRStatus:
        match: re.Match[str] | None = re.match(
            r"https://github\.com/([^/]+/[^/]+)/pull/(\d+)", pr_url,
        )
        if not match:
            return {"state": "unknown", "merged": False, "draft": False}

        repo_name: str = match.group(1)
        pr_number: int = int(match.group(2))

        try:
            repo = client.get_repo(repo_name)
            pr = repo.get_pull(pr_number)
            return {
                "state": "merged" if pr.merged else pr.state,
                "merged": pr.merged,
                "draft": pr.draft,
            }
        except Exception as exc:
            logger.warning("Failed to get PR status for %s: %s", pr_url, exc)
            return {"state": "error", "merged": False, "draft": False}

    def mark_pr_ready(pr_url: str) -> bool:
        import requests as req

        match: re.Match[str] | None = re.match(
            r"https://github\.com/([^/]+/[^/]+)/pull/(\d+)", pr_url,
        )
        if not match:
            logger.warning("Cannot parse PR URL: %s", pr_url)
            return False

        repo_name: str = match.group(1)
        pr_number: int = int(match.group(2))

        repo = client.get_repo(repo_name)
        pr = repo.get_pull(pr_number)
        node_id: str = pr.raw_data.get("node_id", "")

        if not node_id:
            return False

        mutation: str = """
        mutation($pullRequestId: ID!) {
            markPullRequestReadyForReview(input: {pullRequestId: $pullRequestId}) {
                pullRequest { isDraft }
            }
        }
        """
        resp = req.post(
            "https://api.github.com/graphql",
            json={"query": mutation, "variables": {"pullRequestId": node_id}},
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=30,
        )
        if resp.status_code == 200 and "errors" not in resp.json():
            logger.info("Marked PR #%d on %s as ready for review", pr_number, repo_name)
            return True

        logger.warning("GraphQL mark_pr_ready failed: %s", resp.text[:500])
        return False

    def cleanup_pr(pr_url: str) -> CleanupResult:
        result: CleanupResult = {"pr_closed": False, "branch_deleted": False}

        match: re.Match[str] | None = re.match(
            r"https://github\.com/([^/]+/[^/]+)/pull/(\d+)", pr_url,
        )
        if not match:
            return result

        repo_name: str = match.group(1)
        pr_number: int = int(match.group(2))

        try:
            repo = client.get_repo(repo_name)
            pr = repo.get_pull(pr_number)

            if pr.state == "open":
                pr.edit(state="closed")
                result["pr_closed"] = True
                logger.info("Closed PR #%d on %s", pr_number, repo_name)

            head_ref: str = pr.head.ref
            try:
                ref = repo.get_git_ref(f"heads/{head_ref}")
                ref.delete()
                result["branch_deleted"] = True
                logger.info("Deleted branch %s on %s", head_ref, repo_name)
            except Exception as branch_exc:
                logger.warning("Failed to delete branch %s: %s", head_ref, branch_exc)

        except Exception as exc:
            logger.warning("Failed to cleanup PR %s: %s", pr_url, exc)

        return result

    def comment_on_pr(pr_url: str, comment: str) -> bool:
        match: re.Match[str] | None = re.match(
            r"https://github\.com/([^/]+/[^/]+)/pull/(\d+)", pr_url,
        )
        if not match:
            logger.warning("Cannot parse PR URL for comment: %s", pr_url)
            return False

        repo_name: str = match.group(1)
        pr_number: int = int(match.group(2))

        try:
            repo = client.get_repo(repo_name)
            issue = repo.get_issue(pr_number)
            issue.create_comment(comment)
            logger.info("Added comment to PR #%d on %s", pr_number, repo_name)
            return True
        except Exception as exc:
            logger.warning("Failed to comment on PR %s: %s", pr_url, exc)
            return False

    def delete_branch(repo_name: str, branch_name: str) -> bool:
        try:
            repo = client.get_repo(repo_name)
            ref = repo.get_git_ref(f"heads/{branch_name}")
            ref.delete()
            logger.info("Deleted branch %s on %s", branch_name, repo_name)
            return True
        except Exception as exc:
            logger.warning("Failed to delete branch %s on %s: %s", branch_name, repo_name, exc)
            return False

    return {
        "create_draft_pr": create_draft_pr,
        "get_repo_info": get_repo_info,
        "get_pr_status": get_pr_status,
        "mark_pr_ready": mark_pr_ready,
        "comment_on_pr": comment_on_pr,
        "cleanup_pr": cleanup_pr,
        "delete_branch": delete_branch,
    }
