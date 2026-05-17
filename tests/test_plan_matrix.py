"""
Tests for pipeline/plan_matrix.py.

The tests use fake ras-commander APIs and tiny text fixtures so they run
without HEC-RAS, NOAA network calls, or binary NetCDF dependencies.
"""

import os
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))

import plan_matrix as pm


class FakeRasObject:
    def __init__(self, project_folder: Path):
        self.project_folder = Path(project_folder)
        self.project_name = "FakeProject"
        self.prj_file = self.project_folder / "FakeProject.prj"
        self.ras_exe_path = None
        self.initialize(self.project_folder, None)

    def check_initialized(self):
        return None

    def initialize(self, project_folder, ras_exe_path, *args, **kwargs):
        self.project_folder = Path(project_folder)
        self.ras_exe_path = ras_exe_path
        self.prj_file = self.project_folder / "FakeProject.prj"
        self.plan_df = self.get_plan_entries()
        self.unsteady_df = self.get_unsteady_entries()

    def get_plan_entries(self):
        return self._entries("Plan", "plan_number")

    def get_unsteady_entries(self):
        return self._entries("Unsteady", "unsteady_number")

    def _entries(self, entry_type: str, number_column: str):
        rows = []
        if not self.prj_file.exists():
            return pd.DataFrame(columns=[number_column, "full_path"])
        for line in self.prj_file.read_text(encoding="utf-8").splitlines():
            prefix = f"{entry_type} File="
            if line.startswith(prefix):
                file_id = line.split("=", 1)[1].strip()
                number = file_id[1:].zfill(2)
                path = self.project_folder / f"{self.project_name}.{file_id}"
                row = {number_column: number, "full_path": str(path)}
                if entry_type == "Plan" and path.exists():
                    row["Plan Title"] = _read_key(path, "Plan Title") or ""
                    row["Short Identifier"] = _read_key(path, "Short Identifier") or ""
                rows.append(row)
        return pd.DataFrame(rows)


class FakeRasPlan:
    @staticmethod
    def get_plan_value(plan_number_or_path, key, ras_object=None):
        path = _plan_path(plan_number_or_path, ras_object)
        return _read_key(path, key)

    @staticmethod
    def clone_plan(
        template_plan,
        new_shortid=None,
        new_plan_shortid=None,
        new_title=None,
        unsteady_flow=None,
        intervals=None,
        description=None,
        ras_object=None,
        **kwargs,
    ):
        existing = [int(value) for value in ras_object.get_plan_entries()["plan_number"].tolist()]
        new_number = 1
        while new_number in existing:
            new_number += 1
        new_number_text = f"{new_number:02d}"
        source = _plan_path(template_plan, ras_object)
        target = ras_object.project_folder / f"{ras_object.project_name}.p{new_number_text}"
        lines = source.read_text(encoding="utf-8").splitlines(True)
        short_id = new_plan_shortid if new_plan_shortid is not None else new_shortid
        _replace_line(lines, "Plan Title=", f"Plan Title={new_title}\n")
        _replace_line(lines, "Short Identifier=", f"Short Identifier={short_id}\n")
        if unsteady_flow is not None:
            _replace_line(lines, "Flow File=", f"Flow File=u{str(unsteady_flow).zfill(2)}\n")
        target.write_text("".join(lines), encoding="utf-8")
        with ras_object.prj_file.open("a", encoding="utf-8") as handle:
            handle.write(f"Plan File=p{new_number_text}\n")
        ras_object.initialize(ras_object.project_folder, ras_object.ras_exe_path)
        return new_number_text

    @staticmethod
    def update_simulation_date(plan_number_or_path, start_date, end_date, ras_object=None):
        path = _plan_path(plan_number_or_path, ras_object)
        formatted = (
            f"{start_date.strftime('%d%b%Y').upper()},{start_date.strftime('%H%M')},"
            f"{end_date.strftime('%d%b%Y').upper()},{end_date.strftime('%H%M')}"
        )
        lines = path.read_text(encoding="utf-8").splitlines(True)
        _replace_line(lines, "Simulation Date=", f"Simulation Date={formatted}\n")
        path.write_text("".join(lines), encoding="utf-8")

    @staticmethod
    def update_plan_intervals(
        plan_number_or_path,
        computation_interval=None,
        output_interval=None,
        instantaneous_interval=None,
        mapping_interval=None,
        ras_object=None,
    ):
        path = _plan_path(plan_number_or_path, ras_object)
        lines = path.read_text(encoding="utf-8").splitlines(True)
        if computation_interval:
            _replace_line(lines, "Computation Interval=", f"Computation Interval={computation_interval}\n")
        if output_interval:
            _replace_line(lines, "Output Interval=", f"Output Interval={output_interval}\n")
        if instantaneous_interval:
            _replace_line(lines, "Instantaneous Interval=", f"Instantaneous Interval={instantaneous_interval}\n")
        if mapping_interval:
            _replace_line(lines, "Mapping Interval=", f"Mapping Interval={mapping_interval}\n")
        path.write_text("".join(lines), encoding="utf-8")


