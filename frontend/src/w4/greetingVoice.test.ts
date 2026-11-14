import { afterEach, expect, it, vi } from "vitest";
import { resolveGreetingVoice } from "./greetingVoice";

const voice = vi.hoisted(() => ({
  prefetchedVoiceUrl: vi.fn(), prefetchVoiceUrl: vi.fn(),
  forgetPrefetchedVoice: vi.fn(), synthesizeVoice: vi.fn(),
}));
vi.mock("./apiFacade", () => voice);
afterEach(() => vi.resetAllMocks());

it("does not invoke paid synthesis when an existing greeting download fails or is queue-rejected", async () => {
  voice.prefetchedVoiceUrl.mockReturnValue(null);
  voice.prefetchVoiceUrl.mockResolvedValue(null);
  expect(await resolveGreetingVoice({ id: "friend", greeting: "Hello", greetingAudio: "/api/v2/audio/existing.wav" })).toBeNull();
  expect(voice.synthesizeVoice).not.toHaveBeenCalled();
  expect(voice.forgetPrefetchedVoice).toHaveBeenCalledWith("/api/v2/audio/existing.wav");
});

it("uses an existing cached greeting without network or provider work", async () => {
  voice.prefetchedVoiceUrl.mockReturnValue("blob:cached");
  expect(await resolveGreetingVoice({ id: "friend", greeting: "Hello", greetingAudio: "/api/v2/audio/existing.wav" })).toBe("blob:cached");
  expect(voice.prefetchVoiceUrl).not.toHaveBeenCalled();
  expect(voice.synthesizeVoice).not.toHaveBeenCalled();
});

it("synthesizes only an explicit preview whose existing greeting URL is absent", async () => {
  voice.synthesizeVoice.mockResolvedValue("blob:new");
  expect(await resolveGreetingVoice({ id: "friend", greeting: "Hello" })).toBe("blob:new");
  expect(voice.synthesizeVoice).toHaveBeenCalledWith("Hello", "friend");
});
