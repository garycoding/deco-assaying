"""MCP prompts — workflow templates the server ships alongside its
tools.

Clients (Claude Desktop, llama.cpp's MCP integration, etc.) advertise
these to the user as pickable workflows or feed them into the system
/ first-user message of a session. The point: any client picking up
deco-assaying inherits the recommended workflow — including the
"use the URL instead of inlining big artifacts into your prompt"
guidance — without having to read the README first.

Two prompts:

- `analyze_repo(source, focus?)` — full lifecycle walk-through
  starting from a URL or local path.
- `explore_finished_job(job_id, question)` — for callers who already
  have a finished job and want to ask a focused question.

Both render to plain markdown. Argument substitution is via
`str.format` so any `{` / `}` in the templates that aren't
substitutions need to be doubled (`{{`, `}}`).
"""

from __future__ import annotations

from mcp import types

# ---------------------------------------------------------------------------
# Prompt templates


_ANALYZE_REPO = """\
# Goal

Produce a structural analysis of the repository at `{source}`{focus_clause}.

# Workflow (follow these steps)

1. **Kick off the indexing job.** Call `index_repo(source="{source}")`.
   You'll get back `{{"job_id": "..."}}`. The job runs asynchronously
   on the server.

2. **Wait for it to finish.** Poll `get_job_status(job_id)` every
   few seconds until `state == "done"`. (Other terminal states:
   `failed`, `cancelled`.)

3. **Read the manifest first.** Call `get_manifest(job_id)`. Always
   small. Tells you file count, languages (sorted by count in
   `languages_by_count`), test/config/generated buckets, and the
   parse-error count.

4. **Look up artifact sizes before fetching anything big.** Call
   `get_analysis_index(job_id)`. You get a list of every analysis
   file the server produced, with each artifact's exact byte size
   and an absolute download URL.

5. **Drill in efficiently.** Use the cheap MCP tools when sizes are
   manageable:
   - `get_top_level_symbols(job_id, ...)` — start here for
     "what's the shape of this repo?" Cheaper than `get_all_symbols`.
   - `get_tree(job_id, path_prefix="src/auth/")` — narrowed
     directory inventory.
   - `get_file_analysis(job_id, "path/to/file.py", sections=...)`
     — drill into one file. Use `sections=["symbols","imports"]`
     to skip the chunks payload (often the largest part).
   - `get_all_symbols(job_id, prefix="Foo.")` — cross-cutting
     "find every X" queries. Heavier; only when needed.

# When an artifact is too big for your context window

`get_analysis_index` gives every artifact an absolute `url`. If
`size_bytes` for an artifact would blow your context budget,
**don't** ask deco-assaying for it via the MCP tool. Instead:

- Hand the URL to a generic HTTP fetch / read tool you have access
  to.
- Have that tool process the file out-of-band (extract a section,
  summarize, search for a pattern, etc.).
- Feed only the distilled result back into your prompt.

That way big artifacts can still inform your reasoning without
ever having their raw contents loaded into the conversation.

# When you're done

Summarize the repo's purpose, structure, and notable design
decisions based on what you read. Don't fall back to prior
knowledge — rely on the manifest + filtered symbols + drilled-in
file analyses you actually fetched.
"""

_EXPLORE_FINISHED_JOB = """\
# Context

A `deco-assaying` indexing job has already completed for this
repository. The job_id is `{job_id}`.

# Question

> {question}

# How to answer it

You don't need to call `index_repo` — the artifacts already exist.

1. **Start cheap.** Call `get_manifest(job_id)` for the repo summary.
   Then `get_analysis_index(job_id)` to see exact sizes for every
   artifact (and absolute URLs for them).

2. **Map the question to the right tool:**

   - "What's the structure of this repo?" or "what does it do?" →
     `get_top_level_symbols(job_id)` plus `get_tree(job_id)` (with
     `analyzed_only=True` to skip vendored noise).
   - "Find every X" or "where is X used / defined?" →
     `get_all_symbols(job_id, prefix="X")`. Bigger payload; use
     when you actually need methods + nested defs.
   - "What does file Y do?" →
     `get_file_analysis(job_id, "Y", sections=["symbols","imports","module_doc"])`.
     Skip the chunks section unless you need source text.
   - "What broke during indexing?" → `get_errors(job_id)`.

3. **For oversize artifacts, route around the prompt.** If
   `get_analysis_index` shows an artifact whose `size_bytes` would
   blow your context, don't fetch it through MCP. Instead, hand its
   `url` to a fetch tool, process out-of-band, and feed only the
   distilled result back into the conversation.

Answer the user's question by reasoning over what you actually
fetched — don't fall back to prior knowledge about the repo.
"""


# ---------------------------------------------------------------------------
# Public API


def list_prompts() -> list[types.Prompt]:
    return [
        types.Prompt(
            name="analyze_repo",
            description=(
                "Full workflow for analyzing a repository from scratch. "
                "Starts the indexing job, walks through reading the "
                "rollups efficiently, and explains the URL-fetch "
                "fallback for artifacts too big to inline."
            ),
            arguments=[
                types.PromptArgument(
                    name="source",
                    description=(
                        "GitHub URL, GitLab URL, or local directory "
                        "path. Same shape as `index_repo(source=…)`."
                    ),
                    required=True,
                ),
                types.PromptArgument(
                    name="focus",
                    description=(
                        "Optional area to prioritize (e.g. "
                        '"authentication", "the data model"). '
                        "Threaded through the workflow."
                    ),
                    required=False,
                ),
            ],
        ),
        types.Prompt(
            name="explore_finished_job",
            description=(
                "Workflow for asking a focused question about a "
                "repository whose `index_repo` job is already complete. "
                "Maps common question shapes to the cheapest tools and "
                "covers the URL-fetch escape hatch for oversize "
                "artifacts."
            ),
            arguments=[
                types.PromptArgument(
                    name="job_id",
                    description="The id of a finished `index_repo` job.",
                    required=True,
                ),
                types.PromptArgument(
                    name="question",
                    description="The question to answer about the repo.",
                    required=True,
                ),
            ],
        ),
    ]


def get_prompt(name: str, arguments: dict[str, str] | None = None) -> types.GetPromptResult:
    args = arguments or {}
    if name == "analyze_repo":
        source = args.get("source") or ""
        if not source:
            raise ValueError("analyze_repo requires the 'source' argument")
        focus = args.get("focus") or ""
        focus_clause = f', with a focus on "{focus}"' if focus else ""
        text = _ANALYZE_REPO.format(source=source, focus_clause=focus_clause)
        return types.GetPromptResult(
            description=f"Analyze the repository at {source}",
            messages=[
                types.PromptMessage(
                    role="user",
                    content=types.TextContent(type="text", text=text),
                ),
            ],
        )
    if name == "explore_finished_job":
        job_id = args.get("job_id") or ""
        question = args.get("question") or ""
        if not job_id:
            raise ValueError("explore_finished_job requires the 'job_id' argument")
        if not question:
            raise ValueError("explore_finished_job requires the 'question' argument")
        text = _EXPLORE_FINISHED_JOB.format(job_id=job_id, question=question)
        return types.GetPromptResult(
            description=f"Explore finished job {job_id}",
            messages=[
                types.PromptMessage(
                    role="user",
                    content=types.TextContent(type="text", text=text),
                ),
            ],
        )
    raise ValueError(f"Unknown prompt: {name}")
