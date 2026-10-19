import { updateBusStopsVisibility } from "./busStopsLayer.js"
import { downloadTrigger } from "./downloadTriggers.js"
import { map } from "./map.js"
import { routeType } from "./menu.js"
import { createElementFromHTML } from "./utils.js"
import { showContextMenu, startWay, stopWay } from "./waysEndpoint.js"
import { requestCalcBusRoute } from "./waysRoute.js"

export let waysData = null
export let waysRBush = null

const editLegend = document.getElementById("edit-legend")

// member way colors by role: main (both directions), forward/backward (one direction only, shown with arrows),
// and branches (alternative, excursion, ...)
const roleStyles = {
    "": { name: "Main", color: "orangered", hoverColor: "darkred" },
    forward: { name: "Forward", color: "#1e6fd9", hoverColor: "#0b3f85" },
    backward: { name: "Backward", color: "#8e44ad", hoverColor: "#5b2c6f" },
}
const branchStyle = { color: "#2e8b57", hoverColor: "#1b5e20" }

export const getRoleStyle = (role) => roleStyles[role ?? ""] ?? { name: role, ...branchStyle }

// member ways are drawn above non-member ways, markers of short ways above both
map.createPane("nonMemberWays").style.zIndex = 398
map.createPane("memberWays").style.zIndex = 399
map.createPane("shortWays").style.zIndex = 400

const renderers = {
    nonMemberWays: L.canvas({ padding: 0, pane: "nonMemberWays" }),
    memberWays: L.canvas({ padding: 0, pane: "memberWays" }),
    shortWays: L.canvas({ padding: 0, pane: "shortWays" }),
}

const waysLayer = L.layerGroup().addTo(map)

// ways are picked by our own hit test instead of leaflet's: leaflet picks the way drawn last,
// we pick the nearest one, so that short ways squeezed between long ones are selectable
const hitTolerance = 12 // px
// ways not much further away than the nearest one compete with it and the shortest one wins
const hitTieTolerance = 3 // px
// ways shorter than this on screen get a marker to make them visible and easy to hit
const shortWayLength = 14 // px
const shortWayRadius = 6 // px

let idGroupMap = new Map()
let hitRBush = null
let hoveredWayId = null

export function processRelationWaysData(fetchData) {
    if (fetchData) {
        if (fetchData.fetchMerge) {
            const memberMap = new Map()

            if (waysData) {
                for (const way of Object.values(waysData)) {
                    memberMap.set(way.id, way)
                    memberMap.set(way.id.split("_")[0], way)
                }
            }

            waysData = fetchData.ways

            for (const way of Object.values(waysData)) {
                const memberCandidates = [memberMap.get(way.id), memberMap.get(way.id.split("_")[0])]
                const previous = memberCandidates.find((m) => m !== undefined)

                way.member = previous?.member || false
                way.role = previous?.role ?? way.role ?? ""
            }
        } else {
            waysData = fetchData.ways
        }
    } else waysData = null

    onWaysDataChanged()

    if (waysData && !fetchData?.fetchMerge) {
        fitToBounds(fetchData.bounds)
    }
}

function createWaysRBush() {
    if (!waysData) return null

    const maxDistanceMeters = 250
    const maxDistance = maxDistanceMeters / 111111
    const segmentBoundingBoxes = []

    for (const way of Object.values(waysData)) {
        if (!way.member) continue

        for (let i = 0; i < way.latLngs.length - 1; i++) {
            const start = way.latLngs[i]
            const end = way.latLngs[i + 1]

            const bbox = {
                minX: Math.min(start[0], end[0]) - maxDistance,
                minY: Math.min(start[1], end[1]) - maxDistance,
                maxX: Math.max(start[0], end[0]) + maxDistance,
                maxY: Math.max(start[1], end[1]) + maxDistance,
            }

            segmentBoundingBoxes.push(bbox)
        }
    }

    const tree = rbush()
    tree.load(segmentBoundingBoxes)
    return tree
}

// segments of all drawn ways, used by the hit test
function createHitRBush() {
    if (idGroupMap.size === 0) return null

    const segments = []

    for (const { way } of idGroupMap.values()) {
        for (let i = 0; i < way.latLngs.length - 1; i++) {
            const start = way.latLngs[i]
            const end = way.latLngs[i + 1]

            segments.push({
                minX: Math.min(start[0], end[0]),
                minY: Math.min(start[1], end[1]),
                maxX: Math.max(start[0], end[0]),
                maxY: Math.max(start[1], end[1]),
                wayId: way.id,
                start: start,
                end: end,
            })
        }
    }

    const tree = rbush()
    tree.load(segments)
    return tree
}

function onWaysDataChanged() {
    waysRBush = createWaysRBush()

    updateWaysVisibility()
    updateRoleLegend()
    updateBusStopsVisibility()

    requestCalcBusRoute()
}

