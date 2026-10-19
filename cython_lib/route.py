import asyncio
from collections import defaultdict
from collections.abc import Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from functools import partial
from itertools import chain
from math import inf
from typing import NamedTuple, Self

import cython

from cython_lib.geoutils import haversine_distance
from models.element_id import ElementId, split_element_id
from models.fetch_relation import FetchRelationBusStopCollection, FetchRelationElement
from models.final_route import FinalRoute, FinalRouteSection, FinalRouteWay
from relation_builder import SortedBusEntry, sort_bus_on_path
from utils import print_run_time

if cython.compiled:
    from cython.cimports.libc.math import acos, pi

    print(f'{__name__}: 🐇 compiled')
else:
    from math import acos, pi

    print(f'{__name__}: 🐌 not compiled')


@cython.cfunc
def _degrees(x: cython.double) -> cython.double:
    return x * (180 / pi)


BOOL_START = True
BOOL_END = False

VISITED_LIMIT = 2
MAX_LOOP_LENGTH = 1000
MAX_AFTER_FINISH_LENGTH = 1000
MAX_EXTRA_DISTANCE_TO_CONVERT = 1000
MAX_PATH_LENGTH_FACTOR = 2.2


class GraphKey(NamedTuple):
    way_id: ElementId
    is_start: bool


class GraphValue(NamedTuple):
    intersection_id: int
    connected_to: tuple[GraphKey, ...]


class StackElement(NamedTuple):
    path: tuple[GraphKey, ...]
    visited_bus_stops: dict[ElementId, int]
    almost_visited_bus_stops: dict[ElementId, int]
    intersection_bus_stops_snapshot: dict[int, tuple[GraphKey, int]]
    length: float
    complete_path: set[ElementId]
    complete_length: float
    angle_sum: float = 0
    loop_length: float = 0
    after_finish_length: float = 0
    roundabout_enter: GraphKey | None = None


class BestPath(NamedTuple):
    path: tuple[GraphKey, ...]
    visited_bus_stops: dict[ElementId, int]
    bus_stops_count: int
    almost_bus_stops_count: int
    length: float
    complete_path: set[ElementId]
    complete_length: float
    angle_sum: float

    @classmethod
    def zero(cls) -> Self:
        return cls(
            path=(),
            visited_bus_stops={},
            bus_stops_count=0,
            almost_bus_stops_count=0,
            length=0,
            complete_path=set(),
            complete_length=0,
            angle_sum=0,
        )

    def select_best(self, other: Self) -> Self:
        complete_length_diff: cython.double = other.complete_length - self.complete_length
        if abs(complete_length_diff) < 0.1:  # avoid floating point errors
            complete_length_diff = 0

        # more complete
        if complete_length_diff > 0:
            return other
        if complete_length_diff < 0:
            return self

        length_diff: cython.double = other.length - self.length
        if abs(length_diff) < 0.1:  # avoid floating point errors
            length_diff = 0

        bus_stops_count_diff: cython.int = other.bus_stops_count - self.bus_stops_count
        almost_bus_stops_count_diff: cython.int = other.almost_bus_stops_count - self.almost_bus_stops_count

        if bus_stops_count_diff and bus_stops_count_diff + almost_bus_stops_count_diff == 0:
            max_convert_distance: cython.int = MAX_EXTRA_DISTANCE_TO_CONVERT * bus_stops_count_diff

            if length_diff < max_convert_distance < 0:
                return other
            if 0 < max_convert_distance < length_diff:
                return self

        # more bus stops
        if bus_stops_count_diff > 0:
            return other
        if bus_stops_count_diff < 0:
            return self

        if almost_bus_stops_count_diff > 0:
            return other
        if almost_bus_stops_count_diff < 0:
            return self

        # shorter path
        if length_diff < 0:
            return other
        if length_diff > 0:
            return self

        # simpler angles
        if self.angle_sum > other.angle_sum:
            return other
        if self.angle_sum < other.angle_sum:
            return self

        return self  # paths are equal


