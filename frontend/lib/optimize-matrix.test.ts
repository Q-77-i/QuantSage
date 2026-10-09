import { describe, expect, it } from "vitest";

import {
  annualized,
  axisLabels,
  batchTableFromCells,
  distributionFromGrid,
  dsrPercent,
  heatmapFromGrid,
  lineFromGrid,
  overfitInputs,
} from "./optimize-matrix";
import type { OptimizeAxis, OptimizeCell, OptimizeSummary } from "./types";

function cell(overrides: Partial<OptimizeCell> = {}): OptimizeCell {
  return {
    index: 0,
    symbol: "600519",
    strategy: "ma_cross",
    strategy_id: null,
    strategy_name: null,
    params: { fast: 5, slow: 20 },
    ok: true,
    duration_ms: 120,
    metrics: {
      total_return: 0.1,
      annual_return: 0.05,
      max_drawdown: 0.2,
      sharpe: 0.5,
      win_rate: 0.5,
      trade_count: 3,
      final_equity: 1_100_000,
      benchmark_return: 0.02,
      excess_return: 0.08,
    },
    moments: { skew: -0.2, kurt: 4.0, n: 100 },
    window: { start: "2026-07-01", end: "2026-09-30", bars: 65 },
    ...overrides,
  };
}

function failedCell(overrides: Partial<OptimizeCell> = {}): OptimizeCell {
  // 逐字写出来而不是从 `cell()` 解构丢弃：失败格**本来就没有** metrics / moments / window，
  // 解构丢弃会把「它有这些字段」这个错觉带进类型里
  return {
    index: 0,
    symbol: "600519",
    strategy: "ma_cross",
    strategy_id: null,
    strategy_name: null,
    params: { fast: 5, slow: 20 },
    ok: false,
    duration_ms: 12,
    error: { kind: "no_data", message: "区间内没有行情数据" },
    ...overrides,
  };
}

function summary(overrides: Partial<OptimizeSummary> = {}): OptimizeSummary {
  return {
    kind: "grid",
    cells: [],
    cells_total: 0,
    cells_ok: 0,
    best_index: null,
    overfit: {
      dsr: null,
      reason: null,
      reason_text: null,
      note: "N 取全网格格数、未做试验间相关性校正——保守估计",
      n_trials: 0,
      n_valid: 0,
      best_index: null,
      sr: null,
      sr0: null,
      sr_variance: null,
      skew: null,
      kurt: null,
      observations: null,
    },
    window: { start: "2026-07-01", end: "2026-09-30", bars: 65 },
    costs: "佣金万2.5(最低5元)+印花税0.05%卖出 / 滑点5bps",
    pit_mode: "pit",
    adjust: "qfq",
    duration_s: 3.2,
    ...overrides,
  };
}

/** 2×2 网格：fast × slow，夏普 +0.8 / -0.3 / +0.2 / +0.5，最优在下标 0 */
function twoByTwo(): { result: OptimizeSummary; axes: OptimizeAxis[] } {
  const axes: OptimizeAxis[] = [
    { param: "fast", values: [3, 8] },
    { param: "slow", values: [15, 30] },
  ];
  const cells: OptimizeCell[] = [
    cell({ index: 0, params: { fast: 3, slow: 15 }, metrics: { ...cell().metrics!, sharpe: 0.8 } }),
    cell({ index: 1, params: { fast: 3, slow: 30 }, metrics: { ...cell().metrics!, sharpe: -0.3 } }),
    cell({ index: 2, params: { fast: 8, slow: 15 }, metrics: { ...cell().metrics!, sharpe: 0.2 } }),
    cell({ index: 3, params: { fast: 8, slow: 30 }, metrics: { ...cell().metrics!, sharpe: 0.5 } }),
  ];
  return {
    result: summary({ cells, cells_total: 4, cells_ok: 4, best_index: 0 }),
    axes,
  };
}

