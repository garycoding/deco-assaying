

we need to add prompt defs and Workflow Prompts for the MCP to return from:
  mcp.list_prompts
  mcp.get_prompt

we need to make sure that we are capturing the repo's license in the notes we generate.


Ebony reconnoitering (Ebon-recon)
also identify well know algorithms (a-start, quick sort, etc.) and recognize and make note when well know Design Patterns are being used. 
answer: what problem is this repo trying to solve and how is it trying to solve it?


-----------------------------------------------
# Enterprise Github/Lad & Private repos
we want to handle exterprise github and gitlab
we want to handle private repos that require auth

- when asked to do this it proposed what's below and I rejected it and said to postpone this.

## Decisions

- **Configurable hosts via env vars only.** Not per-call MCP
  arguments — those would invite the model to pass arbitrary URLs
  and complicate auth. Ops sets `GITHUB_HOST` / `GITLAB_HOST` once
  on the daemon/container and forgets about it. Default
  `github.com` / `gitlab.com` preserves current behavior.
- **One host per provider per process.** No multi-host fan-out in
  v3. If someone needs both github.com and ghe.company.com from
  the same daemon, they run two daemons — much simpler than
  multi-tenant routing.
- **GHE API path is `/api/v3` not `/`.** GitHub Enterprise puts
  the REST API under `/api/v3/...` rather than the dedicated
  `api.github.com` host. Provider's `_API_BASE` becomes computed
  from `GITHUB_HOST`.
- **GHE raw URL is `<host>/raw/<owner>/<repo>/<ref>/<path>`** (the
  GHE form), not `raw.githubusercontent.com`. We branch the raw
  URL builder on whether host == `github.com`.
- **`git clone` token plumbing via `-c http.extraHeader`.** Avoids
  embedding the token in the URL (which would leak it in process
  listings, command history, and `.git/config`). `git -c
  http.extraHeader='Authorization: Bearer <token>' clone …` is the
  standard pattern.
- **Same env vars for both API and clone.** No separate
  `GITHUB_CLONE_TOKEN`. The token that authenticates the API
  pre-flight is the same one that authenticates the clone.
-----------------------------------------------