class BestPathCollection(NamedTuple):
    invalid: BestPath
    valid: BestPath

    def merge(self, other: Self, ways: dict[ElementId, FetchRelationElement]) -> 'BestPathCollection':
        return BestPathCollection(
            invalid=self.invalid.select_best(other.invalid),
            valid=self.valid.select_best(other.valid),
        )


def _is_directional(way: FetchRelationElement) -> bool:
    # the route follows the way only along (forward) or against (backward) its direction
    return way.role in {'forward', 'backward'}


def _can_enter_at_start(way: FetchRelationElement) -> bool:
    # the role takes precedence over the oneway tag
    if _is_directional(way):
        return way.role == 'forward'
    return True


def _can_enter_at_end(way: FetchRelationElement) -> bool:
    if _is_directional(way):
        return way.role == 'backward'
    return not way.oneway


def build_graph(ways: dict[ElementId, FetchRelationElement]) -> dict[GraphKey, GraphValue]:
    convert_graph: dict[GraphKey, list[GraphKey]] = {}

    for way_id, way in ways.items():

        def find_connections_at(latlon: tuple[float, float]) -> list[GraphKey]:
            connections: list[GraphKey] = []
            for connected_way_id in way.connectedTo:  # noqa: B023
                connected_way = ways.get(connected_way_id)
                if not connected_way:  # skip non-member ways
                    continue
                connected_start = connected_way.latLngs[0]
                connected_end = connected_way.latLngs[-1]
                if latlon == connected_start and _can_enter_at_start(connected_way):
                    connections.append(GraphKey(connected_way_id, BOOL_START))
                elif latlon == connected_end and _can_enter_at_end(connected_way):
                    connections.append(GraphKey(connected_way_id, BOOL_END))
            return connections

        # Build neighbors for START
        start_neighbors = find_connections_at(way.latLngs[0])
        if way.turn_in_place_start and not way.oneway and not way.role:
            # U-turn at START: also connect to ways at END
            start_neighbors.extend(find_connections_at(way.latLngs[-1]))

        # Build neighbors for END
        end_neighbors = find_connections_at(way.latLngs[-1])
        if way.turn_in_place_end and not way.role:
            # U-turn at END: also connect to ways at START
            end_neighbors.extend(find_connections_at(way.latLngs[0]))

        convert_graph[GraphKey(way_id, BOOL_START)] = start_neighbors
        convert_graph[GraphKey(way_id, BOOL_END)] = end_neighbors

    intersection_num: cython.int = -1
    result: dict[GraphKey, GraphValue] = {}
    while convert_graph:
        intersection_num += 1
        key, neighbors = convert_graph.popitem()
        result[key] = GraphValue(intersection_num, tuple(neighbors))
        for neighbor in neighbors:
            if neighbor in convert_graph:
                # normal convert
                neighbor_neighbors = convert_graph.pop(neighbor)
                result[neighbor] = GraphValue(intersection_num, tuple(neighbor_neighbors))
            else:
                # merge convert (happens due to oneway)
                result[neighbor] = result[neighbor]._replace(intersection_id=intersection_num)
    return result


def angle_between_ways(
    latlons1: Sequence[tuple[cython.double, cython.double]],
    latlons2: Sequence[tuple[cython.double, cython.double]],
) -> cython.double:
    start1 = latlons1[0]
    end1 = latlons1[-1]
    start2 = latlons2[0]
    end2 = latlons2[-1]

    # consider very end segments for angle calculation
    if end1 == start2:
        start1 = latlons1[-2]
        end2 = latlons2[1]

        d12: cython.double = haversine_distance(start1, end1)
        d23: cython.double = haversine_distance(end1, end2)
        d13: cython.double = haversine_distance(start1, end2)

    elif end1 == end2:
        start1 = latlons1[-2]
        start2 = latlons2[-2]

        d12: cython.double = haversine_distance(start1, end1)
        d23: cython.double = haversine_distance(end1, start2)
        d13: cython.double = haversine_distance(start1, start2)

    elif start1 == start2:
        end1 = latlons1[1]
        end2 = latlons2[1]

        d12: cython.double = haversine_distance(start1, end1)
        d23: cython.double = haversine_distance(end1, end2)
        d13: cython.double = haversine_distance(start1, end2)

    elif start1 == end2:
        end1 = latlons1[1]
        start2 = latlons2[-2]

        d12: cython.double = haversine_distance(start1, end1)
        d23: cython.double = haversine_distance(end1, start2)
        d13: cython.double = haversine_distance(start1, start2)

    else:
        raise Exception('Ways are not connected')

    # law of cosines
    cos_angle = (d12 * d12 + d23 * d23 - d13 * d13) / (2 * d12 * d23)
    angle = _degrees(acos(min(max(cos_angle, -1), 1)))
    return angle


