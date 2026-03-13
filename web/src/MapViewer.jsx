import { useEffect, useRef, useState, useCallback } from "react"
import { fetchFloodExtent } from "./api"

// Return period color scale (light → dark blue with increasing RP)
const RP_COLORS = {
  10:  "#93c5fd",   // light blue
  25:  "#60a5fa",
  50:  "#3b82f6",   // medium blue
  100: "#1d4ed8",   // dark blue
  500: "#1e3a8a",   // very dark blue
}

// All layer ID prefixes we manage — used for cleanup on job change
const _FLOOD_LAYER_PREFIXES = ["flood-fill-", "flood-outline-"]
const _FLOOD_SOURCE_PREFIX = "flood-source-"

export default function MapViewer({ selectedJob }) {
  const mapRef = useRef(null)
  const mapInstanceRef = useRef(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [availableRPs, setAvailableRPs] = useState([])
  const [visibleRPs, setVisibleRPs] = useState(new Set([10, 50, 100]))

  // Initialize map on mount
  useEffect(() => {
    if (!mapRef.current || mapInstanceRef.current) return
    mapInstanceRef.current = new window.maplibregl.Map({
      container: mapRef.current,
      style: {
        version: 8,
        sources: {
          osm: {
            type: "raster",
            tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
            tileSize: 256,
            attribution: "© OpenStreetMap contributors",
          }
        },
        layers: [{ id: "osm", type: "raster", source: "osm" }]
      },
      center: [-89.5, 40.0],   // Illinois center
      zoom: 7,
    })
    return () => {
      if (mapInstanceRef.current) {
        mapInstanceRef.current.remove()
        mapInstanceRef.current = null
      }
    }
  }, [])

  // Toggle a return period on/off
  const toggleRP = useCallback((rp) => {
    setVisibleRPs(prev => {
      const next = new Set(prev)
      if (next.has(rp)) next.delete(rp)
      else next.add(rp)
      return next
    })
  }, [])

  // Load flood extents for all return periods when selectedJob changes
  useEffect(() => {
    if (!selectedJob || selectedJob.status !== "complete" || !mapInstanceRef.current) return
    const map = mapInstanceRef.current

    // Remove all existing flood layers and sources
    const style = map.getStyle()
    if (style?.layers) {
      style.layers
        .filter(l => _FLOOD_LAYER_PREFIXES.some(p => l.id.startsWith(p)))
        .forEach(l => map.removeLayer(l.id))
    }
    if (style?.sources) {
      Object.keys(style.sources)
        .filter(id => id.startsWith(_FLOOD_SOURCE_PREFIX))
        .forEach(id => map.removeSource(id))
    }

    setAvailableRPs([])
    setLoading(true)
    setError(null)

    // Capture current visibleRPs for initial layer setup (avoids stale closure)
    const currentVisible = visibleRPs

    fetchFloodExtent(selectedJob.id, "all")
      .then(geojson => {
        const map = mapInstanceRef.current
        if (!map) return

        // Group features by return period
        const featuresByRP = {}
        for (const feature of geojson.features) {
          const rp = feature.properties?.return_period_yr
          if (rp != null) {
            if (!featuresByRP[rp]) featuresByRP[rp] = []
            featuresByRP[rp].push(feature)
          }
        }

        const rps = Object.keys(featuresByRP).map(Number).sort((a, b) => a - b)
        setAvailableRPs(rps)

        const allCoords = []

        rps.forEach(rp => {
          const fc = { type: "FeatureCollection", features: featuresByRP[rp] }
          map.addSource(`${_FLOOD_SOURCE_PREFIX}${rp}`, { type: "geojson", data: fc })

          const color = RP_COLORS[rp] || "#3b82f6"
          const visibility = currentVisible.has(rp) ? "visible" : "none"

          map.addLayer({
            id: `flood-fill-${rp}`,
            type: "fill",
            source: `${_FLOOD_SOURCE_PREFIX}${rp}`,
            layout: { visibility },
            paint: { "fill-color": color, "fill-opacity": 0.4 },
          })
          map.addLayer({
            id: `flood-outline-${rp}`,
            type: "line",
            source: `${_FLOOD_SOURCE_PREFIX}${rp}`,
            layout: { visibility },
            paint: { "line-color": color, "line-width": 2 },
          })

          // Click popup
          map.on("click", `flood-fill-${rp}`, (e) => {
            const props = e.features[0].properties
            new window.maplibregl.Popup()
              .setLngLat(e.lngLat)
              .setHTML(
                `<strong>${props.return_period_yr}-year flood</strong><br/>${selectedJob?.name || ""}`
              )
              .addTo(map)
          })
          map.on("mouseenter", `flood-fill-${rp}`, () => {
            map.getCanvas().style.cursor = "pointer"
          })
          map.on("mouseleave", `flood-fill-${rp}`, () => {
            map.getCanvas().style.cursor = ""
          })

          // Collect coordinates for fitBounds
          featuresByRP[rp].forEach(f => {
            f.geometry?.coordinates?.[0]?.forEach(c => allCoords.push(c))
          })
        })

        // Fit bounds to union of all return period polygons
        if (allCoords.length > 0) {
          const lons = allCoords.map(c => c[0])
          const lats = allCoords.map(c => c[1])
          map.fitBounds(
            [[Math.min(...lons), Math.min(...lats)], [Math.max(...lons), Math.max(...lats)]],
            { padding: 40 }
          )
        }

        setLoading(false)
      })
      .catch(err => {
        setError(err.message)
        setLoading(false)
      })
  }, [selectedJob]) // eslint-disable-line react-hooks/exhaustive-deps

  // Sync layer visibility when visibleRPs changes
  useEffect(() => {
    const map = mapInstanceRef.current
    if (!map) return
    availableRPs.forEach(rp => {
      const vis = visibleRPs.has(rp) ? "visible" : "none"
      if (map.getLayer(`flood-fill-${rp}`)) {
        map.setLayoutProperty(`flood-fill-${rp}`, "visibility", vis)
      }
      if (map.getLayer(`flood-outline-${rp}`)) {
        map.setLayoutProperty(`flood-outline-${rp}`, "visibility", vis)
      }
    })
  }, [visibleRPs, availableRPs])

  return (
    <div className="bg-white rounded-lg shadow p-4">
      <h2 className="text-lg font-semibold mb-3">
        Flood Extent Map
        {selectedJob && <span className="text-sm font-normal text-gray-500 ml-2">— {selectedJob.name}</span>}
      </h2>
      {!selectedJob && (
        <div className="text-gray-400 text-sm mb-2">Select a completed job to view flood extent</div>
      )}
      {loading && <div className="text-blue-500 text-sm mb-2">Loading flood extent...</div>}
      {error && <div className="text-red-500 text-sm mb-2">⚠️ {error}</div>}
      <div style={{ position: "relative" }}>
        <div ref={mapRef} style={{ height: "420px", borderRadius: "8px", border: "1px solid #e5e7eb" }} />
        {availableRPs.length > 0 && (
          <div style={{
            position: "absolute", top: 10, right: 10, zIndex: 10,
            background: "white", borderRadius: 6, padding: "8px 12px",
            boxShadow: "0 1px 4px rgba(0,0,0,0.2)",
          }}>
            <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 6, color: "#374151" }}>
              Return Period
            </div>
            {availableRPs.map(rp => (
              <label key={rp} style={{
                display: "flex", alignItems: "center", gap: 6,
                marginBottom: 4, cursor: "pointer",
              }}>
                <input
                  type="checkbox"
                  checked={visibleRPs.has(rp)}
                  onChange={() => toggleRP(rp)}
                />
                <span style={{
                  width: 16, height: 12,
                  background: RP_COLORS[rp] || "#3b82f6",
                  opacity: 0.7, borderRadius: 2, display: "inline-block",
                }} />
                <span style={{ fontSize: 12, color: "#374151" }}>{rp}-year</span>
              </label>
            ))}
          </div>
        )}
      </div>
      <div className="text-xs text-gray-400 mt-1">© OpenStreetMap contributors</div>
    </div>
  )
}
