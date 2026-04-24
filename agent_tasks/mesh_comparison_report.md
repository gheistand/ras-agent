# Mesh Comparison Report

## Point Count Comparison

HDF inspection under `/Geometry/2D Flow Areas/` found these paths:

```text
/Geometry/2D Flow Areas
/Geometry/2D Flow Areas/Attributes           shape=(1,)
/Geometry/2D Flow Areas/Cell Info            shape=(1, 2)
/Geometry/2D Flow Areas/Cell Points          shape=(83600, 2)
/Geometry/2D Flow Areas/Polygon Info         shape=(1, 4)
/Geometry/2D Flow Areas/Polygon Parts        shape=(1, 2)
/Geometry/2D Flow Areas/Polygon Points       shape=(137, 2)
```

There is no `/Geometry/2D Flow Areas/MainArea/Cells Center Coordinate` dataset in this file. The reference point cloud is `/Geometry/2D Flow Areas/Cell Points`. `Attributes[0]["Name"]` is `MainArea`, and `Attributes[0]["Cell Count"]` is `83600`.

Measured counts from the two files:

| Source | Dataset / section | Count |
|---|---|---:|
| RAS Mapper reference HDF | `/Geometry/2D Flow Areas/Cell Points` | 83,600 |
| Our `.g01` text | `Storage Area 2D Points=` | 86,352 |
| Delta | our file minus reference | +2,752 |

Notes:

- The current `.g01` does **not** contain `75,729` points. The file currently declares `Storage Area 2D Points= 86352`.
- The current `.g01` base-grid declaration is `Storage Area Point Generation Data=,,60.960000,60.960000`.
- All parsed breaklines in the current `.g01` use `CellSize Min=30.48`, `CellSize Max=30.48`, `Near Repeats=1`.

Global concentration of the excess points:

| Distance to any breakline | Reference | Ours | Delta |
|---|---:|---:|---:|
| within 60 m | 16,732 | 21,873 | +5,141 |
| within 100 m | 18,923 | 24,547 | +5,624 |
| within 150 m | 22,144 | 27,759 | +5,615 |
| within 200 m | 25,323 | 30,920 | +5,597 |

Base-grid spacing away from breaklines is the same in both datasets:

| Source | Points farther than 150 m from any breakline | NN spacing median | p10 | p90 |
|---|---:|---:|---:|---:|
| Reference | 61,456 | 60.96 m | 60.96 m | 60.96 m |
| Ours | 58,593 | 60.95996 m | 60.95898 m | 60.95996 m |

Interpretation: the mismatch is a breakline-corridor problem, not a base-grid-spacing problem.

## Near-Breakline Analysis

Representative breaklines were chosen from the `.g01` as long, relatively straight channels:

- `Stream20`: 42 vertices, length `1950.09 m`, chord/length `0.9017`
- `Stream31`: 30 vertices, length `1347.64 m`, chord/length `0.9335`
- `Stream35`: 26 vertices, length `1100.35 m`, chord/length `0.9325`

Coordinate snippets from the `.g01` breakline polylines:

```text
Stream20 first3: [525568.486859221, 1877999.12859262], [525505.486859221, 1877990.12859262], [525502.486859221, 1878002.12859262]
Stream20 last3 : [524155.486859221, 1877084.12859262], [524149.486859221, 1877054.12859262], [524119.486859221, 1877003.12859262]

Stream31 first3: [511159.486859221, 1879682.12859262], [511141.486859221, 1879682.12859262], [511108.486859221, 1879670.12859262]
Stream31 last3 : [510034.486859221, 1879373.12859262], [509989.486859221, 1879382.12859262], [509938.486859221, 1879379.12859262]

Stream35 first3: [535720.486859221, 1879739.12859262], [535642.486859221, 1879661.12859262], [535624.486859221, 1879649.12859262]
Stream35 last3 : [535534.486859221, 1878749.12859262], [535528.486859221, 1878743.12859262], [535528.486859221, 1878731.12859262]
```

### 10 m distance histograms

Bins are `0-10, 10-20, ..., 190-200 m`.