describe("heatmapFromGrid", () => {
  it("下标按**轴值书写顺序**，不排序（顺序是契约的一部分）", () => {
    const { result, axes } = twoByTwo();
    const payload = heatmapFromGrid(result, axes)!;

    expect(payload.xLabels).toEqual(["3", "8"]);
    expect(payload.yLabels).toEqual(["15", "30"]);
    expect(payload.points).toEqual([
      [0, 0, 0.8],
      [0, 1, -0.3],
      [1, 0, 0.2],
      [1, 1, 0.5],
    ]);
    expect(payload.cellIndex).toEqual([0, 1, 2, 3]);
  });

  it("色域**对称**于 0 —— 不对称会让中灰跑到数据中点，色阶就不再表示正负", () => {
    const { result, axes } = twoByTwo();
    expect(heatmapFromGrid(result, axes)!.max).toBe(0.8); // 取 |最小| 与 |最大| 的较大者
  });

  it("全为负值时半径仍取绝对值的最大者", () => {
    const { result, axes } = twoByTwo();
    result.cells[0].metrics!.sharpe = -1.5;
    expect(heatmapFromGrid(result, axes)!.max).toBe(1.5);
  });

  it("失败格**留空**并计入 missing，不补 0", () => {
    const { result, axes } = twoByTwo();
    result.cells[1] = failedCell({ index: 1, params: { fast: 3, slow: 30 } });
    const payload = heatmapFromGrid(result, axes)!;

    expect(payload.points).toHaveLength(3);
    expect(payload.missing).toBe(1);
    expect(payload.points.some((point) => point[2] === 0)).toBe(false);
  });

  it("最优格定位到网格下标", () => {
    const { result, axes } = twoByTwo();
    expect(heatmapFromGrid(result, axes)!.best).toEqual([0, 0]);
  });

  it("最优格是失败格时不画标注（best_index 指不过去）", () => {
    const { result, axes } = twoByTwo();
    result.best_index = 1;
    result.cells[1] = failedCell({ index: 1, params: { fast: 3, slow: 30 } });
    expect(heatmapFromGrid(result, axes)!.best).toEqual([0, 1]); // 下标仍在，但该格无值
  });

  it("一维网格不是热力图的形态", () => {
    const { result } = twoByTwo();
    expect(heatmapFromGrid(result, [{ param: "fast", values: [3, 8] }])).toBeNull();
  });

  it("轴值为 0 时也能定位（不能拿 falsy 判缺失）", () => {
    const axes: OptimizeAxis[] = [
      { param: "fast", values: [0, 5] },
      { param: "slow", values: [10, 20] },
    ];
    const cells = [
      cell({ index: 0, params: { fast: 0, slow: 10 }, metrics: { ...cell().metrics!, sharpe: 0.4 } }),
    ];
    const payload = heatmapFromGrid(summary({ cells }), axes)!;
    expect(payload.points).toEqual([[0, 0, 0.4]]);
    expect(payload.missing).toBe(0);
  });
});

describe("lineFromGrid", () => {
  it("按轴值顺序取点，缺失处留 null（折线断开，不连成假的）", () => {
    const axes: OptimizeAxis[] = [{ param: "fast", values: [3, 5, 8] }];
    const cells = [
      cell({ index: 0, params: { fast: 3 }, metrics: { ...cell().metrics!, sharpe: 0.4 } }),
      cell({ index: 1, params: { fast: 5 }, metrics: { ...cell().metrics!, sharpe: 0.9 } }),
    ];
    const payload = lineFromGrid(summary({ cells, best_index: 1 }), axes)!;

    expect(payload.labels).toEqual(["3", "5", "8"]);
    expect(payload.values).toEqual([0.4, 0.9, null]);
    expect(payload.bestIndex).toBe(1);
  });

  it("二维网格不走折线", () => {
    const { result, axes } = twoByTwo();
    expect(lineFromGrid(result, axes)).toBeNull();
  });
});