function updateRoleLegend() {
    const roles = new Set()

    if (waysData && routeType === "bicycle") {
        for (const way of Object.values(waysData)) {
            if (way.member) roles.add(way.role ?? "")
        }
    }

    // only show the legend when the route has split sections or branches
    const visible = roles.size > 1 || (roles.size === 1 && !roles.has(""))
    editLegend.classList.toggle("d-none", !visible)
    editLegend.innerHTML = ""

    if (!visible) return

    // main, forward, backward, then the branches alphabetically
    const rank = (role) => {
        const index = ["", "forward", "backward"].indexOf(role)
        return index === -1 ? 3 : index
    }
    const sortedRoles = [...roles].sort((a, b) => rank(a) - rank(b) || a.localeCompare(b))

    for (const role of sortedRoles) {
        const style = getRoleStyle(role)
        const item = createElementFromHTML(`<span><span class="role-swatch"></span></span>`)
        item.querySelector(".role-swatch").style.background = style.color
        item.appendChild(document.createTextNode(style.name || role))
        editLegend.appendChild(item)
    }
}

// the role of a newly added way: the role of its connected member ways, if they all agree
const inheritRole = (way) => {
    const roles = new Set()

    for (const connectedWayId of way.connectedTo) {
        const connectedWay = waysData[connectedWayId]
        if (connectedWay?.member) roles.add(connectedWay.role ?? "")
    }

    return roles.size === 1 ? [...roles][0] : ""
}

function updateWaysVisibility() {
    if (!waysData) {
        idGroupMap = new Map()
        hitRBush = null
        waysLayer.clearLayers()
        setHoveredWay(null)
        return
    }

    const visibleWays = new Set()

    for (const way of Object.values(waysData)) {
        if (!way.member) continue

        // add the way itself ..
        visibleWays.add(way.id)

        // .. and all the ways it is connected to
        for (const connectedWayId of way.connectedTo) {
            visibleWays.add(connectedWayId)
        }
    }

    for (const wayId of visibleWays) {
        addWay(waysData[wayId])
    }

    for (const wayId of idGroupMap.keys()) {
        if (!visibleWays.has(wayId)) {
            removeGroupFromLayers(wayId)
        }
    }

    hitRBush = createHitRBush()
}

const addWay = (way) => {
    if (idGroupMap.has(way.id)) return

    const roleStyle = getRoleStyle(way.role)
    const lineColor = way.member ? roleStyle.color : "#444"
    const lineHoverColor = way.member ? roleStyle.hoverColor : "#000"
    const lineWeight = way.member ? 7 : 5
    const opacity = way.member ? 1 : 0.55
    const renderer = way.member ? renderers.memberWays : renderers.nonMemberWays

    // forward/backward ways are followed only along/against their direction: show it with arrows
    const directional = way.member && (way.role === "forward" || way.role === "backward")
    const lineLatLngs = way.role === "backward" && directional ? way.latLngs.slice().reverse() : way.latLngs

    const line = L.polyline(lineLatLngs, {
        renderer: renderer,
        interactive: false,
        color: lineColor,
        opacity: opacity,
        weight: lineWeight,
    })

    if (way.oneway || directional) {
        line.arrowheads({
            size: `${Math.min(15, way.length * 0.6)}m`,
            frequency: way.length > 40 ? "40m" : "endonly",
            yawn: 40,
            stroke: false,
            fill: true,
            fillColor: lineHoverColor,
            fillOpacity: opacity,
        })
    }

    const marker = L.circleMarker(way.midpoint, {
        renderer: renderers.shortWays,
        interactive: false,
        radius: shortWayRadius,
        color: lineHoverColor,
        opacity: opacity,
        weight: 2,
        fillColor: lineColor,
        fillOpacity: opacity,
    })

    const group = {
        way: way,
        line: line,
        marker: marker,
        lineColor: lineColor,
        lineHoverColor: lineHoverColor,
        lineWeight: lineWeight,
    }

    addGroupToLayers(way.id, group)
    if (way.id === hoveredWayId) setGroupHovered(group, true)
}

export const setWayRole = (wayId, role) => {
    waysData[wayId].role = role
    removeGroupFromLayers(wayId)
    onWaysDataChanged()
}

export const removeMembersList = (wayIds) => {
    for (const wayId of wayIds) {
        if (wayId === startWay?.id || wayId === stopWay?.id) continue

        waysData[wayId].member = false
        removeGroupFromLayers(wayId)
    }

    onWaysDataChanged()
}

const addGroupToLayers = (id, group) => {
    if (idGroupMap.has(id)) return

    waysLayer.addLayer(group.line)
    updateShortWayMarker(group)

    idGroupMap.set(id, group)
}

const removeGroupFromLayers = (id) => {
    if (!idGroupMap.has(id)) return

    const group = idGroupMap.get(id)

    waysLayer.removeLayer(group.line)
    waysLayer.removeLayer(group.marker)

    idGroupMap.delete(id)
}