```text
Stream20 reference: [1, 126, 4, 5, 121, 1, 6, 17, 15, 11, 15, 13, 16, 16, 8, 19, 19, 12, 12, 18]
Stream20 ours     : [12, 139, 16, 14, 140, 20, 5, 18, 19, 13, 17, 13, 14, 18, 11, 20, 17, 15, 14, 18]

Stream31 reference: [2, 87, 4, 4, 90, 2, 5, 10, 10, 17, 10, 10, 10, 11, 11, 13, 14, 15, 6, 11]
Stream31 ours     : [7, 97, 10, 14, 100, 11, 8, 9, 12, 20, 9, 11, 10, 10, 12, 18, 12, 16, 7, 11]

Stream35 reference: [2, 73, 6, 4, 77, 4, 7, 12, 9, 8, 19, 10, 12, 11, 15, 8, 15, 14, 16, 9]
Stream35 ours     : [9, 88, 5, 9, 92, 15, 11, 11, 17, 8, 18, 16, 13, 14, 15, 7, 19, 19, 14, 11]
```

The reference histograms are sharply concentrated in the `10-20 m` and `40-50 m` bins. Our output keeps those peaks but adds extra points in the `0-10 m`, `20-40 m`, and `50-60 m` bands.

### Closest points to the centerline

For the three selected linear breaklines:

| Breakline | Reference min distance | Reference points < 1 m | Our min distance | Our points < 1 m |
|---|---:|---:|---:|---:|
| Stream20 | 7.9387 m | 0 | 0.00000016 m | 5 |
| Stream31 | 9.2709 m | 0 | 0.5928 m | 1 |
| Stream35 | 1.5487 m | 0 | 1.4064 m | 0 |

Across all 61 breaklines:

| Source | Breaklines with at least one point < 1 m | Total points < 1 m |
|---|---:|---:|
| Reference | 19 | 26 |
| Ours | 39 | 99 |

Interpretation:

- On the three representative linear channels, the reference does **not** place seeds on the centerline.
- The reference does contain a small number of isolated `< 1 m` points elsewhere in the network, but they are sparse and not the dominant row pattern.
- Our output has substantially more near-centerline points.

### Distinct rows and along-breakline spacing

Measured signed row positions for the reference point cloud:

| Breakline | Row -45 m | Row -15 m | Row +15 m | Row +45 m | Median along-row spacing |
|---|---|---|---|---|---:|
| Stream20 | `-45.51 m` (60 pts) | `-15.12 m` (62 pts) | `+15.22 m` (63 pts) | `+45.65 m` (59 pts) | 30.47 m |
| Stream31 | `-45.73 m` (42 pts) | `-15.14 m` (43 pts) | `+15.13 m` (44 pts) | `+45.68 m` (46 pts) | 29.95 m |
| Stream35 | `-45.65 m` (34 pts) | `-15.37 m` (34 pts) | `+15.31 m` (35 pts) | `+45.74 m` (37 pts) | 29.74 m |

Measured extra rows in our point cloud:

| Breakline | Dominant rows retained | Extra ~30 m rows | 60.96 m-band count | Near-centerline points |
|---|---|---|---:|---:|
| Stream20 | yes, at ±15.24 and ±45.72 | `-31.26 m` (3 pts), `+30.17 m` (8 pts) | 11 | 5 |
| Stream31 | yes, at ±15.24 and ±45.72 | `-30.17 m` (5 pts), `+33.34 m` (3 pts) | 10 | 1 |
| Stream35 | yes, at ±15.24 and ±45.72 | `-32.12 m` (2 pts), `+32.21 m` (3 pts) | 9 | 0 |

Aggregated across the three representative breaklines:

| Distance band | Reference count | Our count |
|---|---:|---:|
| around 15.24 m | 281 | 318 |
| around 30.48 m | 5 | 24 |
| around 45.72 m | 278 | 322 |
| around 60.96 m | 10 | 30 |
| points < 1 m | 0 | 6 |

Interpretation:

- The reference pattern is two dominant rows per side at about `0.5 * CellSize Min` and `1.5 * CellSize Min`.
- Along each dominant row, the point spacing is essentially `CellSize Min` (`~30.48 m`).
- Our output keeps those dominant rows but overlays additional rows near `30.48 m`, `60.96 m`, and sometimes on the centerline.

## Inferred RAS Mapper Algorithm

These are inferences from the measured point clouds, not guesses from the UI labels alone.

