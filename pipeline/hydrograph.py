"""
hydrograph.py — Synthetic inflow hydrograph generation

Converts USGS StreamStats peak flows (Qp) into full time-series hydrographs
for HEC-RAS unsteady flow boundary conditions using the NRCS (SCS)
Dimensionless Unit Hydrograph method.

Output is a HEC-DSS compatible time series (time_hours, flow_cfs) ready for
import into HEC-RAS as an unsteady flow boundary condition.

Reference: NRCS NEH Part 630, Chapter 16 (Unit Hydrograph)

Copyright 2026 Glenn Heistand / CHAMP — Illinois State Water Survey
Apache License 2.0
"""

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

# numpy.trapz removed in NumPy 2.0 — use trapezoid with fallback
_trapz = getattr(np, "trapezoid", getattr(np, "trapz", None))

logger = logging.getLogger(__name__)


# ── NRCS Dimensionless Unit Hydrograph ───────────────────────────────────────
#
# Tabulated ratios from NRCS NEH Part 630, Chapter 16, Table 16-1
# t/Tp  : time ratio (time / time to peak)
# q/qp  : discharge ratio (discharge / peak discharge)
#
# Extended to include the full recession limb to near-zero flow.

NRCS_DUH = np.array([
    # t/Tp    q/qp
    [0.000,   0.000],
    [0.100,   0.030],
    [0.200,   0.100],
    [0.300,   0.190],
    [0.400,   0.310],
    [0.500,   0.470],
    [0.600,   0.660],
    [0.700,   0.820],
    [0.800,   0.930],
    [0.900,   0.990],
    [1.000,   1.000],
    [1.100,   0.990],
    [1.200,   0.930],
    [1.300,   0.860],
    [1.400,   0.780],
    [1.500,   0.680],
    [1.600,   0.560],
    [1.700,   0.460],
    [1.800,   0.390],
    [1.900,   0.330],
    [2.000,   0.280],
    [2.200,   0.207],
    [2.400,   0.147],
    [2.600,   0.107],
    [2.800,   0.077],
    [3.000,   0.055],
    [3.500,   0.025],
    [4.000,   0.011],
    [4.500,   0.005],
    [5.000,   0.000],
])


# ── Data Structures ───────────────────────────────────────────────────────────

@dataclass
class HydrographResult:
    """A synthetic inflow hydrograph for a given return period."""
    return_period_yr: int
    peak_flow_cfs: float
    time_to_peak_hr: float
    duration_hr: float
    time_step_hr: float
    times_hr: np.ndarray       # array of time values (hours from start)
    flows_cfs: np.ndarray      # array of flow values (cfs)
    baseflow_cfs: float        # constant baseflow added to hydrograph
    source: str                # "NRCS_DUH" etc.
    metadata: dict = field(default_factory=dict)

    @property
    def peak_time_hr(self) -> float:
        """Time of peak flow from start."""
        return float(self.times_hr[np.argmax(self.flows_cfs)])

    @property
    def volume_acre_ft(self) -> float:
        """Total runoff volume in acre-feet."""
        # Trapezoidal integration; subtract baseflow
        net_flows = np.maximum(self.flows_cfs - self.baseflow_cfs, 0)
        dt_sec = self.time_step_hr * 3600
        volume_cfs_s = _trapz(net_flows, dx=dt_sec)
        return volume_cfs_s / 43560  # cfs·s to acre-ft


@dataclass
class HydrographSet:
    """Collection of hydrographs for multiple return periods."""
    watershed_area_mi2: float
    time_of_concentration_hr: float
    hydrographs: dict[int, HydrographResult] = field(default_factory=dict)

    def get(self, return_period: int) -> Optional[HydrographResult]:
        return self.hydrographs.get(return_period)


# ── Time of Concentration ─────────────────────────────────────────────────────

def kirpich_time_of_concentration(
    channel_length_km: float,
    channel_slope_m_per_m: float,
) -> float:
    """
    Kirpich method for time of concentration (Tc).
    Widely used for small rural watersheds; acceptable for IL streams.

    Tc = 0.0195 * (L^0.77) / (S^0.385)

    where L = channel length (meters), S = slope (m/m)

    Returns: Tc in hours
    """
    L_m = channel_length_km * 1000
    S = max(channel_slope_m_per_m, 0.0001)
    Tc_min = 0.0195 * (L_m ** 0.77) / (S ** 0.385)
    Tc_hr = Tc_min / 60.0
    logger.debug(f"Kirpich Tc = {Tc_hr:.2f} hr (L={channel_length_km:.2f} km, S={S:.5f})")
    return Tc_hr


def lag_time_from_tc(Tc_hr: float) -> float:
    """
    NRCS lag time: Tlag = 0.6 * Tc
    Time to peak: Tp = 0.5 * D + Tlag, where D = storm duration
    For design storms: D ≈ Tc, so Tp ≈ 0.5*Tc + 0.6*Tc = 1.1*Tc
    """
    return 0.6 * Tc_hr


