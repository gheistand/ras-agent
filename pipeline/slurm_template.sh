#!/bin/bash
#SBATCH --job-name=ras-{job_id}
#SBATCH --partition=IllinoisComputes
#SBATCH --account=heistand-ic
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=8
#SBATCH --time=04:00:00
#SBATCH --output=/projects/illinois/eng/cee/heistand/logs/%j.out
#SBATCH --error=/projects/illinois/eng/cee/heistand/logs/%j.err

# HEC-RAS 6.6 Linux environment
export LD_LIBRARY_PATH=/projects/illinois/eng/cee/heistand/hecras-v66-linux/libs:/projects/illinois/eng/cee/heistand/hecras-v66-linux/libs/rhel_8:/projects/illinois/eng/cee/heistand/hecras-v66-linux/libs/mkl:$LD_LIBRARY_PATH
export PATH=/projects/illinois/eng/cee/heistand/hecras-v66-linux/bin:$PATH

cd {work_dir}

echo "Starting RasGeomPreprocess: $(date)"
RasGeomPreprocess {plan_hdf} {geom_ext}
gp_rc=$?
if [ $gp_rc -ne 0 ]; then
    echo "RasGeomPreprocess failed with exit code $gp_rc" >&2
    exit $gp_rc
fi

echo "Starting RasUnsteady: $(date)"
RasUnsteady {plan_hdf} {geom_ext}

if [ $? -eq 0 ]; then
    mv {plan_hdf} {plan_hdf_final}
    echo "Simulation complete: $(date)"
else
    echo "RasUnsteady failed with exit code $?" >&2
    exit 1
fi
