import { describe, expect, it } from "vitest";

import { builtinDescriptors, userDescriptors } from "./optimize-form";
import {
  MAX_CELLS,
  buildBatchRequest,
  buildGridRequest,
  effectiveBaseParams,
  cellErrors,
  expandAxisValues,
  gridFormFromRequest,
  gridSize,
  parseAxisValues,
  parseSymbolList,
  validateBatch,
  validateGrid,
} from "./optimize-form";
import type { BatchFormState, GridFormState } from "./optimize-form";

const MA = builtinDescriptors("ma_cross");
const ED = builtinDescriptors("event_driven");

function grid(overrides: Partial<GridFormState> = {}): GridFormState {
  return {
    strategy: "ma_cross",
    strategyId: null,
    symbol: "600519",
    start: "",
    end: "",
    baseParams: { fast: "", slow: "" },
    axes: [{ param: "fast", values: "3, 5, 8" }],
    ...overrides,
  };
}

describe("parseAxisValues", () => {
  it("逗号、中文逗号、空白都当分隔符", () => {
    expect(parseAxisValues("3, 5,8").values).toEqual([3, 5, 8]);
    expect(parseAxisValues("3，5　8").values).toEqual([3, 5, 8]);
  });

  it("接受小数与负数（夏普可以调的那些参数未必是整数）", () => {
    expect(parseAxisValues("0.5, -1.5").values).toEqual([0.5, -1.5]);
  });

  it("空段报错而不是静默跳过 —— 那是打漏了一个逗号，跳过会悄悄少一格", () => {
    const { values, error } = parseAxisValues("3,,5");
    expect(values).toEqual([]);
    expect(error).toContain("空");
  });

  it("非数字报错并指出是谁", () => {
    const { error } = parseAxisValues("3, abc");
    expect(error).toContain("abc");
  });

  it("空串要的是「至少 2 个」而不是「不是数字」", () => {
    expect(parseAxisValues("  ").error).toContain("至少 2 个");
  });
});

describe("expandAxisValues / gridSize", () => {
  it("笛卡尔积按轴序展开（第一轴最慢）", () => {
    expect(
      expandAxisValues([
        { param: "fast", values: [1, 2] },
        { param: "slow", values: [10, 20] },
      ]),
    ).toEqual([
      { fast: 1, slow: 10 },
      { fast: 1, slow: 20 },
      { fast: 2, slow: 10 },
      { fast: 2, slow: 20 },
    ]);
  });

  it("格数是各轴取值个数之积；解析不出来时为 0", () => {
    expect(gridSize([{ param: "fast", values: "1,2,3" }])).toBe(3);
    expect(
      gridSize([
        { param: "fast", values: "1,2,3" },
        { param: "slow", values: "10,20" },
      ]),
    ).toBe(6);
    expect(gridSize([{ param: "fast", values: "1" }])).toBe(1); // 1 个取值也是 1 格；「至少 2 个」由 validateGrid 判
    expect(gridSize([{ param: "fast", values: "" }])).toBe(0);
  });
});

describe("cellErrors", () => {
  it("逐字段值域：内置策略的 min / max", () => {
    expect(cellErrors({ min_score: 150, hold_days: 5 }, "event_driven", ED)).toEqual([
      "最低评分=150 大于 100",
    ]);
  });

  it("跨字段：双均线的 fast < slow", () => {
    expect(cellErrors({ fast: 20, slow: 10 }, "ma_cross", MA)).toEqual(["fast=20 不小于 slow=10"]);
    expect(cellErrors({ fast: 5, slow: 20 }, "ma_cross", MA)).toEqual([]);
  });

  it("用户策略只按 schema 判值域（跨字段约束不在 schema 里，后端网格路径也不管）", () => {
    const descriptors = userDescriptors({
      n: { type: "int", default: 3, min: 1, max: 20, label: "窗口" },
    });
    expect(cellErrors({ n: 30 }, "user", descriptors)).toEqual(["窗口=30 大于 20"]);
  });
});

