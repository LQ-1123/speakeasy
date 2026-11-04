"use client";

/**
 * 桌面宽屏、未选中会话时的空态。
 *
 * 只用一枚浅灰的品牌剪影（声波同心圆），没有文字、没有按钮、没有底板。
 * 跟微信桌面版那种「留白中央一个淡图标」的处理一致。
 */

export function EmptyState() {
  return (
    <div className="w4-main">
      <div className="w4-empty">
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img
          className="w4-empty-logo"
          src="/brand/empty-state.svg"
          alt=""
          width={140}
          height={140}
        />
      </div>
    </div>
  );
}
