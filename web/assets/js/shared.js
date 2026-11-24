// 常量、纯函数、图表公共样式。放一处，改颜色只改这里。

export const SNAPSHOT_MS = 1000;   // 快照刷新间隔
export const HISTORY_MS = 5000;    // 历史与排行刷新间隔，没必要每秒查库
export const HEALTH_LEN = 60;      // 数据健康走势保留最近 60 秒
export const QUERY_TABLE_LIMIT = 200;   // 表格只渲染前 200 行，完整数据走 CSV
export const QUERY_MAX_STATIONS = 4;    // 查询页最多对比 4 个站，再多线会挤成一团

// 线路色是数据色（图例、柱子、线网图同一份来源），不参与界面配色
export const LINE_COLORS = { L1: "#2f6bff", L2: "#8b5cf6" };
export const QUERY_COLORS = ["#2f6bff", "#8b5cf6", "#12a150", "#d98b00"];

export const PANEL_TOOLTIP = {
  backgroundColor: "#ffffff", borderColor: "#e4e5e0", borderWidth: 1,
  textStyle: { color: "#14161a", fontSize: 12 }, padding: [8, 12]
};

export const AXIS = {
  axisLine: { lineStyle: { color: "#e4e5e0" } },
  axisTick: { show: false },
  axisLabel: { color: "#9aa0a8", fontSize: 12 }
};

export const SPLIT_LINE = { lineStyle: { color: "#f1f2ec", type: "solid" } };

export function lineName(id) {
  if (id === "L1") return "1 号线";
  if (id === "L2") return "2 号线";
  return id || "未分线";
}

// 缺值显示“—”，不拿 0 冒充：接口给 null 就是还没有数
export function fmtNumber(value) {
  return value === null || value === undefined ? "—" : value;
}

export function formatTs(ts, granularity) {
  if (!ts) return "—";
  if (granularity === "hour") return ts.slice(5, 13).replace("T", " ") + ":00";
  if (granularity === "minute") return ts.slice(11, 16);
  return ts.slice(11, 19);
}

export async function getJSON(url) {
  const res = await fetch(url, { cache: "no-store" });
  if (!res.ok) throw new Error(url + " -> " + res.status);
  return res.json();
}
