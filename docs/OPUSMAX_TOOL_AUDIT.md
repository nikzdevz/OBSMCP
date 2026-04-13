# OpusMax Tool Audit

This note captures the verified dependency points before the `OpusMax` MCP server is removed from Claude and replaced with `obsmcp`-backed tool calls.

## Verified current state

1. Claude currently has two MCP servers at the user level:
   - `OpusMax`
   - `obsmcp`

2. Claude still routes its core model API traffic through OpusMax from the user-level Claude settings file:
   - `~/.claude/settings.json`
   - `ANTHROPIC_BASE_URL=https://api.opusmax.pro`

3. `obsmcp` already depends on OpusMax for semantic description generation:
   - [server/llm_client.py](../server/llm_client.py)

4. Before this implementation pass, `obsmcp` did not expose first-class MCP tools for:
   - web search
   - image understanding

## Implemented in phases 2-5

1. Added an internal OpusMax provider abstraction:
   - [server/opusmax_provider.py](../server/opusmax_provider.py)

2. Kept semantic descriptions working through the provider abstraction:
   - [server/llm_client.py](../server/llm_client.py)

3. Added `obsmcp` service methods and MCP tool exposure for:
   - `web_search`
   - `understand_image`
   - [server/service.py](../server/service.py)

4. Added tests for:
   - provider adapters
   - service-level tool exposure and provider-usage logging
   - [tests/test_opusmax_provider.py](../tests/test_opusmax_provider.py)
   - [tests/test_service.py](../tests/test_service.py)

## Cutover Status: COMPLETE (2026-04-13)

1. ✅ Direct `OpusMax` MCP server entry was already absent from `~/.claude.json`
2. ✅ obsmcp is the sole MCP server at `http://127.0.0.1:9300/mcp`
3. ✅ web_search and understand_image tools work through obsmcp
4. ✅ Token usage tracking recorded for both operations

## Remaining Direct Dependencies

1. Claude's core API routing in `~/.claude/settings.json` still uses:
   - `ANTHROPIC_BASE_URL=https://api.opusmax.pro`
   - `ANTHROPIC_AUTH_TOKEN=sk-ant-opm-...` (OpusMax API key)

   This is **intentional** - keeps OpusMax as the API gateway for all Claude API calls.

2. obsmcp's semantic descriptions in `server/llm_client.py` still call OpusMax API directly via `OpusMaxTextProvider` for LLM-powered code descriptions.

## Verified Working (2026-04-13)

- web_search: 4 recorded events, 10 results per query, ~2.5s latency
- understand_image: 1 recorded event, accurate image analysis, ~9s latency
- Both tools track provider usage metrics via get_token_usage_stats
- HTTP(S) URL images may return error 2013 - use base64 or file paths instead