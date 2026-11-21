"""Rate-limit hardening tests for the Sigenergy coordinators."""

from __future__ import annotations

import asyncio
import types
import unittest
from datetime import UTC, datetime, timedelta

from test_coordinator import const, coordinator

RateLimitError = coordinator.SigenergyCloudRateLimitError
UpdateFailed = coordinator.UpdateFailed

DC_SN = "DC0077"
SETTINGS_MODULES = {
    const.MODULE_STATION_SETTINGS,
    const.MODULE_FORECAST,
    const.MODULE_EVDC,
}
# Endpoints that go through the slow cache (and its per-cycle budget).
SLOW_CALLS = {
    "refresh_station",
    "battery_power_limit",
    "solar_power_limit",
    "backup_reserve",
    "dc_charge_mode_soc_range",
    "dc_energy_totals",
    "dc_lifetime_totals",
    "dc_ocpp_status",
    "dc_session_records",
    "active_alarms",
}


class _RecordingSettingsClient:
    """Settings client that records calls and can 429 on the Nth call."""

    dc_sn = DC_SN
    dc_sns = (DC_SN,)

    def __init__(self, *, rate_limit_on_call: int | None = None, tag: str = "new"):
        self.calls: list[str] = []
        self.rate_limit_on_call = rate_limit_on_call
        self.tag = tag

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)

        async def call(*args, **kwargs):
            self.calls.append(name)
            if len(self.calls) == self.rate_limit_on_call:
                msg = "429 Too many requests"
                raise RateLimitError(msg)
            if name == "available_operational_modes":
                return {
                    "defaultWorkingModes": [{"label": "Sigen AI Mode", "value": "0"}],
                    "energyProfileItems": [],
                }
            if name == "instant_manual_control":
                return types.SimpleNamespace(enabled=False)
            if name == "refresh_station":
                return {"batteryCapacity": "24.0", "tag": self.tag}
            return {"endpoint": name, "tag": self.tag}

        return call


def _settings(client) -> coordinator.SigenSettingsCoordinator:
    return coordinator.SigenSettingsCoordinator(
        None, client, enabled_modules=SETTINGS_MODULES
    )


def _run(settings):
    return asyncio.run(settings._async_update_data())


def _slow_calls(client) -> list[str]:
    return [name for name in client.calls if name in SLOW_CALLS]


class _BackoffResetMixin:
    def setUp(self) -> None:
        coordinator._RATE_LIMIT_BACKOFF_UNTIL = 0.0
        coordinator._RATE_LIMIT_BACKOFF_UNTIL_UTC = None

    def tearDown(self) -> None:
        coordinator._RATE_LIMIT_BACKOFF_UNTIL = 0.0
        coordinator._RATE_LIMIT_BACKOFF_UNTIL_UTC = None


class BackoffUntilTest(_BackoffResetMixin, unittest.TestCase):
    """sensor.sigenergy_cloud_rate_limited_until reads rate_limit_backoff_until()."""

    def test_none_without_backoff(self) -> None:
        self.assertIsNone(coordinator.rate_limit_backoff_until())

    def test_wall_clock_end_while_backing_off(self) -> None:
        before = datetime.now(UTC)
        coordinator.activate_rate_limit_backoff("test", RateLimitError("429"))
        until = coordinator.rate_limit_backoff_until()
        self.assertIsNotNone(until)
        self.assertEqual(until.tzinfo, UTC)
        expected = before + coordinator._RATE_LIMIT_BACKOFF_INTERVAL
        self.assertLess(abs((until - expected).total_seconds()), 5)
        # Stable between polls, so the sensor does not write a new state each time.
        self.assertEqual(coordinator.rate_limit_backoff_until(), until)

    def test_a_later_429_extends_the_end(self) -> None:
        coordinator.activate_rate_limit_backoff("first", RateLimitError("429"))
        # As if the first 429 came a minute ago.
        coordinator._RATE_LIMIT_BACKOFF_UNTIL -= 60
        coordinator._RATE_LIMIT_BACKOFF_UNTIL_UTC -= timedelta(seconds=60)
        until = coordinator.rate_limit_backoff_until()
        coordinator.activate_rate_limit_backoff("second", RateLimitError("429"))
        self.assertGreater(coordinator.rate_limit_backoff_until(), until)

    def test_none_once_the_backoff_has_ended(self) -> None:
        coordinator.activate_rate_limit_backoff("test", RateLimitError("429"))
        coordinator._RATE_LIMIT_BACKOFF_UNTIL = 0.0
        self.assertIsNone(coordinator.rate_limit_backoff_until())


