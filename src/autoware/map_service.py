import xml.etree.ElementTree as et
from enum import Enum
from math import atan2
from typing import Dict, List, Tuple, Optional, Set

import lanelet2 as ll2
from lanelet2.core import LaneletMap, Lanelet, ConstLanelet, ConstLineString3d
from lanelet2.io import Origin, load
from lanelet2.routing import RoutingGraph, LaneletPath
from lanelet2 import traffic_rules
from shapely import LineString

from config import ADS_MAP_DIR, SUPPORTED_MAPS
from lanelet2_extension_python.projection import MGRSProjector
import lanelet2_extension_python.utility.query as query
import lanelet2_extension_python.utility.utilities as utilities

from geometry_msgs.msg import Point, Pose
from pathlib import Path
from autoware.utils import construct_lane_boundary_linestring, get_lane_lst, get_lane_lst_seg

LOADED = False


def load_map_service(map_name: str) -> "MapService":
    global LOADED
    if not LOADED:
        MapLoader(map_name)
        LOADED = True
    return MapService.instance()


class LaneBoundary(Enum):
    # type
    LINE_THIN = "line_thin"
    LINE_THICK = "line_thick"
    CURBSTONE = "curbstone"
    VIRTUAL = "virtual"
    ROAD_BORDER = "road_border"
    # subtype
    SOLID = "solid"
    DASHED = "dashed"
    SOLID_SOLID = "solid_solid"
    DASHED_SOLID = "dashed_solid"
    SOLID_DASHED = "solid_dashed"
    HIGH = "high"
    LOW = "low"


class MapLoader:

    def __init__(self, map_name: str):
        self.lanelet_map = None
        if map_name not in SUPPORTED_MAPS:
            raise RuntimeError(f"Map {map_name} not supported")
        self.hd_map_path = Path(ADS_MAP_DIR, map_name, "lanelet2_map.osm")

        if not Path.exists(self.hd_map_path):
            raise RuntimeError(f"Requested map {map_name} does not exist")

        origin = self.get_first_reference_origin(str(self.hd_map_path))
        self.projector = MGRSProjector(origin)

        ll2_map = self.load_map()
        tr_veh = ll2.traffic_rules.create(ll2.traffic_rules.Locations.Germany,
                                          ll2.traffic_rules.Participants.Vehicle)
        tr_ped = ll2.traffic_rules.create(ll2.traffic_rules.Locations.Germany,
                                          ll2.traffic_rules.Participants.Pedestrian)
        tr_bic = ll2.traffic_rules.create(ll2.traffic_rules.Locations.Germany,
                                          ll2.traffic_rules.Participants.Bicycle)

        map_parser = MapService.instance(ll_map=ll2_map,
                                         projector=self.projector,
                                         rg_veh=RoutingGraph(ll2_map, tr_veh),
                                         rg_ped=RoutingGraph(ll2_map, tr_ped),
                                         rg_bic=RoutingGraph(ll2_map, tr_bic),
                                         map_name=map_name)

        self.map_instance = map_parser

    def load_map(self) -> LaneletMap:
        self.lanelet_map = load(str(self.hd_map_path), self.projector)
        return self.lanelet_map

    def get_first_reference_origin(self, hd_map_path: str) -> Origin:
        with open(hd_map_path, 'r') as file:
            xml_data = file.read()

        root = et.fromstring(xml_data)
        first_node = root.find('.//node')

        lat = float(first_node.get('lat'))
        lon = float(first_node.get('lon'))
        return Origin(lat, lon, 0)


