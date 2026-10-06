"use client";

/**
 * 登录 / 注册两页的共用外壳：一屏连续画布，左侧品牌叙述 + 右侧表单卡。
 *
 * 三个刻意的选择：
 *   * **装饰铺满整屏**（网格 / 光晕 / 双曲线），表单是浮在上面的一张半透明卡——
 *     左右两栏因此共享同一个背景，不会像两块拼接的板子；
 *   * 主题切换器放在这里：登录前也得能切深浅，且与登录后共用同一个 `ThemeProvider`
 *     （next-themes 写 localStorage），切了跨页保持一致；
 *   * 装饰全部 CSS/SVG 且只取语义 token，零图片素材、零构建期网络，深浅色自动跟随。
 */

import { ThemeToggle } from "@/components/theme-toggle";

/** 装饰曲线：一段带波动的上行净值（确定性数据，避免 hydration 不一致）。 */
const CURVE =
  "M0,252.3 C10.1,252.3 13.9,260.9 24,260.9 C34.1,260.9 37.9,251.5 48,251.5 " +
  "C58.1,251.5 61.9,262.9 72,262.9 C82.1,262.9 85.9,257.6 96,257.6 C106.1,257.6 109.9,258.4 120,258.4 " +
  "C130.1,258.4 133.9,270.3 144,270.3 C154.1,270.3 157.9,266.1 168,266.1 C178.1,266.1 181.9,278.7 192,278.7 " +
  "C202.1,278.7 205.9,277.1 216,277.1 C226.1,277.1 229.9,288.6 240,288.6 C250.1,288.6 253.9,299.3 264,299.3 " +
  "C274.1,299.3 277.9,298 288,298 C298.1,298 301.9,282.3 312,282.3 C322.1,282.3 325.9,291.8 336,291.8 " +
  "C346.1,291.8 349.9,297.8 360,297.8 C370.1,297.8 373.9,289.2 384,289.2 C394.1,289.2 397.9,269.1 408,269.1 " +
  "C418.1,269.1 421.9,262.3 432,262.3 C442.1,262.3 445.9,262 456,262 C466.1,262 469.9,240.9 480,240.9 " +
  "C490.1,240.9 493.9,253.2 504,253.2 C514.1,253.2 517.9,236.3 528,236.3 C538.1,236.3 541.9,239.9 552,239.9 " +
  "C562.1,239.9 565.9,248.7 576,248.7 C586.1,248.7 589.9,258.4 600,258.4 C610.1,258.4 613.9,261.3 624,261.3 " +
  "C634.1,261.3 637.9,245.9 648,245.9 C658.1,245.9 661.9,253.4 672,253.4 C682.1,253.4 685.9,246.5 696,246.5 " +
  "C706.1,246.5 709.9,237.5 720,237.5 C730.1,237.5 733.9,238.1 744,238.1 C754.1,238.1 757.9,232.4 768,232.4 " +
  "C778.1,232.4 781.9,244.1 792,244.1 C802.1,244.1 805.9,256 816,256 C826.1,256 829.9,262.5 840,262.5 " +
  "C850.1,262.5 853.9,252.1 864,252.1 C874.1,252.1 877.9,250.7 888,250.7 C898.1,250.7 901.9,253.4 912,253.4 " +
  "C922.1,253.4 925.9,246.3 936,246.3 C946.1,246.3 949.9,244 960,244 C970.1,244 973.9,247.2 984,247.2 " +
  "C994.1,247.2 997.9,232.6 1008,232.6 C1018.1,232.6 1021.9,221.4 1032,221.4 C1042.1,221.4 1045.9,226.6 1056,226.6 " +
  "C1066.1,226.6 1069.9,219.9 1080,219.9 C1090.1,219.9 1093.9,215 1104,215 C1114.1,215 1117.9,197.5 1128,197.5 " +
  "C1138.1,197.5 1141.9,185.3 1152,185.3 C1162.1,185.3 1165.9,188.9 1176,188.9 C1186.1,188.9 1189.9,167.6 1200,167.6";

/** 对照曲线：同一走势整体下移并压扁，做纵深（不与主线抢读）。 */
const GHOST_CURVE = CURVE.replace(
  /([MLC])([\d.]+),([\d.]+)/g,
  (_, cmd, x, y) => `${cmd}${x},${(Number(y) * 0.94 + 26).toFixed(1)}`,
);

