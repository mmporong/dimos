# Test runner and test helpers

This folder holds the test runner for the TypeScript agent package, the test that checks the package's import rules, and
the helpers that other tests import. The tests themselves sit beside the code they exercise. `npm test` runs
[run.mjs](run.mjs), which hands every `*.test.ts` file to Node's built-in test runner,
[`node:test`](https://nodejs.org/api/test.html). The helpers open the harness on a scripted model, run it in a child
process that a test can kill, and start a real DimOS system for it to reach.

## Run the tests

Run each command from `dimos/agents`.

- To run every test, run `npm test`. [run.mjs](run.mjs) collects every `**/*.test.ts` outside `node_modules` and `dist`,
  sorts the paths, and passes them to `node --test`. Arguments after `--` go to `node --test` before the file list, as
  in `npm test -- --test-name-pattern="withdrawing"`.
- To run one file, run `NODE_OPTIONS="--import=./test/web-sdk.mjs" node --test harness/inputs/gate.test.ts`. Child
  processes inherit `NODE_OPTIONS`, so tests that start another agent get the same SDK loader.
- To run what CI runs, run `npm run check`. It runs `npm run typecheck`, `npm test` and `npm run build` in that order
  and stops at the first failure.

Node strips the agent files' TypeScript types. [run.mjs](run.mjs) also loads [web-sdk.mjs](web-sdk.mjs) through
`NODE_OPTIONS`: it resolves the web SDK aliases and uses esbuild to transform SDK source syntax that Node cannot strip,
including constructor parameter properties. [web-aliases.mjs](../web-aliases.mjs) supplies the same aliases to the build.
`erasableSyntaxOnly` is disabled in [tsconfig.json](../tsconfig.json) to allow those SDK imports.

The `agent` job in [ci.yml](../../../.github/workflows/ci.yml) installs Node 24 and runs `npm ci`, `npm run typecheck`,
`npm test` and `npm run build`. `ci-complete`, the check that branch protection reads, waits for it. The job creates no
Python environment, so every test that uses `dimosFixture` skips in CI. On a checkout with a `.venv`, `npm test` starts
one DimOS system for each file that calls `dimosFixture`.

The Python tests in this directory, such as [harness/runtime/test_runtime.py](../harness/runtime/test_runtime.py), run
under pytest with the rest of DimOS. [Testing](../../../docs/development/testing.md) covers them.

## Where tests live

A test sits beside the file it tests and takes that file's name with `.test.ts`.
[harness/inputs/gate.test.ts](../harness/inputs/gate.test.ts) tests [gate.ts](../harness/inputs/gate.ts).
[boundaries.test.ts](boundaries.test.ts) is the one exception, because it checks the whole package.

The boundaries test skips `*.test.ts` files, so a test may import any file in the package. The helpers in this folder
are not tests. The boundaries test's package-wide rules apply to them, and no layer rule does.

Pick the helper by what the test needs:

- A test that needs a model and no DimOS opens the agent in its own process with `scriptedModel`.
- A test that kills the agent mid-run, or checks that it never exits on its own, runs it in a child process through
  [agent-child.ts](agent-child.ts).
- A test that needs a real McpServer and HarnessRuntime calls `dimosFixture`.

## The API

### `scriptedModel`

[scripted-model.ts](scripted-model.ts) exports `scriptedModel()`. It takes nothing and returns an object with four
members:

- `models` is a pi-ai `MutableModels` with pi-ai's faux provider registered. Pass it to `openAgent` or `Harness.open`.
- `model` is the `ModelRef` `{ provider: "faux", modelId: "scripted" }`. Use it as a conversation's model. It accepts
  text and images.
- `script(steps)` appends steps to the queue.
- `reset(steps)` replaces the steps still queued.

The model answers each request with the next step. A step is an `AssistantMessage` or a function. Build messages with
pi-ai's `fauxAssistantMessage(content, options)` and `fauxToolCall(name, arguments, { id })`. Give a tool-call message
`{ stopReason: "toolUse" }`. `fauxAssistantMessage` defaults to `"stop"`, and pi-durable runs tool calls only on
`"toolUse"`. Pass `{ id }` to `fauxToolCall` so that the test knows the call id and can find the call's result.

A function step receives the request and returns the answer or a promise of it. Tests use it to record
`request.messages`, which is what the model was sent. [gate.test.ts](../harness/inputs/gate.test.ts) also uses it to
hold an answer until the test releases it. When the queue is empty, the faux provider answers with an error message, so
a test that expects many runs queues spare answers.

### `dimosFixture` and `TestDimos`

[environment.ts](environment.ts) exports `dimosFixture()`. Call it once at the top level of a test file. It registers a
`before` hook and an `after` hook for that file and returns a function. Inside a test, call that function. It returns
the running `TestDimos`, or `null` when the checkout has no `.venv/bin/python`. On `null`, call
`t.skip("no .venv in the checkout")` and return.

The `before` hook takes these steps, with a five-minute limit:

1. It makes a scratch directory in the system temp directory.
2. It picks a free TCP port for McpServer and a free UDP port for a private zenoh scouting address,
   `224.0.0.231:<port>`.
3. It starts `serve_system()` from [harness/runtime/test_runtime.py](../harness/runtime/test_runtime.py) in the
   checkout's `.venv` Python. That blueprint holds `HarnessProbe` and McpServer. `HarnessProbe` publishes a moving
   `position` at 20 Hz and a small `color_image` at 2 Hz, and it has an `echo` skill.
4. It waits up to 60 seconds for McpServer to answer `echo`.
5. It runs `.venv/bin/dimos agent load`, checks that the output names `HarnessRuntime`, and waits up to 20 seconds for
   `tools/list` to include `inventory`.

The `after` hook stops the process with SIGINT, sends SIGKILL after 30 seconds, and deletes the scratch directory.

The system runs with a copy of your environment. The fixture points `XDG_STATE_HOME`, `XDG_CONFIG_HOME`,
`XDG_CACHE_HOME` and `XDG_RUNTIME_DIR` into the scratch directory and sets `MCPSERVER__HOST=127.0.0.1`,
`MCPSERVER__PORT` and `N_WORKERS=1`. It pins the bus whatever your shell sets: `DIMOS_TRANSPORT=zenoh`,
`ZENOH_MODE=peer`, the private `ZENOH_SCOUT_ADDR`, and an empty `ZENOH_CONNECT`, `ROBOT_IP` and `ROBOT_IPS`, so no
session dials a robot or a router. The system shares no state, port or bus with another DimOS on the machine, and its
blueprint has no robot module. The fixture uses zenoh because LCM needs multicast on `lo`, which only root can add. It
needs `.venv/bin/python` and `.venv/bin/dimos` in the checkout, which the developer install creates.

`TestDimos` has these members:

- `install` is the `DimosInstall` that `parseDimosInstall` reads from `env`. A gateway that `dimos agent gateway`
  started for this system would get the same values.
- `env` is the system's environment plus the six variables a gateway reads: `DIMOS_ROOT`, `DIMOS_PYTHON`, `DIMOS_CLI`,
  `DIMOS_MCP_URL`, `DIMOS_RUNS_DIR` and `DIMOS_STATE_DIR`.
- `stop()` stops the process. No McpServer answers until `start()`.
- `start()` starts the system again without HarnessRuntime. The agent under test has to load it back.
- `restart()` calls `stop()` and then `start()`.

A gateway reads its DimOS install from the `DIMOS_*` variables, through `parseDimosInstall` in
[gateway/config/environment.ts](../gateway/config/environment.ts). A test hands the system to the agent in one of two
ways. In its own process, it passes `install: system.install` and `gatewayEnv: system.env` to `openAgent`. In a child,
it passes `system.env` to `ChildAgent.start`, and [agent-child.ts](agent-child.ts) parses its own environment. The agent
then watches the system's runs in `DIMOS_RUNS_DIR` and runs `dimos agent load` in `DIMOS_ROOT` with `gatewayEnv`, which
carries the system's bus settings, `MCPSERVER__HOST`, `MCPSERVER__PORT` and `XDG_STATE_HOME`. `dimos agent load` loads
the checkout's `.env` from any working directory, and a checkout's `.env` can name a real robot, but a variable already
in the environment wins over `.env`. That is why the fixture sets the bus variables itself, the empty ones included.
`DIMOS_ROOT` is an empty directory.

### `ChildAgent` and `ChildCommand`

[agent-child.ts](agent-child.ts) is a script, not a module that tests import. It opens the agent with `openAgent` on a
`scriptedModel`, keeps the store, tool results and render cache in `AGENT_CHILD_SCRATCH`, and reads its DimOS install
from its `DIMOS_*` environment. `AGENT_CHILD_PHASE` picks what it does. `AGENT_CHILD_CLOCK`, when set, freezes the
`Harness` clock at that time in epoch milliseconds.

The agent runs in a child process for two reasons. A test can send SIGKILL during a tool call or a model request, then
open the same store in a new process and check what pi-durable resumes. And an unhandled rejection in the agent ends the
child, not the test runner. The `serve` phase records the rejection in `crash.json` before the child exits.

The phases are these:

- `turn-1`, `turn-2` and `turn-3` serve [harness/main.test.ts](../harness/main.test.ts). That test kills the agent
  during a tool call and again during a model request, then checks that the third process finishes the run.
- `job-create`, `job-attempted` and `job-after` serve [harness/jobs/task.test.ts](../harness/jobs/task.test.ts). That
  test kills a skill job after its `attempted` checkpoint and checks that the skill is not called again.
- `serve` waits for commands. `ChildAgent` drives it.

The phased tests spawn `agent-child.ts` with `node` themselves and read the marker files that each phase writes in the
scratch directory.

[child-driver.ts](child-driver.ts) exports `ChildAgent` and `ChildCommand`. `ChildAgent` has these members:

- `ChildAgent.start(scratch, env)` spawns `agent-child.ts` in the `serve` phase with `env`, and with `scratch` as
  `AGENT_CHILD_SCRATCH`.
- `call<T>(command, timeoutMs = 60_000)` sends one `ChildCommand` and resolves with the child's answer as `T`. It writes
  `cmd-N.json` in `scratch` and polls for `cmd-N.done.json`. It rejects when the child reports an error, exits, or does
  not answer in time. The rejection carries the exit code and the recorded crash.
- `alive` is true until the process exits for any reason. `exited` resolves with the exit code.
- `crash` is what the child recorded before it exited on an unhandled rejection or exception, or `null`.
- `close()` sends `close` and resolves with the exit code, which is `0` after a clean close.
- `kill()` sends SIGKILL.

`ChildCommand` is a union on `op`. The child answers each command with JSON:

| `op`          | Takes                             | Answers                                                                                                                                      |
| ------------- | --------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| `script`      | `messages`, optional `replace`    | `{ scripted }`. It appends to the faux model's queue, or replaces it when `replace` is `true`.                                               |
| `start`       |                                   | `{ conversation }`, a new conversation.                                                                                                      |
| `submit`      | `conversation`, `text`, `request` | `{ status, reason, answer }` after the run settles. When `InputGate` refuses the input, `status` is its `SubmitResult` kind, such as `busy`. |
| `events`      | `conversation`                    | `{ inputs, status }`. Each admitted event input has its UUID `id`, `module`, rendered view kinds, and submission status.                     |
| `toolResults` | `conversation`                    | One `{ toolCallId, toolName, isError, text }` per tool result in the conversation.                                                           |
| `status`      |                                   | `{ link, load }`, the `LinkStatus` and the `LoadStatus`.                                                                                     |
| `close`       |                                   | `{ closed: true }`. Then the child exits with code `0`.                                                                                      |

`submit` hands a user input to `InputGate` with `whenBusy: "reject"`. `script` takes `AssistantMessage` values only,
because a function step cannot cross to the child as JSON.

## Files

- [run.mjs](run.mjs) is the runner that `npm test` calls.
- [web-sdk.mjs](web-sdk.mjs) resolves and transforms web SDK imports in the runner and its child processes.
- [boundaries.test.ts](boundaries.test.ts) checks the import rules and the served contracts. The constants you edit are
  at its top: `LOCAL_LAYERS`, `FAMILIES`, `SUBMITTER`, `MCP_CLIENT` and `CODING_AGENT_IMPORTERS`.
- [scripted-model.ts](scripted-model.ts) exports `scriptedModel`.
- [environment.ts](environment.ts) exports `dimosFixture`. It defines `TestDimos`.
- [child-driver.ts](child-driver.ts) exports `ChildAgent` and `ChildCommand`.
- [agent-child.ts](agent-child.ts) is the child process script and its phases.

## See also

- [The package README](../README.md) maps the folders whose dependencies the boundaries test checks.
- [harness/README.md](../harness/README.md) describes `openAgent` and the `Agent` it returns.
- [harness/inputs/gate.ts](../harness/inputs/gate.ts) defines `InputGate`, which makes the one call to pi-durable's
  `submit`.
- [harness/dimos/](../harness/dimos/connection.ts) holds `DimosConnection` and the HarnessRuntime loader.
- [Testing](../../../docs/development/testing.md) covers the Python tests.

## Real relay SDK test

The real relay SDK test runs automatically when the checkout's `.venv` exists. Set `DIMOS_RELAY_TEST_PYTHON` to use another Python executable. The fixture is [demo_relay.py](../sdk/streams/demo_relay.py). It exercises the Node SDK connection against a relay and native DimOS messages.