def select_neighbors(
    way: FetchRelationElement,
    neighbors: Sequence[GraphKey],
    ways: dict[ElementId, FetchRelationElement],
) -> Sequence[tuple[GraphKey, cython.double]]:
    if not neighbors:
        return ()
    elif len(neighbors) == 1:
        return ((neighbors[0], 0),)

    angles = (
        (
            angle_between_ways(way.latLngs, ways[neighbor.way_id].latLngs),
            neighbor,
        )
        for neighbor in neighbors
    )

    # the angle difference from the straight path
    # TODO: support 0-180 range by utilizing is_start
    angle_differences = tuple(
        (
            neighbor,
            90 - abs(90 - angle),
        )
        for angle, neighbor in angles
    )

    return angle_differences


def get_bus_stops_at(
    neighbor: GraphKey,
    id_sorted_bus_map: dict[ElementId, list[SortedBusEntry]],
) -> tuple[list[SortedBusEntry], list[SortedBusEntry]]:
    neighbor_is_forward = neighbor.is_start

    visited = []
    almost_visited = []

    for sorted_bus in id_sorted_bus_map.get(neighbor.way_id, []):
        if sorted_bus.right_hand_side is None or neighbor_is_forward == sorted_bus.right_hand_side:
            visited.append(sorted_bus)
        else:
            almost_visited.append(sorted_bus)

    if not neighbor_is_forward:
        visited.reverse()
        almost_visited.reverse()

    return visited, almost_visited