describe("validateGrid 的边界（与后端 batch.grid_cells 逐条对应）", () => {
  it("正常一轴：通过并给出格数", () => {
    const { errors, size } = validateGrid(grid(), MA);
    expect(errors).toEqual({});
    expect(size).toBe(3);
  });

  it("轴数 0 与 3 都被拒", () => {
    expect(validateGrid(grid({ axes: [] }), MA).errors.grid).toContain("至少要有 1 条");
    const three = grid({
      axes: [
        { param: "fast", values: "1,2" },
        { param: "slow", values: "3,4" },
        { param: "fast", values: "5,6" },
      ],
    });
    expect(validateGrid(three, MA).errors.grid).toContain("最多 2 条");
  });

  it("轴参数不在该策略的参数集里：列出可用参数", () => {
    const { errors } = validateGrid(grid({ axes: [{ param: "nope", values: "1,2" }] }), MA);
    expect(errors["axis-0"]).toContain("不接受参数");
    expect(errors["axis-0"]).toContain("fast");
  });

  // 「轴参数与基座撞键」在**界面上结构性地不可能**：选作轴之后基座输入框就收起了。
  // 曾经的实现把 state 里那份看不见的默认值照发出去，后端按撞键判 422 ——
  // 界面验证逮到的就是这个（可见性必须等于真值）。
  it("被选作轴的参数不再算基座参数（界面上看不见的东西不进请求）", () => {
    const state = grid({ baseParams: { fast: "5", slow: "20" }, axes: [{ param: "fast", values: "3,5" }] });
    expect(effectiveBaseParams(state)).toEqual({ slow: 20 });
    expect(validateGrid(state, MA).errors).toEqual({});
    expect(buildGridRequest(state).params).toEqual({ slow: 20 });
  });

  it("基座里填了空串不算数（空 = 没设）", () => {
    const { errors } = validateGrid(
      grid({ baseParams: { slow: "" }, axes: [{ param: "slow", values: "10,20" }] }),
      MA,
    );
    expect(errors["axis-0"]).toBeUndefined();
  });

  it("两条轴选了同一个参数", () => {
    const { errors } = validateGrid(
      grid({
        axes: [
          { param: "fast", values: "1,2" },
          { param: "fast", values: "3,4" },
        ],
      }),
      MA,
    );
    expect(errors["axis-1"]).toContain("不止一次");
  });

  it("取值少于 2 个、或有重复", () => {
    expect(validateGrid(grid({ axes: [{ param: "fast", values: "5" }] }), MA).errors["axis-0"]).toContain(
      "至少要有 2 个",
    );
    expect(
      validateGrid(grid({ axes: [{ param: "fast", values: "5, 5" }] }), MA).errors["axis-0"],
    ).toContain("重复");
  });

  it("格数超过上限", () => {
    const { errors } = validateGrid(
      grid({
        baseParams: {},
        axes: [
          { param: "fast", values: Array.from({ length: 11 }, (_, i) => i + 1).join(",") },
          { param: "slow", values: Array.from({ length: 11 }, (_, i) => i + 20).join(",") },
        ],
      }),
      MA,
    );
    expect(errors.grid).toContain(`超过单次上限 ${MAX_CELLS} 格`);
  });

  it("**任一格非法即整单拒绝**，并指出是第几格 —— 与后端同一条规矩", () => {
    const { errors, size } = validateGrid(
      grid({
        baseParams: {},
        axes: [
          { param: "fast", values: "5, 20" },
          { param: "slow", values: "10, 30" },
        ],
      }),
      MA,
    );
    expect(size).toBe(0);
    expect(errors.grid).toContain("第 3 格");
    expect(errors.grid).toContain("fast=20");
    expect(errors.grid).toContain("slow=10");
  });

  it("标的格式与区间先后", () => {
    expect(validateGrid(grid({ symbol: "60051" }), MA).errors.symbol).toBeTruthy();
    expect(
      validateGrid(grid({ start: "2026-09-01", end: "2026-08-01" }), MA).errors.end,
    ).toContain("终点早于起点");
  });

  it("用户策略没选具体策略时报错", () => {
    const { errors } = validateGrid(grid({ strategy: "user", strategyId: null }), MA);
    expect(errors.strategy).toContain("我的策略");
  });
});

