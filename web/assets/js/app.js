// 实时监看的装配层：轮询、状态、图表联动，查询视图交给 query.js。

import { SNAPSHOT_MS, HISTORY_MS, HEALTH_LEN, LINE_COLORS, lineName, fmtNumber,
         formatTs as formatTsBase, getJSON } from "./shared.js";
import { createBarsChart, createTrendChart } from "./charts.js";
import { createQueryPanel } from "./query.js";

const { createApp, ref, reactive, computed, watch, onMounted, onUnmounted, nextTick } = Vue;

createApp({
  setup() {
    // ---- 共享状态：快照每秒覆盖一次，查询页读同一份站点表 ----
    const now = ref(Date.now());
    const online = ref(true);
    const total = ref(0);
    const snapshotTs = ref("");
    const lateCount = ref(0);
    const invalidCount = ref(0);
    const stations = ref([]);
    const rank = ref([]);
    const trends = ref([]);
    const threshold = ref(100);   // 契约 4.1：阈值由服务端下发，这里只是首帧占位
    const alerting = ref(false);
    const line = ref("all");
    const selected = ref("");     // 空串代表全部站点，下钻时存 station_id
    const view = ref("live");
    const helpOpen = ref(false);
    const helpTab = ref("metric");
    const stationMeta = ref({ stations: [], lines: [] });
    const stationMetaLoaded = ref(false);
    const barEl = ref(null);
    const lineEl = ref(null);

    // 环比要用上一秒的数字，迟到/非法要看走势，这两样接口都不给，只能前端自己攒
    const deltas = reactive({});
    const health = ref([]);
    const alertStart = ref(0);
    const alertPeak = ref(0);

    let bars = null;
    let trend = null;
    let observer = null;
    let prevPassengers = new Map();
    const timers = [];

    const clock = computed(() => new Date(now.value).toTimeString().slice(0, 8));
    const stationCount = computed(() => stations.value.length);
    const visibleStations = computed(() =>
      line.value === "all" ? stations.value : stations.value.filter(s => s.line_id === line.value)
    );
    const snapshotLabel = computed(() => (snapshotTs.value ? snapshotTs.value.slice(11, 19) : "—"));
    const rankMax = computed(() => Math.max(1, ...rank.value.map(item => item.passengers)));
    const stationMetaById = computed(() =>
      new Map(stationMeta.value.stations.map(item => [item.station_id, item]))
    );

    function stationLabel(id) {
      if (!id || id === "全部") return id || "全部";
      const meta = stationMetaById.value.get(id);
      return meta && meta.name ? meta.name : id;
    }

    function lineLabel(id) {
      if (!id) return "未分线";
      const meta = stationMeta.value.lines.find(item => item.line_id === id);
      return meta && meta.name ? meta.name : lineName(id);
    }

    function lineColorOf(id) {
      const meta = stationMeta.value.lines.find(item => item.line_id === id);
      const color = meta && meta.color;
      return /^#[0-9a-f]{6}$/i.test(color || "") ? color : (LINE_COLORS[id] || "#2f6bff");
    }

    const stationOptions = computed(() => [
      { value: "", label: "全部" },
      ...stations.value.map(s => ({ value: s.station_id, label: stationLabel(s.station_id) }))
    ]);

    // 线路选项优先用 /api/stations 的线路名；接口还没修好时退回 L1/L2 的固定名称
    const lineOptions = computed(() => {
      const lines = stationMeta.value.lines.length
        ? stationMeta.value.lines.map(item => ({ value: item.line_id, label: item.name || lineName(item.line_id) }))
        : [
            { value: "L1", label: "1 号线" },
            { value: "L2", label: "2 号线" }
          ];
      return [{ value: "all", label: "全部线路" }, ...lines];
    });

    const alertSeconds = computed(() =>
      alertStart.value ? Math.max(0, Math.round((now.value - alertStart.value) / 1000)) : 0
    );

    // 快照是 1 秒窗口，停更超过 10 秒基本就是采集或消费断了，用颜色把这件事说出来
    const age = computed(() => {
      if (!snapshotTs.value) return null;
      const stamp = Date.parse(snapshotTs.value);
      return Number.isNaN(stamp) ? null : Math.max(0, Math.round((now.value - stamp) / 1000));
    });
    const freshText = computed(() => {
      if (age.value === null) return "等待数据";
      if (age.value <= 1) return "刚刚更新";
      // 超过一分钟换成分钟，否则停更久了会显示成 3000 多秒，读起来费劲
      if (age.value >= 60) return Math.floor(age.value / 60) + " 分钟前更新";
      return age.value + " 秒前更新";
    });
    const freshLevel = computed(() => {
      if (age.value === null) return "";
      if (age.value > 30) return "bad";
      return age.value > 10 ? "warn" : "";
    });

    // 线网图：站点表里只有线路和客流，没有坐标，按线路分行、站序等距排开就够了
    const flowMax = computed(() => Math.max(1, ...stations.value.map(s => s.passengers)));
    function groupByLine() {
      const groups = new Map();
      for (const s of stations.value) {
        const key = s.line_id || "";
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(s);
      }
      return groups;
    }
    const flowLines = computed(() => {
      const groups = groupByLine();
      const keys = [...groups.keys()].sort();
      const gap = keys.length > 1 ? 105 : 0;
      return keys.map((key, i) => ({
        key,
        short: key ? lineLabel(key) : "—",
        color: lineColorOf(key),
        y: keys.length > 1 ? 100 + i * gap : 145
      }));
    });
    const flowNodes = computed(() => {
      const groups = groupByLine();
      const keys = [...groups.keys()].sort();
      const gap = keys.length > 1 ? 105 : 0;
      const nodes = [];
      keys.forEach((key, li) => {
        const rows = groups.get(key);
        const step = rows.length > 1 ? 200 / (rows.length - 1) : 0;
        rows.forEach((s, si) => {
          nodes.push({
            id: s.station_id,
            passengers: s.passengers,
            color: lineColorOf(key),
            x: rows.length > 1 ? 62 + si * step : 150,
            y: keys.length > 1 ? 100 + li * gap : 145,
            // 半径按开方缩放：面积和客流成正比，小站才不会被大站压没
            r: 10 + 18 * Math.sqrt(s.passengers / flowMax.value)
          });
        });
      });
      return nodes;
    });

    // 迷你走势不用再开两个 ECharts 实例，拼 polyline 就够了
    const spark = computed(() => {
      const rows = health.value;
      const max = Math.max(1, ...rows.map(h => Math.max(h.late, h.invalid)));
      const step = 120 / Math.max(1, HEALTH_LEN - 1);
      const build = key => rows
        .map((row, i) => (i * step).toFixed(1) + "," + (28 - (row[key] / max) * 24).toFixed(1))
        .join(" ");
      return { late: build("late"), invalid: build("invalid") };
    });

    function barWidth(passengers) {
      // 留 4% 下限，否则小客流的名次条几乎看不见
      return Math.max(4, Math.round((passengers / rankMax.value) * 100)) + "%";
    }

    function openHelp(tab) {
      helpTab.value = tab || "metric";
      helpOpen.value = true;
    }

    function onKeydown(e) {
      if (e.key === "Escape") helpOpen.value = false;
    }

    function pickStation(id) {
      // 再点一次同一个站回到全部，省得另外放一个“取消”按钮
      selected.value = selected.value === id ? "" : id;
    }

    function drawBars() {
      if (!bars) return;
      bars.draw({
        rows: visibleStations.value, deltas, line: line.value, selected: selected.value,
        stationLabel, lineLabel, lineColor: lineColorOf
      });
    }

    function drawTrend() {
      if (!trend) return;
      trend.draw({ points: trends.value, threshold: threshold.value, selected: selected.value, stationLabel });
    }

    function trackHealth(late, invalid) {
      health.value.push({ late, invalid });
      if (health.value.length > HEALTH_LEN) health.value.shift();
    }

    function trackAlert(active) {
      if (active) {
        if (!alertStart.value) alertStart.value = now.value;
        alertPeak.value = Math.max(alertPeak.value, total.value);
      } else {
        alertStart.value = 0;
        alertPeak.value = 0;
      }
    }

    async function loadStationMeta() {
      try {
        const data = await getJSON("/api/stations");
        const items = Array.isArray(data.stations) ? data.stations : [];
        const lines = Array.isArray(data.lines) ? data.lines : [];
        // 采集表结构变更后服务端可能暂时把 lines/stations 当成两个伪站；只留带 line_id 的真站点
        stationMeta.value = {
          stations: items.filter(item => item && item.station_id && item.line_id),
          lines: lines.filter(item => item && item.line_id)
        };
        stationMetaLoaded.value = true;
      } catch (e) {
        stationMeta.value = { stations: [], lines: [] };
      }
    }

    async function loadSnapshot() {
      try {
        const data = await getJSON("/api/snapshot");
        online.value = true;
        total.value = data.total || 0;
        snapshotTs.value = data.ts || "";
        lateCount.value = data.late_count || 0;
        invalidCount.value = data.invalid_count || 0;
        stations.value = Array.isArray(data.stations) ? data.stations : [];
        if (Number.isFinite(data.threshold)) threshold.value = data.threshold;
        alerting.value = data.alerting === true;

        // 增量基于上一秒同一站的客流，首帧没有对比对象就不显示
        const next = new Map();
        for (const s of stations.value) {
          next.set(s.station_id, s.passengers);
          deltas[s.station_id] = prevPassengers.has(s.station_id)
            ? s.passengers - prevPassengers.get(s.station_id)
            : 0;
        }
        prevPassengers = next;
        trackHealth(lateCount.value, invalidCount.value);
        trackAlert(alerting.value);
        drawBars();
      } catch (e) {
        online.value = false;
      }
    }

    async function loadHistory() {
      try {
        const seriesUrl = selected.value
          ? "/api/series?station_id=" + encodeURIComponent(selected.value) + "&minutes=5"
          : "/api/series?minutes=5";
        const [series, top] = await Promise.all([getJSON(seriesUrl), getJSON("/api/rank?top=5")]);
        trends.value = series.points || [];
        rank.value = top.top || [];
        drawTrend();
      } catch (e) {
        // 历史接口挂掉不影响主看板，下一轮再试
      }
    }

    const query = createQueryPanel({
      stations, rank, stationMeta, stationMetaById, stationLabel, lineColorOf, view
    });

    // 表格里的时间要跟当前粒度对齐（秒/分钟/小时），所以包一层再交给模板
    const formatTs = ts => formatTsBase(ts, query.granularity.value);

    async function switchView(next) {
      if (view.value === next) return;
      view.value = next;
      query.syncHash();
      await nextTick();
      if (next === "live") {
        // 从查询页回来时容器刚从 display:none 恢复，必须重算尺寸
        bars && bars.resize();
        trend && trend.resize();
        return;
      }
      query.initChart();
      query.resize();
      if (!stationMetaLoaded.value) loadStationMeta();
      if (!query.queryLoaded.value) await query.loadQuery();
      if (!query.summaryLoaded.value) await query.loadSummary();
    }

    watch(line, drawBars);
    // 阈值可能随环境变量热改，拿到新值就重画，否则参考线要等下一轮历史刷新
    watch(threshold, drawTrend);
    // 下钻要同时换趋势数据和柱子的高亮，历史接口本身支持 station_id
    watch(selected, () => {
      drawBars();
      loadHistory();
    });

    onMounted(() => {
      query.readHash();   // 允许用 #query?minutes=30&stations=A01,A02 直接打开一组查询条件
      bars = createBarsChart(barEl.value);
      trend = createTrendChart(lineEl.value);
      // 用 ResizeObserver 盯容器，比只听 window.resize 更准（侧栏折叠、字体变化也能触发）
      observer = new ResizeObserver(() => {
        bars && bars.resize();
        trend && trend.resize();
        query.resize();
      });
      observer.observe(barEl.value);
      observer.observe(lineEl.value);
      if (query.queryEl.value) observer.observe(query.queryEl.value);

      loadSnapshot();
      loadHistory();
      loadStationMeta();
      if (view.value === "query") {
        nextTick(() => {
          query.initChart();
          query.loadQuery();
          query.loadSummary();
        });
      }

      timers.push(setInterval(loadSnapshot, SNAPSHOT_MS));
      timers.push(setInterval(loadHistory, HISTORY_MS));
      timers.push(setInterval(() => { now.value = Date.now(); }, 1000));
      // 查询页默认手动刷新；打开自动刷新才每 5 秒重查，避免看历史时时间轴自己跳
      timers.push(setInterval(() => {
        if (view.value === "query" && query.autoRefresh.value &&
            !query.queryLoading.value && !query.summaryLoading.value) {
          query.refreshAll();
        }
      }, HISTORY_MS));
      // Esc 关闭帮助抽屉，值班时手不离键盘也能退出
      document.addEventListener("keydown", onKeydown);
    });

    onUnmounted(() => {
      timers.forEach(clearInterval);
      document.removeEventListener("keydown", onKeydown);
      observer && observer.disconnect();
      bars && bars.dispose();
      trend && trend.dispose();
      query.dispose();
    });

    return {
      // 实时监看
      total, lateCount, invalidCount, rank, trends, online, line, lineOptions, threshold,
      alerting, clock, stationCount, visibleStations, snapshotLabel, freshText, freshLevel,
      barWidth, barEl, lineEl, selected, stationOptions, pickStation, flowLines, flowNodes,
      spark, health, alertSeconds, alertPeak, helpOpen, helpTab, openHelp, view, switchView,
      stationLabel, lineLabel, fmtNumber, formatTs,
      // 客流查询
      ...query
    };
  }
}).mount("#app");