def modified_dfs_worker(
    graph: dict[GraphKey, GraphValue],
    ways: dict[ElementId, FetchRelationElement],
    end_way: ElementId,
    id_sorted_bus_map: dict[ElementId, list[SortedBusEntry]],
    stack: list[StackElement],
    best_path: BestPathCollection,
    max_length: cython.double,
    max_iter: cython.int,
) -> tuple[list[StackElement], BestPathCollection]:
    message_ref = [f'Worker with {len(stack)} stack size']
    current_iter = 0

    with print_run_time(message_ref):
        for current_iter in range(1, max_iter + 1):  # noqa: B007
            if not stack:
                break

            s = stack.pop()

            current_key = s.path[-1]
            exit_at_key = current_key._replace(is_start=not current_key.is_start)

            current_best_path = BestPath(
                s.path,
                visited_bus_stops=s.visited_bus_stops | s.almost_visited_bus_stops,
                bus_stops_count=len(s.visited_bus_stops),
                almost_bus_stops_count=len(s.almost_visited_bus_stops),
                length=s.length,
                complete_path=s.complete_path,
                complete_length=s.complete_length,
                angle_sum=s.angle_sum,
            )

            if current_key.way_id == end_way:
                if (replace := best_path.valid.select_best(current_best_path)) == current_best_path:
                    best_path = best_path._replace(valid=replace)
            else:
                if (replace := best_path.invalid.select_best(current_best_path)) == current_best_path:
                    best_path = best_path._replace(invalid=replace)

            current_way = ways[current_key.way_id]
            neighbors = graph[exit_at_key].connected_to
            valid_neighbors = select_neighbors(current_way, neighbors, ways)

            intersection_id = graph[exit_at_key].intersection_id

            if (t := s.intersection_bus_stops_snapshot.get(intersection_id, None)) is not None:
                intersection_bus_stops_count, intersection_visit_count = t
            else:
                intersection_bus_stops_count = None
                intersection_visit_count = 0

            new_intersection_bus_stops_snapshot = s.intersection_bus_stops_snapshot.copy()

            if (intersection_bus_stops_count is None) or (
                intersection_bus_stops_count < len(s.visited_bus_stops) + len(s.almost_visited_bus_stops)
            ):
                new_intersection_visit_count = 1
                new_intersection_bus_stops_snapshot[intersection_id] = (
                    len(s.visited_bus_stops) + len(s.almost_visited_bus_stops),
                    new_intersection_visit_count,
                )
            elif intersection_visit_count < VISITED_LIMIT:
                new_intersection_visit_count = intersection_visit_count + 1
                new_intersection_bus_stops_snapshot[intersection_id] = (
                    intersection_bus_stops_count,
                    new_intersection_visit_count,
                )
            else:
                continue

            for neighbor, neighbor_angle in valid_neighbors:
                neighbor_way = ways[neighbor.way_id]

                # directional ways (split sections) are used at most once, otherwise the path would loop through
                # the return leg of a split section to cover more ways
                if _is_directional(neighbor_way) and neighbor.way_id != end_way and neighbor.way_id in s.complete_path:
                    continue

                new_path = (*s.path, neighbor)

                visited_bus_stops, almost_visited_bus_stops = get_bus_stops_at(neighbor, id_sorted_bus_map)

                if visited_bus_stops or almost_visited_bus_stops:
                    new_visited_bus_stops = s.visited_bus_stops.copy()
                    new_almost_visited_bus_stops = s.almost_visited_bus_stops.copy()

                    for b in visited_bus_stops:
                        new_visited_bus_stops.setdefault(b.bus_stop_collection.best.id, len(new_path))

                    for b in almost_visited_bus_stops:
                        new_almost_visited_bus_stops.setdefault(b.bus_stop_collection.best.id, len(new_path))

                    new_almost_visited_bus_stops = {
                        k: v for k, v in new_almost_visited_bus_stops.items() if k not in new_visited_bus_stops
                    }
                else:
                    new_visited_bus_stops = s.visited_bus_stops
                    new_almost_visited_bus_stops = s.almost_visited_bus_stops

                new_length = s.length + neighbor_way.length

                if new_length > max_length:
                    continue

                if neighbor_way.id not in s.complete_path:
                    new_complete_path = s.complete_path.copy()
                    new_complete_path.add(neighbor_way.id)
                    new_complete_length = s.complete_length + neighbor_way.length
                else:
                    new_complete_path = s.complete_path
                    new_complete_length = s.complete_length

                # roundabout looping and exits are free
                if current_way.roundabout:  # noqa: SIM108
                    new_angle_sum = s.angle_sum
                else:
                    new_angle_sum = s.angle_sum + neighbor_angle

                if new_intersection_visit_count > 1:  # noqa: SIM108
                    new_loop_length = s.loop_length + neighbor_way.length
                else:
                    new_loop_length = 0

                # stop path if too long loop
                if new_loop_length > MAX_LOOP_LENGTH:
                    continue

                if s.after_finish_length > 0 or neighbor.way_id == end_way:
                    new_after_finish_length = s.after_finish_length + neighbor_way.length
                else:
                    new_after_finish_length = 0

                # stop path if too long after finish
                if new_after_finish_length > MAX_AFTER_FINISH_LENGTH:
                    continue

                if neighbor_way.roundabout:
                    if s.roundabout_enter:
                        # stop path if looping in roundabout
                        if s.roundabout_enter == neighbor:
                            continue
                        else:
                            new_roundabout_enter = s.roundabout_enter
                    else:
                        new_roundabout_enter = neighbor
                else:
                    new_roundabout_enter = None

                stack.append(
                    StackElement(
                        path=new_path,
                        visited_bus_stops=new_visited_bus_stops,
                        almost_visited_bus_stops=new_almost_visited_bus_stops,
                        intersection_bus_stops_snapshot=new_intersection_bus_stops_snapshot,
                        length=new_length,
                        complete_path=new_complete_path,
                        complete_length=new_complete_length,
                        angle_sum=new_angle_sum,
                        loop_length=new_loop_length,
                        after_finish_length=new_after_finish_length,
                        roundabout_enter=new_roundabout_enter,
                    )
                )

        message_ref[0] += f' and {current_iter} iterations'

    return stack, best_path