class SettingsRateLimitTest(_BackoffResetMixin, unittest.TestCase):
    def _warm_coordinator(self, client):
        """Return a coordinator with complete last-known data, as after warm cycles."""
        settings = _settings(_RecordingSettingsClient(tag="old"))
        for _ in range(5):
            settings.data = _run(settings)
        settings.client = client
        return settings

    def test_settings_rate_limit_keeps_fetched_and_last_known(self) -> None:
        client = _RecordingSettingsClient(rate_limit_on_call=4)
        settings = self._warm_coordinator(client)
        # Make every slow endpoint due, so they would be fetched without the 429.
        settings._slow_cache_updated_at.clear()

        data = _run(settings)

        # Calls 1-3 succeed, call 4 is rate-limited, then nothing else is sent.
        self.assertEqual(
            client.calls,
            [
                "refresh_station",
                "instant_manual_control",
                "current_operational_mode",
                "battery_levels",
            ],
        )
        self.assertEqual(data["station_home"]["tag"], "new")
        self.assertEqual(data["operational_mode"]["tag"], "new")
        # Everything after the 429 keeps its last-known value.
        self.assertEqual(data["battery_level"]["tag"], "old")
        self.assertEqual(data["export_limit"]["tag"], "old")
        self.assertEqual(data["peak_shaving"]["tag"], "old")
        self.assertEqual(data["prediction_data"]["tag"], "old")
        self.assertEqual(data["dc_charge_mode"]["tag"], "old")
        self.assertEqual(data["dc_chargers"][DC_SN]["session_records"]["tag"], "old")
        self.assertGreater(coordinator.rate_limit_backoff_remaining(), 0)

    def test_rate_limited_slow_endpoint_stays_due(self) -> None:
        client = _RecordingSettingsClient(rate_limit_on_call=2)
        settings = _settings(client)

        _run(settings)

        # station_home was rate-limited: no timestamp, so it is fetched next time.
        self.assertNotIn("station_home", settings._slow_cache_updated_at)

    def test_settings_first_refresh_during_backoff_raises_update_failed(self) -> None:
        coordinator.activate_rate_limit_backoff("test", RateLimitError("429"))
        client = _RecordingSettingsClient()
        settings = _settings(client)

        with self.assertRaises(UpdateFailed):
            _run(settings)
        self.assertEqual(client.calls, [])

    def test_settings_backoff_with_data_returns_last_known(self) -> None:
        settings = _settings(_RecordingSettingsClient())
        settings.data = {"operational_mode": "x"}
        coordinator.activate_rate_limit_backoff("test", RateLimitError("429"))

        self.assertEqual(_run(settings), {"operational_mode": "x"})

    def test_status_first_refresh_during_backoff_raises_update_failed(self) -> None:
        coordinator.activate_rate_limit_backoff("test", RateLimitError("429"))
        status = coordinator.SigenStatusCoordinator(
            None, _RecordingSettingsClient(), enabled_modules=SETTINGS_MODULES
        )

        with self.assertRaises(UpdateFailed):
            asyncio.run(status._async_update_data())

    def test_settings_reload_seeds_from_last_good(self) -> None:
        old = self._warm_coordinator(_RecordingSettingsClient(tag="old"))
        state = old.export_state()

        client = _RecordingSettingsClient()
        settings = _settings(client)
        settings.restore_state(state)
        self.assertEqual(settings.data["battery_level"]["tag"], "old")

        data = _run(settings)

        self.assertLessEqual(len(client.calls), 12)
        self.assertEqual(_slow_calls(client), [])
        self.assertNotIn("available_operational_modes", client.calls)
        self.assertNotIn("prediction_data", client.calls)
        self.assertEqual(data["operational_mode"]["tag"], "new")
        self.assertEqual(data["station_home"]["tag"], "old")

    def test_cold_start_slow_refresh_budget(self) -> None:
        client = _RecordingSettingsClient()
        settings = _settings(client)

        for cycle in range(1, 5):
            client.calls.clear()
            settings.data = _run(settings)
            self.assertLessEqual(len(_slow_calls(client)), 3, f"cycle {cycle}")

        self.assertEqual(
            set(settings._slow_cache_updated_at),
            {
                "station_home",
                "battery_power_limit",
                "solar_power_limit",
                "backup_reserve",
                "dc_charge_soc_range",
                f"dc_charger_energy {DC_SN}",
                f"dc_charger_total {DC_SN}",
                f"ocpp_status {DC_SN}",
                f"dc_charger_session_records {DC_SN}",
                "active_alarms",
            },
        )
        # Endpoints that drive control are fetched from the very first cycle.
        client.calls.clear()
        cold = _settings(client)
        data = _run(cold)
        for name in ("battery_levels", "grid_export_limit", "prediction_data"):
            self.assertIn(name, client.calls)
        self.assertIsNotNone(data["prediction_data"])

    def test_dc_history_endpoints_poll_every_6h(self) -> None:
        client = _RecordingSettingsClient()
        settings = _settings(client)
        for _ in range(4):
            settings.data = _run(settings)

        # Two hours later: hourly endpoints are due, charger history is not.
        two_hours_ago = datetime.now(UTC) - timedelta(hours=2)
        for label in settings._slow_cache_updated_at:
            settings._slow_cache_updated_at[label] = two_hours_ago
        client.calls.clear()
        for _ in range(3):
            settings.data = _run(settings)
        self.assertIn("dc_ocpp_status", client.calls)
        self.assertNotIn("dc_session_records", client.calls)
        self.assertNotIn("dc_energy_totals", client.calls)

        # Six hours later they are refreshed.
        seven_hours_ago = datetime.now(UTC) - timedelta(hours=7)
        for label in settings._slow_cache_updated_at:
            settings._slow_cache_updated_at[label] = seven_hours_ago
        client.calls.clear()
        for _ in range(4):
            settings.data = _run(settings)
        self.assertIn("dc_session_records", client.calls)
        self.assertIn("dc_energy_totals", client.calls)


