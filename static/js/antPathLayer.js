import { map } from "./map.js"

const antPathLayer = L.layerGroup().addTo(map)

export function clearAntPath() {
    antPathLayer.clearLayers()
}

const addAntPath = (latLngs) => {
    if (latLngs.length < 2) return

    L.polyline
        .antPath(latLngs, {
            delay: 3500,
            weight: 4,
            dashArray: [22, 20],
            color: "transparent",
            pulseColor: "#ffffff",
            opacity: 0.75,
            interactive: false,
        })
        .addTo(antPathLayer)
}

export function processRouteAntPath(route) {
    clearAntPath()

    // the main (forward) path
    addAntPath(route.latLngs)

    // backward sections and branches, animated in their direction of travel
    for (const section of route.sections ?? []) {
        addAntPath(section.latLngs)
    }
}
