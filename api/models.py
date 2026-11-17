from django.db import models


class MetricWindow(models.Model):
    """每秒每站点一行指标，用于历史查询与离线对账。

    字段口径见 docs/02-设计/接口契约.md 第 3 节，
    指标含义见 docs/02-设计/指标口径.md 第 2 节：
    passengers 是该窗口内的新增客流，不是累计值。
    """

    ts = models.DateTimeField(db_index=True, help_text="窗口起点")
    station_id = models.CharField(max_length=16, help_text="站点编码")
    passengers = models.IntegerField(default=0, help_text="窗口内新增客流")
    # 契约 v1.3 新增两列。默认 0 而不是允许 NULL：迁移先落地、旧消费者继续只写
    # passengers 时，这两列读出来是 0，接口不会因为 None 参与求和而炸。
    in_passengers = models.IntegerField(default=0, help_text="窗口内进站量")
    out_passengers = models.IntegerField(default=0, help_text="窗口内出站量")
    late_count = models.IntegerField(default=0, help_text="该站点在本窗口内的迟到事件数")

    class Meta:
        db_table = "metric_window"
        indexes = [models.Index(fields=["station_id", "ts"], name="idx_station_ts")]
        # 同一窗口同一站点只允许一行，消费者重试或重放时靠它做幂等写入
        constraints = [
            models.UniqueConstraint(fields=["ts", "station_id"], name="uniq_ts_station")
        ]
        ordering = ["-ts"]

    def __str__(self):
        return f"{self.ts} {self.station_id} {self.passengers}"


class MetricQuality(models.Model):
    """每窗口一行数据质量计数，供 /api/summary/today 的 quality 使用。

    为什么不并进 MetricWindow：非法事件可能连 station_id 都没有，
    摊到每站会把同一个非法事件重复计 N 次。窗口级计数只在窗口粒度存一份，
    才有唯一正确的值。

    ts 是窗口起点，与 MetricWindow.ts 同源，唯一约束保证一个窗口只有一行。
    """

    ts = models.DateTimeField(unique=True, help_text="窗口起点")
    late_count = models.IntegerField(default=0, help_text="窗口内迟到事件数")
    invalid_count = models.IntegerField(default=0, help_text="窗口内非法事件数")

    class Meta:
        db_table = "metric_quality"
        # 汇总按 ts 区间取，唯一索引已覆盖等值查；这里再给区间扫一个明确顺序
        ordering = ["-ts"]

    def __str__(self):
        return f"{self.ts} late={self.late_count} invalid={self.invalid_count}"
