Read AGENTS.md and the project issue detail file named by PROJECT_ISSUE_DETAIL. Do not modify any
files, run tests, inspect production, or begin implementation.

Classify the issue only by its implementation complexity and risk. Choose exactly one supported
implementation profile:

- gpt-5.6-terra / medium: focused, low-risk changes within one layer or component.
- gpt-5.6-sol / medium: changes spanning multiple layers or components, persistence contracts,
  UI plus backend, substantial tests, or moderate operational risk.
- gpt-5.6-sol / high: difficult cross-cutting behavior, security-sensitive handling, concurrency,
  migrations, or a non-obvious root cause whose incorrect resolution has material impact.
- gpt-6-astra / high: only for unusually broad or high-risk work where the issue requires major
  architectural decisions across several subsystems and the lower profiles would be unsafe.

Prefer the least expensive profile that can reliably complete the documented acceptance criteria.
Return only one JSON object, with no Markdown or surrounding prose:
{"model":"<allowed model>","reasoning_effort":"<allowed effort>","rationale":"<brief non-secret reason>"}
