"""Heartbeat 辅函数单元测试:active_hours 时段判断。"""
from datetime import time as dt_time

from miniunicorn.cli._heartbeat import _is_within_active_hours, _parse_hhmm
from zoneinfo import ZoneInfo


class TestParseHhmm:
    def test_normal_time(self):
        assert _parse_hhmm("08:30") == dt_time(8, 30)

    def test_midnight(self):
        assert _parse_hhmm("00:00") == dt_time(0, 0)

    def test_2400(self):
        assert _parse_hhmm("24:00") == dt_time(23, 59, 59)

    def test_invalid_format(self):
        import pytest
        with pytest.raises(ValueError):
            _parse_hhmm("25:00")  # 超范围
        with pytest.raises(ValueError):
            _parse_hhmm("08:60")  # 分钟超范围
        with pytest.raises(ValueError):
            _parse_hhmm("not-a-time")  # 非法格式


class TestIsWithinActiveHours:
    tz = ZoneInfo("UTC")

    def test_none_active_hours_always_true(self):
        """active_hours 为 None 时不限制。"""
        assert _is_within_active_hours(None, self.tz) is True

    def test_empty_dict_always_true(self):
        assert _is_within_active_hours({}, self.tz) is True

    def test_normal_window_inside(self):
        """08:00-24:00 窗口,白天应在窗口内。"""
        # 用 mock 难以控制 datetime.now,这里只测逻辑:窗口内返回 True
        # 实际时间不固定,所以只测边界逻辑
        import unittest.mock as mock
        from datetime import datetime
        # 模拟中午 12:00
        fake_now = datetime(2026, 1, 1, 12, 0, tzinfo=self.tz)
        with mock.patch("miniunicorn.cli._heartbeat.datetime") as m:
            m.now.return_value = fake_now
            result = _is_within_active_hours({"start": "08:00", "end": "24:00"}, self.tz)
        assert result is True

    def test_normal_window_outside(self):
        """08:00-24:00 窗口,凌晨 03:00 应在窗口外。"""
        import unittest.mock as mock
        from datetime import datetime
        fake_now = datetime(2026, 1, 1, 3, 0, tzinfo=self.tz)
        with mock.patch("miniunicorn.cli._heartbeat.datetime") as m:
            m.now.return_value = fake_now
            result = _is_within_active_hours({"start": "08:00", "end": "24:00"}, self.tz)
        assert result is False

    def test_overnight_window_inside(self):
        """22:00-06:00 跨夜窗口,23:00 应在窗口内。"""
        import unittest.mock as mock
        from datetime import datetime
        fake_now = datetime(2026, 1, 1, 23, 0, tzinfo=self.tz)
        with mock.patch("miniunicorn.cli._heartbeat.datetime") as m:
            m.now.return_value = fake_now
            result = _is_within_active_hours({"start": "22:00", "end": "06:00"}, self.tz)
        assert result is True

    def test_overnight_window_morning_inside(self):
        """22:00-06:00 跨夜窗口,02:00 应在窗口内。"""
        import unittest.mock as mock
        from datetime import datetime
        fake_now = datetime(2026, 1, 1, 2, 0, tzinfo=self.tz)
        with mock.patch("miniunicorn.cli._heartbeat.datetime") as m:
            m.now.return_value = fake_now
            result = _is_within_active_hours({"start": "22:00", "end": "06:00"}, self.tz)
        assert result is True

    def test_overnight_window_noon_outside(self):
        """22:00-06:00 跨夜窗口,12:00 应在窗口外。"""
        import unittest.mock as mock
        from datetime import datetime
        fake_now = datetime(2026, 1, 1, 12, 0, tzinfo=self.tz)
        with mock.patch("miniunicorn.cli._heartbeat.datetime") as m:
            m.now.return_value = fake_now
            result = _is_within_active_hours({"start": "22:00", "end": "06:00"}, self.tz)
        assert result is False

    def test_invalid_active_hours_skips_check(self):
        """无效 active_hours 格式应跳过检查(返回 True)。"""
        assert _is_within_active_hours({"start": "abc", "end": "08:00"}, self.tz) is True
        assert _is_within_active_hours({"start": "", "end": ""}, self.tz) is True
