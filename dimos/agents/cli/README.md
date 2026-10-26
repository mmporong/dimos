# Command line

This folder is the Node program behind `dimos agent`. Python's `dimos agent`, in
[agent.py](../../cli/commands/agent.py), checks Node and the build, then replaces itself with `node dist/cli.js`, which
esbuild bundles from [main.ts](main.ts). `main.ts` parses the arguments and runs one `Command` from
[commands/](commands/). The `gateway` command runs the gateway in this process and `version` prints this build. The
other commands reach a running gateway over its Unix socket through the SDK, and [process.ts](process.ts) finds, starts
and stops that gateway.

## Built on the SDK, Chord and `parseArgs`

A command is a client of the gateway's Chord objects, and it reaches them through the [SDK](../sdk/README.md).
`connect(unixSocket({ path, label }))` from [sdk/main.ts](../sdk/main.ts) and [sdk/node.ts](../sdk/node.ts) dials the
gateway's Unix socket until the link is live and returns a `Client`. The gateway refuses the upgrade unless `label` is 1
to 32 letters, digits, `_` or `-`, and it grants every client on the 0600 socket the `owner` variant. `connect` has no
timeout. It redials a missing or silent socket forever, so a command calls `startGateway` or `gatewayState` first.

`await client.host.get(Contract, input)` resolves to a Chord reference once the object is live and each of its state
members has its first value. A state member is a cell, read with `.value` and watched with `.subscribe`. Only the
`owner` variant has methods, and each resolves to an Outcome, `{ ok: true, value }` or `{ ok: false, refusal }`.
Commands race each `get` against `client.ended`, which rejects once the link closes for good, so a gateway that goes
away fails the command instead of leaving it waiting. A `finally` block releases every reference and closes the client.
`refusalOfFailure` in [sdk/failures.ts](../sdk/failures.ts) turns any failure into a `Refusal`, and
[refusalText](../shared/view-models/outcome.ts) turns that into the sentence every frontend prints. The
[Chord guide](https://github.com/earendil-works/pi/tree/main/packages/chord) covers references, variants and errors.

Chord binds a reference only when the gateway's copy of a contract covers the client's copy, and otherwise `get` fails
with `contract_mismatch`. [LifecycleContract](../shared/protocol/lifecycle/contract.ts) is frozen at version 1, with one
state member, `process`, and one method, `stop`. Anything that may change goes on `GatewayContract`. So a CLI of any
build can hold `LifecycleContract` on a gateway of any other build, read its build and pid, and stop it. For that reason
`status` and `stop` hold it first, and `gatewayState` and `stopGateway` hold no other contract.

Chord's check does not catch a changed state shape. Gateway state members are declared with `carried<T>()`, which puts
no schema on the wire, so a member whose shape changed still binds. The CLI compares builds itself. `buildInfo().build`
in [build.ts](../gateway/config/build.ts) is a hash of the running script, so each rebuild of `dist/cli.js` changes it,
and a CLI run from source as `node cli/main.ts` hashes `main.ts` instead. On a mismatch `gatewayState` reports
`other_build`, and `startGateway` throws, so `run`, `conversations` and `tui` refuse that gateway. A gateway whose Chord
link protocol differs never delivers `LifecycleContract`, so it reads as `not_answering`, and `stopGateway` throws
rather than stop it.

`main.ts` parses the command line once with Node's [`parseArgs`](https://nodejs.org/api/util.html#utilparseargsconfig),
in strict mode with positionals allowed. All commands share one option set, and an undeclared option makes the CLI exit
1 with the parser's message. A new flag needs an entry in the `options` of `main.ts` and a field in `CommandContext`.

## The API

### How `dimos agent` starts `main.ts`

Python runs `load` and `service` itself. It hands every other command to Node with its arguments unparsed, `--`
and `--help` included. A command line that names no Python command, such as `dimos agent --conversation 3`, goes to Node
whole. Python first checks that `dimos/agents/package.json` exists and that `node` on `PATH` is version 24 or 26, and it
exits 1 with a message when one fails. Then it replaces itself with `node dist/cli.js ARGS` through `os.execve`, so
Node's exit code is the command's.

Before that, every command but `gateway`, `stop` and `status` builds what changed, under a lock on `dist/.build.lock`.
It runs `npm ci` when `package.json` or `package-lock.json` differs from what `node_modules/.dimos-stamp` records,
keeping the previous `node_modules` until the install succeeds. It refuses to install while `status` shows a gateway
running this checkout's build. It runs `npm run build` when a file listed in `dist/inputs.json` changed. `build.mjs`
writes that list with the hash of every file it read. npm gets the same environment as Node. `gateway`, `stop` and
`status` run the existing build and fail when there is none; `gateway` first waits out a build in progress.

The environment is Python's own, minus each variable whose value `dimos` loaded from `.env` and that is not a
`GlobalConfig` setting, because the gateway's `bash` tool inherits the gateway's environment and `.env` holds API keys.
Python adds six variables that say where DimOS is:

| Variable          | Value                                                                   |
| ----------------- | ----------------------------------------------------------------------- |
| `DIMOS_ROOT`      | The DimOS checkout                                                      |
| `DIMOS_PYTHON`    | The venv's Python, `sys.executable`                                     |
| `DIMOS_CLI`       | The venv's `dimos` script, which the gateway runs as `dimos agent load` |
| `DIMOS_MCP_URL`   | McpServer's URL, from its module settings                               |
| `DIMOS_RUNS_DIR`  | The run registry, `~/.local/state/dimos/runs/`                          |
| `DIMOS_STATE_DIR` | DimOS's state directory, `~/.local/state/dimos/`                        |

`DIMOS_MCP_URL` comes from the host and port of `McpServerConfig`, which `MCPSERVER__HOST` and `MCPSERVER__PORT` set
in the environment or `.env`; the host defaults to `listen_host`. `dimos agent` resolves them the same way
`dimos agent load` configures the McpServer it adds.

[`parseDimosInstall`](../gateway/config/environment.ts) reads the six into a `DimosInstall` and throws when one is
missing. `gateway` and `tui` call it, and a gateway that `startGateway` spawns inherits the command's environment. So
`node dist/cli.js status` works without Python. `gateway`, `tui` and a command that has to start a gateway do not.

`main.ts` then runs these steps:

1. It checks the Node major version again and exits 1 unless it is 24 or 26.
2. It parses `--conversation`, `--view`, `--json`, `--cwd` and `--help`. The first positional names the command, `tui`
   when there is none, and the rest become `args`.
3. `help` or `--help` prints the usage and exits 0. An unknown command prints the usage on stderr and exits 2.
4. It builds one `CommandContext`, runs the command from its `commands` map, waits for stdout and stderr to drain, and
   exits with the number the command returns. A thrown error prints its message on stderr and exits 1.

### `Command` and `CommandContext`

[commands/base.ts](commands/base.ts) defines the family. A `Command` is
`(context: CommandContext) => number | Promise<number>`, and the number is the exit code. `main.ts` is the family's one
consumer. [test/boundaries.test.ts](../test/boundaries.test.ts) fails when a file in `commands/` does not import
`base.ts`, or when `main.ts` does not import it as a value.

| Field              | Holds                                                                                           |
| ------------------ | ----------------------------------------------------------------------------------------------- |
| `paths`            | `AgentPaths` from [`agentPaths(env)`](../gateway/config/paths.ts), the socket and log included  |
| `env`              | The process environment, which `startGateway` passes to the gateway it spawns                   |
| `conversation`     | `--conversation` as a `ConversationId`, or `null`. A value below 1 or not a whole number throws |
| `cwd`              | `--cwd` as an absolute path, or `null`                                                          |
| `view`             | `--view`. `tui` opens the `--conversation` as a viewer, and exits 2 without one                 |
| `json`             | `--json`. `status` prints JSON                                                                  |
| `args`             | The positionals after the command name                                                          |
| `bundledSkillsDir` | `harness/skills/`, one directory up from `cli/main.ts` and from `dist/cli.js`                   |
| `startGateway`     | `ensureGateway` with this process's own command line and `gateway` as the command               |

The [Commands section of agent.md](../../../docs/usage/agent.md#commands) lists the commands for a user. [Files](#files)
says what each one does with the gateway.

`runStatus` in [status.ts](commands/status.ts) shows the path from a command to the gateway:

1. `gatewayState(paths, 500)` reads the gateway from outside. For `other_build` it prints the gateway's build and the
   restart hint and exits 1. It exits 3 for `stopped` and 1 for `stale_socket` and `not_answering`.
2. `connect(unixSocket({ path: paths.socket, label: "status" }))` opens a client.
3. It holds `LifecycleContract`, then `GatewayContract`, racing each `get` against `client.ended`.
4. With `--json`, `statusJson(refs)` returns `{ kind: "running", process, gateway }`, with the value of every gateway
   state member under `gateway`. Without it, `statusText` from [status.ts](../shared/view-models/status.ts) builds the
   lines from the derivations the TUI draws.
5. A `finally` releases both references and closes the client. A failure prints
   `refusalText(refusalOfFailure(error, now))` on stderr and exits 1.

## Files

- [main.ts](main.ts) is the entry. It holds `USAGE`, the `parseArgs` options, the `commands` map and the
  `CommandContext` it builds. [build.mjs](../build.mjs) bundles it to `dist/cli.js`.
- [process.ts](process.ts) exports `LaunchState`, `gatewayState`, `ensureGateway` and `stopGateway`. The gateway's tests
  in [host.test.ts](../gateway/server/host.test.ts) call `gatewayState` and `stopGateway` on stale and silent sockets.

`commands/` is a family with one file per subcommand. [base.ts](commands/base.ts) defines `Command` and
`CommandContext`, and `main.ts` imports every member.

- [conversations.ts](commands/conversations.ts) starts a gateway, holds `GatewayContract`, and prints one line per
  conversation from `conversations` through `switcherRows` and `switcherLine`.
- [gateway.ts](commands/gateway.ts) runs `runGateway` in this process with `parseDimosInstall(env)` and only pi-ai's
  built-in model providers, until the gateway stops.
- [run.ts](commands/run.ts) exports `printRun`. It starts a gateway, creates a conversation unless `--conversation`
  names one, holds it as owner, and sends the prompt as a follow-up with `retryByRequestId`. Its `AnswerPrinter` streams
  text from `live` to stdout and each tool name as `[name]` to stderr. The command waits for its submission in
  `outcomes`, then for the transcript to carry the answer, and exits 0 when answered and 1 when unanswered or refused.
- [status.ts](commands/status.ts) exports `runStatus` and `statusJson`. It never starts a gateway.
- [stop.ts](commands/stop.ts) calls `stopGateway` and prints whether a gateway was running.
- [tui.ts](commands/tui.ts) starts a gateway and calls `runTerminal` from [frontends/tui](../frontends/tui/README.md) as
  owner, or as viewer with `--view`, which needs `--conversation`.
- [version.ts](commands/version.ts) prints this process's `buildInfo()` as JSON without reaching a gateway.

## See also

- [sdk/README.md](../sdk/README.md) for `connect`, references, `Outcome` and retries.
- [gateway/README.md](../gateway/README.md) for `runGateway`, the socket server and `AgentPaths`.
- [shared/protocol/README.md](../shared/protocol/README.md) for the contracts and the actions behind their methods.
- [boundaries.test.ts](../test/boundaries.test.ts), which checks the `commands/` family.
- [The on-robot agent](../../../docs/usage/agent.md#commands) for the commands, the service and the state files.
- [How the pieces run](../../../docs/development/agent-design.md#how-the-pieces-run) for the gateway's place beside
  DimOS.