const CORE_POINTS = ["多 Agent 并行取证", "PIT 约束语义检索", "结论可回测验证"];

export function AuthShell({ children }: { children: React.ReactNode }) {
  return (
    <div className="relative min-h-dvh overflow-hidden">
      <Backdrop />

      <div className="absolute top-5 right-5 z-10">
        <ThemeToggle />
      </div>

      <div className="relative mx-auto grid min-h-dvh max-w-[1320px] content-center items-center gap-12 px-6 py-16 lg:grid-cols-[1fr_minmax(360px,400px)] lg:gap-20 lg:px-14">
        <BrandColumn />

        <section className="w-full justify-self-center lg:justify-self-end">
          <div className="auth-card">{children}</div>
        </section>
      </div>
    </div>
  );
}

/** 装饰层：网格 → 光晕 → 双曲线 → 底部遮罩，顺序即层序。 */
function Backdrop() {
  return (
    <>
      <div aria-hidden className="auth-grid absolute inset-0" />
      <div aria-hidden className="auth-glow absolute inset-0" />
      <svg
        aria-hidden
        viewBox="0 0 1200 320"
        preserveAspectRatio="none"
        className="absolute inset-x-0 bottom-0 h-[52%] w-full"
      >
        <defs>
          <linearGradient id="auth-curve-fill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="var(--series-1)" stopOpacity="0.22" />
            <stop offset="100%" stopColor="var(--series-1)" stopOpacity="0" />
          </linearGradient>
        </defs>
        <path
          d={GHOST_CURVE}
          fill="none"
          stroke="var(--series-2)"
          strokeOpacity="0.18"
          strokeWidth="1"
          strokeDasharray="5 6"
          vectorEffect="non-scaling-stroke"
        />
        <path d={`${CURVE} L1200,320 L0,320 Z`} fill="url(#auth-curve-fill)" />
        <path
          d={CURVE}
          fill="none"
          stroke="var(--series-1)"
          strokeOpacity="0.5"
          strokeWidth="1.5"
          vectorEffect="non-scaling-stroke"
        />
      </svg>
      {/* 底部遮罩：曲线压到文字之下，且让卡片下缘落在干净的底色上 */}
      <div
        aria-hidden
        className="absolute inset-x-0 bottom-0 h-44 bg-gradient-to-t from-background via-background/70 to-transparent"
      />
    </>
  );
}

function BrandColumn() {
  return (
    <div className="flex flex-col gap-10 self-center lg:gap-14">
      <p className="font-heading text-xl font-semibold tracking-tight lg:text-2xl">
        <span aria-hidden className="mr-2.5 inline-block h-5 w-[3px] translate-y-0.5 bg-primary" />
        知策{" "}
        <span className="font-sans text-[0.8em] tracking-[0.08em] text-muted-foreground">
          QuantSage
        </span>
      </p>

      <div>
        <h1 className="font-heading text-[1.95rem] leading-[1.16] font-bold tracking-tight text-balance lg:text-[2.5rem] xl:text-[3rem]">
          前视偏差为零的
          <br />
          AI 投研 Agent
        </h1>

        {/* 行距 1.15rem：三条能力点是并列论据，挤在一起会读成一坨 */}
        <ul className="mt-10 hidden gap-[1.15rem] lg:grid">
          {CORE_POINTS.map((point) => (
            <li key={point} className="flex items-center gap-3.5 text-[0.9375rem] text-ink-2">
              <span aria-hidden className="h-px w-6 bg-primary/60" />
              {point}
            </li>
          ))}
        </ul>
      </div>

      {/* 钩子：一句话说清护城河（不放 PIT 三时间戳那种术语堆砌） */}
      <figure className="hidden border-l-2 border-primary/50 pl-4 lg:block">
        <figcaption className="text-[0.7rem] tracking-[0.18em] text-ink-3 uppercase">
          Point-in-time
        </figcaption>
        <blockquote className="font-heading mt-1.5 text-xl leading-snug font-semibold">
          不让未来的信息，
          <br />
          参与过去的决策。
        </blockquote>
      </figure>
    </div>
  );
}
