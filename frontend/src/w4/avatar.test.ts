/**
 * 头像统一解析器（`resolveAvatar`）的回归测试。
 *
 * 对应线上问题：「聊天的头像没有更新，或者更新慢；头像仍然存在两种情况，没有统一」。
 * 关键约束：
 *   1. 同一个人（好友 payload / persona payload）解析成同一个字符串；
 *   2. 头像部件（avatar_options）变化时结果立刻变化；
 *   3. 轮询重复解析不再重新跑 DiceBear（LRU 缓存命中）；
 *   4. 好友列表批量补头像时不能把已解析好的 Avataaars 覆写成 personas 画风。
 */

import { beforeEach, describe, expect, it } from "vitest";

import {
  avatarCacheStats,
  avatarSeedOf,
  clearAvatarCache,
  friendlyAvataaarsOptions,
  normalizeAvatarOptions,
  renderAvataaars,
  resolveAvatar,
} from "./avatar";
import { mapClientBootstrap } from "./api";
import { previewDiscoverNext, previewEvents, previewFeed, previewFriends, previewPersona } from "./previewFixtures";

const OPTIONS: Record<string, unknown> = {
  top: ["shortFlat"],
  topProbability: 100,
  skinColor: ["edb98a"],
  hairColor: ["2c1b18"],
  clothing: ["hoodie"],
  clothesColor: ["262e33"],
  eyes: ["default"],
  eyebrows: ["defaultNatural"],
  mouth: ["smile"],
  backgroundColor: ["e8f3ec"],
  accessoriesProbability: 0,
  facialHairProbability: 0,
};

/** persona 接口 / 信息流作者 / 通知人物 payload（avatar_options 是对象） */
const PERSONA_RAW = {
  id: "p_jiwoo",
  nickname: "Jiwoo",
  avatar_seed: "persona-jiwoo",
  avatar_style: "avataaars",
  avatar_options: OPTIONS,
  gender: "female",
  country: "Korea",
  avatar: "/api/v3/avatar/p_jiwoo.svg?g=female&r=east_asian",
};

/** /api/v2/friends 好友行 payload（avatar_options 是 DB 里的 JSON 字符串） */
const FRIEND_RAW = {
  id: "fr_jiwoo_01",
  name: "Jiwoo",
  avatar_seed: "persona-jiwoo",
  avatar_style: "avataaars",
  avatar_options: JSON.stringify(OPTIONS),
  gender: "female",
  nationality: "Korea",
  avatar_url: "/api/v3/avatar/p_jiwoo.svg?g=female&r=east_asian",
};

beforeEach(() => {
  clearAvatarCache();
});

describe("resolveAvatar · 同一张脸", () => {
  it("同一个人：好友 payload 与 persona payload 解析结果完全一致", () => {
    expect(resolveAvatar(FRIEND_RAW)).toBe(resolveAvatar(PERSONA_RAW));
  });

  it("avatar_options 是 JSON 字符串还是对象，结果一致", () => {
    expect(resolveAvatar({ ...PERSONA_RAW, avatar_options: JSON.stringify(OPTIONS) })).toBe(
      resolveAvatar({ ...PERSONA_RAW, avatar_options: OPTIONS }),
    );
  });

  it("有 seed 没有部件：统一用 Avataaars 画风（不再是 personas 插画）", () => {
    const rendered = resolveAvatar({
      id: "p_jiwoo",
      nickname: "Jiwoo",
      avatar_seed: "persona-jiwoo",
      gender: "female",
      country: "Korea",
      avatar: "/api/v3/avatar/p_jiwoo.svg",
    });
    expect(rendered.startsWith("data:image/svg+xml")).toBe(true);
    /* 与「友好确定性部件」渲染结果一致 —— 同一 seed 同一张脸 */
    expect(rendered).toBe(
      renderAvataaars("persona-jiwoo", friendlyAvataaarsOptions("persona-jiwoo", "female")),
    );
  });

  it("老数据缺 avatar_seed 时退回 id，仍然稳定可复现", () => {
    const legacy = { id: "p_adrian", name: "Adrian", gender: "male", country: "Ireland" };
    const first = resolveAvatar(legacy);
    clearAvatarCache();
    expect(resolveAvatar(legacy)).toBe(first);
    expect(first.startsWith("data:image/svg+xml")).toBe(true);
  });

  it("预览/老数据：persona-N 的 id 与好友行 seed=N 归一到同一个人", () => {
    const friendRaw = { id: "fixture-1", name: "Callum", avatar_seed: "1", gender: "male" };
    const personaRaw = {
      id: "persona-1",
      nickname: "Callum",
      gender: "male",
      avatar: "/mock/avatars/callum.jpg",
    };
    expect(resolveAvatar(personaRaw)).toBe(resolveAvatar(friendRaw));
  });
});

