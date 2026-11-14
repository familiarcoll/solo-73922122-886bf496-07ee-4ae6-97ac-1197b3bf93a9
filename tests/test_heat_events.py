"""热区事件 API 测试：连通语义、时间轴/经度/规模校验、参数校验。"""

import json

import numpy as np
import pandas as pd
import pytest
import xarray as xr

LATS = [0.0, 10.0, 20.0, 30.0]
LONS = [0.0, 10.0, 20.0, 30.0, 40.0]  # 非整圈


def _heat_nc(tmp_path, name, values, lats=None, lons=None, times=None, var="tas"):
    ntime = np.asarray(values).shape[0]
    if lats is None:
        lats = LATS
    if lons is None:
        lons = LONS
    if times is None:
        times = pd.date_range("2026-01-01", periods=ntime, freq="D")
    da = xr.DataArray(
        np.asarray(values, dtype="float32"),
        dims=("time", "lat", "lon"),
        coords={"time": times, "lat": lats, "lon": lons},
        name=var,
    )
    path = str(tmp_path / name)
    da.to_dataset().to_netcdf(path, engine="netcdf4")
    return path


def _cold(ntime, nlat=None, nlon=None, fill=10.0):
    return np.full(
        (ntime, nlat or len(LATS), nlon or len(LONS)), fill, dtype="float32"
    )


def _post_heat(client, nc_path, variable="tas", threshold=30.0, min_duration_days=2):
    c, _ = client
    with open(nc_path, "rb") as fh:
        return c.post(
            "/api/heat-events",
            files={"file": ("grid.nc", fh, "application/x-netcdf")},
            data={
                "variable": variable,
                "threshold": str(threshold),
                "min_duration_days": str(min_duration_days),
            },
        )


def test_single_persistent_event(client, tmp_path):
    values = _cold(4)
    values[:, 1, 2] = 35.0  # 同一格点连续 4 天达标
    path = _heat_nc(tmp_path, "a.nc", values)

    r = _post_heat(client, path)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["variable"] == "tas"
    assert body["threshold"] == 30.0
    assert body["min_duration_days"] == 2
    assert body["grid"]["lon_full_circle"] is False
    assert body["event_count"] == 1

    ev = body["events"][0]
    assert ev["event_id"] == 1
    assert ev["start_date"] == "2026-01-01"
    assert ev["end_date"] == "2026-01-04"
    assert ev["duration_days"] == 4
    assert ev["total_cells"] == 4
    assert [d["date"] for d in ev["days"]] == [
        "2026-01-01",
        "2026-01-02",
        "2026-01-03",
        "2026-01-04",
    ]
    for day in ev["days"]:
        assert day["point_count"] == 1
        assert day["points"] == [{"lat": 10.0, "lon": 20.0}]


def test_threshold_is_inclusive(client, tmp_path):
    values = _cold(3)
    values[:, 0, 0] = 30.0  # 恰好等于阈值也算达标
    path = _heat_nc(tmp_path, "inclusive.nc", values)
    body = _post_heat(client, path, threshold=30.0).json()
    assert body["event_count"] == 1
    assert body["events"][0]["duration_days"] == 3


def test_min_duration_filters_short_events(client, tmp_path):
    values = _cold(4)
    values[0, 0, 0] = 35.0  # 单日事件
    values[2, 3, 4] = 35.0  # 两日事件
    values[3, 3, 4] = 35.0
    path = _heat_nc(tmp_path, "filter.nc", values)

    body = _post_heat(client, path, min_duration_days=2).json()
    assert body["event_count"] == 1
    ev = body["events"][0]
    assert ev["start_date"] == "2026-01-03"
    assert ev["end_date"] == "2026-01-04"
    assert ev["duration_days"] == 2

    body = _post_heat(client, path, min_duration_days=3).json()
    assert body["event_count"] == 0
    assert body["events"] == []


def test_fork_and_remerge_is_single_event(client, tmp_path):
    # 第 0 天一条带 → 第 1 天分成两段 → 第 2 天重新汇合：仍属同一事件
    values = _cold(3)
    for j in (1, 2, 3):
        values[0, 1, j] = 35.0
    values[1, 1, 1] = 35.0
    values[1, 1, 3] = 35.0
    for j in (1, 2, 3):
        values[2, 1, j] = 35.0
    path = _heat_nc(tmp_path, "fork.nc", values)

    body = _post_heat(client, path).json()
    assert body["event_count"] == 1
    ev = body["events"][0]
    assert ev["duration_days"] == 3
    assert ev["start_date"] == "2026-01-01"
    assert ev["end_date"] == "2026-01-03"
    assert ev["total_cells"] == 8
    # 分叉当天是两段分离的格点，但都归入同一事件
    assert ev["days"][1]["point_count"] == 2