describe("buildGridRequest", () => {
  it("空区间不带字段（带空串会变成日期解析失败）", () => {
    const body = buildGridRequest(grid({ start: "", end: "" }));
    expect(body).not.toHaveProperty("start");
    expect(body).not.toHaveProperty("end");
    expect(body.pit_mode).toBe("pit");
  });

  it("轴值解析成数字、基座只带非空的", () => {
    const body = buildGridRequest(
      grid({ baseParams: { fast: "", slow: "20" }, axes: [{ param: "fast", values: "3, 5" }] }),
    );
    expect(body.params).toEqual({ slow: 20 });
    expect(body.axes).toEqual([{ param: "fast", values: [3, 5] }]);
  });

  it("用户策略带上 strategy_id", () => {
    const body = buildGridRequest(
      grid({
        strategy: "user",
        strategyId: "11111111-1111-1111-1111-111111111111",
        baseParams: { n: "" },
        axes: [{ param: "n", values: "2,3" }],
      }),
    );
    expect(body.strategy_id).toBe("11111111-1111-1111-1111-111111111111");
  });
});

describe("gridFormFromRequest", () => {
  it("重开时把当时的样子还回去（轴值拼回逗号串）", () => {
    const form = gridFormFromRequest({
      kind: "grid",
      strategy: "ma_cross",
      symbol: "600519",
      axes: [
        { param: "fast", values: [3, 5, 8] },
        { param: "slow", values: [15, 20] },
      ],
      params: { fast: 5 },
      start: "2026-07-01",
      end: "2026-09-30",
      adjust: "qfq",
      pit_mode: "pit",
      costs: { fees: true, slippage: true, slippage_bps: 5 },
    });
    expect(form.axes).toEqual([
      { param: "fast", values: "3, 5, 8" },
      { param: "slow", values: "15, 20" },
    ]);
    expect(form.baseParams).toEqual({ fast: "5" });
    expect(form.start).toBe("2026-07-01");
  });
});

describe("批量", () => {
  const picks = [
    { key: "ma_cross", label: "双均线", strategy: "ma_cross" as const },
    { key: "user:abc", label: "我的策略", strategy: "user" as const, strategyId: "abc" },
  ];
  // 凑够 6 条才能把 20 个标的顶过 100 格的上限
  const sixPicks = Array.from({ length: 6 }, (_, i) => ({
    key: `ma_cross:${i}`,
    label: `策略 ${i}`,
    strategy: "ma_cross" as const,
  }));
  const batch = (overrides: Partial<BatchFormState> = {}): BatchFormState => ({
    symbols: "600519\n000001",
    picks: ["ma_cross"],
    start: "",
    end: "",
    ...overrides,
  });

  it("标的按行或逗号切分并去重报错", () => {
    expect(parseSymbolList("600519, 000001").symbols).toEqual(["600519", "000001"]);
    const { symbols, errors } = parseSymbolList("600519\n600519\n12345");
    expect(symbols).toEqual(["600519"]);
    expect(errors).toHaveLength(2);
  });

  it("格数 = 标的 × 选中策略；超上限时拒绝", () => {
    expect(validateBatch(batch(), picks).total).toBe(2);
    const many = batch({
      symbols: Array.from({ length: 20 }, (_, i) => String(600000 + i)).join(","),
      picks: ["ma_cross", "user:abc"],
    });
    expect(validateBatch(many, picks).total).toBe(40); // 20 × 2，没超

    const over = batch({
      symbols: Array.from({ length: 20 }, (_, i) => String(600000 + i)).join(","),
      picks: sixPicks.map((pick) => pick.key), // 20 × 6 = 120
    });
    expect(validateBatch(over, sixPicks).errors.picks).toContain("超过单次上限");
  });

  it("没选策略 / 没填标的都报错", () => {
    expect(validateBatch(batch({ picks: [] }), picks).errors.picks).toContain("至少选一条");
    expect(validateBatch(batch({ symbols: "" }), picks).errors.symbols).toContain("至少一个");
  });

  it("请求体按选中的 picks 组装（用户策略带 id）", () => {
    const body = buildBatchRequest(batch({ picks: ["user:abc"] }), picks);
    expect(body.symbols).toEqual(["600519", "000001"]);
    expect(body.strategies).toEqual([{ strategy: "user", strategy_id: "abc" }]);
    expect(body.pit_mode).toBe("pit");
  });
});