describe("resolveAvatar · 更新及时", () => {
  it("avatar_options 变化时头像立刻变化（缓存 key 含 options）", () => {
    const before = resolveAvatar(PERSONA_RAW);
    const changed = resolveAvatar({ ...PERSONA_RAW, avatar_options: { ...OPTIONS, top: ["hat"] } });
    expect(changed).not.toBe(before);
    expect(changed.startsWith("data:image/svg+xml")).toBe(true);
  });

  it("没有部件时，seed 变化时头像变化", () => {
    const base = {
      id: "p_seed_a",
      nickname: "A",
      avatar_seed: "persona-seed-a",
      gender: "female",
      country: "Korea",
    };
    const before = resolveAvatar(base);
    const changed = resolveAvatar({ ...base, id: "p_seed_b", avatar_seed: "persona-seed-b" });
    expect(changed).not.toBe(before);
  });
});

describe("resolveAvatar · 缓存（轮询不重复渲染）", () => {
  it("同一个输入重复解析只渲染一次，返回同一个字符串", () => {
    const first = resolveAvatar(PERSONA_RAW);
    const second = resolveAvatar(PERSONA_RAW);
    expect(second).toBe(first);
    const stats = avatarCacheStats();
    expect(stats.renders).toBe(1);
    expect(stats.hits).toBe(1);
  });

  it("好友行与 persona payload 共享缓存（同一个 key）", () => {
    resolveAvatar(FRIEND_RAW);
    const stats = avatarCacheStats();
    expect(stats.renders).toBe(1);
    expect(resolveAvatar(PERSONA_RAW)).toBe(resolveAvatar(FRIEND_RAW));
    /* 第二个 payload 命中缓存，没有第二次 DiceBear 渲染 */
    expect(avatarCacheStats().renders).toBe(1);
  });

  it("不同的人各自渲染并各自缓存", () => {
    resolveAvatar(PERSONA_RAW);
    resolveAvatar({
      id: "p_adrian",
      nickname: "Adrian",
      avatar_seed: "persona-adrian",
      avatar_style: "avataaars",
      avatar_options: { ...OPTIONS, top: ["sides"] },
      gender: "male",
      country: "Ireland",
    });
    expect(avatarCacheStats().renders).toBe(2);
    expect(avatarCacheStats().cacheSize).toBe(2);
  });
});

describe("resolveAvatar · 兜底顺序", () => {
  it("完全没有 seed：先用服务端兜底 URL", () => {
    expect(resolveAvatar({ avatar: "/api/v3/avatar/legacy.svg" })).toBe(
      "/api/v3/avatar/legacy.svg",
    );
  });

  it("连兜底 URL 都没有才回落 diceAvatar 插画", () => {
    const fallback = resolveAvatar({});
    expect(fallback.startsWith("data:image/svg+xml")).toBe(true);
  });

  it("normalizeAvatarOptions 容忍空值/坏 JSON/数组", () => {
    expect(normalizeAvatarOptions(undefined)).toBeNull();
    expect(normalizeAvatarOptions("")).toBeNull();
    expect(normalizeAvatarOptions("{}")).toBeNull();
    expect(normalizeAvatarOptions("not json")).toBeNull();
    expect(normalizeAvatarOptions([1, 2])).toBeNull();
    expect(normalizeAvatarOptions('{"top":["hat"]}')).toEqual({ top: ["hat"] });
  });
});

describe("mapFriend 集成", () => {
  it("bootstrap 好友头像不会被 assignAvatars 覆写成 personas 画风", () => {
    const snapshot = mapClientBootstrap({
      profile: {},
      friends: [FRIEND_RAW],
      requests: [],
      conversations: [],
      expressions: [],
      capabilities: {},
      unread_events: 0,
    });
    const friend = snapshot.friends[0];
    expect(friend.avatar.startsWith("data:image/svg+xml")).toBe(true);
    /* 与人物接口同一套 seed + 部件 → 同一张脸 */
    expect(friend.avatar).toBe(resolveAvatar(PERSONA_RAW));
  });

  it("好友申请（requests）与好友列表头像一致", () => {
    const requestRaw = { ...FRIEND_RAW, id: "fr_request_01" };
    const snapshot = mapClientBootstrap({
      profile: {},
      friends: [FRIEND_RAW],
      requests: [requestRaw],
      conversations: [],
      expressions: [],
      capabilities: {},
      unread_events: 0,
    });
    expect(snapshot.requests[0].avatar).toBe(snapshot.friends[0].avatar);
  });

  it("预览 fixtures：同一个人的好友/信息流/通知/主页/偶遇解析成同一张脸", () => {
    const friends = previewFriends(12);
    const friend = friends[0];
    const author = previewFeed(1)[0].author;
    const eventPersona = previewEvents()[0].persona;
    const personaProfile = previewPersona(0);
    const discover = previewDiscoverNext().persona;

    const expected = resolveAvatar(friend);
    expect(resolveAvatar(author)).toBe(expected);
    expect(resolveAvatar(eventPersona)).toBe(expected);
    expect(resolveAvatar(personaProfile)).toBe(expected);

    /* 偶遇 Camila = 好友列表第 5 位（index 4） */
    expect(resolveAvatar(discover)).toBe(resolveAvatar(friends[4]));
  });
});