def test_longitude_wraparound_full_circle(client, tmp_path):
    lons = [0.0, 90.0, 180.0, 270.0]  # 4×90=360 → 整圈，首末列相邻
    values = _cold(3, nlat=4, nlon=4)
    values[:, 0, 0] = 35.0
    values[:, 0, 3] = 35.0  # 仅通过整圈环绕与首列相邻
    path = _heat_nc(tmp_path, "wrap.nc", values, lats=LATS, lons=lons)

    body = _post_heat(client, path).json()
    assert body["grid"]["lon_full_circle"] is True
    assert body["event_count"] == 1
    day0 = body["events"][0]["days"][0]
    assert day0["point_count"] == 2
    assert {p["lon"] for p in day0["points"]} == {0.0, 270.0}


def test_no_wraparound_when_not_full_circle(client, tmp_path):
    lons = [0.0, 90.0, 180.0]  # 3×90=270 ≠ 360 → 不环绕
    values = _cold(3, nlat=4, nlon=3)
    values[:, 0, 0] = 35.0
    values[:, 0, 2] = 35.0
    path = _heat_nc(tmp_path, "nowrap.nc", values, lats=LATS, lons=lons)

    body = _post_heat(client, path).json()
    assert body["grid"]["lon_full_circle"] is False
    assert body["event_count"] == 2  # 首末列不相邻，各自成事件


@pytest.mark.parametrize(
    "lons",
    [
        [0.0, 90.0, 180.0, 270.0, 360.0],  # 0 与 360 同一条物理经线
        [-180.0, -90.0, 0.0, 90.0, 180.0],  # -180 与 180 同一条物理经线
    ],
)
def test_duplicate_physical_meridian_rejected(client, tmp_path, lons):
    values = _cold(2, nlat=4, nlon=len(lons))
    path = _heat_nc(tmp_path, "dup.nc", values, lats=LATS, lons=lons)
    r = _post_heat(client, path)
    assert r.status_code == 400
    assert "重复物理经线" in r.json()["detail"]


def test_missing_day_breaks_event(client, tmp_path):
    times = pd.DatetimeIndex(
        ["2026-01-01", "2026-01-02", "2026-01-04", "2026-01-05"]
    )  # 缺 2026-01-03
    values = _cold(4)
    values[:, 1, 1] = 35.0  # 同一格点四条记录都达标，但中间缺日
    path = _heat_nc(tmp_path, "gap.nc", values, times=times)

    body = _post_heat(client, path).json()
    assert body["event_count"] == 2
    assert (body["events"][0]["start_date"], body["events"][0]["end_date"]) == (
        "2026-01-01",
        "2026-01-02",
    )
    assert (body["events"][1]["start_date"], body["events"][1]["end_date"]) == (
        "2026-01-04",
        "2026-01-05",
    )


def test_nan_breaks_connectivity(client, tmp_path):
    values = _cold(5)
    values[[0, 1, 3, 4], 2, 2] = 35.0
    values[2, 2, 2] = np.nan  # 中间一天缺测 → 前后不连通
    path = _heat_nc(tmp_path, "nan.nc", values)

    body = _post_heat(client, path).json()
    assert body["event_count"] == 2
    assert body["events"][0]["end_date"] == "2026-01-02"
    assert body["events"][1]["start_date"] == "2026-01-04"


def test_temporal_link_requires_same_cell(client, tmp_path):
    # 热斑每天向东移一格：相邻日期没有同一格点同时达标 → 无跨日事件
    values = _cold(3)
    for t in range(3):
        values[t, 0, t] = 35.0
    path = _heat_nc(tmp_path, "move.nc", values)
    body = _post_heat(client, path).json()
    assert body["event_count"] == 0


def test_non_midnight_time_rejected(client, tmp_path):
    times = pd.date_range("2026-01-01", periods=3, freq="D") + pd.Timedelta(hours=6)
    path = _heat_nc(tmp_path, "noon.nc", _cold(3), times=times)
    r = _post_heat(client, path)
    assert r.status_code == 400
    assert "零点" in r.json()["detail"]


def test_duplicate_time_rejected(client, tmp_path):
    times = pd.DatetimeIndex(["2026-01-01", "2026-01-01", "2026-01-02"])
    path = _heat_nc(tmp_path, "dupt.nc", _cold(3), times=times)
    r = _post_heat(client, path)
    assert r.status_code == 400
    assert "严格递增" in r.json()["detail"]


