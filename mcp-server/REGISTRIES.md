# MCP registry submissions — prepared, NOT yet submitted

Per approval gates, nothing below has been submitted. Each venue lists exactly
what remains. Do them in order; the official registry propagates furthest.

## 0. Prerequisites (all venues)

- [ ] Code merged to `github.com/7Fpuf/emerovia` (e.g. `mcp-server/` subtree or its own repo)
- [ ] Package published to PyPI as `emerovia-mcp` version 0.1.0
      (`python -m build && twine upload dist/*`; README carries the
      `<!-- mcp-name: io.github.7Fpuf/emerovia-mcp -->` ownership marker)
- [ ] `server.json` at the repo root (draft in this directory; regenerate with
      `mcp-publisher init` at publish time to keep the `$schema` current)

## 1. Official MCP Registry (canonical)

Propagates to PulseMCP, the VS Code @mcp gallery, and GitHub's registry UI.

```bash
brew install mcp-publisher            # or the linux tarball from the releases page
cd mcp-server
mcp-publisher publish                 # after `mcp-publisher login github` (device flow)
```

Verify: `curl "https://registry.modelcontextprotocol.io/v0.1/servers?search=io.github.7Fpuf/emerovia-mcp"`

## 2. Smithery (most-used registry)

- Push the repo (with `smithery.yaml`) to GitHub, then connect it at
  https://smithery.ai/new — Smithery builds and hosts the server.
- No CLI needed; listing goes live after their build passes.

## 3. Glama (91K+ servers)

- Glama auto-discovers GitHub repos containing MCP server configs.
  Ensure the repo is public and `server.json` is at root, then claim/verify
  the listing at https://glama.ai/mcp/servers.

## 4. awesome-mcp-servers (~95K stars)

- PR to the awesome-mcp-servers repo adding emerovia-mcp under Python
  community servers, per their contributing format. PRs typically merge
  within ~18 hours.

## 5. mcp.so

- Submit the GitHub repo URL via their "Submit" form; listing after review.

## 6. PulseMCP (directory + newsletter)

- Auto-ingests from the official registry once #1 is live; optionally pitch
  the newsletter at https://www.pulsemcp.com with a one-paragraph summary.

## 7. wshobson/agents marketplace plugin (39.9K stars)

- Needs a coding-agent skill first (a SKILL.md describing Emerovia playbooks).
  Once the skill exists, submit per that repo's plugin format.