async def modified_dfs(
    graph: dict[GraphKey, GraphValue],
    ways: dict[ElementId, FetchRelationElement],
    start_way: ElementId,
    end_way: ElementId,
    id_sorted_bus_map: dict[ElementId, list[SortedBusEntry]],
    executor: ProcessPoolExecutor,
    n_processes: cython.int,
) -> BestPath:
    max_length = MAX_PATH_LENGTH_FACTOR * sum(w.length for w in ways.values())

    start_start_key = GraphKey(start_way, BOOL_START)
    start_end_key = GraphKey(start_way, BOOL_END)

    def init_stack_element(key: GraphKey) -> StackElement:
        intersection_id = graph[key].intersection_id
        visited_bus_stops, almost_visited_bus_stops = get_bus_stops_at(key, id_sorted_bus_map)

        return StackElement(
            path=(key,),
            visited_bus_stops={b.bus_stop_collection.best.id: 1 for b in visited_bus_stops},
            almost_visited_bus_stops={b.bus_stop_collection.best.id: 1 for b in almost_visited_bus_stops},
            intersection_bus_stops_snapshot={
                intersection_id: (len(visited_bus_stops) + len(almost_visited_bus_stops), 1)
            },
            length=ways[start_way].length,
            complete_path={key.way_id},
            complete_length=ways[start_way].length,
        )

    stack: list[StackElement] = [
        init_stack_element(start_start_key),
        init_stack_element(start_end_key),
    ]

    best_path = BestPathCollection(valid=BestPath.zero(), invalid=BestPath.zero())

    # for reference:
    # AMD Ryzen 9 5950X: 10,000 iterations in ~ 0.1s
    sync_max_iter = 3000  # .03s
    async_max_iter = 10000  # .10s

    # run a few iterations synchronously to get a head start
    stack, best_path = modified_dfs_worker(
        graph,
        ways,
        end_way,
        id_sorted_bus_map,
        stack,
        best_path,
        max_length=max_length,
        max_iter=sync_max_iter,
    )

    async def worker(
        stack_slice: list[StackElement],
        best_path: BestPathCollection,
        max_iter: cython.int,
    ) -> tuple[list[StackElement], BestPathCollection]:
        loop = asyncio.get_running_loop()

        return await loop.run_in_executor(
            executor,
            partial(
                modified_dfs_worker,
                graph,
                ways,
                end_way,
                id_sorted_bus_map,
                stack_slice,
                best_path,
                max_length=max_length,
                max_iter=max_iter,
            ),
        )

    tasks: list[asyncio.Task] = []
    while stack or tasks:
        stack_slices_len_target = n_processes - len(tasks)
        stack_slice_size_target, remainder = divmod(len(stack), stack_slices_len_target)
        stack_slices: list[list[StackElement]] = []

        for i in range(stack_slices_len_target):
            current_slice_size = stack_slice_size_target + (1 if i < remainder else 0)
            if current_slice_size == 0:
                break

            stack_slices.append(stack[:current_slice_size])
            stack = stack[current_slice_size:]

        assert not stack, 'Stack must be empty after slicing'

        tasks.extend(
            asyncio.create_task(
                worker(
                    stack_slice,
                    best_path,
                    async_max_iter,
                )
            )
            for stack_slice in stack_slices
        )

        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

        for task in done:
            stack_slice, best_path_slice = task.result()
            stack += stack_slice
            best_path = best_path.merge(best_path_slice, ways)

        tasks = list(pending)

    return best_path.valid if best_path.valid.path else best_path.invalid