# ── NRCS Unit Hydrograph ─────────────────────────────────────────────────────

def nrcs_unit_hydrograph(
    peak_flow_cfs: float,
    drainage_area_mi2: float,
    channel_length_km: float,
    channel_slope_m_per_m: float,
    time_step_hr: float = 0.25,
    baseflow_cfs: float = 0.0,
    duration_multiplier: float = 5.0,
) -> HydrographResult:
    """
    Generate a synthetic design hydrograph using the NRCS dimensionless
    unit hydrograph method (NRCS NEH Part 630, Chapter 16).

    The unit hydrograph is scaled to match the target peak flow (Qp)
    from StreamStats/regression equations.

    Args:
        peak_flow_cfs:        Target peak discharge (cfs) from StreamStats
        drainage_area_mi2:    Drainage area (mi²)
        channel_length_km:    Main channel length (km)
        channel_slope_m_per_m: Main channel slope (m/m)
        time_step_hr:         Output time step (hours); 0.25 hr = 15 min recommended
        baseflow_cfs:         Constant baseflow to add (cfs); default 0
        duration_multiplier:  Hydrograph duration as multiple of time-to-peak

    Returns:
        HydrographResult with time/flow arrays
    """
    # Time of concentration and lag time
    Tc_hr = kirpich_time_of_concentration(channel_length_km, channel_slope_m_per_m)
    Tlag_hr = lag_time_from_tc(Tc_hr)

    # Storm duration (D) for design storm: D = Tc for most small watersheds
    D_hr = Tc_hr
    # Time to peak: Tp = D/2 + Tlag
    Tp_hr = D_hr / 2.0 + Tlag_hr

    # Total hydrograph duration
    duration_hr = duration_multiplier * Tp_hr

    # Build time array
    times_hr = np.arange(0, duration_hr + time_step_hr, time_step_hr)

    # Interpolate NRCS DUH at each time ratio t/Tp
    t_ratio = times_hr / Tp_hr
    q_ratio = np.interp(t_ratio, NRCS_DUH[:, 0], NRCS_DUH[:, 1], left=0.0, right=0.0)

    # Scale to peak flow
    flows_cfs = q_ratio * peak_flow_cfs + baseflow_cfs

    # Ensure non-negative
    flows_cfs = np.maximum(flows_cfs, baseflow_cfs)

    logger.info(
        f"NRCS hydrograph: Qp={peak_flow_cfs:.0f} cfs, "
        f"Tp={Tp_hr:.2f} hr, Tc={Tc_hr:.2f} hr, "
        f"duration={duration_hr:.1f} hr, "
        f"volume={_trapz(flows_cfs - baseflow_cfs, dx=time_step_hr*3600)/43560:.1f} ac-ft"
    )

    return HydrographResult(
        return_period_yr=0,       # caller sets this
        peak_flow_cfs=peak_flow_cfs,
        time_to_peak_hr=Tp_hr,
        duration_hr=duration_hr,
        time_step_hr=time_step_hr,
        times_hr=times_hr,
        flows_cfs=flows_cfs,
        baseflow_cfs=baseflow_cfs,
        source="NRCS_DUH",
        metadata={
            "Tc_hr": Tc_hr,
            "Tlag_hr": Tlag_hr,
            "D_hr": D_hr,
            "drainage_area_mi2": drainage_area_mi2,
        },
    )


# ── Hydrograph Set Generation ─────────────────────────────────────────────────

def generate_hydrograph_set(
    peak_flows: "PeakFlowEstimates",   # from streamstats.py
    channel_length_km: float,
    channel_slope_m_per_m: float,
    return_periods: Optional[list[int]] = None,
    time_step_hr: float = 0.25,
    baseflow_fraction: float = 0.02,   # baseflow as fraction of Q2
) -> HydrographSet:
    """
    Generate a complete set of design hydrographs for multiple return periods.

    Args:
        peak_flows:           PeakFlowEstimates from streamstats.get_peak_flows()
        channel_length_km:    Main channel length (km)
        channel_slope_m_per_m: Channel slope (m/m)
        return_periods:       List of return periods to generate (default: all available)
        time_step_hr:         Output time step (hours)
        baseflow_fraction:    Baseflow as fraction of Q2 (low-flow approximation)

    Returns:
        HydrographSet with hydrographs keyed by return period
    """
    if return_periods is None:
        return_periods = [2, 5, 10, 25, 50, 100, 500]

    Tc_hr = kirpich_time_of_concentration(channel_length_km, channel_slope_m_per_m)

    # Baseflow estimate
    q2 = peak_flows.Q2 or 0.0
    baseflow_cfs = max(q2 * baseflow_fraction, 0.1)

    hydrographs = {}
    flows_dict = peak_flows.as_dict()

    for rp in return_periods:
        qp = flows_dict.get(rp)
        if qp is None:
            logger.warning(f"No Q{rp} available — skipping")
            continue

        hydro = nrcs_unit_hydrograph(
            peak_flow_cfs=qp,
            drainage_area_mi2=peak_flows.drainage_area_mi2,
            channel_length_km=channel_length_km,
            channel_slope_m_per_m=channel_slope_m_per_m,
            time_step_hr=time_step_hr,
            baseflow_cfs=baseflow_cfs,
        )
        hydro.return_period_yr = rp
        hydrographs[rp] = hydro

    return HydrographSet(
        watershed_area_mi2=peak_flows.drainage_area_mi2,
        time_of_concentration_hr=Tc_hr,
        hydrographs=hydrographs,
    )


