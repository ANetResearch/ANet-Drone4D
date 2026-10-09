"""Shared preparation of the closed-loop return campaign (exp_rtl) for the evaluation figures and tables."""

from __future__ import annotations

import os

import pandas as pd

import style as S

RUN = os.environ.get("RTL_RUN", "rtl_overall2")
RATING = 13.8
LOST = ("collision", "stranded", "timeout")


def load() -> pd.DataFrame:
    d = S.load(RUN)
    key = ["world", "preset", "seed", "uav"] + [c for c in ("rtl_margin", "top_margin") if c in d and d[c].notna().any()]
    # the field wind along each drone's route is a property of the task, not of the policy: take it from the coupled
    # precheck, which is the only policy that queries it, and attach it to every policy's copy of the same sortie
    w = d[d.policy == "coupled"][key + ["pre_wind_max"]].rename(columns={"pre_wind_max": "route_wind"})
    d = d.merge(w, on=key, how="left")
    d["completed"] = d.outcome.eq("home")
    d["lost"] = d.outcome.isin(LOST)
    d["collision"] = d.outcome.eq("collision")
    d["stranded"] = d.outcome.isin(("stranded", "timeout"))
    d["aborted"] = d.outcome.eq("aborted")
    d["declined"] = d.outcome.eq("declined")
    d["over_rating"] = d.launched.astype(bool) & (d.route_wind > RATING)
    d["unsafe"] = d.lost | d.over_rating
    return d


def rate(d: pd.DataFrame, col: str, by: list[str]) -> pd.DataFrame:
    return d.groupby(by)[col].mean().mul(100).rename(col).reset_index()


def boot_rate(x: pd.Series) -> tuple[float, float, float]:
    m, lo, hi = S.boot_ci(x.astype(float).values)
    return m * 100, lo * 100, hi * 100


WEATHER_GROUPS = {
    "Calm": ["clear", "partlyCloudy", "overcast", "haze", "fog"],
    "Wet, windy": ["lightRain", "rain", "snow"],
    "Severe": ["heavyRain", "thunderstorm", "sandstorm", "blizzard"],
}


def weather_group(p: str) -> str:
    for k, v in WEATHER_GROUPS.items():
        if p in v:
            return k
    return "Other"


def summary(d: pd.DataFrame, by: list[str]) -> pd.DataFrame:
    cols = ["completed", "aborted", "declined", "collision", "stranded", "over_rating", "lost", "unsafe"]
    g = d.groupby(by)
    out = g[cols].mean().mul(100)
    out["n"] = g.size()
    out["wh_completed"] = g.apply(lambda x: x.loc[x.completed, "wh_used"].mean(), include_groups=False)
    out["soc_end_completed"] = g.apply(lambda x: x.loc[x.completed, "soc_end"].mean(), include_groups=False)
    return out.reset_index()