describe("distributionFromGrid", () => {
  it("只收有效格；最优点被标出来", () => {
    const { result } = twoByTwo();
    result.cells[1] = failedCell({ index: 1, params: { fast: 3, slow: 30 } });
    const payload = distributionFromGrid(result);

    expect(payload.count).toBe(3);
    expect(payload.points.filter((point) => point.best)).toHaveLength(1);
    expect(payload.points.find((point) => point.best)!.value[0]).toBe(0.8);
  });

  it("同值的点分到不同错位层 —— 否则几个格重叠成一个点，看着像只有一格", () => {
    const { result } = twoByTwo();
    result.cells[1].metrics!.sharpe = 0.8;
    result.cells[2].metrics!.sharpe = 0.8;
    const layers = distributionFromGrid(result)
      .points.filter((point) => point.value[0] === 0.8)
      .map((point) => point.value[1]);

    expect(layers).toEqual([0, 1, 2]);
  });

  it("范围留余量，端点不贴边", () => {
    const { result } = twoByTwo();
    const payload = distributionFromGrid(result);
    expect(payload.min).toBeLessThan(-0.3);
    expect(payload.max).toBeGreaterThan(0.8);
  });

  it("全失败时不出点，范围退化但不 NaN", () => {
    const cells = [failedCell({ index: 0 }), failedCell({ index: 1 })];
    const payload = distributionFromGrid(summary({ cells }));
    expect(payload.count).toBe(0);
    expect(Number.isFinite(payload.min)).toBe(true);
    expect(Number.isFinite(payload.max)).toBe(true);
  });
});

describe("batchTableFromCells", () => {
  it("行列由**请求**给，不从 cells 推 —— 有格失败时少一行正是最该看见的", () => {
    const cells = [
      cell({ index: 0, symbol: "600519", strategy: "ma_cross" }),
      failedCell({ index: 1, symbol: "000001", strategy: "ma_cross" }),
    ];
    const payload = batchTableFromCells(summary({ kind: "batch", cells }), ["600519", "000001"], [
      { key: "ma_cross", label: "双均线" },
    ]);

    expect(payload.symbols).toEqual(["600519", "000001"]);
    expect(payload.grid[0][0]?.ok).toBe(true);
    expect(payload.grid[1][0]?.ok).toBe(false);
  });

  it("缺格留 null，不补 0", () => {
    const payload = batchTableFromCells(summary({ kind: "batch", cells: [] }), ["600519"], [
      { key: "ma_cross", label: "双均线" },
    ]);
    expect(payload.grid).toEqual([[null]]);
  });

  it("按下标定位：同一条策略带不同参数出现两次也不会互相覆盖", () => {
    // 键里没有参数，用 `(标的, 策略)` 查的话这两格会撞成一个
    const cells = [
      cell({ index: 0, symbol: "600519", strategy: "ma_cross", metrics: { ...cell().metrics!, sharpe: 0.5 } }),
      cell({ index: 1, symbol: "600519", strategy: "ma_cross", metrics: { ...cell().metrics!, sharpe: 0.9 } }),
    ];
    const payload = batchTableFromCells(summary({ kind: "batch", cells }), ["600519"], [
      { key: "ma_cross:0", label: "双均线 A" },
      { key: "ma_cross:1", label: "双均线 B" },
    ]);
    expect(payload.grid[0][0]?.metrics?.sharpe).toBe(0.5);
    expect(payload.grid[0][1]?.metrics?.sharpe).toBe(0.9);
  });

  it("格序是「标的在外、策略在内」（后端定死的契约）", () => {
    const cells = [
      cell({ index: 0, symbol: "600519", strategy: "ma_cross" }),
      cell({ index: 1, symbol: "600519", strategy: "event_driven" }),
      cell({ index: 2, symbol: "000001", strategy: "ma_cross" }),
      cell({ index: 3, symbol: "000001", strategy: "event_driven" }),
    ];
    const payload = batchTableFromCells(summary({ kind: "batch", cells }), ["600519", "000001"], [
      { key: "ma_cross", label: "双均线" },
      { key: "event_driven", label: "事件驱动" },
    ]);
    expect(payload.grid[1][0]?.index).toBe(2);
    expect(payload.grid[1][1]?.index).toBe(3);
  });
});

