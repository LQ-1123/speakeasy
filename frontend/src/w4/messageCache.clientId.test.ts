import { describe, expect, it } from "vitest";
import {
  createClientId,
  isOptimisticMessage,
  mergeMessages,
  reconcileThread,
} from "./messageCache";
import type { Message } from "./model";

const message = (patch: Partial<Message> & Pick<Message, "id" | "time">): Message => ({
  friendId: "friend-1",
  from: "friend",
  kind: "text",
  text: patch.id,
  ...patch,
});

const optimistic = (patch: Partial<Message> & Pick<Message, "id" | "time">): Message =>
  message({ friendId: "friend-1", from: "me", clientId: "client-1", read: false, ...patch });

describe("mergeMessages / clientId", () => {
  it("服务端消息到达时按 clientId 替换乐观消息（即使 id 不同）", () => {
    const older = message({ id: "srv-1", time: "2026-01-01T00:00:01.000Z" });
    const pending = optimistic({ id: "pending-1", time: "2026-01-01T00:00:02.000Z" });
    const saved = message({
      id: "srv-2",
      time: "2026-01-01T00:00:02.000Z",
      from: "me",
      clientId: "client-1",
      read: true,
      text: "hello",
    });

    const merged = mergeMessages([older, pending], [saved]);

    expect(merged.map((item) => item.id)).toEqual(["srv-1", "srv-2"]);
    expect(merged.filter((item) => item.clientId === "client-1")).toHaveLength(1);
    expect(merged[1].read).toBe(true);
    expect(merged[1].text).toBe("hello");
  });

  it("服务端确认后清掉 failed（重试成功的乐观消息不再显示发送失败）", () => {
    const pending = optimistic({
      id: "pending-1",
      time: "2026-01-01T00:00:02.000Z",
      failed: true,
    });
    const saved = message({
      id: "srv-2",
      time: "2026-01-01T00:00:02.000Z",
      from: "me",
      clientId: "client-1",
      read: false,
    });

    const merged = mergeMessages([pending], [saved]);

    expect(merged).toHaveLength(1);
    expect(merged[0].failed).toBeUndefined();
  });

  it("不同 clientId 的乐观消息不会被误删，removeIds 仍然生效", () => {
    const first = optimistic({ id: "pending-1", clientId: "client-1", time: "2026-01-01T00:00:02.000Z" });
    const second = optimistic({ id: "pending-2", clientId: "client-2", time: "2026-01-01T00:00:03.000Z" });
    const serverFirst = message({
      id: "srv-1",
      time: "2026-01-01T00:00:02.000Z",
      from: "me",
      clientId: "client-1",
    });

    const merged = mergeMessages([first, second], [serverFirst], ["pending-1"]);

    expect(merged.map((item) => item.id)).toEqual(["srv-1", "pending-2"]);
  });
});

describe("reconcileThread / loadThread 对齐", () => {
  it("比服务端最新消息还新的乐观消息保持发送中", () => {
    const pending = optimistic({ id: "pending-1", time: "2026-01-01T00:00:05.000Z" });
    const server = message({ id: "srv-1", time: "2026-01-01T00:00:04.000Z" });

    const reconciled = reconcileThread([pending], [server], "friend-1");

    expect(reconciled.find((item) => item.id === "pending-1")?.failed).toBeUndefined();
  });

  it("服务端最新消息之后仍未确认的乐观消息标记 failed（可重试，不重复落库）", () => {
    const pending = optimistic({ id: "pending-1", time: "2026-01-01T00:00:03.000Z" });
    const server = message({ id: "srv-1", time: "2026-01-01T00:00:04.000Z" });

    const reconciled = reconcileThread([pending], [server], "friend-1");

    expect(reconciled.find((item) => item.id === "pending-1")?.failed).toBe(true);
  });

  it("空线程刷新不会误伤其它好友或已失败的消息", () => {
    const pending = optimistic({ id: "pending-1", time: "2026-01-01T00:00:03.000Z", friendId: "friend-2" });
    const failed = optimistic({ id: "pending-2", time: "2026-01-01T00:00:03.000Z", failed: true });

    const reconciled = reconcileThread([pending, failed], [], "friend-1");

    expect(reconciled.find((item) => item.id === "pending-1")?.failed).toBeUndefined();
    expect(reconciled.find((item) => item.id === "pending-2")?.failed).toBe(true);
  });
});

describe("createClientId / isOptimisticMessage", () => {
  it("clientId 符合后端幂等键契约（8–64 位 [A-Za-z0-9._:-]）", () => {
    const ids = Array.from({ length: 20 }, () => createClientId());
    for (const id of ids) {
      expect(id).toMatch(/^[A-Za-z0-9._:-]{8,64}$/);
    }
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("只有带 clientId 的 pending 前缀本方消息才算乐观消息", () => {
    expect(isOptimisticMessage(optimistic({ id: "pending-1", time: "2026-01-01T00:00:00.000Z" }))).toBe(true);
    expect(isOptimisticMessage(message({ id: "pending-1", time: "2026-01-01T00:00:00.000Z" }))).toBe(false);
    expect(isOptimisticMessage(optimistic({ id: "srv-1", time: "2026-01-01T00:00:00.000Z" }))).toBe(false);
  });
});