class FakeRasUnsteady:
    calls = []

    @staticmethod
    def set_gridded_precipitation(unsteady_file, netcdf_path, interpolation="Bilinear", ras_object=None):
        number = str(unsteady_file).zfill(2)
        path = ras_object.project_folder / f"{ras_object.project_name}.u{number}"
        lines = path.read_text(encoding="utf-8").splitlines(True)
        _replace_line(
            lines,
            "Met BC=Precipitation|Gridded Source=",
            "Met BC=Precipitation|Gridded Source=GDAL Raster File(s)\n",
        )
        _replace_line(
            lines,
            "Met BC=Precipitation|Gridded GDAL Filename=",
            f"Met BC=Precipitation|Gridded GDAL Filename={Path(netcdf_path).name}\n",
            append=True,
        )
        path.write_text("".join(lines), encoding="utf-8")
        FakeRasUnsteady.calls.append((number, Path(netcdf_path)))


class FakeRasPermutation:
    calls = []

    @staticmethod
    def _partition_dataframe(parameters_df, batch_size):
        FakeRasPermutation.calls.append(batch_size)
        return [
            parameters_df.iloc[start : start + batch_size].copy()
            for start in range(0, len(parameters_df), batch_size)
        ]


class FakeAbmHyetographGrid:
    calls = []

    @staticmethod
    def generate(
        bounds,
        ari_years,
        storm_duration_hours=24.0,
        timestep_minutes=15,
        peak_position_percent=50.0,
        output_netcdf="abm_hyetograph.nc",
        **kwargs,
    ):
        path = Path(output_netcdf)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"fake netcdf {ari_years} {storm_duration_hours} {timestep_minutes} {bounds}",
            encoding="utf-8",
        )
        FakeAbmHyetographGrid.calls.append(
            {
                "bounds": bounds,
                "ari_years": ari_years,
                "storm_duration_hours": storm_duration_hours,
                "timestep_minutes": timestep_minutes,
                "peak_position_percent": peak_position_percent,
                "output_netcdf": path,
            }
        )
        return path


def _fake_apis():
    return pm._RasCommanderApis(
        init_ras_project=lambda project_dir, *args, **kwargs: FakeRasObject(Path(project_dir)),
        RasPlan=FakeRasPlan,
        RasUnsteady=FakeRasUnsteady,
        RasPermutation=FakeRasPermutation,
        AbmHyetographGrid=FakeAbmHyetographGrid,
    )


def _make_fake_project(tmp_path: Path) -> Path:
    project_dir = tmp_path / "FakeProject"
    project_dir.mkdir()
    (project_dir / "FakeProject.prj").write_text(
        "Proj Title=Fake Project\n"
        "Plan File=p01\n"
        "Unsteady File=u01\n",
        encoding="utf-8",
    )
    (project_dir / "FakeProject.p01").write_text(
        "Plan Title=Base Plan\n"
        "Short Identifier=Base\n"
        "Program Version=6.60\n"
        "Geom File=g01\n"
        "Flow File=u01\n"
        "Simulation Date=01JAN2000,0000,02JAN2000,0000\n"
        "Computation Interval=30SEC\n"
        "Output Interval=1HOUR\n"
        "Instantaneous Interval=1HOUR\n"
        "Mapping Interval=1HOUR\n",
        encoding="utf-8",
    )
    (project_dir / "FakeProject.u01").write_text(
        "Flow Title=Base Flow\n"
        "Met BC=Precipitation|Gridded Source=DSS\n"
        "Met BC=Precipitation|Gridded DSS Pathname=/A/B/PRECIP//15MIN/F/\n",
        encoding="utf-8",
    )
    return project_dir


@pytest.fixture(autouse=True)
def reset_fakes(monkeypatch):
    FakeRasUnsteady.calls.clear()
    FakeRasPermutation.calls.clear()
    FakeAbmHyetographGrid.calls.clear()
    monkeypatch.setattr(pm, "_load_ras_commander", _fake_apis)


def test_design_storm_spec_defaults():
    spec = pm.DesignStormSpec()

    assert spec.aep_years == [10, 50, 100, 500]
    assert spec.durations_hours == [6, 12, 24]
    assert spec.timestep_minutes == 15
    assert spec.peak_position_percent == 50.0