const isShortOnScreen = (way) => {
    let length = 0
    let previous = map.latLngToLayerPoint(way.latLngs[0])

    for (let i = 1; i < way.latLngs.length; i++) {
        const current = map.latLngToLayerPoint(way.latLngs[i])
        length += previous.distanceTo(current)
        if (length >= shortWayLength) return false
        previous = current
    }

    return true
}

const updateShortWayMarker = (group) => {
    if (isShortOnScreen(group.way)) waysLayer.addLayer(group.marker)
    else waysLayer.removeLayer(group.marker)
}

map.on("zoomend", () => {
    for (const group of idGroupMap.values()) updateShortWayMarker(group)
})

// the nearest way within the tolerance, preferring shorter ways when several are about as near
const findWayAt = (containerPoint) => {
    if (!hitRBush) return null

    const tolerance = L.point(hitTolerance, hitTolerance)
    const corner1 = map.containerPointToLatLng(containerPoint.subtract(tolerance))
    const corner2 = map.containerPointToLatLng(containerPoint.add(tolerance))

    const segments = hitRBush.search({
        minX: Math.min(corner1.lat, corner2.lat),
        minY: Math.min(corner1.lng, corner2.lng),
        maxX: Math.max(corner1.lat, corner2.lat),
        maxY: Math.max(corner1.lng, corner2.lng),
    })

    const distances = new Map()

    for (const segment of segments) {
        const start = map.latLngToContainerPoint(segment.start)
        const end = map.latLngToContainerPoint(segment.end)
        const distance = L.LineUtil.pointToSegmentDistance(containerPoint, start, end)

        if (distance > hitTolerance) continue

        const previous = distances.get(segment.wayId)
        if (previous === undefined || distance < previous) distances.set(segment.wayId, distance)
    }

    if (distances.size === 0) return null

    const nearest = Math.min(...distances.values())
    let best = null

    for (const [wayId, distance] of distances) {
        if (distance > nearest + hitTieTolerance) continue

        const way = waysData[wayId]
        if (!best || way.length < best.length) best = way
    }

    return best
}

const setGroupHovered = (group, hovered) => {
    group.line.setStyle({
        color: hovered ? group.lineHoverColor : group.lineColor,
        weight: hovered ? group.lineWeight + 2 : group.lineWeight,
    })

    group.marker.setStyle({
        fillColor: hovered ? group.lineHoverColor : group.lineColor,
        radius: hovered ? shortWayRadius + 1 : shortWayRadius,
    })
}

const setHoveredWay = (wayId) => {
    if (wayId === hoveredWayId) return

    const previous = idGroupMap.get(hoveredWayId)
    if (previous) setGroupHovered(previous, false)

    hoveredWayId = wayId

    const current = idGroupMap.get(wayId)
    if (current) setGroupHovered(current, true)

    map.getContainer().style.cursor = current ? "pointer" : ""
}

// ignore events on markers, popups and tooltips: they handle their own clicks
const isWayTarget = (target) =>
    target.closest(".leaflet-map-pane") &&
    !target.closest(".leaflet-marker-pane, .leaflet-popup-pane, .leaflet-tooltip-pane")

// the hover listens to the dom directly: leaflet throttles mousemove over a canvas and may drop the last one
L.DomEvent.on(map.getContainer(), "mousemove", (e) => {
    if (map.dragging.moving()) return

    const way = isWayTarget(e.target) ? findWayAt(map.mouseEventToContainerPoint(e)) : null
    setHoveredWay(way?.id ?? null)
})

L.DomEvent.on(map.getContainer(), "mouseleave", () => setHoveredWay(null))
map.on("dragstart", () => setHoveredWay(null))

map.on("click", (e) => {
    if (!isWayTarget(e.originalEvent.target)) return

    const way = findWayAt(e.containerPoint)
    if (!way) return
    if (way.id === startWay?.id || way.id === stopWay?.id) return

    const newMember = !way.member

    waysData[way.id].member = newMember
    if (newMember) waysData[way.id].role = inheritRole(way)
    removeGroupFromLayers(way.id)
    onWaysDataChanged()

    if (newMember) downloadTrigger(way.id)

    setHoveredWay(findWayAt(e.containerPoint)?.id ?? null)
})

map.on("contextmenu", (e) => {
    if (!isWayTarget(e.originalEvent.target)) return

    const way = findWayAt(e.containerPoint)
    if (way?.member) showContextMenu(e, way)
})

function fitToBounds(bounds) {
    const southWest = L.latLng(bounds.minlat, bounds.minlon)
    const northEast = L.latLng(bounds.maxlat, bounds.maxlon)
    const latLngBounds = L.latLngBounds(southWest, northEast)
    map.fitBounds(latLngBounds)
}