def _path_latlngs(route_ways: Sequence[FinalRouteWay]) -> tuple[tuple[float, float], ...]:
    route_latlons_gen = (
        route_way.way.latLngs[::-1] if route_way.reversed_latLngs else route_way.way.latLngs for route_way in route_ways
    )
    return tuple(chain.from_iterable(latlons if i == 0 else latlons[1:] for i, latlons in enumerate(route_latlons_gen)))


def finalize_route(
    best_path: BestPath,
    ways: dict[ElementId, FetchRelationElement],
    bus_stop_collections: Sequence[FetchRelationBusStopCollection],
    tags: dict[str, str],
) -> FinalRoute:
    route_ways = tuple(
        FinalRouteWay(
            way=ways[key.way_id],
            reversed_latLngs=not key.is_start,
        )
        for key in best_path.path
    )

    route_latlons = _path_latlngs(route_ways)
    route_latlons_set = set(route_latlons)

    id_collection_map = {collection.best.id: collection for collection in bus_stop_collections}

    route_bus_stops = []

    for stop_id, _ in sorted(best_path.visited_bus_stops.items(), key=lambda x: x[1]):
        collection = id_collection_map[stop_id]

        if collection.stop is not None and collection.stop.latLng not in route_latlons_set:
            collection = replace(collection, stop=None)

        if collection.platform is None and collection.stop is None:
            continue

        route_bus_stops.append(collection)

    return FinalRoute(
        ways=route_ways,
        latLngs=route_latlons,
        busStops=tuple(route_bus_stops),
        tags=tags,
    )


async def calc_bus_route(
    ways_members: dict[ElementId, FetchRelationElement],
    start_way: ElementId,
    end_way: ElementId,
    bus_stop_collections: Sequence[FetchRelationBusStopCollection],
    tags: dict[str, str],
    executor: ProcessPoolExecutor,
    n_processes: cython.int,
) -> FinalRoute:
    with print_run_time('Sorting bus stops'):
        sorted_buses = sort_bus_on_path(bus_stop_collections, ways_members.values())

    id_sorted_bus_map: dict[ElementId, list[SortedBusEntry]] = {}

    for sorted_bus in sorted_buses:
        id_sorted_bus_map.setdefault(sorted_bus.neighbor_id, []).append(sorted_bus)

    with print_run_time('Building graph'):
        graph = build_graph(ways_members)

    with print_run_time('Calculating route'):
        best_path = await modified_dfs(
            graph,
            ways_members,
            start_way,
            end_way,
            id_sorted_bus_map,
            executor,
            n_processes,
        )

    return finalize_route(best_path, ways_members, bus_stop_collections, tags)


# --- routes without stops, with split sections (forward/backward) and branches (alternative, excursion, ...) ---


def _endpoints_distance(way1: FetchRelationElement, way2: FetchRelationElement) -> float:
    return min(
        haversine_distance(latLng1, latLng2)
        for latLng1 in (way1.latLngs[0], way1.latLngs[-1])
        for latLng2 in (way2.latLngs[0], way2.latLngs[-1])
    )


def _graph_endpoint_ids(ways: Mapping[ElementId, FetchRelationElement]) -> list[ElementId]:
    """Ways which are connected to at most one other way within the given graph."""
    return [
        way_id
        for way_id, way in ways.items()
        if sum(1 for connected_way_id in way.connectedTo if connected_way_id in ways) <= 1
    ]


def _pick_endpoint(target: FetchRelationElement, ways: Mapping[ElementId, FetchRelationElement]) -> ElementId:
    """Pick the way to start/end at within the given graph: the target itself, or the nearest dead end."""
    if target.id in ways:
        return target.id
    candidates = _graph_endpoint_ids(ways) or list(ways)
    return min(candidates, key=lambda way_id: _endpoints_distance(target, ways[way_id]))


