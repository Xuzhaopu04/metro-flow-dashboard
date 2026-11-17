# Contract v1.3: metric_window 增加进站/出站拆分列。
# 只加不删；passengers 保留旧语义（乘降量，in + out），
# 旧数据两列默认为 0，不崩。

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='metricwindow',
            name='in_passengers',
            field=models.IntegerField(default=0, help_text='窗口内进站量，按 event_type=in 统计'),
        ),
        migrations.AddField(
            model_name='metricwindow',
            name='out_passengers',
            field=models.IntegerField(default=0, help_text='窗口内出站量，按 event_type=out 统计'),
        ),
    ]