def test_generate_plan_matrix_default_matrix_writes_manifest_and_unique_files(tmp_path):
    project_dir = _make_fake_project(tmp_path)
    ras_object = FakeRasObject(project_dir)
    spec = pm.DesignStormSpec(
        time_of_concentration_hours=2.0,
        simulation_start=datetime(2020, 1, 1, 0, 0),
    )

    manifest = pm.generate_plan_matrix(
        project_dir,
        "01",
        spec,
        (-90.0, 39.0, -89.0, 40.0),
        ras_object=ras_object,
    )

    assert len(manifest) == 12
    assert manifest["plan_number"].tolist() == [f"{value:02d}" for value in range(2, 14)]
    assert manifest["plan_short_id"].tolist()[:3] == ["Q10_6H", "Q10_12H", "Q10_24H"]
    assert manifest["netcdf_path"].is_unique
    assert manifest["unsteady_file"].is_unique
    assert all(Path(path).exists() for path in manifest["netcdf_path"])

    manifest_csv = Path(manifest.attrs["manifest_csv"])
    assert manifest_csv == project_dir / pm.DEFAULT_MANIFEST_FILENAME
    assert manifest_csv.exists()
    csv_manifest = pd.read_csv(manifest_csv)
    assert len(csv_manifest) == 12

    q100_24h = manifest[
        (manifest["aep_years"] == 100) & (manifest["duration_hours"] == 24.0)
    ].iloc[0]
    plan_file = project_dir / f"FakeProject.p{q100_24h['plan_number']}"
    unsteady_file = Path(q100_24h["unsteady_file"])

    assert _read_key(plan_file, "Short Identifier") == "Q100_24H"
    assert _read_key(plan_file, "Flow File") == f"u{q100_24h['unsteady_number']}"
    assert _read_key(plan_file, "Simulation Date") == "01JAN2020,0000,02JAN2020,0300"
    assert _read_key(plan_file, "Output Interval") == "15MIN"
    assert "Gridded Source=GDAL Raster File(s)" in unsteady_file.read_text(encoding="utf-8")
    assert Path(q100_24h["netcdf_path"]).name in unsteady_file.read_text(encoding="utf-8")

    assert len(FakeAbmHyetographGrid.calls) == 12
    assert any(call["ari_years"] == 100 and call["storm_duration_hours"] == 24.0 for call in FakeAbmHyetographGrid.calls)
    assert len(FakeRasUnsteady.calls) == 12


def test_generate_plan_matrix_partitions_large_matrix_into_batch_projects(tmp_path):
    project_dir = _make_fake_project(tmp_path)
    spec = pm.DesignStormSpec(
        aep_years=list(range(1, 102)),
        durations_hours=[1],
        timestep_minutes=60,
        simulation_start=datetime(2020, 1, 1, 0, 0),
    )

    manifest = pm.generate_plan_matrix(
        project_dir,
        "01",
        spec,
        (-90.0, 39.0, -89.0, 40.0),
    )

    assert len(manifest) == 101
    assert manifest["batch_index"].tolist().count(1) == 98
    assert manifest["batch_index"].tolist().count(2) == 3
    assert FakeRasPermutation.calls == [98]
    assert manifest.attrs["batch_count"] == 2
    assert manifest.attrs["max_plans_per_batch"] == 98

    batch_1 = tmp_path / "FakeProject_design_storms_001"
    batch_2 = tmp_path / "FakeProject_design_storms_002"
    assert (batch_1 / pm.DEFAULT_MANIFEST_FILENAME).exists()
    assert (batch_2 / pm.DEFAULT_MANIFEST_FILENAME).exists()
    assert Path(manifest.attrs["manifest_csv"]) == project_dir / f"master_{pm.DEFAULT_MANIFEST_FILENAME}"
    assert Path(manifest.attrs["manifest_csv"]).exists()
    assert set(Path(path).parent.parent for path in manifest["netcdf_path"]) == {batch_1, batch_2}


def test_design_storm_spec_validation_rejects_bad_timestep():
    spec = pm.DesignStormSpec(aep_years=[10], durations_hours=[1], timestep_minutes=40)

    with pytest.raises(ValueError, match="does not evenly divide"):
        spec.validate()


def _plan_path(plan_number_or_path, ras_object: FakeRasObject) -> Path:
    path = Path(str(plan_number_or_path))
    if path.exists():
        return path
    return ras_object.project_folder / f"{ras_object.project_name}.p{str(plan_number_or_path).zfill(2)}"


def _read_key(path: Path, key: str):
    prefix = f"{key}="
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(prefix):
            return line.split("=", 1)[1].strip()
    return None


def _replace_line(lines, prefix: str, replacement: str, append: bool = False):
    for index, line in enumerate(lines):
        if line.startswith(prefix):
            lines[index] = replacement
            return
    if append:
        lines.append(replacement)
        return
    raise AssertionError(f"Missing line prefix {prefix!r}")
