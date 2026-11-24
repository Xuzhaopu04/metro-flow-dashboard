// ECharts 封装：每个函数只管一个实例，画什么由调用方把数据递进来。
// 图表里不加渐变、不加阴影，颜色只用来区分类别和表达涨跌。

import { AXIS, SPLIT_LINE, PANEL_TOOLTIP, QUERY_COLORS } from "./shared.js";

// 涨跌标签用散点单独画：柱子的 label 只有一种颜色，涨和跌区分不开。
// 红涨绿跌，跟国内看盘习惯一致。
function deltaSeries(up, down) {
  const make = (data, color, fmt) => ({
    type: "scatter", silent: true, symbolSize: 0, tooltip: { show: false }, data,
    label: { show: true, position: "top", offset: [0, -20], fontSize: 12, color, formatter: fmt }
  });
  return [make(up, "#e5484d", p => "+" + p.value[2]),
          make(down, "#12a150", p => String(p.value[2]))];
}

export function createBarsChart(el) {
  const chart = echarts.init(el);

  function draw({ rows, deltas, line, selected, stationLabel, lineLabel, lineColor }) {
    const up = [];
    const down = [];
    // 全部线路时按线路着色，单线路时统一用主色，避免柱子和图例对不上
    const data = rows.map((s, i) => {
      const dv = deltas[s.station_id] || 0;
      if (dv > 0) up.push([i, s.passengers, dv]);
      if (dv < 0) down.push([i, s.passengers, dv]);
      const picked = selected && selected === s.station_id;
      return {
        value: s.passengers,
        itemStyle: {
          color: line === "all" ? lineColor(s.line_id) : "#2f6bff",
          // 选中的站用描边对上右侧排行，不换颜色
          borderColor: picked ? "#14161a" : "transparent",
          borderWidth: picked ? 2 : 0
        }
      };
    });

    chart.setOption({
      // 每秒换一次数据，动画只会让柱子追不上数字
      animation: false,
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "shadow", shadowStyle: { color: "rgba(47,107,255,.05)" } },
        ...PANEL_TOOLTIP,
        formatter: items => {
          const row = rows[items[0].dataIndex];
          const dv = deltas[row.station_id] || 0;
          const dvText = dv === 0 ? "持平" : (dv > 0 ? "+" : "") + dv;
          return stationLabel(row.station_id) + " · " + lineLabel(row.line_id) +
                 "<br/>客流 <b>" + row.passengers + "</b> 人次/秒" +
                 "<br/>较上一秒 <b>" + dvText + "</b>";
        }
      },
      grid: { left: 48, right: 22, top: 44, bottom: 30 },
      xAxis: { type: "category", data: rows.map(s => stationLabel(s.station_id)), ...AXIS },
      yAxis: {
        type: "value", name: "人次/秒", minInterval: 1,
        nameTextStyle: { color: "#9aa0a8", fontSize: 12 }, splitLine: SPLIT_LINE, ...AXIS
      },
      series: [{
        type: "bar",
        barMaxWidth: 46,
        data,
        label: { show: true, position: "top", color: "#9aa0a8", fontSize: 12 }
      }, ...deltaSeries(up, down)]
    });
  }

  return { draw, resize: () => chart.resize(), dispose: () => chart.dispose() };
}

