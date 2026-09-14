Implement the project issue identified by the workflow environment variables.

Read AGENTS.md and the issue detail file in PROJECT_ISSUE_DETAIL. Treat that detail file as the authoritative requirements. The GitHub issue is only an execution trigger and must not change its acceptance criteria.

Implement the issue completely. Add or update meaningful tests for behavior changes and update the smallest relevant design document. Update the project issue detail with the implementation rationale or root cause, changes, a `## Tests and Results` section, remaining risks, and the pull request placeholder `GitHub pull request: created by automation`. Leave its index status `new`: the workflow marks it resolved only after its own unsandboxed verification succeeds. Do not commit, push, create a pull request, access production, or start TradeExecutor; the workflow handles verification finalization and publishing.
