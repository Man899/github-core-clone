# github-core-clone-01

[![make.fun](https://make.fun/badge/github-core-clone-01.svg)](https://make.fun/bounty/github-core-clone-01)
[![rank](https://make.fun/badge/github-core-clone-01/Man899/github-core-clone/rank.svg)](https://make.fun/bounty/github-core-clone-01)
[![reward](https://make.fun/badge/github-core-clone-01/Man899/github-core-clone/reward.svg)](https://make.fun/bounty/github-core-clone-01)

A GitHub-compatible git forge in a single Python 3.13 stdlib file
(`main.py`, ~137 KiB, no dependencies, no git binary, no outside service).
One process serves, from its own in-memory store:

| Surface | Routes |
| --- | --- |
| git smart HTTP | `/<owner>/<repo>.git/info/refs`, `git-upload-pack`, `git-receive-pack` — protocol v2 and v0, packfile read/write (OFS/REF deltas), shallow clone, deepen, non-fast-forward rejection |
| REST v3 | `/api/v3/` — repositories, branches, commits, contents, issues, labels, comments, pull requests, squash merges |
| GraphQL | `POST /api/graphql` — the documents `gh` and `github-mcp-server` send (queries, mutations, introspection, batching, cursor pagination, opaque global ids) |
| MCP | `POST /mcp` — JSON-RPC 2.0 Streamable HTTP, 12 tools (`create_repo`, `get_repo`, `create_issue`, `list_issues`, `create_pull_request`, `merge_pull_request`, …) |

Built for the [make.fun bounty](https://make.fun/bounty/github-core-clone-01)
"Build a GitHub-compatible git forge".

## Run it

```sh
python3 main.py
```

With no `MAKEFUN_ENV_URL` set it serves standalone on an ephemeral
127.0.0.1 port and prints it. Under the suite it follows the starter
lifecycle: bind port 0, `POST $MAKEFUN_ENV_URL/register {"port": N}`,
poll `GET $MAKEFUN_ENV_URL/done` until `{"done": true}`, exit 0.
It never binds 8080. Authentication is `Authorization: Bearer
$MAKEFUN_SHOP_TOKEN`.

## Design notes

- **Deterministic:** every timestamp in the store is fixed
  (`2025-01-01T00:00:00Z` for REST, `1700000000 + commit_seq` for git
  commits), so two runs on the same instance leave the same state
  export byte for byte (verified: identical `state.json` across two
  local grading runs).
- **Fast hot paths:** single-`sendall` responses with `TCP_NODELAY`
  (kills the ~40 ms Nagle/delayed-ACK stall per request), a 256-deep
  listen backlog for the 50-concurrent-clone workload, and a global
  lock only around mutations.
- **Protocol fidelity:** the v2 fetch omits the `acknowledgments`
  section once the client sends `done` (real git dies on it), an unborn
  HEAD is advertised as `unborn HEAD symref-target:…`, and a `have`
  that is also a shallow boundary contributes only its shallow closure
  (so `deepen` re-delivers the parent commit).

## Measured locally (sealed suite, practice instance, this machine)

- Conformance: **120/120** — git 30/30, repos 15/15, issues 15/15,
  pulls 20/20, GraphQL 20/20, MCP 15/15, protocol 5/5.
- Perf (median of 3 rounds): push 541 ms (45 pts), clone 99 ms
  (43 pts), read 860 ms (46 pts), write 140 ms (42 pts) —
  **176/200 perf points**; a second run measured 164/200 (timing
  noise on a shared VM). Feature 600 + agent 200 + perf ≈ **964–976/1000**.

## License

MIT — see [LICENSE](LICENSE).
