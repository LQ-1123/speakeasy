import { describe, expect, it } from "vitest";

import { extractTavernStyleCandidates } from "./promptStyleImport";

describe("extractTavernStyleCandidates", () => {
  it("从 Sudachi 的 setvar::style 条目里抽出文风正文并去掉注释", () => {
    const preset = JSON.stringify({
      prompts: [
        {
          identifier: "jailbreak",
          name: "📙文风",
          enabled: true,
          content: "<style>\n# 核心文风\n{{getvar::style}}\n</style>",
        },
        {
          identifier: "2906c3c6",
          name: "🔖轻小说",
          enabled: false,
          content: "{{//单选}}{{setvar::style::\n文风: 日式轻小说\n特点:\n  - 语言简洁轻盈\n  - 对话与心理描写占比大\n  - 叙述节奏快\n}}",
        },
        {
          identifier: "58b1885f",
          name: "✒自定义文风",
          enabled: true,
          content: "<Writing style>\n网络文学文体指导:\n- 基调: 中国网络文学\n- 读者: 年轻人\n- 简化环境描写\n</Writing style>",
        },
      ],
    });

    const result = extractTavernStyleCandidates(preset);
    expect(result.error).toBeUndefined();
    const names = result.candidates.map((item) => item.name);
    expect(names).toContain("🔖轻小说");
    expect(names).toContain("✒自定义文风");
    const light = result.candidates.find((item) => item.name === "🔖轻小说");
    expect(light?.text).toContain("文风: 日式轻小说");
    expect(light?.text).not.toContain("setvar");
    // 只有 getvar 模板、没有实际文风的条目不应入选
    expect(names).not.toContain("📙文风");
  });

  it("支持数据银行的 entries 对象", () => {
    const dlc = JSON.stringify({
      entries: {
        0: {
          content:
            "文风: 舞台剧\n特点:\n  - 对白占比高\n  - 语言生活化但充满潜台词\n  - 场景用括号标注\n  - 角色语气克制真实",
        },
      },
    });
    const result = extractTavernStyleCandidates(dlc);
    expect(result.error).toBeUndefined();
    expect(result.candidates).toHaveLength(1);
    expect(result.candidates[0].name).toBe("未命名文风");
  });

  it("过滤过短/无文字/关闭的条目", () => {
    const preset = JSON.stringify({
      prompts: [
        { name: "文风", content: "短" },
        { name: "文风", content: "{{//注释}}{{setvar::style::}}" },
        { name: "文风", content: "abcdef".repeat(30), enabled: false },
      ],
    });
    const result = extractTavernStyleCandidates(preset);
    expect(result.candidates).toHaveLength(0);
    expect(result.error).toContain("没有识别到");
  });

  it("非法 JSON 返回可读错误", () => {
    expect(extractTavernStyleCandidates("{oops").error).toContain("JSON");
  });
});
