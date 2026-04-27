# Benchmarks

This directory is reserved for method-equivalence benchmarking of the Illinois-first hydro-processing stack.

Benchmark order:

1. direct TauDEM CLI baseline
2. `rivnet` / `traudem` reference comparison
3. WhiteboxTools comparison in a separate worktree

Required comparisons:

- outlet snapping
- stream network geometry
- subbasin boundaries
- basin area totals
- derived channel metrics
- runtime

Constraints:

- direct TauDEM remains authoritative for mainline decisions
- R-based comparison tooling must stay out of the shipped runtime dependency chain
- WhiteboxTools experiments must not change the mainline API until benchmark results justify it
