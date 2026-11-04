"use client";

/** 通讯录在宽屏下的右栏：没有选中好友时的说明页。 */

import { UserPlus } from "lucide-react";

import type { Route } from "../model";
import { useApp } from "../store";
import { Empty } from "../ui";

export function ContactsIntro({ navigate }: { navigate: (r: Route) => void }) {
  const { friends, requests } = useApp();
  return (
    <div className="w4-main">
      <div className="w4-head">
        <div className="w4-head-main">
          <div className="w4-head-title">通讯录</div>
          <div className="w4-head-sub">
            {friends.length} 位好友
            {requests.length ? ` · ${requests.length} 个申请` : ""}
          </div>
        </div>
      </div>
      <Empty
        icon={<UserPlus size={26} strokeWidth={1.5} />}
        title="选一位好友，看看 TA 的资料"
        text={
          <>
            点左边的名字查看对方的城市、作息，
            <br />
            以及你们一起聊过什么。
          </>
        }
        action={
          <button className="w4-btn is-primary" onClick={() => navigate({ name: "discover" })}>
            <UserPlus size={15} />
            认识新朋友
          </button>
        }
      />
    </div>
  );
}
