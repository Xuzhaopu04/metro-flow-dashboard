from django.db import models


class MetricWindow(models.Model):
    """每秒每站点一行指标，用于历史查询与离线对账。

    字段口径见 docs/api-contract.md 第 3 节：
    passengers 是该窗口内的新增客流，不是累计值。
    """

    ts = models.DateTimeField(db_index=True, help_text="窗口结束时间")
    station_id = models.CharField(max_length=16, help_text="站点编码")
    passengers = models.IntegerField(default=0, help_text="窗口内新增客流")
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
