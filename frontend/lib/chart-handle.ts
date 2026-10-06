/**
 * 图表的命令式句柄。
 *
 * 缩放状态是图表的内部状态（一个在 canvas 里、一个在 ECharts 实例里），"回到全览"
 * 只能命令式触发，没法用 props 表达。两个图表共用这一个类型，父组件才拿得住。
 */
export interface ChartHandle {
  resetZoom: () => void;
}