def _connected_component(
    seed: ElementId, ways: Mapping[ElementId, FetchRelationElement]
) -> dict[ElementId, FetchRelationElement]:
    component: dict[ElementId, FetchRelationElement] = {seed: ways[seed]}
    stack = [seed]
    while stack:
        way_id = stack.pop()
        for connected_way_id in ways[way_id].connectedTo:
            if connected_way_id in ways and connected_way_id not in component:
                component[connected_way_id] = ways[connected_way_id]
                stack.append(connected_way_id)
    return component


def _entry_node(route_way: FinalRouteWay) -> int:
    return route_way.way.nodes[-1] if route_way.reversed_latLngs else route_way.way.nodes[0]


def _exit_node(route_way: FinalRouteWay) -> int:
    return route_way.way.nodes[0] if route_way.reversed_latLngs else route_way.way.nodes[-1]


async def _calc_path(
    ways: dict[ElementId, FetchRelationElement],
    start_way: ElementId,
    end_way: ElementId,
    executor: ProcessPoolExecutor,
    n_processes: cython.int,
) -> tuple[tuple[FinalRouteWay, ...], bool]:
    """Calculate the best path from start_way to end_way, returns the path and whether the end was reached."""
    graph = build_graph(ways)
    best_path = await modified_dfs(graph, ways, start_way, end_way, {}, executor, n_processes)
    route_ways = tuple(
        FinalRouteWay(
            way=ways[key.way_id],
            reversed_latLngs=not key.is_start,
        )
        for key in best_path.path
    )
    complete = bool(route_ways) and route_ways[-1].way.id == end_way
    return route_ways, complete


async def _calc_component_path(
    component: dict[ElementId, FetchRelationElement],
    candidates: Sequence[tuple[ElementId, ElementId]],
    executor: ProcessPoolExecutor,
    n_processes: cython.int,
) -> tuple[FinalRouteWay, ...]:
    """Route a connected component, trying the given (start, end) pairs: the first complete path wins, else the longest."""
    best: tuple[FinalRouteWay, ...] = ()
    for start_way, end_way in candidates:
        path, complete = await _calc_path(component, start_way, end_way, executor, n_processes)
        if complete:
            return path
        if len(path) > len(best):
            best = path
    return best or (FinalRouteWay(way=component[candidates[0][0]], reversed_latLngs=False),)


async def _calc_branch_sections(
    role: str,
    branch_ways: dict[ElementId, FetchRelationElement],
    order_key,
    executor: ProcessPoolExecutor,
    n_processes: cython.int,
) -> list[FinalRouteSection]:
    """
    Sort the ways of a branch role (e.g., alternative) into one or more sections.

    Each connected component becomes a section, routed from its first to its last endpoint (by order_key).
    Ways left over by the routing form additional sections, so that no way is lost.
    """
    sections: list[FinalRouteSection] = []
    remaining = dict(branch_ways)

    while remaining:
        seed = min(remaining, key=order_key)
        component = _connected_component(seed, remaining)
        endpoints = sorted(_graph_endpoint_ids(component), key=order_key)

        if len(endpoints) >= 2:
            candidates = [(endpoints[0], endpoints[-1])]
        elif endpoints:
            candidates = [(endpoints[0], endpoints[0])]
        else:
            candidates = [(seed, seed)]

        path = await _calc_component_path(component, candidates, executor, n_processes)
        sections.append(FinalRouteSection(role=role, ways=path, latLngs=_path_latlngs(path)))

        for route_way in path:
            remaining.pop(route_way.way.id, None)

    return sections