describe("DSR 卡", () => {
  it("无定义时返回 null —— 不画一根 0 的条", () => {
    expect(dsrPercent(null)).toBeNull();
    expect(dsrPercent(Number.NaN)).toBeNull();
  });

  it("百分比夹在 0~100", () => {
    expect(dsrPercent(0.5183)).toBeCloseTo(51.83, 6);
    expect(dsrPercent(-0.1)).toBe(0);
    expect(dsrPercent(1.2)).toBe(100);
  });

  it("输入清单只列有值的行（无定义时不该出现一排「—」）", () => {
    const undefinedCase = overfitInputs(summary());
    expect(undefinedCase.map((row) => row.label)).toEqual(["试验数 N"]);

    const defined = overfitInputs(
      summary({
        overfit: {
          ...summary().overfit,
          dsr: 0.5183,
          n_trials: 25,
          n_valid: 25,
          sr: 0.019,
          sr0: 0.018,
          sr_variance: 0.0002,
          skew: -0.5,
          kurt: 4.0,
          observations: 101,
        },
      }),
    );
    expect(defined.map((row) => row.label)).toEqual([
      "试验数 N",
      "最优夏普 SR",
      "期望最大 SR₀",
      "试验方差 V",
      "偏度 γ₃",
      "峰度 γ₄",
      "观测数 T",
    ]);
    expect(defined[0].value).toBe("25（有效 25）");
  });

  it("每期口径 × √252 还原成年化（公式进的是每期值，界面上给年化）", () => {
    expect(annualized(0.019)!).toBeCloseTo(0.3016, 3);
    expect(annualized(null)).toBeNull();
  });
});

describe("axisLabels", () => {
  it("用参数值的字符串形式，与请求里写的那个数一致", () => {
    expect(axisLabels({ param: "fast", values: [3, 0.5] })).toEqual(["3", "0.5"]);
  });
});

/**
 * 运行中的 `cells` **带空洞**（逐格填充，未到达的槽位是 `undefined`）。
 *
 * 这一组是浏览器逮出来的：第一版所有遍历都直接解引用，结果是**运行中整页白屏**
 * （`TypeError: Cannot read properties of undefined (reading 'ok')`）。
 * 单测里我构造的永远是「跑完之后」的整齐数组，跑不到这个态。
 */
describe("运行中的空洞（逐格填充时的不完整数组）", () => {
  const holed = (): OptimizeCell[] => {
    const cells = new Array<OptimizeCell>(4);
    cells[0] = cell({ index: 0, params: { fast: 3, slow: 15 }, metrics: { ...cell().metrics!, sharpe: 0.6 } });
    cells[2] = cell({ index: 2, params: { fast: 8, slow: 15 }, metrics: { ...cell().metrics!, sharpe: -0.2 } });
    return cells;
  };

  it("热力图：还没到的格按「没有值」处理，不抛", () => {
    const axes: OptimizeAxis[] = [
      { param: "fast", values: [3, 8] },
      { param: "slow", values: [15, 30] },
    ];
    const payload = heatmapFromGrid(summary({ cells: holed() }), axes)!;
    expect(payload.points).toEqual([
      [0, 0, 0.6],
      [1, 0, -0.2],
    ]);
    expect(payload.cellIndex).toEqual([0, 2]); // 与 points 逐位对应：点格重跑靠它
    // `missing` 数的是「**跑过但没值**」的格，不含「还没跑到」的——运行中报「2 格没有值」
    // 会让人以为它们失败了。进度由进度条如实说（已到达 X / 共 N）
    expect(payload.missing).toBe(0);
  });

  it("分布：只画已经到了的格，且 index 取格自己的（不是切片后的下标）", () => {
    const payload = distributionFromGrid(summary({ cells: holed() }));
    expect(payload.count).toBe(2);
    expect(payload.points.map((point) => point.index)).toEqual([0, 2]);
  });

  it("折线：未到达处留 null（断开，不连成假的）", () => {
    const cells = new Array<OptimizeCell>(3);
    cells[0] = cell({ index: 0, params: { fast: 3 }, metrics: { ...cell().metrics!, sharpe: 0.6 } });
    const payload = lineFromGrid(summary({ cells }), [{ param: "fast", values: [3, 5, 8] }])!;
    expect(payload.values).toEqual([0.6, null, null]);
  });

  it("批量表：已到达的格照常对上，其余留 null", () => {
    const payload = batchTableFromCells(
      summary({ kind: "batch", cells: holed() }),
      ["600519", "000001"],
      [{ key: "ma_cross", label: "双均线" }],
    );
    // `holed()` 的 0 号格（下标 0 = 行 0 列 0）已经到了，就该被找到
    expect(payload.grid[0][0]?.index).toBe(0);
    expect(payload.grid[1][0]).toBeNull();
  });

  it("DSR 卡：全空时不抛，且不显示任何输入行", () => {
    const empty = new Array<OptimizeCell>(3);
    expect(overfitInputs(summary({ cells: empty })).map((row) => row.label)).toEqual(["试验数 N"]);
  });
});