def test_grid_size_limit(client, tmp_path):
    from app import main as main_mod

    object.__setattr__(main_mod.settings, "max_heat_cells", 10)
    path = _heat_nc(tmp_path, "big.nc", _cold(4))  # 4×4×5=80 > 10
    r = _post_heat(client, path)
    assert r.status_code == 400
    assert "超过上限" in r.json()["detail"]


@pytest.mark.parametrize("threshold", ["nan", "inf", "-inf"])
def test_threshold_must_be_finite(client, tmp_path, threshold):
    path = _heat_nc(tmp_path, "fin.nc", _cold(2))
    r = _post_heat(client, path, threshold=threshold)
    assert r.status_code == 400
    assert "有限" in r.json()["detail"]


def test_min_duration_at_least_two(client, tmp_path):
    path = _heat_nc(tmp_path, "mind.nc", _cold(2))
    r = _post_heat(client, path, min_duration_days=1)
    assert r.status_code == 400
    assert "至少为 2" in r.json()["detail"]


def test_descending_lat_and_180_lon_grid(client, tmp_path):
    lats = [30.0, 20.0, 10.0, 0.0]  # 降序
    lons = [-180.0, -90.0, 0.0]  # 负经度、非整圈
    values = _cold(3, nlat=4, nlon=3)
    values[:, 1, 1] = 35.0
    path = _heat_nc(tmp_path, "desc.nc", values, lats=lats, lons=lons)

    body = _post_heat(client, path).json()
    assert body["event_count"] == 1
    pt = body["events"][0]["days"][0]["points"][0]
    assert pt == {"lat": 20.0, "lon": -90.0}  # 输出文件原始坐标


def test_no_hot_cells_returns_empty(client, tmp_path):
    path = _heat_nc(tmp_path, "cold.nc", _cold(3))
    body = _post_heat(client, path).json()
    assert body["event_count"] == 0
    assert body["events"] == []


def test_single_day_file_returns_empty(client, tmp_path):
    values = _cold(1)
    values[0, 0, 0] = 40.0  # 单日达标也无法满足最短持续 2 天
    path = _heat_nc(tmp_path, "oneday.nc", values)
    r = _post_heat(client, path)
    assert r.status_code == 200
    assert r.json()["event_count"] == 0


def test_unknown_variable_rejected(client, tmp_path):
    path = _heat_nc(tmp_path, "var.nc", _cold(2))
    r = _post_heat(client, path, variable="missing")
    assert r.status_code == 400
    assert "不存在" in r.json()["detail"]


def test_non_gregorian_rejected(client, paths):
    r = _post_heat(client, paths["noleap"])
    assert r.status_code == 400
    assert "公历" in r.json()["detail"]


def test_oversized_upload_rejected(client, tmp_path):
    from app import main as main_mod

    object.__setattr__(main_mod.settings, "max_upload_bytes", 128)
    path = _heat_nc(tmp_path, "bigfile.nc", _cold(2))
    r = _post_heat(client, path)
    assert r.status_code == 400
    assert "上限" in r.json()["detail"]


def test_heat_events_in_openapi_docs(client):
    c, _ = client
    schema = c.get("/openapi.json").json()
    assert "/api/heat-events" in schema["paths"]
    # 原站点提取接口保持不变
    assert "/api/tasks" in schema["paths"]


def test_events_sorted_and_ids_sequential(client, tmp_path):
    values = _cold(6)
    values[4:, 0, 0] = 35.0  # 较晚的 2 日事件
    values[0:3, 3, 4] = 35.0  # 较早的 3 日事件
    path = _heat_nc(tmp_path, "sorted.nc", values)

    body = _post_heat(client, path).json()
    assert body["event_count"] == 2
    first, second = body["events"]
    assert first["event_id"] == 1
    assert second["event_id"] == 2
    assert first["start_date"] == "2026-01-01"
    assert first["duration_days"] == 3
    assert second["start_date"] == "2026-01-05"


def test_station_extraction_still_works(client, paths):
    """原站点提取功能不受新接口影响。"""
    c, _ = client
    with open(paths["grid"], "rb") as fh:
        r = c.post(
            "/api/tasks",
            files={"file": ("grid.nc", fh, "application/x-netcdf")},
            data={
                "variable": "tas",
                "stations_json": json.dumps(
                    {"stations": [{"id": "S1", "lat": 10.0, "lon": 20.0}]}
                ),
            },
        )
    assert r.status_code == 201
    task_id = r.json()["task_id"]
    import time

    deadline = time.time() + 30
    while time.time() < deadline:
        state = c.get(f"/api/tasks/{task_id}").json()["state"]
        if state in {"completed", "failed", "cancelled", "timeout"}:
            break
        time.sleep(0.1)
    assert state == "completed"