- For `Near Repeats=1`, RAS Mapper is behaving like **two dominant rows per side**, not one row per side.
- The measured row offsets are approximately `15.24 m` and `45.72 m` from the centerline, i.e. `0.5 * 30.48` and `1.5 * 30.48`.
- On the selected straight channels, RAS Mapper does **not** deliberately place a centerline seed row.
- `CellSize Min` is being used as the **along-breakline spacing** and also as the **row-to-row spacing**. The first row is half a cell off the centerline.
- The base grid away from breaklines remains `60.96 m`.
- Near breaklines, the reference pattern looks like a **local replacement corridor**, not a simple overlay of extra seeds on top of the 60.96 m grid. The strongest evidence is the sharp reference peaks at `10-20 m` and `40-50 m`, with very few reference points in the `20-40 m` and `50-70 m` bands.
- Our point cloud is over-populating the breakline corridor. Relative to the reference, it adds roughly `+5.1k` points within `60 m` of any breakline and introduces extra `~30 m`, `~60 m`, and near-centerline rows.

Most likely RAS Mapper rule set for this case:

1. Keep the far-field base grid at `60.96 m`.
2. Replace the local breakline corridor with rows at signed offsets `±15.24 m` and `±45.72 m`.
3. Space points along those rows at `30.48 m`.
4. Avoid a deliberate `0 m` centerline row on straight channels.

## Required Code Changes in `_generate_breakline_seeds()`

Current logic reference: `G:/GH/ras-commander/ras_commander/geom/GeomMesh.py`.

### 1. Fix the offset formula

Current code at `GeomMesh.py:357-368`:

```python
for rep in range(1, near_repeats + 1):
    offset_dist = rep * bl_spacing
```

Measured mismatch:

- With `near_repeats=1`, this creates only one offset row per side at `30.48 m`.
- The reference uses dominant rows at `15.24 m` and `45.72 m` per side.

Required change:

- Start at a half-cell offset and generate `near_repeats + 1` rows per side.
- Best-fit formula from the measurements:

```python
for rep in range(near_repeats + 1):
    offset_dist = (rep + 0.5) * bl_spacing
```

For `near_repeats=1`, that yields `15.24 m` and `45.72 m`.

### 2. Widen the base-grid suppression corridor

Current code at `GeomMesh.py:381-385`:

```python
near_bl = any(pt.distance(bl) < bl_spacing * 0.8 for bl in bl_lines)
```

Measured mismatch:

- `0.8 * 30.48 = 24.384 m` is too narrow to clear the corridor that the reference actually replaces.
- The reference has very few points in the `20-40 m` and `50-70 m` bins, while our output still has clear extra populations there.

Required change:

- Replace the fixed `0.8 * bl_spacing` filter with a row-aware suppression radius that clears the corridor through the second dominant row.
- Best-fit inference from this dataset:

```python
clear_radius = (near_repeats + 1) * bl_spacing
```

For `near_repeats=1`, that is `60.96 m`.

This is an inference from the measured corridor pattern, but it matches the observed gap structure far better than `24.384 m`.

### 3. Keep the along-row spacing at `bl_spacing`

Current code at `GeomMesh.py:366-369` interpolates each offset line at `bl_spacing`.

Measured result:

- Reference dominant-row spacing is `29.74-30.47 m`.

Required change:

- Keep the along-row interpolation step at `bl_spacing`.
- The row placement is wrong; the along-row spacing is already directionally correct.

### 4. Add a final post-merge centerline cleanup

Current docstring at `GeomMesh.py:321-324` says centerline seeds should not be placed. The reference representative breaklines agree with that, but our current `.g01` still has too many `< 1 m` points.

Required change:

- After merging base-grid and breakline rows, drop any point whose final distance to a breakline is `< 1 m`, unless there is an explicit future reason to preserve endpoint/confluence exceptions.

Why:

- Reference: `26` points `< 1 m` across `19` breaklines.
- Ours: `99` points `< 1 m` across `39` breaklines.
- On the three representative linear channels, the reference has `0` such points and ours has `6`.

## Bottom Line

The reference mesh is not using a single `30.48 m` row per side. It is using a half-cell-started refinement corridor with dominant rows at `±15.24 m` and `±45.72 m`, `30.48 m` spacing along those rows, and a 60.96 m far-field base grid. Our current output keeps the far-field base grid correct but over-seeds the breakline corridor with extra `~0 m`, `~30 m`, and `~60 m` rows.
