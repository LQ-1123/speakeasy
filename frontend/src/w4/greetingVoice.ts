import { forgetPrefetchedVoice, prefetchVoiceUrl, prefetchedVoiceUrl, synthesizeVoice } from "./apiFacade";
import type { Friend } from "./model";

/** Called only after an explicit preview click; failed downloads never trigger paid synthesis. */
export async function resolveGreetingVoice(friend: Pick<Friend, "id" | "greeting" | "greetingAudio">): Promise<string | null> {
  if (friend.greetingAudio) {
    const local = prefetchedVoiceUrl(friend.greetingAudio) ?? await prefetchVoiceUrl(friend.greetingAudio);
    if (!local) forgetPrefetchedVoice(friend.greetingAudio);
    return local;
  }
  return friend.greeting ? synthesizeVoice(friend.greeting, friend.id) : null;
}
