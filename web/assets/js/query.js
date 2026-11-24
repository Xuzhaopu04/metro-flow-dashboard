// 客流查询视图：条件、请求、明细、导出。跟实时监看共用一份站点与快照状态。

import { QUERY_TABLE_LIMIT, QUERY_MAX_STATIONS, getJSON, formatTs, lineName } from "./shared.js";
import { createQueryChart } from "./charts.js";

export function createQueryPanel(deps) {
  const { ref, computed, nextTick } = Vue;
  const { stations, rank, stationMeta, stationMetaById, stationLabel, lineColorOf, view } = deps;

  const minuteOptions = [1, 5, 15, 30, 60];
  const minutes = ref(30);              // 回溯范围，接口支持 1—60 分钟
  const rangeMode = ref("minutes");     // minutes=最近 N 分钟，today=今日 0 点到现在
  const granularity = ref("second");
  const granularityOptions = [
    { value: "second", label: "秒" },
    { value: "minute", label: "分钟" },
    { value: "hour", label: "小时" }
  ];
  const direction = ref("all");         // 对应契约 v1.3 的 direction
  const directionOptions = [
    { value: "all", label: "合计" },
    { value: "in", label: "进站" },
    { value: "out", label: "出站" }
  ];
  const selectedLine = ref("");         // 空串代表全部线路
  const selectedStations = ref([]);     // 空数组代表全部站点

  // 查询页的"全部"必须用空串：实时页的 "all" 一旦当成线路编码发给接口，只会筛出空集。
  // 线路名优先用 /api/stations，接口还没回来时退回固定名称，避免按钮突然消失。
  const queryLineOptions = computed(() => {
    const lines = stationMeta.value.lines.length
      ? stationMeta.value.lines.map(item => ({
          value: item.line_id, label: item.name || lineName(item.line_id)
        }))
      : [
          { value: "L1", label: "1 号线" },
          { value: "L2", label: "2 号线" }
        ];
    return [{ value: "", label: "全部线路" }, ...lines];
  });

  const summary = ref(null);            // /api/summary/today
  const summaryLoading = ref(false);
  const summaryError = ref("");
  const summaryLoaded = ref(false);
  const querySeries = ref([]);          // [{ station_id, points }]
  const queryRank = ref([]);
  const queryTruncated = ref(false);
  const queryLoading = ref(false);
  const queryError = ref("");
  const queryLoaded = ref(false);       // 首次进查询页才请求，避免拖慢首屏
  const autoRefresh = ref(false);       // 查询默认手动刷新，看历史时不被自动滚动打断
  const queryEl = ref(null);

  let chart = null;
  let seq = 0;   // 连续切时间范围时只接受最后一次请求的结果

  // 查询页的站点来源：优先用当前快照；消费者刚停时用排行兜底，避免选择器突然空掉
  const stationList = computed(() => {
    const map = new Map();
    for (const s of stations.value) map.set(s.station_id, s);
    for (const r of rank.value) {
      if (!map.has(r.station_id)) map.set(r.station_id, { station_id: r.station_id, line_id: "" });
    }
    const items = [...map.values()];
    // 选了线路就只展示这条线的站点，避免用户选了 L1 又点 L2 的站，查出来是空集
    return selectedLine.value ? items.filter(s => s.line_id === selectedLine.value) : items;
  });

  const queryTotalLabel = computed(() => {
    const base = selectedStations.value.length ? "所选站点" : "全部站点";
    const metric = direction.value === "all" ? "乘降量" : (direction.value === "in" ? "进站" : "出站");
    return base + metric;
  });

  function pointValue(point) {
    if (direction.value === "in") return point.in;
    if (direction.value === "out") return point.out;
    return point.total != null ? point.total : point.passengers;
  }

  // 多站时把各站序列摊平成明细；同一时间戳下按站点名排序，导出的 CSV 也能直接对账
  const queryRows = computed(() => {
    const rows = [];
    for (const series of querySeries.value) {
      const who = series.station_id || "全部";
      for (const point of series.points || []) {
        rows.push({
          key: who + point.ts,
          ts: point.ts,
          station: who,
          in: point.in,
          out: point.out,
          total: point.total != null ? point.total : point.passengers,
          value: pointValue(point)
        });
      }
    }
    rows.sort((a, b) => (a.ts === b.ts ? a.station.localeCompare(b.station) : a.ts.localeCompare(b.ts)));
    return rows;
  });

  // 方向未知时接口给 null：合计不能拿 0 冒充，也不能把 total 顶到进/出站上
  const queryTotal = computed(() => {
    const rows = queryRows.value;
    if (!rows.length) return 0;
    if (rows.some(row => row.value === null || row.value === undefined)) return null;
    return rows.reduce((sum, row) => sum + row.value, 0);
  });

  const queryPeak = computed(() => {
    const rows = queryRows.value;
    if (!rows.length) return 0;
    if (rows.some(row => row.value === null || row.value === undefined)) return null;
    return rows.reduce((max, row) => Math.max(max, row.value), 0);
  });

  const directionUnknown = computed(() =>
    direction.value !== "all" && queryRows.value.length > 0 &&
    queryRows.value.every(row => row.value === null || row.value === undefined)
  );

  function rankNumber(item) {
    if (direction.value === "in") return item.in;
    if (direction.value === "out") return item.out;
    return item.passengers != null ? item.passengers : item.total;
  }

  function rankText(item) {
    const value = rankNumber(item);
    return value === null || value === undefined ? "—" : value;
  }

  const queryRankMax = computed(() =>
    Math.max(1, ...queryRank.value.map(item => Number(rankNumber(item)) || 0))
  );

  const queryRankLabel = computed(() =>
    rangeMode.value === "today" ? "今日 Top 5" : "近 " + minutes.value + " 分钟 Top 5"
  );

  function rankBarWidth(item) {
    const value = Number(rankNumber(item));
    return value > 0 ? Math.max(4, Math.round(value / queryRankMax.value * 100)) + "%" : "0%";
  }

  function initChart() {
    if (!chart && queryEl.value) chart = createQueryChart(queryEl.value);
    return chart;
  }

  function drawQuery() {
    if (!chart) return;
    chart.draw({
      seriesRows: querySeries.value,
      pointValue,
      stationLabel,
      stationMetaById: stationMetaById.value,
      lineColorOf,
      formatTs: ts => formatTs(ts, granularity.value)
    });
  }

  async function loadSummary() {
    summaryLoading.value = true;
    summaryError.value = "";
    try {
      summary.value = await getJSON("/api/summary/today");
      summaryLoaded.value = true;
      if (rangeMode.value === "today") queryRank.value = summary.value.top_stations || [];
    } catch (e) {
      summaryError.value = "今日汇总加载失败";
    } finally {
      summaryLoading.value = false;
    }
  }

  async function loadRank() {
    if (rangeMode.value === "today") {
      queryRank.value = summary.value ? (summary.value.top_stations || []) : [];
      return;
    }
    const params = new URLSearchParams({ top: "5", minutes: String(minutes.value) });
    if (selectedLine.value) params.set("line_id", selectedLine.value);
    if (direction.value !== "all") params.set("direction", direction.value);
    try {
      const data = await getJSON("/api/rank?" + params.toString());
      queryRank.value = Array.isArray(data.top) ? data.top : [];
    } catch (e) {
      queryRank.value = [];
    }
  }

  function refreshAll() {
    loadQuery();
    loadSummary();
  }

  function setRangeMode(mode) {
    rangeMode.value = mode;
    if (mode === "today") granularity.value = "minute";
    syncHash();
    loadQuery();
  }

  function setGranularity(value) {
    granularity.value = value;
    syncHash();
    loadQuery();
  }

  function setLine(value) {
    // 兼容旧链接里残留的 all
    selectedLine.value = value === "all" ? "" : value;
    selectedStations.value = [];
    syncHash();
    loadQuery();
  }

  function setDirection(value) {
    direction.value = value;
    syncHash();
    loadQuery();
  }

  function setMinutes(value) {
    rangeMode.value = "minutes";
    minutes.value = value;
    // 大范围用秒级会超过 2000 点被抽稀，默认切到分钟；用户仍可手动改回秒
    if (value <= 15) granularity.value = "second";
    else if (granularity.value === "second") granularity.value = "minute";
    syncHash();
    if (view.value === "query") loadQuery();
  }

  function selectAllStations() {
    selectedStations.value = [];
    syncHash();
    if (view.value === "query") loadQuery();
  }

  function toggleStation(id) {
    const next = selectedStations.value.filter(item => item !== id);
    if (next.length === selectedStations.value.length) {
      if (next.length >= QUERY_MAX_STATIONS) return;
      next.push(id);
    }
    selectedStations.value = next;
    syncHash();
    if (view.value === "query") loadQuery();
  }

  function toggleAutoRefresh() {
    autoRefresh.value = !autoRefresh.value;
    if (autoRefresh.value && view.value === "query") refreshAll();
  }

  // 查询条件写进 hash，刷新或转发链接还能回到同一组站点和时间范围
  function syncHash() {
    if (view.value !== "query") {
      history.replaceState(null, "", "#live");
      return;
    }
    const params = new URLSearchParams();
    params.set("range", rangeMode.value);
    if (rangeMode.value === "minutes") params.set("minutes", String(minutes.value));
    params.set("granularity", granularity.value);
    if (direction.value !== "all") params.set("direction", direction.value);
    if (selectedLine.value) params.set("line_id", selectedLine.value);
    if (selectedStations.value.length) params.set("stations", selectedStations.value.join(","));
    history.replaceState(null, "", "#query?" + params.toString());
  }

  function readHash() {
    const hash = window.location.hash || "";
    if (!hash.startsWith("#query")) return;
    view.value = "query";
    const params = new URLSearchParams(hash.split("?")[1] || "");
    rangeMode.value = params.get("range") === "today" ? "today" : "minutes";
    const parsed = Number.parseInt(params.get("minutes") || "", 10);
    if (Number.isFinite(parsed) && parsed >= 1 && parsed <= 60) minutes.value = parsed;
    const gran = params.get("granularity");
    if (["second", "minute", "hour"].includes(gran)) granularity.value = gran;
    else if (rangeMode.value === "today") granularity.value = "minute";
    const dir = params.get("direction");
    if (["all", "in", "out"].includes(dir)) direction.value = dir;
    const lineParam = params.get("line_id") || "";
    selectedLine.value = lineParam === "all" ? "" : lineParam;
    const stationsParam = params.get("stations") || "";
    selectedStations.value = stationsParam.split(",").map(id => id.trim())
      .filter(Boolean).slice(0, QUERY_MAX_STATIONS);
  }

  function queryParams() {
    const params = new URLSearchParams();
    if (rangeMode.value === "today") {
      const start = new Date();
      start.setHours(0, 0, 0, 0);
      // 优先用 summary 的项目时区起点，避免浏览器时区与 Asia/Shanghai 不一致
      params.set("from", summary.value ? summary.value.since : start.toISOString());
      params.set("to", new Date().toISOString());
    } else {
      params.set("minutes", String(minutes.value));
    }
    params.set("granularity", granularity.value);
    if (direction.value !== "all") params.set("direction", direction.value);
    if (selectedLine.value) params.set("line_id", selectedLine.value);
    return params;
  }

  async function loadQuery() {
    const ticket = ++seq;
    queryLoading.value = true;
    queryError.value = "";
    try {
      const ids = selectedStations.value.slice();
      const base = queryParams();
      // 接口一次只接受一个 station_id；多站对比就在前端并发请求后合并
      const urls = ids.length
        ? ids.map(id => {
            const params = new URLSearchParams(base);
            params.set("station_id", id);
            return "/api/series?" + params.toString();
          })
        : ["/api/series?" + base.toString()];
      const results = await Promise.all(urls.map(getJSON));
      if (ticket !== seq) return;
      queryTruncated.value = results.some(result => result.truncated === true);
      querySeries.value = results.map((result, index) => ({
        station_id: ids[index] || "",
        points: Array.isArray(result.points) ? result.points : []
      }));
      queryLoaded.value = true;
      loadRank();
      await nextTick();
      drawQuery();
    } catch (e) {
      if (ticket !== seq) return;
      queryError.value = "加载失败，请检查接口或稍后重试";
      querySeries.value = [];
      queryTruncated.value = false;
      chart && chart.clear();
    } finally {
      if (ticket === seq) queryLoading.value = false;
    }
  }

  function exportCsv() {
    if (!queryRows.value.length) return;
    const csvCell = value => '"' + String(value === null || value === undefined ? "" : value).replace(/"/g, '""') + '"';
    const lines = ["时间,站点,方向,合计,进站,出站,粒度"];
    for (const row of queryRows.value) {
      lines.push([
        row.ts, stationLabel(row.station), direction.value,
        row.total, row.in, row.out, granularity.value
      ].map(csvCell).join(","));
    }
    const blob = new Blob(["\ufeff" + lines.join("\n")], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    const suffix = rangeMode.value === "today" ? "今日" : minutes.value + "分钟";
    link.download = "客流查询_" + suffix + "_" + granularity.value + ".csv";
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 0);
  }

  return {
    minuteOptions, minutes, rangeMode, setRangeMode, setMinutes, granularity, granularityOptions,
    setGranularity, direction, directionOptions, setDirection, selectedLine, setLine, queryLineOptions,
    selectedStations, stationList, toggleStation, selectAllStations,
    summary, summaryLoading, summaryError, summaryLoaded, refreshAll,
    querySeries, queryRows, queryLoading, queryError, queryLoaded, queryTotal, queryPeak,
    queryTotalLabel, queryTruncated, directionUnknown, queryRank, queryRankLabel, rankText,
    rankBarWidth, queryTableLimit: QUERY_TABLE_LIMIT, queryEl, autoRefresh, toggleAutoRefresh,
    loadQuery, loadSummary, exportCsv, syncHash, readHash, initChart, drawQuery,
    resize: () => chart && chart.resize(),
    dispose: () => chart && chart.dispose()
  };
}
