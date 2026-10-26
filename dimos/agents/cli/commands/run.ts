import type { ConversationId, EntryId, LiveState } from "@earendil-works/pi-durable";
import type { AssistantMessage } from "@earendil-works/pi-ai";
import type { Ref } from "@earendil-works/chord";
import type { Client } from "../../sdk/main.ts";
import { connect } from "../../sdk/main.ts";
import { unixSocket } from "../../sdk/node.ts";
import { owner, refusalOfFailure } from "../../sdk/failures.ts";
import { retryByRequestId } from "../../sdk/retry.ts";
import { ConversationContract } from "../../shared/protocol/conversation/contract.ts";
import { GatewayContract } from "../../shared/protocol/gateway/contract.ts";
import type { EpochMs } from "../../shared/protocol/time.ts";
import { until } from "../../sdk/until.ts";
import { newActionRequest, newUserRequest } from "../../shared/protocol/actions/request.ts";
import type { Transcript } from "../../shared/protocol/conversation/state.ts";
import { refusalText } from "../../shared/view-models/outcome.ts";
import type { Command, CommandContext } from "./base.ts";

function textOf(content: readonly { readonly type: string; readonly text?: string }[]): string {
  return content.flatMap((block) => (block.type === "text" && block.text !== undefined ? [block.text] : [])).join("");
}

/**
 * Writes the answer as it streams. The partial in `pi.live` is throttled, so the text printed so far is a prefix of
 * the assistant item that lands; each item's remainder and a newline close it.
 */
class AnswerPrinter {
  private printed = "";
  private readonly seenItems = new Set<EntryId>();
  private readonly seenCalls = new Set<string>();
  atLineStart = true;

  constructor(before: Transcript) {
    for (const item of before.items) this.seenItems.add(item.entry);
  }

  live(live: LiveState): void {
    for (const slot of live.tools ?? []) {
      if (this.seenCalls.has(slot.callId)) continue;
      this.seenCalls.add(slot.callId);
      process.stderr.write(`[${slot.name}]\n`);
    }
    const message = live.generation?.message;
    if (message === undefined) return;
    const text = textOf(message.content);
    if (text === "") return;
    if (!text.startsWith(this.printed)) {
      this.finishLine();
      this.printed = "";
    }
    this.write(text.slice(this.printed.length));
    this.printed = text;
  }

  transcript(transcript: Transcript): void {
    for (const item of transcript.items) {
      if (this.seenItems.has(item.entry)) continue;
      this.seenItems.add(item.entry);
      if (item.kind === "assistant") this.answer(item.message);
    }
  }

  finishLine(): void {
    if (this.atLineStart) return;
    process.stdout.write("\n");
    this.atLineStart = true;
  }

  private answer(message: AssistantMessage): void {
    const full = textOf(message.content);
    if (full === "") return;
    this.write(full.startsWith(this.printed) ? full.slice(this.printed.length) : full);
    this.printed = "";
    this.finishLine();
  }

  private write(text: string): void {
    if (text === "") return;
    process.stdout.write(text);
    this.atLineStart = text.endsWith("\n");
  }
}

async function shownConversation(
  client: Client,
  gateway: Ref<typeof GatewayContract>,
  options: CommandContext,
): Promise<{ readonly conversation: Ref<typeof ConversationContract>; readonly id: ConversationId } | string> {
  let id = options.conversation;
  if (id === null) {
    const created = await retryByRequestId(client, { request: newActionRequest(), cwd: options.cwd }, (sameArgs) =>
      owner(gateway).createConversation(sameArgs),
    );
    if (!created.ok) return refusalText(created.refusal);
    id = created.value.conversation;
  }
  const conversation = await Promise.race([
    client.host.get(ConversationContract, { conversation: id, role: "owner" }),
    client.ended,
  ]);
  if (conversation.variant !== "owner") {
    conversation.release();
    return refusalText({ code: "viewing", conversation: id });
  }
  return { conversation, id };
}

/**
 * `dimos agent run PROMPT`: one follow-up into a new or given conversation, its streamed text on stdout, `[tool]`
 * lines on stderr, and its outcome as the exit code: 0 when answered, 1 when unanswered or refused. A reconnect resends
 * `outcomes` whole, so the outcome cannot be missed.
 */
export const printRun: Command = async (options) => {
  const prompt = options.args.join(" ").trim();
  if (prompt === "") throw new Error("dimos agent run needs a PROMPT");
  await options.startGateway();
  try {
    const client = await connect(unixSocket({ path: options.paths.socket, label: "print" }));
    let gateway: Ref<typeof GatewayContract> | undefined;
    try {
      gateway = await Promise.race([client.host.get(GatewayContract, {}), client.ended]);
      const shown = await shownConversation(client, gateway, options);
      if (typeof shown === "string") {
        process.stderr.write(`${shown}\n`);
        return 1;
      }
      const { conversation, id } = shown;
      try {
        process.stderr.write(`Conversation ${id}\n`);
        const printer = new AnswerPrinter(conversation.transcript.value);
        const stops = [
          conversation.live.subscribe(() => printer.live(conversation.live.value)),
          conversation.transcript.subscribe(() => printer.transcript(conversation.transcript.value)),
        ];
        try {
          const sent = await retryByRequestId(
            client,
            { request: newUserRequest(), text: prompt, images: [], whenBusy: "followUp" as const },
            (sameArgs) => owner(conversation).send(sameArgs),
          );
          if (!sent.ok) {
            process.stderr.write(`${refusalText(sent.refusal)}\n`);
            return 1;
          }
          try {
            const ended = await Promise.race([
              until(conversation.outcomes, (rows) => rows.find((row) => row.submission === sent.value.submission)),
              client.ended,
            ]);
            if (ended.status === "unanswered") {
              printer.finishLine();
              process.stderr.write(`Unanswered: ${ended.reason}\n`);
              return 1;
            }
            // Members arrive independently: the outcome can land before the transcript carries the answer it names.
            const answer = ended.answer;
            await Promise.race([
              until(conversation.transcript, (transcript) =>
                transcript.items.some((item) => item.entry === answer) ? transcript : undefined,
              ),
              client.ended,
            ]);
          } catch (error) {
            printer.finishLine();
            process.stderr.write(
              `The gateway disconnected before the answer finished: ${refusalText(refusalOfFailure(error, Date.now() as EpochMs))}\n`,
            );
            return 1;
          }
          printer.transcript(conversation.transcript.value);
          printer.finishLine();
          return 0;
        } finally {
          for (const stop of stops) stop();
          printer.finishLine();
        }
      } finally {
        conversation.release();
      }
    } finally {
      gateway?.release();
      await client.close();
    }
  } catch (error) {
    process.stderr.write(`${refusalText(refusalOfFailure(error, Date.now() as EpochMs))}\n`);
    return 1;
  }
};