async def _calc_return_legs(
    directional_ways: dict[ElementId, FetchRelationElement],
    forward_path: Sequence[FinalRouteWay],
    executor: ProcessPoolExecutor,
    n_processes: cython.int,
) -> list[FinalRouteSection]:
    """
    Sort the directional ways which are not used by the forward path into the return legs of split sections.

    Each leg is routed in its direction of travel (merge point -> split point) and attached to the forward path
    at its split point: the leg is listed after the forward leg, see FinalRouteSection.position.
    """
    # the junction nodes of the forward path: junctions[i] is the node before forward_path[i]
    junctions: list[int] = [_entry_node(forward_path[0])] if forward_path else []
    junctions.extend(_exit_node(route_way) for route_way in forward_path)

    def find_position(leg: Sequence[FinalRouteWay]) -> int | None:
        split_node = _exit_node(leg[-1])
        merge_node = _entry_node(leg[0])
        for i, node in enumerate(junctions):
            if node != split_node:
                continue
            for j in range(i + 1, len(junctions)):
                if junctions[j] == merge_node:
                    return j
            return None
        return None

    sections: list[FinalRouteSection] = []
    remaining = dict(directional_ways)

    while remaining:
        seed = next(iter(remaining))
        component = _connected_component(seed, remaining)
        endpoints = _graph_endpoint_ids(component)[:4]

        # the direction of travel is given by the roles: try the endpoints in both orders
        candidates = [(a, b) for a in endpoints for b in endpoints if a != b] or [(seed, seed)]
        path = await _calc_component_path(component, candidates, executor, n_processes)

        sections.append(
            FinalRouteSection(role='backward', ways=path, latLngs=_path_latlngs(path), position=find_position(path))
        )

        for route_way in path:
            remaining.pop(route_way.way.id, None)

    return sections


async def calc_bicycle_route(
    ways_members: dict[ElementId, FetchRelationElement],
    start_way: ElementId,
    end_way: ElementId,
    tags: dict[str, str],
    member_order: Mapping[int, int],
    executor: ProcessPoolExecutor,
    n_processes: cython.int,
) -> FinalRoute:
    """
    Calculate a route without stops, supporting split sections and branches (by way role).

    Ways with the forward/backward role are followed only along/against their direction (see Relation:route).
    The forward path is routed over the main and directional ways, from start_way to end_way.
    The directional ways which are left over form the return legs of split sections.
    Each other role (e.g., alternative, excursion) is routed on its own, into one or more sections.
    """
    route_ways: dict[ElementId, FetchRelationElement] = {}
    branch_role_ways: dict[str, dict[ElementId, FetchRelationElement]] = defaultdict(dict)
    for way_id, way in ways_members.items():
        if way.role in {'', 'forward', 'backward'}:
            route_ways[way_id] = way
        else:
            branch_role_ways[way.role][way_id] = way

    if not route_ways:
        # nothing to route: treat everything as the main route
        route_ways = ways_members
        branch_role_ways = {}

    def native_id(way_id: ElementId) -> int:
        return split_element_id(way_id).id

    origin_latlng = ways_members[start_way].latLngs[0]

    def order_key(way_id: ElementId) -> tuple[float, float]:
        # prefer the current relation order, fallback to the distance from the route start
        return (
            member_order.get(native_id(way_id), inf),
            haversine_distance(origin_latlng, ways_members[way_id].midpoint),
        )

    def role_order_key(role: str) -> tuple[float, str]:
        return min((member_order.get(native_id(way_id), inf) for way_id in branch_role_ways[role]), default=inf), role

    with print_run_time('Calculating route'):
        branch_tasks = [
            asyncio.ensure_future(_calc_branch_sections(role, branch_role_ways[role], order_key, executor, n_processes))
            for role in sorted(branch_role_ways, key=role_order_key)
        ]

        forward_path, _ = await _calc_path(
            route_ways,
            _pick_endpoint(ways_members[start_way], route_ways),
            _pick_endpoint(ways_members[end_way], route_ways),
            executor,
            n_processes,
        )

        used_way_ids = {route_way.way.id for route_way in forward_path}
        directional_ways = {
            way_id: way  #
            for way_id, way in route_ways.items()
            if way_id not in used_way_ids and _is_directional(way)
        }
        return_legs = await _calc_return_legs(directional_ways, forward_path, executor, n_processes)

        branch_sections = await asyncio.gather(*branch_tasks)

    return FinalRoute(
        ways=forward_path,
        latLngs=_path_latlngs(forward_path),
        busStops=(),
        tags=tags,
        sections=(*return_legs, *chain.from_iterable(branch_sections)),
    )