class _StatusClient:
    dc_sn = DC_SN
    dc_sns = (DC_SN,)
    TOPO_DEVICE_TYPE_DC_CHARGER = 5

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)

        async def call(*args, **kwargs):
            self.calls.append(name)
            if name == "device_topology":
                return {"stationStatus": 1}
            return {}

        return call

    def iter_topology_nodes(self, topology):
        return []


class TopologyIntervalTest(_BackoffResetMixin, unittest.TestCase):
    def test_topology_idle_interval_10min_but_forced_after_aio_power(self) -> None:
        self.assertEqual(coordinator._TOPOLOGY_IDLE_INTERVAL, timedelta(minutes=10))
        client = _StatusClient()
        status = coordinator.SigenStatusCoordinator(
            None, client, enabled_modules=SETTINGS_MODULES
        )

        status.data = asyncio.run(status._async_update_data())
        self.assertEqual(client.calls.count("device_topology"), 1)

        # Two minutes later (the status poll interval) topology is not re-polled.
        status._topology_updated_at -= 120
        status.data = asyncio.run(status._async_update_data())
        self.assertEqual(client.calls.count("device_topology"), 1)

        # Eleven minutes after the first poll it is.
        status._topology_updated_at -= 540
        status.data = asyncio.run(status._async_update_data())
        self.assertEqual(client.calls.count("device_topology"), 2)

        # An AIO power change forces it immediately.
        status.request_force_device_status_poll()
        status.data = asyncio.run(status._async_update_data())
        self.assertEqual(client.calls.count("device_topology"), 3)


if __name__ == "__main__":
    unittest.main()


class _FlowClient(_RecordingSettingsClient):
    dc_sn = None
    dc_sns = ()

    def __init__(self, flow):
        super().__init__()
        self.flow = flow

    async def energy_flow(self):
        if isinstance(self.flow, Exception):
            raise self.flow
        return self.flow


class OnGridTest(_BackoffResetMixin, unittest.TestCase):
    """binary_sensor.sigenergy_cloud_on_grid reads only this poll's onGrid."""

    def _status(self, flow, last=None):
        status = coordinator.SigenStatusCoordinator(
            None, _FlowClient(flow), enabled_modules=SETTINGS_MODULES
        )
        status.data = last
        return asyncio.run(status._async_update_data())

    def test_on_grid_and_island_come_from_energy_flow(self) -> None:
        on = self._status({"onGrid": True, "onOffGridStatus": 0})
        self.assertIs(on["on_grid"], True)
        self.assertEqual(on["on_off_grid_status"], 0)
        self.assertIs(self._status({"onGrid": False})["on_grid"], False)

    def test_failed_energy_flow_does_not_carry_the_last_on_grid(self) -> None:
        last = {"on_grid": True, "grid_power": 1.0}
        data = self._status(RuntimeError("boom"), last=last)
        self.assertIsNone(data["on_grid"])
        self.assertEqual(data["grid_power"], 1.0)
