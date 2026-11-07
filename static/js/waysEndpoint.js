import { map, openInOpenStreetMap } from "./map.js"
import { clearRangeStart, rangeStartWay, removeRangeTo, setRangeStart } from "./waysRemoveRange.js"
import { requestCalcBusRoute } from "./waysRoute.js"

let startMarker = null
let stopMarker = null
let popup = null

export let startWay = null
export let stopWay = null

export function processRelationEndpointData(fetchData) {
    if (fetchData) {
        if (fetchData.fetchMerge) {
            if (startWay && !fetchData.ways[startWay.id]) {
                setStartMarker(null)
                clearPopup()
            }

            if (stopWay && !fetchData.ways[stopWay.id]) {
                setStopMarker(null)
                clearPopup()
            }
        } else {
            setStartMarker(fetchData.startWay)
            setStopMarker(fetchData.stopWay)
        }
    } else {
        setStartMarker(null)
        setStopMarker(null)
        clearPopup()
    }
}

function createEndpointIcon(iconUrl) {
    const size = 24

    return L.icon({
        className: "endpoint-icon",
        iconUrl: iconUrl,
        iconSize: [size, size],
        iconAnchor: [size / 2, size / 2],
    })
}

function onEndpointDataChanged() {
    requestCalcBusRoute()
}

function setStartMarker(way) {
    if (startMarker) {
        startMarker.removeFrom(map)
        startMarker = null
    }

    startWay = way

    if (startWay) {
        startMarker = L.marker(way.midpoint, {
            icon: createEndpointIcon("/static/img/start.webp"),
            interactive: false,
        }).addTo(map)
    }

    onEndpointDataChanged()
}

function setStopMarker(way) {
    if (stopMarker) {
        stopMarker.removeFrom(map)
        stopMarker = null
    }

    stopWay = way

    if (stopWay) {
        stopMarker = L.marker(way.midpoint, {
            icon: createEndpointIcon("/static/img/finish.webp"),
            interactive: false,
        }).addTo(map)
    }

    onEndpointDataChanged()
}

function clearPopup() {
    if (popup) {
        popup.removeFrom(map)
        popup = null
    }
}

const scissorsIcon = `
    <svg class="mb-1" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#dc3545" stroke-width="2"
        stroke-linecap="round" aria-hidden="true">
        <circle cx="6" cy="6" r="3" />
        <circle cx="6" cy="18" r="3" />
        <line x1="8.5" y1="7.5" x2="20" y2="18" />
        <line x1="8.5" y1="16.5" x2="20" y2="6" />
    </svg>`

// the label of the "from - to" removal button: start a range, finish it, or cancel it
const removeRangeLabel = (way) => {
    if (!rangeStartWay) return "Remove <b>FROM</b>"
    if (rangeStartWay.id === way.id) return "Cancel <b>REMOVE</b>"
    return "Remove <b>TO</b>"
}

export function showContextMenu(e, way) {
    clearPopup()

    popup = L.popup(e.latlng, {
        content: `
            <div class="btn-group text-center">
                <button class="btn btn-sm btn-light d-flex flex-column align-items-center" id="ep-set-start">
                    <img class="mb-1" src="/static/img/start.webp" width="24" alt="Start icon">
                    <div>Set <b>START</b></div>
                </button>
                <button class="btn btn-sm btn-light d-flex flex-column align-items-center" id="ep-set-stop">
                    <img class="mb-1" src="/static/img/finish.webp" width="24" alt="Finish icon">
                    <div>Set <b>END</b></div>
                </button>
                <button class="btn btn-sm btn-light d-flex flex-column align-items-center" id="ep-remove-range">
                    ${scissorsIcon}
                    <div>${removeRangeLabel(way)}</div>
                </button>
                <button class="btn btn-sm btn-light d-flex flex-column align-items-center" id="ep-open-osm">
                    <img class="mb-1" src="/static/img/brands/openstreetmap.webp" width="24" alt="OpenStreetMap logo">
                    <div>Inspect</div>
                </button>
            </div>`,
        closeButton: false,
        className: "popup-sm",
    }).openOn(map)

    const setStartButton = document.getElementById("ep-set-start")
    const setStopButton = document.getElementById("ep-set-stop")
    const removeRangeButton = document.getElementById("ep-remove-range")
    const openOsmButton = document.getElementById("ep-open-osm")

    setStartButton.onclick = () => {
        setStartMarker(way)
        popup.close()
    }

    setStopButton.onclick = () => {
        setStopMarker(way)
        popup.close()
    }

    removeRangeButton.onclick = () => {
        if (!rangeStartWay) setRangeStart(way)
        else if (rangeStartWay.id === way.id) clearRangeStart()
        else removeRangeTo(way)
        popup.close()
    }

    openOsmButton.onclick = () => {
        const id = way.id.split("_")[0]
        openInOpenStreetMap(`way/${id}`)
        popup.close()
    }
}
