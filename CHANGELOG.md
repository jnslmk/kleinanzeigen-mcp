# Changelog

## [0.2.0](https://github.com/jnslmk/kleinanzeigen-mcp/compare/kleinanzeigen-mcp-v0.1.3...kleinanzeigen-mcp-v0.2.0) (2026-08-17)


### Features

* MCP server for Kleinanzeigen.de listing search ([5d97f49](https://github.com/jnslmk/kleinanzeigen-mcp/commit/5d97f4930322c3849fea4fff5134b1449f0789fd))
* queue concurrent MCP tool calls behind a process-wide scrape gate ([4ffdc39](https://github.com/jnslmk/kleinanzeigen-mcp/commit/4ffdc39d37a4a824a1c9362986c588dcb4c43ff6))


### Bug Fixes

* deadlock-free browser context pool; accept numeric strings for price params ([6c401f3](https://github.com/jnslmk/kleinanzeigen-mcp/commit/6c401f324fbc833d787c94c70c462d2385d71908))
* **pool:** snapshot pages before waking waiters in release_context ([33082c9](https://github.com/jnslmk/kleinanzeigen-mcp/commit/33082c9d11f508834fe030ff73ea288f9525967e))
* **search:** accept page_count and max_pages on both search tools ([6931939](https://github.com/jnslmk/kleinanzeigen-mcp/commit/6931939adb3e37014447a18eddfee43b733f40cb))
