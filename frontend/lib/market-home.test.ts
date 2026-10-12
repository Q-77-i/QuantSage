/**
 * 研究首页视图模型的用例（`lib/market-home.ts`）。
 *
 * 重点钉三类容易错且不报错的口径：
 *   · **缺失不补 0**（无涨跌幅的行、判不了的涨跌停、没有标签的标的）；
 *   · **色阶档位是相对当批极值**归一（不是固定阈值）——断了这条，色阶会退化成两色图；
 *   · **图的顺序与表的顺序必须一致**（连续色标要求有可读的表格孪生，两者错位就等于没有）。
 */

import { describe, expect, it } from "vitest";

import {
  amountCard,
  asOf,
  breadthCard,
  coverageText,
  indexCard,
  limitNote,
  limitsCard,
  maxAbs,
  rampIndex,
  sectorBars,
  sectorTable,
  tone,
} from "./market-home";
import { EMPTY } from "./format";
import type { MarketOverview, MarketSectors, SectorRow } from "./types";

function overview(overrides: Partial<MarketOverview> = {}): MarketOverview {
  return {
    trade_date: "2026-09-30",
    adjust: "qfq",
    amount: 1_450_089_493_893,
    breadth: { total: 5561, up: 2568, down: 2819, flat: 169, without_pct: 5 },
    limits: { limit_up: 58, limit_down: 16, judged: 5556, skipped: 5 },
    limits_meta: { adjust: "raw", st_source: "dictionary", st_symbols: 231, note: "口径说明原文" },
    index: {
      kind: "market_equal_weight",
      level: 1968.1217911243787,
      change_pct: -0.10222038503521416,
      base_level: 1000,
      base_date: "2020-01-02",
      days: 1636,
      excluded: 1581,
      kept: 5556,
      note: "数据源不覆盖指数（M2a 已核）；本基准为全市场等权组合代理",
    },
    as_of: { bars: "2026-09-30", events: "2026-10-09" },
    ...overrides,
  };
}

function sectors(items: SectorRow[]): MarketSectors {
  return {
    trade_date: "2026-09-30",
    adjust: "qfq",
    min_symbols: 10,
    count: items.length,
    coverage: { total: 5556, covered: 4873, ratio: 0.8770698344132469 },
    items,
    note: "归属口径原文",
  };
}

const ROWS: SectorRow[] = [
  { industry: "医药制造", symbols: 22, amount: 7_603_661_152, avg_pct: 2.56, median_pct: 2.6 },
  { industry: "医药生物", symbols: 786, amount: 280_800_000_000, avg_pct: 1.18, median_pct: 0.96 },
  { industry: "化工", symbols: 16, amount: 4_831_000_000, avg_pct: 0.0, median_pct: 0.0 },
  { industry: "电子信息", symbols: 33, amount: 15_280_000_000, avg_pct: -2.18, median_pct: -2.74 },
];

describe("tone", () => {
  it("零与缺失都走中性——不借用涨跌色去说一件中性的事", () => {
    expect(tone(0)).toBe("flat");
    expect(tone(null)).toBe("flat");
    expect(tone(undefined)).toBe("flat");
    expect(tone(Number.NaN)).toBe("flat");
  });

  it("正负各归各的", () => {
    expect(tone(0.01)).toBe("up");
    expect(tone(-0.01)).toBe("down");
  });
});

describe("maxAbs / rampIndex", () => {
  it("分母取当批极值，且忽略缺失", () => {
    expect(maxAbs([1, -3, null, 2])).toBe(3);
    expect(maxAbs([])).toBe(0);
    expect(maxAbs([null, null])).toBe(0);
  });

  it("值映射到 0–6 档，中点落在 3", () => {
    expect(rampIndex(0, 2)).toBe(3);
    expect(rampIndex(2, 2)).toBe(6);
    expect(rampIndex(-2, 2)).toBe(0);
    expect(rampIndex(1, 2)).toBe(5);
  });

  it("超出极值也被夹住（不会越界取到不存在的档）", () => {
    expect(rampIndex(99, 2)).toBe(6);
    expect(rampIndex(-99, 2)).toBe(0);
  });

  it("分母为 0 或值缺失时退回中性档", () => {
    expect(rampIndex(1, 0)).toBe(3);
    expect(rampIndex(null, 5)).toBe(3);
  });
});

