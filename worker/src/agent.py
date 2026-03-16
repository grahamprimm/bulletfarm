"""LangChain agent setup with dynamic skill/tool system.

All dependencies are injected via the factory — no global state.
Skills are loaded dynamically based on the task's skill list.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
from typing import Any, Callable

from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.tools import StructuredTool
from pydantic import SecretStr

from src.config import WorkerConfig
from src.github_tools import GitHubClient
from src.memory import MemoryStore
from src.models import TaskPhase, TaskRequest, TaskStatus

logger: logging.Logger = logging.getLogger(__name__)

SYSTEM_PROMPT: str = (
    "You are a software engineering agent working on a local clone of a GitHub repository. "
    "Your job is to make code changes to accomplish the given task.\n\n"
    "WORKFLOW:\n"
    "1. Use read_file to understand the existing code before making changes.\n"
    "2. Use list_files to explore the repository structure if needed.\n"
    "3. Use code_edit to modify existing files or create new files.\n"
    "   - To EDIT: pass the exact old_content string and the new_content replacement.\n"
    "   - To CREATE a new file: pass old_content as empty string '' and new_content as the full file.\n"
    "4. Always use FULL ABSOLUTE paths for all file operations.\n"
    "5. Do NOT use create_draft_pr — PR creation is handled automatically after you finish.\n\n"
    "RULES:\n"
    "- Read files before editing them so you know the exact content to replace.\n"
    "- Create parent directories automatically when creating new files.\n"
    "- Make real, working code changes — not placeholders or TODOs.\n"
    "- If creating tests, write actual test implementations, not just stubs.\n"
    "- Be precise with string matching in code_edit — copy exact content from read_file."
)

ProgressCallback = Callable[[str, int, str], None]


# ---------------------------------------------------------------------------
# Core tools (always available)
# ---------------------------------------------------------------------------

def _build_core_tools(github_tools: GitHubClient, memory_store: MemoryStore) -> dict[str, StructuredTool]:
    """Build the always-available core tools (read, list, search)."""

    def _read_file(file_path: str) -> str:
        try:
            with open(file_path) as f:
                content: str = f.read()
            if len(content) > 5000:
                return f"[File: {file_path}, {len(content)} chars, truncated]\n{content[:5000]}\n...(truncated)"
            return f"[File: {file_path}, {len(content)} chars]\n{content}"
        except FileNotFoundError:
            return f"Error: File not found: {file_path}"
        except Exception as e:
            return f"Error reading {file_path}: {e}"

    def _list_files(directory: str) -> str:
        try:
            result: subprocess.CompletedProcess[str] = subprocess.run(
                ["find", directory, "-type", "f", "-not", "-path", "*/.git/*"],
                capture_output=True, text=True, timeout=10,
            )
            files: str = result.stdout.strip()
            return f"Files in {directory}:\n{files}" if files else f"No files found in {directory}"
        except Exception as e:
            return f"Error listing files in {directory}: {e}"

    return {
        "read_file": StructuredTool.from_function(
            func=_read_file,
            name="read_file",
            description="Read the full contents of a file. Args: file_path (absolute path).",
        ),
        "list_files": StructuredTool.from_function(
            func=_list_files,
            name="list_files",
            description="List all files in a directory recursively (excludes .git). Args: directory (absolute path).",
        ),
        "search_shared_knowledge": StructuredTool.from_function(
            func=lambda query_text, skills=None, limit=5: memory_store["search_shared"](query_text, skills, limit),
            name="search_shared_knowledge",
            description="Search shared memory for relevant knowledge from past tasks.",
        ),
    }


# ---------------------------------------------------------------------------
# Skill tools (loaded per task)
# ---------------------------------------------------------------------------

def _build_skill_tools() -> dict[str, Callable[[], StructuredTool]]:
    """Registry of skill-name -> tool factory."""

    def _code_edit_tool() -> StructuredTool:
        def code_edit(file_path: str, old_content: str, new_content: str) -> str:
            try:
                if old_content == "" or old_content.strip() == "":
                    os.makedirs(os.path.dirname(file_path), exist_ok=True)
                    with open(file_path, "w") as f:
                        f.write(new_content)
                    return f"Successfully created {file_path}"
                else:
                    with open(file_path) as f:
                        content: str = f.read()
                    if old_content not in content:
                        return (
                            f"Error: old_content not found in {file_path}. "
                            f"File contains {len(content)} chars. First 200: {content[:200]}"
                        )
                    content = content.replace(old_content, new_content, 1)
                    with open(file_path, "w") as f:
                        f.write(content)
                    return f"Successfully edited {file_path}"
            except Exception as e:
                return f"Error editing {file_path}: {e}"

        return StructuredTool.from_function(
            func=code_edit,
            name="code_edit",
            description=(
                "Edit or create a code file. "
                "To EDIT: pass old_content (exact string to find) and new_content (replacement). "
                "To CREATE a new file: pass old_content as '' and new_content as the full file content. "
                "Always use full absolute paths. Args: file_path, old_content, new_content."
            ),
        )

    def _test_generator_tool() -> StructuredTool:
        def test_generator(file_path: str, test_framework: str = "pytest") -> str:
            try:
                with open(file_path) as f:
                    content: str = f.read()
                if file_path.endswith(".py"):
                    functions: list[str] = re.findall(r"def (\w+)\(", content)
                    test_code: str = f'"""Auto-generated tests for {file_path}."""\n\nimport pytest\n\n'
                    for func_name in functions:
                        if not func_name.startswith("_"):
                            test_code += f"def test_{func_name}():\n    # TODO: implement test\n    pass\n\n"
                elif file_path.endswith((".js", ".ts")):
                    routes: list[tuple[str, str]] = re.findall(
                        r"app\.(get|post|put|delete|patch)\s*\(\s*['\"]([^'\"]+)", content,
                    )
                    exports: list[str] = re.findall(r"(?:module\.exports|export)\s*=\s*(\w+)", content)
                    test_code = f"// Auto-generated tests for {file_path}\n\n"
                    test_code += "const request = require('supertest');\n"
                    if exports:
                        test_code += f"const app = require('{file_path.replace('.js', '')}');\n\n"
                    for method, path in routes:
                        test_code += f"describe('{method.upper()} {path}', () => {{\n"
                        test_code += f"  test('should respond', async () => {{\n"
                        test_code += f"    const res = await request(app).{method}('{path}');\n"
                        test_code += f"    expect(res.statusCode).toBeDefined();\n"
                        test_code += f"  }});\n}});\n\n"
                else:
                    test_code = f"// No test generator available for {file_path}\n"
                return test_code if test_code.strip() else "No testable functions/routes found."
            except FileNotFoundError:
                return f"Error: Source file not found: {file_path}"
            except Exception as e:
                return f"Error generating tests: {e}"

        return StructuredTool.from_function(
            func=test_generator,
            name="test_generator",
            description="Generate test stubs for a source file (Python/JS/TS). Args: file_path, test_framework.",
        )

    def _graphql_debug_tool() -> StructuredTool:
        def graphql_debug(endpoint: str, query: str) -> str:
            try:
                import requests
                response = requests.post(endpoint, json={"query": query}, timeout=30)
                return f"Status: {response.status_code}\nResponse: {response.text[:2000]}"
            except Exception as e:
                return f"GraphQL debug error: {e}"

        return StructuredTool.from_function(
            func=graphql_debug,
            name="graphql_debug",
            description="Debug a GraphQL query against an endpoint. Args: endpoint, query.",
        )

    def _doc_update_tool() -> StructuredTool:
        def doc_update(file_path: str, section: str, content: str) -> str:
            try:
                existing: str = ""
                try:
                    with open(file_path) as f:
                        existing = f.read()
                except FileNotFoundError:
                    pass

                if f"## {section}" in existing:
                    pattern: str = rf"(## {re.escape(section)}\n)(.*?)(?=\n## |\Z)"
                    replacement: str = f"## {section}\n{content}\n"
                    existing = re.sub(pattern, replacement, existing, flags=re.DOTALL)
                else:
                    existing += f"\n## {section}\n{content}\n"

                with open(file_path, "w") as f:
                    f.write(existing)
                return f"Updated section '{section}' in {file_path}"
            except Exception as e:
                return f"Error updating docs: {e}"

        return StructuredTool.from_function(
            func=doc_update,
            name="doc_update",
            description="Update or append a section in a documentation file. Args: file_path, section, content.",
        )

    return {
        "code-edit": _code_edit_tool,
        "testing": _test_generator_tool,
        "test-generator": _test_generator_tool,
        "graphql": _graphql_debug_tool,
        "graphql-debug": _graphql_debug_tool,
        "documentation": _doc_update_tool,
        "doc-update": _doc_update_tool,
    }


def _select_tools(
    skills: list[str],
    core_tools: dict[str, StructuredTool],
    skill_registry: dict[str, Callable[[], StructuredTool]],
) -> list[StructuredTool]:
    """Select tools based on requested skills. Always includes core tools."""
    tools: list[StructuredTool] = list(core_tools.values())
    for skill_name in skills:
        if skill_name in skill_registry:
            tools.append(skill_registry[skill_name]())
            logger.info("Loaded skill: %s", skill_name)
        else:
            logger.warning("Unknown skill requested: %s (skipping)", skill_name)
    return tools


# ---------------------------------------------------------------------------
# Agent factory
# ---------------------------------------------------------------------------

def create_agent(
    config: WorkerConfig,
    github_tools: GitHubClient,
    memory_store: MemoryStore,
) -> dict[str, Any]:
    """Factory: creates a configured LangChain agent with dynamic skill loading."""

    if config.llm_provider == "ollama":
        from langchain_community.chat_models import ChatOllama
        llm = ChatOllama(model=config.llm_model, base_url=config.ollama_base_url)
    else:
        from langchain_openai import ChatOpenAI
        llm = ChatOpenAI(model=config.llm_model, api_key=SecretStr(config.openai_api_key))

    core_tools: dict[str, StructuredTool] = _build_core_tools(github_tools, memory_store)
    skill_registry: dict[str, Callable[[], StructuredTool]] = _build_skill_tools()

    async def run_task(
        task_request: TaskRequest,
        progress_callback: ProgressCallback | None = None,
    ) -> TaskStatus:
        """Execute an agent task end-to-end."""
        workspace: str = f"/workspace/{task_request.task_id}"
        pr_url: str = task_request.pr_url
        output_text: str = ""
        branch_name: str = task_request.target_branch or f"task-{task_request.task_id}"

        try:
            # --- Step 1: Clone repo ---
            if progress_callback:
                progress_callback(task_request.task_id, 10, "Cloning repository")

            clone_url: str = (
                f"https://x-access-token:{config.github_token}@github.com/"
                f"{task_request.repository}.git"
            )

            if task_request.is_retry:
                subprocess.run(
                    ["git", "clone", clone_url, workspace],
                    check=True, capture_output=True, text=True,
                )
                fetch_result: subprocess.CompletedProcess[str] = subprocess.run(
                    ["git", "fetch", "origin", branch_name],
                    cwd=workspace, capture_output=True, text=True,
                )
                if fetch_result.returncode == 0:
                    subprocess.run(
                        ["git", "checkout", branch_name],
                        cwd=workspace, check=True, capture_output=True, text=True,
                    )
                    logger.info("Retry: checked out existing branch %s", branch_name)
                else:
                    subprocess.run(
                        ["git", "checkout", "-b", branch_name],
                        cwd=workspace, check=True, capture_output=True, text=True,
                    )
                    logger.info("Retry: created new branch %s (remote didn't exist)", branch_name)
            else:
                subprocess.run(
                    ["git", "clone", "--depth=1", clone_url, workspace],
                    check=True, capture_output=True, text=True,
                )
                subprocess.run(
                    ["git", "checkout", "-b", branch_name],
                    cwd=workspace, check=True, capture_output=True, text=True,
                )
                logger.info("First attempt: created branch %s", branch_name)

            logger.info("Cloned %s to %s (retry=%s)", task_request.repository, workspace, task_request.is_retry)

            subprocess.run(["git", "config", "user.email", "agent@bulletfarm.io"], cwd=workspace, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "BulletFarm Agent"], cwd=workspace, check=True, capture_output=True)

            # --- Step 2: Gather context ---
            if progress_callback:
                progress_callback(task_request.task_id, 20, "Gathering context")

            repo_info: dict[str, str] = github_tools["get_repo_info"](task_request.repository)

            task_history: list[dict[str, Any]] = memory_store["history"](task_request.agent_ref, limit=5)
            logger.info("[Memory] Task history for agent=%s: %d previous tasks", task_request.agent_ref, len(task_history))

            shared_context: str = ""
            if task_request.skills:
                shared_results: list[dict[str, Any]] = memory_store["search_shared"](
                    task_request.prompt, task_request.skills, limit=3,
                )
                logger.info("[Memory] Shared knowledge: %d results", len(shared_results))
                if shared_results:
                    shared_context = "\n\nRelevant knowledge from past tasks:\n"
                    for r in shared_results:
                        shared_context += f"- {r.get('summary', '')}\n"

            file_listing: str = subprocess.run(
                ["find", ".", "-type", "f", "-not", "-path", "./.git/*"],
                cwd=workspace, capture_output=True, text=True,
            ).stdout[:3000]

            # --- Step 3: Run LLM agent ---
            if progress_callback:
                progress_callback(task_request.task_id, 30, "Running LLM agent")

            workspace_tools: list[StructuredTool] = list(core_tools.values())
            for skill_name in task_request.skills:
                if skill_name in skill_registry:
                    workspace_tools.append(skill_registry[skill_name]())

            enriched_prompt: str = (
                f"You are working on a local clone of {repo_info['name']}.\n"
                f"The repo is cloned at: {workspace}\n"
                f"Base branch: {repo_info['default_branch']}\n"
                f"Task branch: {branch_name}\n"
                f"Skills: {', '.join(task_request.skills)}\n"
                f"{shared_context}\n\n"
                f"Files in repo:\n{file_listing}\n\n"
                f"Task: {task_request.prompt}\n\n"
                f"IMPORTANT: Use the code_edit tool with FULL paths starting with {workspace}/ "
                f"to make changes. Create new files by passing old_content as empty string ''. "
                f"Make real, meaningful changes to accomplish the task."
            )

            from langgraph.prebuilt import create_react_agent as create_langgraph_agent

            agent_graph = create_langgraph_agent(model=llm, tools=workspace_tools)

            if progress_callback:
                progress_callback(task_request.task_id, 40, "Agent executing")

            max_llm_retries: int = 3
            max_agent_steps: int = 50
            result: dict[str, Any] | None = None
            step_count: int = 0

            for attempt in range(max_llm_retries):
                try:
                    step_count = 0
                    async for event in agent_graph.astream_events(
                        {"messages": [{"role": "user", "content": enriched_prompt}]},
                        config={"recursion_limit": max_agent_steps},
                        version="v2",
                    ):
                        kind: str = event.get("event", "")
                        if kind == "on_tool_start":
                            step_count += 1
                            tool_name: str = event.get("name", "?")
                            tool_input: str = str(event.get("data", {}).get("input", ""))[:200]
                            logger.info("[Agent Step %d] Tool: %s | Input: %s", step_count, tool_name, tool_input)
                        elif kind == "on_tool_end":
                            tool_output: str = str(event.get("data", {}).get("output", ""))[:300]
                            logger.info("[Agent Step %d] Result: %s", step_count, tool_output)
                        elif kind == "on_chat_model_end":
                            output = event.get("data", {}).get("output", None)
                            if output and hasattr(output, "content"):
                                result = {"messages": [output]}

                    if result is None:
                        result = await agent_graph.ainvoke(
                            {"messages": [{"role": "user", "content": enriched_prompt}]},
                            config={"recursion_limit": max_agent_steps},
                        )
                    logger.info("[Agent] Completed after %d tool calls", step_count)
                    break

                except Exception as llm_exc:
                    exc_msg: str = str(llm_exc).lower()
                    is_rate_limit: bool = any(
                        m in exc_msg for m in ["rate limit", "429", "too many requests", "quota"]
                    )
                    is_recursion: bool = "recursion" in exc_msg

                    if is_recursion:
                        logger.warning("Agent hit recursion limit (%d steps): %s", max_agent_steps, llm_exc)
                        recursion_msg: HumanMessage = HumanMessage(content=(
                            f"Agent hit the maximum step limit ({max_agent_steps} steps) "
                            f"without completing the task."
                        ))
                        result = {"messages": [recursion_msg]}
                        break
                    elif is_rate_limit and attempt < max_llm_retries - 1:
                        wait_secs: int = (2 ** attempt) * 15
                        logger.warning("Rate limited, waiting %ds (attempt %d/%d)", wait_secs, attempt + 1, max_llm_retries)
                        if progress_callback:
                            progress_callback(task_request.task_id, 45, f"Rate limited, retrying in {wait_secs}s")
                        await asyncio.sleep(wait_secs)
                    else:
                        raise

            messages: list[BaseMessage] = result.get("messages", []) if result else []
            raw_content: Any = messages[-1].content if messages else "No output from agent"
            output_text = raw_content if isinstance(raw_content, str) else str(raw_content)

            if progress_callback:
                progress_callback(task_request.task_id, 70, "Evaluating changes")

            # --- Step 4: Evaluate changes ---
            subprocess.run(["git", "add", "-A"], cwd=workspace, check=True, capture_output=True)

            diff_result: subprocess.CompletedProcess[str] = subprocess.run(
                ["git", "diff", "--cached", "--stat"],
                cwd=workspace, capture_output=True, text=True,
            )
            has_changes: bool = bool(diff_result.stdout.strip())

            # Collect tool usage
            tools_called: list[str] = []
            for msg in messages:
                if hasattr(msg, "tool_calls") and getattr(msg, "tool_calls", None):
                    for tc in msg.tool_calls:  # type: ignore[union-attr]
                        name: str = tc.get("name", "unknown") if isinstance(tc, dict) else getattr(tc, "name", "unknown")
                        tools_called.append(name)
                if hasattr(msg, "name") and getattr(msg, "name", None):
                    tools_called.append(msg.name)  # type: ignore[union-attr]
            unique_tools: list[str] = list(set(tools_called))

            # Incomplete detection
            incomplete_markers: list[str] = [
                "i cannot", "i can't", "i'm unable", "could not",
                "please verify", "you may need to", "manually",
                "i was unable", "not able to", "failed to create",
                "would you like me to", "shall i", "let me know",
                "i don't have access", "permission denied",
            ]
            output_lower: str = output_text.lower()
            agent_admitted_failure: bool = any(marker in output_lower for marker in incomplete_markers)
            no_meaningful_tools: bool = not any(t in unique_tools for t in ["code_edit", "test_generator", "doc_update"])
            task_incomplete: bool = (not has_changes) and (agent_admitted_failure or no_meaningful_tools)

            if task_incomplete:
                return _handle_incomplete(
                    task_request, workspace, branch_name, repo_info,
                    output_text, unique_tools, github_tools, memory_store,
                    progress_callback,
                )

            # --- Step 5: Commit and push ---
            if progress_callback:
                progress_callback(task_request.task_id, 75, "Committing and pushing changes")

            commit_msg: str = f"feat: {task_request.prompt[:72]}\n\nAutomated by BulletFarm agent.\nTask: {task_request.task_id}"
            subprocess.run(["git", "commit", "-m", commit_msg], cwd=workspace, check=True, capture_output=True, text=True)
            subprocess.run(["git", "push", "-u", "origin", branch_name], cwd=workspace, check=True, capture_output=True, text=True)
            logger.info("Pushed branch %s to %s", branch_name, task_request.repository)

            # --- Step 6: Create or update PR ---
            if progress_callback:
                progress_callback(task_request.task_id, 85, "Creating draft PR")

            files_modified: list[str] = []
            try:
                diff_files: subprocess.CompletedProcess[str] = subprocess.run(
                    ["git", "diff", "--name-only", "HEAD~1"], cwd=workspace, capture_output=True, text=True,
                )
                if diff_files.returncode == 0:
                    files_modified = [f for f in diff_files.stdout.strip().split("\n") if f]
            except Exception:
                pass

            if task_request.pr_url:
                pr_url = task_request.pr_url
                logger.info("Retry: pushed new commits to existing PR %s", pr_url)
                github_tools["comment_on_pr"](
                    pr_url,
                    f"## ✅ Retry succeeded\n\nNew commits pushed.\n\n"
                    f"### Files modified\n{''.join(f'- `{f}`{chr(10)}' for f in files_modified)}",
                )
            else:
                pr_body: str = (
                    f"## Summary\n\n{output_text[:1000]}\n\n"
                    f"### Changes\n\n{''.join(f'- `{f}`{chr(10)}' for f in files_modified)}\n"
                    f"---\n**Task:** {task_request.prompt}\n"
                    f"**Agent:** {task_request.agent_ref}\n"
                    f"**Skills:** {', '.join(task_request.skills)}\n"
                    f"**Task ID:** {task_request.task_id}\n"
                )
                pr_result: dict[str, Any] = github_tools["create_draft_pr"](
                    repo_name=task_request.repository,
                    head=branch_name,
                    base=repo_info["default_branch"],
                    title=f"[Agent] {task_request.prompt[:80]}",
                    body=pr_body,
                    task_id=task_request.task_id,
                    agent_ref=task_request.agent_ref,
                )
                pr_url = pr_result["url"]
                logger.info("Created draft PR: %s", pr_url)

            # --- Step 7: Store in ES memory ---
            if progress_callback:
                progress_callback(task_request.task_id, 95, "Storing results in memory")

            memory_store["store"](
                task_id=task_request.task_id,
                result={
                    "agent_ref": task_request.agent_ref,
                    "repository": task_request.repository,
                    "prompt": task_request.prompt,
                    "output": output_text[:2000],
                    "skills_used": task_request.skills,
                    "tools_called": unique_tools,
                    "files_modified": files_modified,
                    "branch": branch_name,
                    "has_code_changes": True,
                    "methodology": (
                        f"Cloned repo, created branch '{branch_name}', "
                        f"ran LLM agent with {len(workspace_tools)} tools, "
                        f"called {len(unique_tools)} unique tools, "
                        f"modified {len(files_modified)} files, "
                        f"committed real changes, pushed and created draft PR."
                    ),
                    "phase": TaskPhase.SUCCEEDED.value,
                    "pr_url": pr_url,
                },
            )

            return TaskStatus(
                task_id=task_request.task_id,
                phase=TaskPhase.SUCCEEDED,
                progress=100,
                message=output_text[:500],
                pull_request_url=pr_url,
            )

        except Exception as exc:
            logger.exception("Agent task failed: %s", task_request.task_id)

            exc_msg_lower: str = str(exc).lower()
            is_rl: bool = any(m in exc_msg_lower for m in ["rate limit", "429", "too many requests", "quota"])

            try:
                memory_store["store"](
                    task_id=task_request.task_id,
                    result={
                        "agent_ref": task_request.agent_ref,
                        "repository": task_request.repository,
                        "prompt": task_request.prompt,
                        "error": str(exc),
                        "skills_used": task_request.skills,
                        "phase": TaskPhase.FAILED.value,
                        "rate_limited": is_rl,
                    },
                )
            except Exception:
                logger.warning("Failed to store error in ES")

            return TaskStatus(
                task_id=task_request.task_id,
                phase=TaskPhase.FAILED,
                message=f"Agent error: {str(exc)}"[:500],
                pull_request_url=pr_url,
                rate_limited=is_rl,
            )

    return {"run_task": run_task}


def _handle_incomplete(
    task_request: TaskRequest,
    workspace: str,
    branch_name: str,
    repo_info: dict[str, str],
    output_text: str,
    unique_tools: list[str],
    github_tools: GitHubClient,
    memory_store: MemoryStore,
    progress_callback: ProgressCallback | None,
) -> TaskStatus:
    """Handle the case where the agent couldn't make meaningful changes."""
    logger.warning(
        "Task %s incomplete: tools=%s",
        task_request.task_id, unique_tools,
    )

    if progress_callback:
        progress_callback(task_request.task_id, 90, "Task incomplete — recording reason")

    incomplete_reason: str = (
        f"The agent was unable to complete this task. "
        f"No code changes were produced. "
        f"Tools called: {', '.join(unique_tools) if unique_tools else 'none'}. "
        f"Agent response: {output_text[:300]}"
    )

    # Push incomplete analysis
    analysis_path: str = os.path.join(workspace, ".bulletfarm-incomplete.md")
    with open(analysis_path, "w") as f:
        f.write(f"# Task: {task_request.prompt}\n\n")
        f.write("## Status: INCOMPLETE\n\n")
        f.write("The agent was unable to complete this task.\n\n")
        f.write(f"## Agent Analysis\n\n{output_text[:3000]}\n\n")
        f.write(f"## Tools Called\n\n{', '.join(unique_tools) if unique_tools else 'None'}\n")

    subprocess.run(["git", "add", "-A"], cwd=workspace, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", f"chore: incomplete task analysis for {task_request.task_id}"],
        cwd=workspace, check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "push", "-u", "origin", branch_name],
        cwd=workspace, check=True, capture_output=True, text=True,
    )

    pr_url: str = task_request.pr_url
    if not pr_url:
        pr_body: str = (
            f"## ⚠️ Incomplete Task\n\n"
            f"The agent was unable to complete this task.\n\n"
            f"### Agent Analysis\n\n{output_text[:1000]}\n\n"
            f"---\n**Task:** {task_request.prompt}\n"
            f"**Agent:** {task_request.agent_ref}\n"
            f"**Task ID:** {task_request.task_id}\n"
            f"**Status:** INCOMPLETE\n"
        )
        pr_result: dict[str, Any] = github_tools["create_draft_pr"](
            repo_name=task_request.repository,
            head=branch_name,
            base=repo_info["default_branch"],
            title=f"[Agent] [INCOMPLETE] {task_request.prompt[:65]}",
            body=pr_body,
            task_id=task_request.task_id,
            agent_ref=task_request.agent_ref,
        )
        pr_url = pr_result["url"]

    github_tools["comment_on_pr"](
        pr_url,
        f"## 🤖 BulletFarm Agent — Task Incomplete\n\n"
        f"No meaningful code changes were produced.\n\n"
        f"### Agent's Response\n\n> {output_text[:500]}\n\n"
        f"### Recommendation\n\n"
        f"- Close this PR or retry with a more specific prompt\n",
    )

    memory_store["store"](
        task_id=task_request.task_id,
        result={
            "agent_ref": task_request.agent_ref,
            "repository": task_request.repository,
            "prompt": task_request.prompt,
            "output": output_text[:2000],
            "skills_used": task_request.skills,
            "tools_called": unique_tools,
            "has_code_changes": False,
            "incomplete_reason": incomplete_reason,
            "phase": TaskPhase.INCOMPLETE.value,
            "pr_url": pr_url,
        },
    )

    return TaskStatus(
        task_id=task_request.task_id,
        phase=TaskPhase.INCOMPLETE,
        progress=100,
        message=f"Task incomplete: {incomplete_reason[:300]}",
        pull_request_url=pr_url,
        incomplete_reason=incomplete_reason,
    )
