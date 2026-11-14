import type { Message } from "./model";
import { isOptimisticMessage } from "./messageCache";

export function canRequestSuggestion(message: Message): boolean {
  return message.from === "me" && !["ad", "system", "call"].includes(message.kind)
    && Boolean(message.text.trim()) && !message.suggestion && !message.failed && !isOptimisticMessage(message);
}