describe("大盘块", () => {
  it("代理指数卡：点位是英雄数字，涨跌带号，口径写明「不是指数」", () => {
    const card = indexCard(overview().index)!;
    expect(card.level).toBe("1,968.12");
    expect(card.change).toBe("-0.1%");
    expect(card.tone).toBe("down");
    // 这一条是验收里的「卡上能读到非指数口径字」
    expect(card.caption).toContain("等权代理");
  });

  it("没有指数块时返回 null，不编一个 0", () => {
    expect(indexCard(null)).toBeNull();
  });

  it("涨跌家数：主读数是涨的家数，平的跟在副读数里", () => {
    const card = breadthCard(overview())!;
    expect(card.value).toBe("2,568");
    expect(card.note).toContain("跌 2,819");
    expect(card.note).toContain("平 169");
    expect(card.note).toContain("无涨跌幅 5");
  });

  it("无涨跌幅为 0 时那句整段不出现（不给一个恒为 0 的噪声）", () => {
    const o = overview({ breadth: { total: 1, up: 1, down: 0, flat: 0, without_pct: 0 } });
    expect(breadthCard(o)!.note).not.toContain("无涨跌幅");
  });

  it("涨跌停：判不了的家数照登——不说出来就成了「它们没涨停」", () => {
    expect(limitsCard(overview())!.note).toContain("判不了 5");
    const clean = overview({ limits: { limit_up: 1, limit_down: 0, judged: 5, skipped: 0 } });
    expect(limitsCard(clean)!.note).not.toContain("判不了");
  });

  it("成交额走亿元，缺失时整卡不出现", () => {
    expect(amountCard(overview())!.value).toBe("14,500.9");
    expect(amountCard(overview({ amount: null }))).toBeNull();
  });

  it("两个「截至」并列——它们天然不同步", () => {
    expect(asOf(overview())).toEqual({ bars: "2026-09-30", events: "2026-10-09" });
    expect(asOf(overview({ as_of: null }))).toBeNull();
  });

  it("口径说明照抄服务端，前端不自己拼", () => {
    expect(limitNote(overview())).toBe("口径说明原文");
    expect(limitNote(null)).toBe("");
  });
});

describe("sectorBars", () => {
  it("按后端给的顺序（平均涨跌幅降序）原样呈现", () => {
    expect(sectorBars(sectors(ROWS)).map((b) => b.industry)).toEqual([
      "医药制造",
      "医药生物",
      "化工",
      "电子信息",
    ]);
  });

  it("色阶档位对当批极值归一：+2.56 到正极、0 到中点、−2.18 靠近负极", () => {
    const bars = sectorBars(sectors(ROWS));
    expect(bars[0].rampIndex).toBe(6); // 2.56 是本批最大
    expect(bars[2].rampIndex).toBe(3); // 0
    expect(bars[3].rampIndex).toBe(0); // −2.18 / 2.56 ≈ −0.85 → 归一后 0.07，落在最负那档
  });

  it("条上标注带号带 %，右侧两个读数都给", () => {
    const first = sectorBars(sectors(ROWS))[0];
    expect(first.label).toBe("+2.56%");
    expect(first.meta).toBe("22 只 · 76 亿"); // 小数位是上限，76.04 不补成 76.0
  });

  it("均值与中位数差得远就标出来（这一档被少数极端值主导）", () => {
    const bars = sectorBars(
      sectors([{ industry: "化工", symbols: 16, amount: 1, avg_pct: 2.16, median_pct: 0.34 }]),
    );
    expect(bars[0].skewed).toBe(true);
  });

  it("缺失值不补 0：avg_pct 为 null 时标注是「—」且落在中性档", () => {
    const bars = sectorBars(
      sectors([{ industry: "X", symbols: 5, amount: null, avg_pct: null, median_pct: null }]),
    );
    expect(bars[0].label).toBe(EMPTY);
    expect(bars[0].rampIndex).toBe(3);
    expect(bars[0].meta).toContain(EMPTY);
  });

  it("没有数据时是空数组，不是一张画了 0 的图", () => {
    expect(sectorBars(null)).toEqual([]);
    expect(sectorBars(sectors([]))).toEqual([]);
  });
});

describe("sectorTable", () => {
  it("与图**同序同值**——错位就等于没有孪生", () => {
    const data = sectors(ROWS);
    const bars = sectorBars(data);
    const table = sectorTable(data);
    expect(table.map((r) => r.industry)).toEqual(bars.map((b) => b.industry));
    expect(table.map((r) => r.avg)).toEqual(bars.map((b) => b.label));
  });

  it("中位数单列一栏（与均值并列才看得出偏斜）", () => {
    const table = sectorTable(sectors(ROWS));
    expect(table[0].median).toBe("+2.6%");
    expect(table[2].symbols).toBe("16");
  });
});

describe("coverageText", () => {
  it("覆盖率写成「已覆盖 / 全体（百分比）」", () => {
    expect(coverageText({ total: 5556, covered: 4873, ratio: 0.877 })).toBe("4,873 / 5,556（87.7%）");
  });

  it("分母为 0 或缺失时是「—」，不是 0%", () => {
    expect(coverageText({ total: 0, covered: 0, ratio: null })).toBe(EMPTY);
    expect(coverageText(null)).toBe(EMPTY);
  });
});