# ── Export ────────────────────────────────────────────────────────────────────

def save_hydrographs_csv(hydro_set: HydrographSet, output_dir: Path) -> dict[int, Path]:
    """Save each hydrograph to a CSV file (time_hr, flow_cfs)."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {}

    for rp, hydro in hydro_set.hydrographs.items():
        path = output_dir / f"hydrograph_Q{rp:04d}yr.csv"
        df = pd.DataFrame({
            "time_hr": hydro.times_hr,
            "flow_cfs": hydro.flows_cfs,
        })
        df.to_csv(path, index=False)
        paths[rp] = path

    logger.info(f"Saved {len(paths)} hydrographs to {output_dir}")
    return paths


def save_hydrographs_hecras_dss_input(
    hydro_set: HydrographSet,
    output_dir: Path,
    start_datetime: str = "01JAN2000 00:00:00",
) -> dict[int, Path]:
    """
    Save hydrographs as HEC-RAS unsteady flow boundary condition text files.
    Format: space-delimited flow table compatible with HEC-RAS .u## flow files.

    Args:
        hydro_set:      HydrographSet to export
        output_dir:     Output directory
        start_datetime: Start date/time string for HEC-RAS (e.g. "01JAN2000 00:00:00")

    Returns:
        Dict of {return_period: path_to_flow_file}
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {}

    for rp, hydro in hydro_set.hydrographs.items():
        path = output_dir / f"flow_hydrograph_Q{rp:04d}yr.txt"

        lines = [
            f"# HEC-RAS Unsteady Flow Boundary Condition",
            f"# Return Period: {rp}-year",
            f"# Peak Flow: {hydro.peak_flow_cfs:.1f} cfs",
            f"# Time to Peak: {hydro.time_to_peak_hr:.2f} hr",
            f"# Duration: {hydro.duration_hr:.1f} hr",
            f"# Time Step: {hydro.time_step_hr:.2f} hr",
            f"# Source: {hydro.source}",
            f"# Start: {start_datetime}",
            f"#",
            f"# time_hr    flow_cfs",
        ]

        for t, q in zip(hydro.times_hr, hydro.flows_cfs):
            lines.append(f"{t:10.3f}  {q:12.1f}")

        with open(path, "w") as f:
            f.write("\n".join(lines))

        paths[rp] = path

    logger.info(f"Saved {len(paths)} HEC-RAS flow files to {output_dir}")
    return paths


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    parser = argparse.ArgumentParser(description="Generate design hydrographs")
    parser.add_argument("--qp",     type=float, required=True, help="Peak flow (cfs)")
    parser.add_argument("--area",   type=float, required=True, help="Drainage area (mi²)")
    parser.add_argument("--length", type=float, required=True, help="Channel length (km)")
    parser.add_argument("--slope",  type=float, default=0.002,  help="Channel slope (m/m)")
    parser.add_argument("--dt",     type=float, default=0.25,   help="Time step (hours)")
    parser.add_argument("--output", type=str,   default="data/hydrographs")
    args = parser.parse_args()

    hydro = nrcs_unit_hydrograph(
        peak_flow_cfs=args.qp,
        drainage_area_mi2=args.area,
        channel_length_km=args.length,
        channel_slope_m_per_m=args.slope,
        time_step_hr=args.dt,
    )
    hydro.return_period_yr = 100

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    out = output_dir / "hydrograph_test.csv"
    pd.DataFrame({"time_hr": hydro.times_hr, "flow_cfs": hydro.flows_cfs}).to_csv(out, index=False)

    print(f"\nHydrograph generated:")
    print(f"  Peak flow:    {hydro.peak_flow_cfs:.1f} cfs")
    print(f"  Time to peak: {hydro.time_to_peak_hr:.2f} hr")
    print(f"  Duration:     {hydro.duration_hr:.1f} hr")
    print(f"  Volume:       {hydro.volume_acre_ft:.1f} ac-ft")
    print(f"  Saved to:     {out}")
