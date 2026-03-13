import { useEffect, useRef, useState } from "react"
import { fetchFloodExtent } from "./api"

// Return period color scale
const RP_COLORS = {
  2:   "#93c5fd",  // light blue
  10:  "#60a5fa",
  25:  "#3b82f6",
  50:  "#2563eb",
  100: "#1d4ed8",  // dark blue
  500: "#1e3a8a",  // very dark blue
}

export default function MapViewer({ selectedJob }) {
  const mapRef = useRef(null)
  const mapInstanceRef = useRef(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

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

  // Load flood extent when selectedJob changes
  useEffect(() => {
    if (!selectedJob || selectedJob.status !== "complete" || !mapInstanceRef.current) return
    setLoading(true)
    setError(null)
    fetchFloodExtent(selectedJob.id)
      .then(geojson => {
        const map = mapInstanceRef.current
        // Remove existing flood layers
        ;["flood-fill", "flood-outline"].forEach(id => {
          if (map.getLayer(id)) map.removeLayer(id)
        })
        if (map.getSource("flood")) map.removeSource("flood")
        // Add new source + layers
        map.addSource("flood", { type: "geojson", data: geojson })
        map.addLayer({
          id: "flood-fill",
          type: "fill",
          source: "flood",
          paint: { "fill-color": "#3b82f6", "fill-opacity": 0.4 }
        })
        map.addLayer({
          id: "flood-outline",
          type: "line",
          source: "flood",
          paint: { "line-color": "#1d4ed8", "line-width": 2 }
        })
        // Fit map to flood extent
        const coords = geojson.features[0]?.geometry?.coordinates[0]
        if (coords && coords.length > 0) {
          const lons = coords.map(c => c[0])
          const lats = coords.map(c => c[1])
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
  }, [selectedJob])

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
      <div ref={mapRef} style={{ height: "420px", borderRadius: "8px", border: "1px solid #e5e7eb" }} />
      <div className="text-xs text-gray-400 mt-1">© OpenStreetMap contributors</div>
    </div>
  )
}
