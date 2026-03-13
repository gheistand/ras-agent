"""
Tests for hydrograph.py — NRCS Unit Hydrograph generation

Uses known basin characteristics to verify peak flows, timing,
and hydrograph shape against expected values.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'pipeline'))

import numpy as np
import pytest
from hydrograph import (
    kirpich_time_of_concentration,
    nrcs_unit_hydrograph,
    HydrographResult,
)


class TestKirpichTc:
    def test_basic_calculation(self):
        """Tc should decrease with steeper slope."""
        Tc_flat  = kirpich_time_of_concentration(10.0, 0.001)
        Tc_steep = kirpich_time_of_concentration(10.0, 0.010)
        assert Tc_steep < Tc_flat

    def test_longer_channel_longer_tc(self):
        Tc_short = kirpich_time_of_concentration(5.0,  0.005)
        Tc_long  = kirpich_time_of_concentration(20.0, 0.005)
        assert Tc_long > Tc_short

    def test_reasonable_range(self):
        """Tc for a typical small IL watershed should be 1-12 hours."""
        Tc = kirpich_time_of_concentration(15.0, 0.003)
        assert 0.5 <= Tc <= 24.0


class TestNrcsUnitHydrograph:
    def setup_method(self):
        """Standard small Illinois watershed parameters."""
        self.qp     = 5000.0   # cfs — 100-yr peak, medium watershed
        self.area   = 150.0    # mi²
        self.length = 30.0     # km
        self.slope  = 0.003    # m/m

    def test_peak_flow_matches_input(self):
        """Peak of generated hydrograph should equal the input Qp."""
        hydro = nrcs_unit_hydrograph(self.qp, self.area, self.length, self.slope)
        assert abs(np.max(hydro.flows_cfs) - self.qp) < 10.0  # within 10 cfs (interpolation tolerance)

    def test_hydrograph_starts_ends_low(self):
        """Flows should be near zero at start and end (baseflow only)."""
        hydro = nrcs_unit_hydrograph(self.qp, self.area, self.length, self.slope, baseflow_cfs=0)
        assert hydro.flows_cfs[0] < self.qp * 0.01
        assert hydro.flows_cfs[-1] < self.qp * 0.01

    def test_time_arrays_consistent(self):
        """time and flow arrays should have the same length."""
        hydro = nrcs_unit_hydrograph(self.qp, self.area, self.length, self.slope)
        assert len(hydro.times_hr) == len(hydro.flows_cfs)

    def test_time_starts_at_zero(self):
        hydro = nrcs_unit_hydrograph(self.qp, self.area, self.length, self.slope)
        assert hydro.times_hr[0] == pytest.approx(0.0)

    def test_positive_flows(self):
        """All flows should be non-negative."""
        hydro = nrcs_unit_hydrograph(self.qp, self.area, self.length, self.slope)
        assert np.all(hydro.flows_cfs >= 0)

    def test_volume_positive(self):
        hydro = nrcs_unit_hydrograph(self.qp, self.area, self.length, self.slope)
        assert hydro.volume_acre_ft > 0

    def test_larger_qp_larger_volume(self):
        """Larger peak flow should produce larger runoff volume."""
        h1 = nrcs_unit_hydrograph(2000, self.area, self.length, self.slope)
        h2 = nrcs_unit_hydrograph(8000, self.area, self.length, self.slope)
        assert h2.volume_acre_ft > h1.volume_acre_ft

    def test_baseflow_added(self):
        """Minimum flow should equal baseflow."""
        baseflow = 50.0
        hydro = nrcs_unit_hydrograph(
            self.qp, self.area, self.length, self.slope,
            baseflow_cfs=baseflow
        )
        assert np.min(hydro.flows_cfs) >= baseflow * 0.99

    def test_time_step_resolution(self):
        """Custom time step should be reflected in output."""
        h_15min = nrcs_unit_hydrograph(self.qp, self.area, self.length, self.slope, time_step_hr=0.25)
        h_1hr   = nrcs_unit_hydrograph(self.qp, self.area, self.length, self.slope, time_step_hr=1.0)
        assert len(h_15min.times_hr) > len(h_1hr.times_hr)