export function createTrendChart(el) {
  const chart = echarts.init(el);

  function series(points, threshold) {
    const line = {
      type: "line", smooth: true, showSymbol: false,
      data: points.map(p => p.passengers),
      lineStyle: { color: "#2f6bff", width: 2 }, itemStyle: { color: "#2f6bff" }
    };
    // 阈值线只在有阈值时画，否则会在 0 处多出一条误导性的虚线
    if (threshold > 0) {
      line.markLine = {
        silent: true, symbol: "none", label: { show: false },
        lineStyle: { color: "rgba(229,72,77,.45)", type: "dashed" },
        data: [{ yAxis: threshold }]
      };
    }
    return line;
  }

  function draw({ points, threshold, selected, stationLabel }) {
    chart.setOption({
      animation: false,
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "line", lineStyle: { color: "rgba(47,107,255,.30)", type: "dashed" } },
        ...PANEL_TOOLTIP,
        formatter: items => {
          const p = points[items[0].dataIndex];
          const who = selected ? stationLabel(selected) : "全部站点";
          return p.ts.slice(11, 19) + " · " + who +
                 "<br/>客流 <b>" + p.passengers + "</b> 人次/秒";
        }
      },
      grid: { left: 48, right: 62, top: 30, bottom: 26 },
      xAxis: {
        // 左右各留 1%：最后一个时间标签居中在端点，不留白会被容器边缘裁掉半个
        type: "category", boundaryGap: ["1%", "1%"], data: points.map(p => p.ts.slice(11, 19)), ...AXIS,
        // 5 分钟 1 秒一个点最多 300 个，标签交给 ECharts 自动抽稀
        axisLabel: { ...AXIS.axisLabel, hideOverlap: true }
      },
      yAxis: {
        type: "value", name: "人次/秒", minInterval: 1,
        nameTextStyle: { color: "#9aa0a8", fontSize: 12 }, splitLine: SPLIT_LINE, ...AXIS
      },
      series: [series(points, threshold)]
    });
  }

  return { draw, resize: () => chart.resize(), dispose: () => chart.dispose() };
}

export function createQueryChart(el) {
  const chart = echarts.init(el);

  function draw({ seriesRows, pointValue, stationLabel, stationMetaById, lineColorOf, formatTs }) {
    const times = [...new Set(seriesRows.flatMap(row => (row.points || []).map(p => p.ts)))].sort();
    const series = seriesRows.map((row, index) => {
      const byTs = new Map((row.points || []).map(p => [p.ts, pointValue(p)]));
      const meta = row.station_id ? stationMetaById.get(row.station_id) : null;
      // 单站优先用线路色；多站时同一条线可能有多个站，改用调色板区分
      const color = seriesRows.length === 1
        ? (meta && meta.line_id ? lineColorOf(meta.line_id) : QUERY_COLORS[0])
        : QUERY_COLORS[index % QUERY_COLORS.length];
      return {
        name: row.station_id ? stationLabel(row.station_id) : "全部站点",
        type: "line", smooth: true, showSymbol: false, connectNulls: false,
        data: times.map(ts => (byTs.has(ts) ? byTs.get(ts) : null)),
        lineStyle: { width: 2, color }, itemStyle: { color }
      };
    });

    chart.setOption({
      animation: false,
      color: QUERY_COLORS,
      legend: {
        show: seriesRows.length > 1, top: 0, right: 8,
        itemWidth: 10, itemHeight: 10, textStyle: { color: "#6b7280", fontSize: 12 }
      },
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "line", lineStyle: { color: "rgba(47,107,255,.30)", type: "dashed" } },
        ...PANEL_TOOLTIP,
        formatter: items => {
          const time = items[0] && items[0].axisValue ? items[0].axisValue : "";
          return time + items.map(item =>
            "<br/>" + item.marker + item.seriesName + "：<b>" +
            (item.value == null ? "—" : item.value) + "</b> 人次/秒"
          ).join("");
        }
      },
      grid: { left: 48, right: 24, top: seriesRows.length > 1 ? 34 : 24, bottom: 44 },
      xAxis: {
        type: "category", boundaryGap: ["1%", "1%"], data: times.map(ts => formatTs(ts)), ...AXIS,
        axisLabel: { ...AXIS.axisLabel, hideOverlap: true }
      },
      yAxis: {
        type: "value", name: "人次/秒", minInterval: 1,
        nameTextStyle: { color: "#9aa0a8", fontSize: 12 }, splitLine: SPLIT_LINE, ...AXIS
      },
      dataZoom: [
        { type: "inside" },
        { type: "slider", height: 14, bottom: 8, borderColor: "transparent",
          backgroundColor: "#f1f2ee", fillerColor: "rgba(47,107,255,.10)",
          handleStyle: { color: "#2f6bff" }, textStyle: { color: "#9aa0a8", fontSize: 12 } }
      ],
      series
    }, true);
  }

  return { draw, resize: () => chart.resize(), dispose: () => chart.dispose(), clear: () => chart.clear() };
}
