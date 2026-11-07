import { map } from "./map.js"
import { showMessage } from "./messageBox.js"
import { removeMembersList, waysData } from "./waysLayer.js"

// remove all member ways "from - to": the user marks the first way, then the last way,
// and every member way on the shortest connection between them is removed

// above the member ways, so the marked way stays visible
map.createPane("rangeStart").style.zIndex = 450

const rangeLayer = L.layerGroup().addTo(map)

export let rangeStartWay = null

export function setRangeStart(way) {
    clearRangeStart()
    rangeStartWay = way

    L.polyline(way.latLngs, {
        color: "#ffc107",
        weight: 13,
        opacity: 0.9,
        dashArray: "8 8",
        interactive: false,
        pane: "rangeStart",
    })
        .bindTooltip("Remove from here: right-click the last way to remove", {
            permanent: true,
            direction: "top",
        })
        .addTo(rangeLayer)
}

export function clearRangeStart() {
    rangeStartWay = null
    rangeLayer.clearLayers()
}

// forget the range start once its way is gone or no longer a member,
// and follow the way object when merged fetch data replaces it
export function validateRangeStart() {
    if (!rangeStartWay) return

    const way = waysData?.[rangeStartWay.id]
    if (!way?.member) clearRangeStart()
    else rangeStartWay = way
}

export function removeRangeTo(endWay) {
    const startWay = rangeStartWay
    if (!startWay) return

    const path = findMemberPath(startWay.id, endWay.id)

    if (!path) {
        showMessage(
            "warning",
            "Cannot remove range",
            "The two ways are not connected by member ways. Remove the ways individually instead.",
        )
        return
    }

    clearRangeStart()
    removeMembersList(path)
}

// shortest path (by length) from startId to endId, only through member ways
const findMemberPath = (startId, endId) => {
    if (!waysData?.[startId]?.member || !waysData?.[endId]?.member) return null

    const distances = new Map([[startId, waysData[startId].length]])
    const previous = new Map()
    const visited = new Set()

    while (true) {
        // member counts are small, a linear scan for the closest way is fast enough
        let currentId = null
        let currentDistance = Number.POSITIVE_INFINITY

        for (const [wayId, distance] of distances) {
            if (!visited.has(wayId) && distance < currentDistance) {
                currentId = wayId
                currentDistance = distance
            }
        }

        if (currentId === null) return null
        if (currentId === endId) break

        visited.add(currentId)

        for (const connectedId of waysData[currentId].connectedTo) {
            const connectedWay = waysData[connectedId]
            if (!connectedWay?.member || visited.has(connectedId)) continue

            const distance = currentDistance + connectedWay.length
            if (distance < (distances.get(connectedId) ?? Number.POSITIVE_INFINITY)) {
                distances.set(connectedId, distance)
                previous.set(connectedId, currentId)
            }
        }
    }

    const path = [endId]
    while (path[path.length - 1] !== startId) {
        path.push(previous.get(path[path.length - 1]))
    }
    return path.reverse()
}

document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") clearRangeStart()
})