class MapService:
    map_name = None
    _instance = None
    ll_map: LaneletMap
    rg_veh: RoutingGraph
    rg_ped: RoutingGraph
    rg_bic: RoutingGraph

    all_ln_ids: Set[int] = None
    veh_ln_ids: List[int] = None
    bic_ln_ids: List[int] = None
    ped_ln_ids: List[int] = None

    speed_limits: Dict[int, float] = None

    non_junc_lns: List[int] = None
    junc_lns: List[int] = None
    kMaxHeadingDiff = 1.0

    @classmethod
    def instance(cls, **kwargs):
        if cls._instance is None:
            cls._instance = cls.__new__(cls)
            for key, value in kwargs.items():
                setattr(cls._instance, key, value)
        return cls._instance

    def __init__(self):
        raise RuntimeError('Call instance() instead')

    def __proc_lanes(self):
        if not self.all_ln_ids:
            self.all_ln_ids = set()
            self.junc_lns = list()
            self.non_junc_lns = list()
            self.veh_ln_ids = list()
            self.bic_ln_ids = list()
            self.ped_ln_ids = list()
            for ll in self.ll_map.laneletLayer:
                self.all_ln_ids.add(ll.id)
                self.__proc_lane_types(ll)
                self.__find_junction_lanes(ll)
            self.__remove_duplicate()

    def __proc_lane_types(self, lane):
        participant_exists = self.__contains_participants(lane)
        if participant_exists:
            for p in participant_exists:
                if 'vehicle' in p and lane.attributes[p] == 'yes':
                    if lane.attributes['subtype'] != 'speed_bump':
                        self.veh_ln_ids.append(lane.id)
                elif 'bicycle' in p and lane.attributes[p] == 'yes':
                    self.bic_ln_ids.append(lane.id)
                elif 'pedestrian' in p and lane.attributes[p] == 'yes':
                    self.ped_ln_ids.append(lane.id)
        else:
            self.__proc_lane(lane)

    def __proc_lane(self, lane):
        match lane.attributes['subtype']:
            case LaneSubtype.ROAD.value:
                self.veh_ln_ids.append(lane.id)
                self.bic_ln_ids.append(lane.id)
            # case LaneSubtype.HIGHWAY.value | LaneSubtype.ROAD_SHOULDER.value:
            case LaneSubtype.HIGHWAY.value:
                self.veh_ln_ids.append(lane.id)
            case LaneSubtype.CROSSWALK.value | LaneSubtype.WALKWAY.value:
                self.ped_ln_ids.append(lane.id)
            case LaneSubtype.BICYCLE_LANE.value:
                self.bic_ln_ids.append(lane.id)
            case LaneSubtype.PLAY_STREET.value:
            # case LaneSubtype.PLAY_STREET.value | LaneSubtype.EXIT.value:
                self.veh_ln_ids.append(lane.id)
                self.bic_ln_ids.append(lane.id)
                self.ped_ln_ids.append(lane.id)
            case LaneSubtype.SHARED_WALKWAY.value | LaneSubtype.PEDESTRIAN_LANE.value:
                self.ped_ln_ids.append(lane.id)
                self.bic_ln_ids.append(lane.id)
            case _:
                pass
                # raise NotImplementedError(f"Unknown lane subtype: {lane.attributes['subtype']}")

    def __contains_participants(self, lane):
        return [p for p in lane.attributes.keys() if 'participant' in p]

    def __remove_duplicate(self):
        self.veh_ln_ids = list(set(self.veh_ln_ids))
        self.bic_ln_ids = list(set(self.bic_ln_ids))
        self.ped_ln_ids = list(set(self.ped_ln_ids))
        self.junc_lns = list(set(self.junc_lns))
        self.non_junc_lns = list(set(self.non_junc_lns))

    def get_lane_by_id(self, lane_id: int) -> Lanelet:
        return self.ll_map.laneletLayer[lane_id]

    def get_center_line_lst_by_id(self, lane_id: int) -> LineString:
        lane = self.get_lane_by_id(lane_id)
        cl = lane.centerline
        return get_lane_lst(cl)

    def get_lane_boundary_types_by_id(self, lane_id: int) -> Tuple[Dict, Dict]:
        lane = self.get_lane_by_id(lane_id)
        left = lane.leftBound
        right = lane.rightBound
        return left.attributes, right.attributes

    def get_lane_boundaries_by_id(self, lane_id: int) -> Tuple[LineString, LineString]:
        lane = self.get_lane_by_id(lane_id)
        return construct_lane_boundary_linestring(lane)

    def get_lane_boundaries(self) -> dict:
        boundaries = dict()
        for lane in self.ll_map.laneletLayer:
            lane_id = lane.id
            l, r = construct_lane_boundary_linestring(lane)
            boundaries[f'{lane_id}_L'] = l
            boundaries[f'{lane_id}_R'] = r
        return boundaries

    def get_vehicle_shortest_path_src_tgt(self, start_lane_id: int, end_lane_id: int, with_lane_change=True) -> \
            Optional[LaneletPath]:
        return self.rg_veh.shortestPath(self.get_lane_by_id(start_lane_id),
                                        self.get_lane_by_id(end_lane_id), withLaneChanges=with_lane_change)

    def get_pedestrian_shortest_path_src_tgt(self, start: int, end: int, with_lane_change: bool):
        return self.rg_ped.shortestPath(self.get_lane_by_id(start), self.get_lane_by_id(end),
                                        withLaneChanges=with_lane_change)

    def get_changable_neighbours(self, lane_id: int) -> List[int]:
        cg_neighbors = query.getLaneChangeableNeighbors(self.rg_veh, self.get_lane_by_id(lane_id))
        if cg_neighbors is None:
            return []
        return [x.id for x in cg_neighbors if x.id != lane_id]

    def get_bicycle_shortest_path_src_tgt(self, start: int, end: int, with_lane_change: bool):
        return self.rg_bic.shortestPath(self.get_lane_by_id(start), self.get_lane_by_id(end),
                                        withLaneChanges=with_lane_change)

    def __find_junction_lanes(self, lane):
        if lane.attributes['subtype'] not in [LaneSubtype.ROAD.value,
                                              LaneSubtype.HIGHWAY.value,
                                              LaneSubtype.PLAY_STREET.value,
                                              # LaneSubtype.EXIT.value,
                                              # LaneSubtype.ROAD_SHOULDER.value
                                              ]:
            return
        if 'turn_direction' not in lane.attributes:
            self.non_junc_lns.append(lane.id)
        else:
            self.junc_lns.append(lane.id)

    def get_junction_lanes(self):
        if not self.junc_lns:
            self.__proc_lanes()
        return self.junc_lns

    def get_non_junction_lanes(self):
        if not self.non_junc_lns:
            self.__proc_lanes()
        return self.non_junc_lns

    def get_vehicle_lanes(self):
        if not self.veh_ln_ids:
            self.__proc_lanes()
        return self.veh_ln_ids

    def get_bicycle_lanes(self):
        if not self.bic_ln_ids:
            self.__proc_lanes()
        return self.bic_ln_ids

    def get_pedestrian_lanes(self):
        if not self.ped_ln_ids:
            self.__proc_lanes()
        return self.ped_ln_ids

    def get_avail_lanes(self, _t: str):
        if _t == "vehicle":
            return self.get_vehicle_lanes()
        elif _t == "bicycle":
            return self.get_bicycle_lanes()
        elif _t == "pedestrian":
            return self.get_pedestrian_lanes()
        else:
            raise NotImplementedError(f"Unknown obstacle type: {_t}")

    def get_speed_limits(self):
        if not self.speed_limits:
            self.speed_limits = dict()
            vehicle_lanes = self.get_vehicle_lanes()
            for v_lid in vehicle_lanes:
                self.speed_limits[v_lid] = float(self.get_lane_by_id(v_lid).attributes['speed_limit'])
        return self.speed_limits

    def get_lane_coord_and_heading(self, lane_id: int, s: float):
        """
        Parameters:
            - lane_id: lanelet id
            - s: distance along the lane
        Returns:
            - Point: x, y, z
            - heading: angle in radians
        """
        from shapely.geometry import Point
        lst = self.get_center_line_lst_by_id(lane_id)
        ip = lst.interpolate(s)

        segments = get_lane_lst_seg(lst)
        segments.sort(key=lambda x: ip.distance(x))
        line = segments[0]
        x1, x2 = line.xy[0]
        y1, y2 = line.xy[1]
        return Point(ip.x, ip.y, 0.0), atan2(y2 - y1, x2 - x1)

    def get_closest_lane(self, pose: Pose) -> ConstLanelet:
        return query.getClosestLanelet(self.ll_map.laneletLayer, pose)

    def get_nearest_lanes_w_range(self, pose: Pose, rng: float):
        return query.getLaneletsWithinRange(self.ll_map.laneletLayer, pose.position, rng)

    def get_nearest_lanes_with_range(self, lane_id: int, s: float, rng: float):
        point, _ = self.get_lane_coord_and_heading(lane_id, s)
        lanes = query.getLaneletsWithinRange(self.ll_map.laneletLayer, Point(x=point.x, y=point.y, z=0.0), rng)
        return [ll.id for ll in lanes]

    def is_in_lane(self, pose: Pose, ll: int | Lanelet, radius=0.0) -> bool:
        if isinstance(ll, int):
            ll = self.get_lane_by_id(ll)
        return utilities.isInLanelet(pose, ll, radius)

    def get_veh_current_lanelets(self, point: Point):
        """
            if the point is not in any lanelet, return None
            else return the lanelets
        """
        lanes = query.getCurrentLanelets(self.ll_map.laneletLayer, point)
        if len(lanes) == 0:
            return []
        candidate_lanes = list()
        for lane in lanes:
            if lane.id in self.get_vehicle_lanes():
                candidate_lanes.append(lane)
        return candidate_lanes

    def get_veh_current_lane(self, pose: Pose):
        lls = self.get_veh_current_lanelets(pose.position)
        if len(lls) == 0:
            return []
        closet_lane = self.get_closest_lane(pose)
        if closet_lane in lls:
            return [closet_lane]
        else:
            return lls

    def get_nearest_lanes_with_heading(self, pose: Pose):
        in_rng_lanes = self.get_nearest_lanes_w_range(pose, 3)
        if len(in_rng_lanes) == 0:
            return []

        candidate_lanes = list()
        for ln in in_rng_lanes:
            if ln.id not in self.get_vehicle_lanes():
                continue
            if self.is_in_lane(pose, ln):
                candidate_lanes.append(ln.id)
        return candidate_lanes

    def get_predecessors_for_lane(self, lane_id: int) -> List[int]:
        return [x.id for x in self.rg_veh.previous(self.ll_map.laneletLayer[lane_id])]

    def get_successors_for_lane(self, lane_id: int) -> List[int]:
        return [x.id for x in self.rg_veh.following(self.ll_map.laneletLayer[lane_id])]

    def get_reachable_descendants(self, lane_id: int, _t: str = "vehicle", allow_lane_change: bool = True) -> Set[int]:
        if _t == "vehicle":
            return set([x.id for x in self.rg_veh.reachableSet(self.ll_map.laneletLayer[lane_id], 1e10,
                                                               allowLaneChanges=allow_lane_change)])
        elif _t == "bicycle":
            return set([x.id for x in self.rg_bic.reachableSet(self.ll_map.laneletLayer[lane_id], 1e10,
                                                               allowLaneChanges=allow_lane_change)])
        elif _t == "pedestrian":
            return set([x.id for x in self.rg_ped.reachableSet(self.ll_map.laneletLayer[lane_id], 1e10,
                                                               allowLaneChanges=allow_lane_change)])
        else:
            raise NotImplementedError(f"Unknown obstacle type: {_t}")

    def get_reachable_to(self, lane_id, allow_lane_change: bool):
        return self.rg_veh.reachableSetTowards(self.get_lane_by_id(lane_id), 10e9, allowLaneChanges=allow_lane_change)

    def get_length_of_lane(self, lane_id: int) -> float:
        return utilities.getLaneletLength2d(self.ll_map.laneletLayer[lane_id])

    def get_signals(self):
        return query.trafficLights(self.ll_map.laneletLayer)

    def get_stop_lines(self):
        return query.stopLinesLanelets(self.ll_map.laneletLayer)

    def get_stop_signs(self) -> List[ConstLineString3d]:
        return query.stopSignStopLines(self.ll_map.laneletLayer)


class LaneSubtype(Enum):
    ROAD = 'road'
    HIGHWAY = 'highway'
    PLAY_STREET = 'play_street'
    BICYCLE_LANE = 'bicycle_lane'
    # EXIT = 'exit'
    WALKWAY = 'walkway'
    SHARED_WALKWAY = 'shared_walkway'
    CROSSWALK = 'crosswalk'

    # Autoware specific
    # ROAD_SHOULDER = 'road_shoulder'
    PEDESTRIAN_LANE = 'pedestrian_lane'

    # veh: road, highway, play_street, exit, road_shoulder
    # bicycle: road, play_street, bicycle_lane, exit, shared_walkway, pedestrian_lane
    # pedestrian: play_street, walkway, shared_walkway, crosswalk, pedestrian_lane
