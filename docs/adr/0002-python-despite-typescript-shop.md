# Python, despite being a TypeScript shop

**Context.** The rest of the organization's stack is TypeScript (NestJS, React). This tool is the exception.

**Decision.** The marketing-trends repo is Python. We chose it specifically for the mature DIY-scraping ecosystem (TikTokApi, instaloader, snscrape, Playwright-Python), which does much of the work a paid provider would otherwise do — directly relevant under the zero-budget mandate ([ADR-0001](./0001-diy-scraping-under-zero-budget.md)).

**Trade-off accepted.** Lower familiarity / maintainability for the team, in exchange for far less scraping code to write and maintain. If this repo ever stops being scraping-dominated, the rationale weakens and TypeScript would be reconsidered.

**Shape.** Monorepo: a `core` package (canonical schema, storage, digest UI) + `adapters/{platform}` packages, each owned by a separate parallel build session, all implementing one adapter interface.
