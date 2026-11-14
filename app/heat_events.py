"""跨日延续高温区域（热区）事件检测。

语义约定：
- 每天达到阈值（``value >= threshold``）的格点按**四邻接**（纬向/经向）连通；
  经度覆盖整圈（``nlon * dlon == 360°``）时首尾两列也算相邻。
- 相邻日期之间**只通过同一高温格点**（同 lat/lon 且两日均达标）连接。
- 时间轴缺日（相邻记录间隔 ≠ 1 天）与格点缺测（NaN）都**不连通**。
- 分叉后再汇合的区域归为**同一事件**——整体按图连通分量计算，
  而不是逐日简单追踪，因此先分开再合流的区域不会被拆成多个事件。
- 持续天数 = 事件覆盖的日历日数（事件内部日期必然连续，因为缺日不连通）。
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .netcdf_guard import locked_netcdf
from .validation import ValidationError

# 经度整圈/重复经线判定的角度容差（度）
_LON_TOL = 360.0 * 1e-6


def _validate_time_axis(times: np.ndarray) -> np.ndarray:
    """热区检测对时间轴的额外要求：每日零点、严格递增且不重复。"""
    if not np.issubdtype(times.dtype, np.datetime64):
        raise ValidationError("time 坐标无法按 CF 时间解码为公历日期")
    if np.isnat(times).any():
        raise ValidationError("time 坐标包含无法解析的时间值（NaT）")
    # 每日零点：向下取整到天后必须与原值相等
    if not np.array_equal(times.astype("datetime64[D]").astype(times.dtype), times):
        raise ValidationError("热区事件检测仅接受每日零点（00:00:00）的时间记录")
    if times.size > 1 and not np.all(times[1:] > times[:-1]):
        raise ValidationError("time 坐标必须严格递增且不重复")
    return times


def _analyze_longitude(lon: np.ndarray) -> bool:
    """检查重复物理经线并判断是否覆盖整圈（首尾相邻）。

    规则经纬轴下，跨度 (nlon-1)*dlon 达到 360° 即意味着首尾落在同一物理
    经线（如 0..360 或 -180..180），属于重复物理经线，直接报错。
    """
    nlon = lon.size
    dlon = float(lon[1]) - float(lon[0])
    span = (nlon - 1) * dlon
    if span >= 360.0 - _LON_TOL:
        raise ValidationError(
            "lon 坐标存在重复物理经线（如 0..360 或 -180..180 首尾同线），请移除重复列"
        )
    return abs(nlon * dlon - 360.0) <= _LON_TOL


class _UnionFind:
    """路径压缩 + 按秩合并的并查集（热格点图连通分量）。"""

    __slots__ = ("parent", "rank")

    def __init__(self, n: int) -> None:
        self.parent = list(range(n))
        self.rank = [0] * n

    def find(self, x: int) -> int:
        parent = self.parent
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.rank[ra] < self.rank[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        if self.rank[ra] == self.rank[rb]:
            self.rank[ra] += 1


def _date_str(ts: np.datetime64) -> str:
    return pd.Timestamp(ts).strftime("%Y-%m-%d")


def detect_heat_events(
    nc_path: str,
    variable: str,
    threshold: float,
    min_duration_days: int,
    max_cells: int,
) -> dict:
    """检测持续达标的高温区域事件，返回可 JSON 序列化的结果字典。

    调用前应先通过 validation.inspect_netcdf 完成结构校验；
    本函数在此基础上追加时间轴/规模/经度整圈等热区专属校验。
    """
    if not math.isfinite(threshold):
        raise ValidationError(f"阈值必须是有限数值，收到 {threshold!r}")
    if min_duration_days < 2:
        raise ValidationError(f"最短持续天数至少为 2，收到 {min_duration_days}")

    import xarray as xr  # 延迟导入，降低模块加载成本

    with locked_netcdf():
        with xr.open_dataset(nc_path, decode_times=True) as ds:
            times = np.asarray(ds["time"].values)
            lat = np.asarray(ds["lat"].values, dtype=float)
            lon = np.asarray(ds["lon"].values, dtype=float)
            values = np.asarray(ds[variable].values, dtype=np.float64)

    times = _validate_time_axis(times)
    ntime, nlat, nlon = values.shape
    total_cells = ntime * nlat * nlon
    if total_cells > max_cells:
        raise ValidationError(
            f"网格规模 time×lat×lon = {ntime}×{nlat}×{nlon} = {total_cells} "
            f"超过上限 {max_cells}"
        )
    full_circle = _analyze_longitude(lon)

    # 缺测（NaN）天然不满足 >=，不会参与连通
    hot = values >= threshold
    n_hot = int(hot.sum())

    # 每个热格点一个编号（行主序，与 np.nonzero 顺序一致）
    cell_id = np.full(hot.shape, -1, dtype=np.int64)
    cell_id[hot] = np.arange(n_hot, dtype=np.int64)
    uf = _UnionFind(n_hot)

    edge_a: list[np.ndarray] = []
    edge_b: list[np.ndarray] = []

    def _add_edges(id_a: np.ndarray, id_b: np.ndarray) -> None:
        edge_a.append(id_a)
        edge_b.append(id_b)

    # 1) 同日四邻接：纬向（南北）与经向（东西）各取单向避免重复边
    m = hot[:, :-1, :] & hot[:, 1:, :]
    _add_edges(cell_id[:, :-1, :][m], cell_id[:, 1:, :][m])
    m = hot[:, :, :-1] & hot[:, :, 1:]
    _add_edges(cell_id[:, :, :-1][m], cell_id[:, :, 1:][m])
    # 经度整圈：首列与末列相邻
    if full_circle:
        m = hot[:, :, 0] & hot[:, :, -1]
        _add_edges(cell_id[:, :, 0][m], cell_id[:, :, -1][m])

    # 2) 相邻日期：仅当间隔恰好 1 天（缺日不连通）且同一格点两日都达标
    one_day = np.timedelta64(1, "D")
    for t in range(ntime - 1):
        if times[t + 1] - times[t] != one_day:
            continue
        m = hot[t] & hot[t + 1]
        _add_edges(cell_id[t][m], cell_id[t + 1][m])

    if edge_a:
        ea = np.concatenate(edge_a).tolist()
        eb = np.concatenate(edge_b).tolist()
        for a, b in zip(ea, eb):
            uf.union(a, b)

    # 3) 按连通分量聚合事件
    t_idx, y_idx, x_idx = np.nonzero(hot)
    groups: dict[int, list[int]] = {}
    for k in range(n_hot):
        groups.setdefault(uf.find(k), []).append(k)

    events: list[dict] = []
    for members in groups.values():
        mem = np.asarray(members, dtype=np.int64)
        mem_t = t_idx[mem]
        order = np.argsort(mem_t, kind="stable")
        mem = mem[order]
        mem_t = mem_t[order]
        day_values, seg_starts = np.unique(mem_t, return_index=True)
        duration = int(day_values.size)
        if duration < min_duration_days:
            continue
        days = []
        for seg in np.split(mem, seg_starts[1:]):
            t = int(t_idx[seg[0]])
            points = [
                {"lat": float(lat[y_idx[k]]), "lon": float(lon[x_idx[k]])}
                for k in seg
            ]
            days.append(
                {
                    "date": _date_str(times[t]),
                    "point_count": len(points),
                    "points": points,
                }
            )
        events.append(
            {
                "start_date": _date_str(times[int(mem_t[0])]),
                "end_date": _date_str(times[int(mem_t[-1])]),
                "duration_days": duration,
                "total_cells": int(mem.size),
                "days": days,
            }
        )

    # 稳定排序：起始日 → 结束日 → 规模从大到小
    events.sort(key=lambda e: (e["start_date"], e["end_date"], -e["total_cells"]))
    for i, ev in enumerate(events, start=1):
        ev["event_id"] = i

    return {
        "variable": variable,
        "threshold": float(threshold),
        "min_duration_days": int(min_duration_days),
        "grid": {
            "ntime": int(ntime),
            "nlat": int(nlat),
            "nlon": int(nlon),
            "total_cells": int(total_cells),
            "lon_full_circle": bool(full_circle),
        },
        "event_count": len(events),
        "events": events,
    }
