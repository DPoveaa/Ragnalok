"""Read map elevations and collision from .gat / data.grf, A* pathfinding and viewport projection."""

from dataclasses import dataclass
import heapq
import json
import math
import os
from pathlib import Path
import random
import re
import struct
import sys
import unicodedata
import zlib


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


CELL_SIZE = 5.0


class Gat:
    def __init__(self, data, name="unknown"):
        self.data = data
        self.name = name
        if len(data) < 14:
            raise ValueError("Assinatura .gat inválida.")
        magic, self.width, self.height = struct.unpack_from("<6sII", data, 0)
        if not magic.startswith(b"GRAT"):
            raise ValueError("Assinatura .gat inválida.")
        expected_len = 14 + 20 * self.width * self.height
        if not self.width or not self.height or len(data) != expected_len:
            raise ValueError("Dimensões .gat inválidas.")

    def get_elevation(self, fx: float, fy: float) -> float:
        """Elevação 3D contínua por interpolação bilinear dos 4 cantos da célula.
        No formato .gat do Ragnarok, alturas negativas representam elevação acima de zero.
        """
        cell_x = int(fx)
        cell_y = int(fy)
        if not (0 <= cell_x < self.width and 0 <= cell_y < self.height):
            return 0.0
        offset = 14 + 20 * (cell_y * self.width + cell_x)
        sw, se, nw, ne = struct.unpack_from("<4f", self.data, offset)
        if not (math.isfinite(sw) and math.isfinite(se) and math.isfinite(nw) and math.isfinite(ne)):
            return 0.0
        u = fx - cell_x
        v = fy - cell_y
        h_bot = sw * (1.0 - u) + se * u
        h_top = nw * (1.0 - u) + ne * u
        return -(h_bot * (1.0 - v) + h_top * v)

    def height_at(self, x, y):
        return self.get_elevation(float(x), float(y))

    def cell_type_at(self, x, y):
        cx, cy = int(x), int(y)
        if not (0 <= cx < self.width and 0 <= cy < self.height):
            return 1  # Fora dos limites = parede
        offset = 14 + 20 * (cy * self.width + cx) + 16
        return struct.unpack_from("<I", self.data, offset)[0]

    def is_walkable(self, x, y):
        cx, cy = int(x), int(y)
        if 0 <= cx < self.width and 0 <= cy < self.height:
            offset = 14 + 20 * (cy * self.width + cx) + 16
            # Células tipo 0 (chão caminhável) e 3 (água rasa caminhável) são transitáveis
            return self.data[offset] in (0, 3) and self.data[offset + 1] == 0
        return False

    def count_walkable_neighbors(self, x, y):
        cx, cy = int(x), int(y)
        count = 0
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if dx == 0 and dy == 0:
                    continue
                if self.is_walkable(cx + dx, cy + dy):
                    count += 1
        return count

    def is_safe_walkable(self, x, y, min_neighbors=7):
        """Verifica se a célula é caminhável e possui pelo menos min_neighbors (padrão 7 de 8) vizinhos caminháveis."""
        cx, cy = int(x), int(y)
        if not self.is_walkable(cx, cy):
            return False
        return self.count_walkable_neighbors(cx, cy) >= min_neighbors

    def find_safe_alternative_step(self, target_cell, player_cell, blocked_cells=None, min_neighbors=7, max_radius=3):
        """Busca uma célula vizinha livre a target_cell com >= min_neighbors caminháveis e dist >= 2.2 do player."""
        tx, ty = int(target_cell[0]), int(target_cell[1])
        px, py = float(player_cell[0]), float(player_cell[1])
        blocked_set = set(blocked_cells) if blocked_cells else set()

        candidates = []
        for r in range(1, max_radius + 1):
            for dx in range(-r, r + 1):
                for dy in range(-r, r + 1):
                    if max(abs(dx), abs(dy)) != r:
                        continue
                    nx, ny = tx + dx, ty + dy
                    if (nx, ny) in blocked_set:
                        continue
                    if math.hypot(nx - px, ny - py) < 2.2:
                        continue
                    if self.is_safe_walkable(nx, ny, min_neighbors=min_neighbors):
                        dist_t = math.hypot(dx, dy)
                        dist_p = math.hypot(nx - px, ny - py)
                        candidates.append((dist_t, -dist_p, (nx, ny)))
        if candidates:
            candidates.sort()
            return candidates[0][2]
        return None

    def find_nearest_walkable(self, x, y, max_radius=4, blocked_cells=None):
        cx, cy = int(x), int(y)
        blocked_set = set(blocked_cells) if blocked_cells else set()
        if self.is_walkable(cx, cy) and (cx, cy) not in blocked_set:
            return (cx, cy)
        best = None
        best_dist = 999.0
        for r in range(1, max_radius + 1):
            for dx in range(-r, r + 1):
                for dy in range(-r, r + 1):
                    if max(abs(dx), abs(dy)) != r:
                        continue
                    nx, ny = cx + dx, cy + dy
                    if self.is_walkable(nx, ny) and (nx, ny) not in blocked_set:
                        dist = math.hypot(dx, dy)
                        if dist < best_dist:
                            best_dist = dist
                            best = (nx, ny)
            if best is not None:
                return best
        return None

    def find_path(self, start_xy, goal_xy, blocked_cells=None, max_iterations=7000, *,
                  cell_costs=None, goal_test=None):
        """A* pathfinding em 8 direções evitando corte de quinas em paredes (preferindo passos diagonais primeiro)."""
        start = (int(start_xy[0]), int(start_xy[1]))
        goal = (int(goal_xy[0]), int(goal_xy[1])) if goal_test is None else None
        blocked_set = set(blocked_cells) if blocked_cells else set()

        if not self.is_walkable(*start):
            alt_start = self.find_nearest_walkable(*start, max_radius=3, blocked_cells=blocked_set)
            if alt_start is None:
                return []
            start = alt_start

        if goal is not None and (not self.is_walkable(*goal) or goal in blocked_set):
            alt_goal = self.find_nearest_walkable(*goal, max_radius=4, blocked_cells=blocked_set)
            if alt_goal is None:
                return []
            goal = alt_goal

        if start == goal:
            return [start]

        def octile_heuristic(a, b):
            if b is None:
                return 0
            dx = abs(a[0] - b[0])
            dy = abs(a[1] - b[1])
            return 10 * (max(dx, dy) - min(dx, dy)) + 14 * min(dx, dy)

        # 8 direções: 4 diagonais primeiro (custo 14), depois 4 ortogonais (custo 10) - padrão rAthena
        neighbors_step = [
            (1, 1, 14), (1, -1, 14),
            (-1, 1, 14), (-1, -1, 14),
            (1, 0, 10), (-1, 0, 10),
            (0, 1, 10), (0, -1, 10),
        ]

        open_heap = []
        h0 = octile_heuristic(start, goal)
        heapq.heappush(open_heap, (h0, h0, 0, start))
        g_score = {start: 0}
        came_from = {}
        closed_set = set()
        iterations = 0
        counter = 0

        while open_heap and iterations < max_iterations:
            iterations += 1
            f, h_val, _, current = heapq.heappop(open_heap)

            if current == goal or (goal_test is not None and goal_test(current)):
                # Reconstrói caminho
                path = [current]
                while current in came_from:
                    current = came_from[current]
                    path.append(current)
                path.reverse()
                return path

            if current in closed_set:
                continue
            closed_set.add(current)

            cur_x, cur_y = current
            current_g = g_score[current]
            for dx, dy, step_cost in neighbors_step:
                nx, ny = cur_x + dx, cur_y + dy
                neighbor = (nx, ny)

                if neighbor in closed_set:
                    continue

                if not self.is_walkable(nx, ny) or neighbor in blocked_set:
                    continue

                # Prevenção de corte de quina (Corner Cutting):
                # Se o passo for diagonal, ambas as células ortogonais adjacentes DEVEM ser caminháveis.
                if dx != 0 and dy != 0:
                    adj1 = (cur_x + dx, cur_y)
                    adj2 = (cur_x, cur_y + dy)
                    if not self.is_walkable(*adj1) or adj1 in blocked_set:
                        continue
                    if not self.is_walkable(*adj2) or adj2 in blocked_set:
                        continue

                tentative_g = current_g + step_cost
                if cell_costs is not None:
                    tentative_g += cell_costs.get(neighbor, 0)
                if tentative_g < g_score.get(neighbor, float("inf")):
                    came_from[neighbor] = current
                    g_score[neighbor] = tentative_g
                    h = octile_heuristic(neighbor, goal)
                    counter += 1
                    heapq.heappush(open_heap, (tentative_g + h, h, counter, neighbor))

        return []

    def pick_next_waypoint(self, player_xy, visited_cells=None, min_dist=12, max_dist=28, blocked_cells=None, rng=None, heatmap=None):
        """Seleciona um destino caminhável distante do jogador priorizando áreas não exploradas recentemente e regiões com maior calor de spawn."""
        px, py = int(player_xy[0]), int(player_xy[1])
        blocked_set = set(blocked_cells) if blocked_cells else set()
        recent_visited = list(visited_cells)[-60:] if visited_cells else []

        rand = rng or random.Random()
        candidates = []

        if heatmap is None and getattr(self, "name", None):
            heatmap = load_heatmap(self.name)

        # Amostra feixe de ângulos ao redor do jogador
        angles = [i * (2 * math.pi / 24) for i in range(24)]
        rand.shuffle(angles)

        for dist in range(max_dist, min_dist - 1, -3):
            for angle in angles:
                cx = int(round(px + dist * math.cos(angle)))
                cy = int(round(py + dist * math.sin(angle)))
                cand = (cx, cy)
                if cand in blocked_set:
                    continue
                if not self.is_walkable(cx, cy):
                    continue
                min_neigh = 3 if dist <= 4 else (4 if dist <= 12 else 5)
                if self.count_walkable_neighbors(cx, cy) < min_neigh:
                    continue

                # Calcula score de exploração (maior distância dos visitados recentes)
                if recent_visited:
                    min_visited_dist = min(max(abs(cx - vx), abs(cy - vy)) for vx, vy in recent_visited)
                else:
                    min_visited_dist = dist

                # Heatmap score: setor (cx // 16, cy // 16)
                heatmap_score = 0.0
                if heatmap:
                    sec = (cx // 16, cy // 16)
                    heatmap_score = float(heatmap.get(sec, 0))

                score = min_visited_dist + 2.5 * heatmap_score + rand.uniform(-2.0, 2.0)
                candidates.append((score, cand))

            if len(candidates) >= 12:
                break

        if candidates:
            candidates.sort(key=lambda item: item[0], reverse=True)
            return candidates[0][1]

        # Fallback: qualquer célula caminhável no raio
        for r in range(min_dist, max_dist + 1, 2):
            for dx in (-r, 0, r):
                for dy in (-r, 0, r):
                    if dx == 0 and dy == 0:
                        continue
                    cand = (px + dx, py + dy)
                    if self.is_walkable(*cand) and cand not in blocked_set and self.count_walkable_neighbors(*cand) >= 6:
                        return cand

        return None


class Terrain:
    def __init__(self, path="D:/Jogos/Ragnarok/data.grf", local_maps_dir="./maps"):
        self.grf_path = Path(path)
        self.local_maps_dir = Path(local_maps_dir)
        self.client_data_dir = Path("D:/Jogos/Ragnarok/data")
        self.archive = None
        self.aliases = {}
        self.cached_gats = {}
        self.name = None
        self.gat = None
        self._init_grf()

    def _init_grf(self):
        try:
            from ragx.grf import GrfArchive
            if self.grf_path.is_file():
                self.archive = GrfArchive(self.grf_path)
                if "data/resnametable.txt" in self.archive:
                    rows = self.archive.read("data/resnametable.txt").decode("cp949", errors="replace")
                    for row in rows.splitlines():
                        source, _, target = row.lower().partition("#")
                        target = target.split("#", 1)[0]
                        if source.endswith(".gat") and target.endswith(".gat"):
                            self.aliases[source] = target
        except Exception as exc:
            print(f"[Terrain] Aviso ao carregar GRF ({self.grf_path}): {exc}", flush=True)

    def for_map(self, name):
        if not name:
            return None
        norm_name = name.lower().removesuffix(".gat").removesuffix(".rsw").removesuffix(".gnd")
        if norm_name in self.cached_gats:
            self.name = norm_name
            self.gat = self.cached_gats[norm_name]
            return self.gat

        key = f"{norm_name}.gat"
        candidates = list(dict.fromkeys((key, self.aliases.get(key, key))))

        # 1. Procura em arquivos .gat locais (./maps/, script_dir/maps/, D:\Jogos\Ragnarok\data\)
        search_dirs = [
            self.local_maps_dir,
            Path(__file__).parent / "maps",
            self.client_data_dir,
            self.client_data_dir / "maps",
        ]

        for cand in candidates:
            for sdir in search_dirs:
                file_path = sdir / cand
                if file_path.is_file():
                    try:
                        data = file_path.read_bytes()
                        gat = Gat(data, name=norm_name)
                        self.cached_gats[norm_name] = gat
                        self.name = norm_name
                        self.gat = gat
                        return gat
                    except Exception as exc:
                        print(f"[Terrain] Erro ao carregar .gat local {file_path}: {exc}", flush=True)

        # 2. Fallback para leitura direta do .grf principal via GrfArchive (zlib)
        if self.archive is not None:
            for cand in candidates:
                for grf_key in (f"data/{cand}", cand):
                    try:
                        data = self.archive.read(grf_key)
                        gat = Gat(data, name=norm_name)
                        self.cached_gats[norm_name] = gat
                        self.name = norm_name
                        self.gat = gat
                        return gat
                    except (FileNotFoundError, KeyError):
                        continue
                    except Exception as exc:
                        print(f"[Terrain] Erro ao ler {cand} do GRF: {exc}", flush=True)

        # 3. Fallback adicional para outros .grf na pasta do jogo
        game_dir = Path("D:/Jogos/Ragnarok")
        if game_dir.is_dir():
            for other_grf in game_dir.glob("*.grf"):
                if other_grf.resolve() == self.grf_path.resolve():
                    continue
                try:
                    from ragx.grf import GrfArchive
                    with GrfArchive(other_grf) as extra_arc:
                        for cand in candidates:
                            for grf_key in (f"data/{cand}", cand):
                                if grf_key in extra_arc:
                                    data = extra_arc.read(grf_key)
                                    gat = Gat(data, name=norm_name)
                                    self.cached_gats[norm_name] = gat
                                    self.name = norm_name
                                    self.gat = gat
                                    return gat
                except Exception:
                    pass

        raise ValueError(f"Mapa {name!r} (.gat) não encontrado em locais nem no GRF.")

    def close(self):
        if self.archive is not None:
            try:
                self.archive.close()
            except Exception:
                pass
            self.archive = None


@dataclass
class MapCameraProfile:
    """Perfil óptico e cinemático nativo da câmera por mapa (extraído do cliente RO)."""
    map_name: str
    is_indoor: bool = False
    yaw_min: float = -360.0
    yaw_max: float = 360.0
    yaw_default: float = 0.0
    pitch_min: float = 25.0
    pitch_max: float = 65.0
    pitch_default: float = 50.0
    distance_min: float = 200.0
    distance_max: float = 600.0
    distance_default: float = 399.0


_INDOOR_RSW_CACHE = None
_VIEWPOINT_TABLE_CACHE = None


def _load_camera_tables(grf_path="D:/Jogos/Ragnarok/data.grf", data_dir="D:/Jogos/Ragnarok/data"):
    """Varre data/ e arquivos .grf para carregar e cachear indoorrswtable.txt e ViewpointTable.txt."""
    global _INDOOR_RSW_CACHE, _VIEWPOINT_TABLE_CACHE
    if _INDOOR_RSW_CACHE is not None and _VIEWPOINT_TABLE_CACHE is not None:
        return

    _INDOOR_RSW_CACHE = set()
    _VIEWPOINT_TABLE_CACHE = {}

    indoor_raw = None
    viewpoint_raw = None

    # 1. Tenta carregar arquivos locais em data_dir e caminhos do projeto
    search_dirs = [
        Path(data_dir),
        Path("D:/Jogos/Ragnarok/data"),
        Path(__file__).parent / "data",
        Path("./data"),
    ]
    for dpath in search_dirs:
        if not dpath.is_dir():
            continue
        if indoor_raw is None:
            for cand in ("indoorrswtable.txt", "indoorrswtable", "IndoorRswTable.txt"):
                p = dpath / cand
                if p.is_file():
                    try:
                        indoor_raw = p.read_bytes()
                        break
                    except Exception:
                        pass
        if viewpoint_raw is None:
            for cand in ("ViewpointTable.txt", "viewpointtable.txt", "viewpointtable", "ViewPointTable.txt"):
                p = dpath / cand
                if p.is_file():
                    try:
                        viewpoint_raw = p.read_bytes()
                        break
                    except Exception:
                        pass

    # 2. Tenta carregar do data.grf e outros .grf
    grf_file = Path(grf_path)
    grf_candidates = []
    if grf_file.is_file():
        grf_candidates.append(grf_file)
    game_dir = Path("D:/Jogos/Ragnarok")
    if game_dir.is_dir():
        for extra in game_dir.glob("*.grf"):
            if extra.resolve() not in [c.resolve() for c in grf_candidates]:
                grf_candidates.append(extra)

    for gfile in grf_candidates:
        if indoor_raw is not None and viewpoint_raw is not None:
            break
        try:
            from ragx.grf import GrfArchive
            with GrfArchive(gfile) as arc:
                if indoor_raw is None:
                    for k in ("data/indoorrswtable.txt", "data\\indoorrswtable.txt", "indoorrswtable.txt"):
                        if k in arc:
                            try:
                                indoor_raw = arc.read(k)
                                break
                            except Exception:
                                pass
                if viewpoint_raw is None:
                    for k in (
                        "data/ViewpointTable.txt", "data/viewpointtable.txt",
                        "data\\ViewpointTable.txt", "data\\viewpointtable.txt",
                        "ViewpointTable.txt"
                    ):
                        if k in arc:
                            try:
                                viewpoint_raw = arc.read(k)
                                break
                            except Exception:
                                pass
        except Exception:
            pass

    # Parseia data/indoorrswtable.txt
    if indoor_raw:
        try:
            text = indoor_raw.decode("cp949", errors="replace")
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith("//"):
                    continue
                entry = line.split("//")[0].split("#")[0].strip().lower()
                if entry:
                    clean = entry.removesuffix(".rsw").removesuffix(".gat").removesuffix(".gnd")
                    _INDOOR_RSW_CACHE.add(clean)
                    _INDOOR_RSW_CACHE.add(f"{clean}.rsw")
                    _INDOOR_RSW_CACHE.add(f"{clean}.gat")
        except Exception as exc:
            print(f"[rathena_terrain] Aviso ao processar indoorrswtable: {exc}", flush=True)

    # Parseia data/ViewpointTable.txt
    if viewpoint_raw:
        try:
            text = viewpoint_raw.decode("cp949", errors="replace")
            for line in text.splitlines():
                line = line.split("//")[0].strip()
                if not line:
                    continue
                parts = [p.strip() for p in line.split("#") if p.strip()]
                if len(parts) >= 10:
                    map_entry = parts[0].lower().removesuffix(".rsw").removesuffix(".gat").removesuffix(".gnd")
                    try:
                        range_val = float(parts[1])
                        scope_val = float(parts[2])
                        zoom_max = float(parts[3])
                        rot_from = float(parts[4])
                        rot_to = float(parts[5])
                        rot_def = float(parts[6])
                        alt_from = abs(float(parts[7]))
                        alt_to = abs(float(parts[8]))
                        alt_def = abs(float(parts[9]))
                    except ValueError:
                        continue

                    dist_default = zoom_max if zoom_max > 0 else 399.0
                    dist_min = max(200.0, range_val)
                    dist_max = max(dist_default, scope_val)

                    is_indoor = (map_entry in _INDOOR_RSW_CACHE) or (f"{map_entry}.rsw" in _INDOOR_RSW_CACHE) or ("_in" in map_entry)

                    if is_indoor and rot_from == -360.0:
                        yaw_min = 0.0
                        yaw_max = 90.0
                        yaw_default = 45.0
                    else:
                        yaw_min = rot_from
                        yaw_max = rot_to
                        yaw_default = rot_def

                    p_min = min(alt_from, alt_to) if alt_from != alt_to else alt_from
                    p_max = max(alt_from, alt_to) if alt_from != alt_to else alt_to
                    p_def = alt_def if alt_def > 0 else 50.0
                    if is_indoor and p_def == 50.0:
                        p_def = 45.0
                    if is_indoor and dist_default <= 399.0:
                        dist_default = 465.0

                    _VIEWPOINT_TABLE_CACHE[map_entry] = MapCameraProfile(
                        map_name=map_entry,
                        is_indoor=is_indoor,
                        yaw_min=yaw_min,
                        yaw_max=yaw_max,
                        yaw_default=yaw_default,
                        pitch_min=p_min,
                        pitch_max=p_max,
                        pitch_default=p_def,
                        distance_min=dist_min,
                        distance_max=dist_max,
                        distance_default=dist_default,
                    )
        except Exception as exc:
            print(f"[rathena_terrain] Aviso ao processar ViewpointTable: {exc}", flush=True)


def get_map_camera_profile(map_name: str) -> MapCameraProfile:
    """Retorna o perfil exato de câmera calibrado nativamente pelo cliente RO para o mapa."""
    _load_camera_tables()
    norm = Path(str(map_name)).name.lower()
    norm = norm.removesuffix(".gat").removesuffix(".rsw").removesuffix(".gnd")

    if _VIEWPOINT_TABLE_CACHE and norm in _VIEWPOINT_TABLE_CACHE:
        prof = _VIEWPOINT_TABLE_CACHE[norm]
        return MapCameraProfile(
            map_name=norm,
            is_indoor=prof.is_indoor,
            yaw_min=prof.yaw_min,
            yaw_max=prof.yaw_max,
            yaw_default=prof.yaw_default,
            pitch_min=prof.pitch_min,
            pitch_max=prof.pitch_max,
            pitch_default=prof.pitch_default,
            distance_min=prof.distance_min,
            distance_max=prof.distance_max,
            distance_default=prof.distance_default,
        )

    is_indoor = (
        (_INDOOR_RSW_CACHE is not None and (norm in _INDOOR_RSW_CACHE or f"{norm}.rsw" in _INDOOR_RSW_CACHE))
        or ("_in" in norm)
    )
    if is_indoor:
        return MapCameraProfile(
            map_name=norm,
            is_indoor=True,
            yaw_min=0.0,
            yaw_max=90.0,
            yaw_default=45.0,
            pitch_min=25.0,
            pitch_max=65.0,
            pitch_default=45.0,
            distance_min=200.0,
            distance_max=600.0,
            distance_default=465.0,
        )

    return MapCameraProfile(
        map_name=norm,
        is_indoor=False,
        yaw_min=-360.0,
        yaw_max=360.0,
        yaw_default=0.0,
        pitch_min=25.0,
        pitch_max=65.0,
        pitch_default=50.0,
        distance_min=200.0,
        distance_max=600.0,
        distance_default=399.0,
    )


@dataclass
class RagnarokProjector3D:
    """Motor de projeção 3D perspectiva fiel ao cliente Ragnarok Online.
    Escala real: 1 célula = 5.0 unidades de mundo (CELL_SIZE = 5.0).
    Óptica real do Ragexe.exe: FOV vertical = 15.0°, distância padrão outdoor = 399.0.
    """
    yaw_deg: float = 0.0
    pitch_deg: float = 50.0
    distance: float = 399.0
    fov_deg: float = 15.0

    @property
    def rotation(self):
        return self.yaw_deg

    @rotation.setter
    def rotation(self, val):
        self.yaw_deg = float(val)

    @property
    def tilt(self):
        return self.pitch_deg

    @tilt.setter
    def tilt(self, val):
        self.pitch_deg = float(val)

    @property
    def zoom(self):
        return self.distance

    @zoom.setter
    def zoom(self, val):
        self.distance = float(val)

    def projector(self, viewport, origin, height_fn=None):
        """Retorna função de projeção 3D para um dado viewport e alvo da câmera.
        viewport: (left, top, width, height)
        origin: (cam_x, cam_y) ou (cam_x, cam_y, cam_z)
        height_fn: função (x, y) -> elevação float (ex: gat.get_elevation)
        """
        vp_x, vp_y, vp_w, vp_h = viewport
        focal = (vp_h * 0.5) / math.tan(math.radians(self.fov_deg * 0.5))

        cam_x = origin[0]
        cam_y = origin[1]
        if len(origin) >= 3:
            cam_z = origin[2]
        elif height_fn is not None:
            cam_z = height_fn(cam_x, cam_y)
        else:
            cam_z = 0.0

        yaw_rad = math.radians(self.yaw_deg)
        cos_yaw = math.cos(yaw_rad)
        sin_yaw = math.sin(yaw_rad)

        pitch_rad = math.radians(self.pitch_deg)
        cos_pitch = math.cos(pitch_rad)
        sin_pitch = math.sin(pitch_rad)

        dist = self.distance
        center_x = vp_w * 0.5
        center_y = vp_h * 0.5

        def project(fx, fy, sprite_z=0.0):
            wx = (fx - cam_x) * CELL_SIZE
            wy = (fy - cam_y) * CELL_SIZE
            gz = height_fn(fx, fy) if height_fn is not None else 0.0
            wz = (gz + sprite_z) - cam_z

            # 1. Rotação horizontal Yaw (eixo Z vertical)
            rx = wx * cos_yaw - wy * sin_yaw
            ry = wx * sin_yaw + wy * cos_yaw
            rz = wz

            # 2. Inclinação Pitch
            cam_space_x = rx
            cam_space_y = ry * sin_pitch + rz * cos_pitch
            depth = dist + ry * cos_pitch - rz * sin_pitch

            if depth < 1.0:
                depth = 1.0

            # 3. Projeção perspectiva
            u = 0.5 + (cam_space_x * focal / depth) / vp_w
            v = 0.5 - (cam_space_y * focal / depth) / vp_h
            return u, v

        def project_screen(fx, fy, sprite_z=0.0):
            u, v = project(fx, fy, sprite_z)
            return vp_x + u * vp_w, vp_y + v * vp_h

        project.project_screen = project_screen
        return project


Camera = RagnarokProjector3D


# ── Estrutura de Rotas Entre Cidades e Funcionárias Kafra ─────────────────

CITY_NAMES = {
    "prontera": "Prontera",
    "izlude": "Izlude",
    "geffen": "Geffen",
    "payon": "Payon",
    "morocc": "Morocc",
    "alberta": "Alberta",
    "aldebaran": "Aldebaran",
    "rachel": "Rachel",
    "veins": "Veins",
    "yuno": "Juno",
    "juno": "Juno",
    "comodo": "Comodo",
    "einbroch": "Einbroch",
    "lighthalzen": "Lighthalzen",
    "hugel": "Hugel",
}

# Dados de cada cidade: coordenadas de Kafras e opções de teleporte direto
CITY_KAFRA = {
    "prontera": {
        "name": "Prontera",
        "map": "prontera",
        "kafra_pos": (151, 29),  # Kafra Sul (151, 29) / Central (156, 187)
        "kafra_names": ("kafra", "funcionária kafra", "funcionaria kafra"),
        "teleports": {
            "izlude": 600,
            "geffen": 1200,
            "payon": 1200,
            "morocc": 1200,
            "alberta": 1800,
        },
    },
    "izlude": {
        "name": "Izlude",
        "map": "izlude",
        "kafra_pos": (128, 148),
        "kafra_names": ("kafra", "funcionária kafra", "funcionaria kafra"),
        "teleports": {
            "geffen": 1200,
            "payon": 1200,
            "morocc": 1200,
            "alberta": 1200,
        },
    },
    "geffen": {
        "name": "Geffen",
        "map": "geffen",
        "kafra_pos": (120, 62),
        "kafra_names": ("kafra", "funcionária kafra", "funcionaria kafra"),
        "teleports": {
            "prontera": 1200,
            "aldebaran": 1200,
            "payon": 1200,
        },
    },
    "payon": {
        "name": "Payon",
        "map": "payon",
        "kafra_pos": (160, 58),
        "kafra_names": ("kafra", "funcionária kafra", "funcionaria kafra"),
        "teleports": {
            "prontera": 1200,
            "alberta": 1200,
            "morocc": 1200,
        },
    },
    "morocc": {
        "name": "Morocc",
        "map": "morocc",
        "kafra_pos": (156, 97),
        "kafra_names": ("kafra", "funcionária kafra", "funcionaria kafra"),
        "teleports": {
            "prontera": 1200,
            "payon": 1200,
            "alberta": 1800,
            "comodo": 1800,
        },
    },
    "alberta": {
        "name": "Alberta",
        "map": "alberta",
        "kafra_pos": (113, 60),
        "kafra_names": ("kafra", "funcionária kafra", "funcionaria kafra"),
        "teleports": {
            "prontera": 1800,
            "payon": 1200,
            "morocc": 1200,
        },
    },
    "aldebaran": {
        "name": "Aldebaran",
        "map": "aldebaran",
        "kafra_pos": (143, 119),
        "kafra_names": ("kafra", "funcionária kafra", "funcionaria kafra"),
        "teleports": {
            "geffen": 1200,
            "yuno": 1200,
            "izlude": 1800,
        },
    },
    "yuno": {
        "name": "Juno",
        "map": "yuno",
        "kafra_pos": (153, 187),
        "kafra_names": ("kafra", "funcionária kafra", "funcionaria kafra"),
        "teleports": {
            "aldebaran": 1200,
        },
    },
    "comodo": {
        "name": "Comodo",
        "map": "comodo",
        "kafra_pos": (195, 150),
        "kafra_names": ("kafra", "funcionária kafra", "funcionaria kafra"),
        "teleports": {
            "morocc": 1800,
        },
    },
    "einbroch": {
        "name": "Einbroch",
        "map": "einbroch",
        "kafra_pos": (242, 205),
        "kafra_names": ("kafra", "funcionária kafra", "funcionaria kafra"),
        "teleports": {
            "einbech": 600,
        },
    },
    "lighthalzen": {
        "name": "Lighthalzen",
        "map": "lighthalzen",
        "kafra_pos": (158, 92),
        "kafra_names": ("kafra", "funcionária kafra", "funcionaria kafra"),
        "teleports": {},
    },
    "hugel": {
        "name": "Hugel",
        "map": "hugel",
        "kafra_pos": (88, 145),
        "kafra_names": ("kafra", "funcionária kafra", "funcionaria kafra"),
        "teleports": {},
    },
    "rachel": {
        "name": "Rachel",
        "map": "rachel",
        "kafra_pos": (109, 138),
        "kafra_names": ("kafra", "funcionária kafra", "funcionaria kafra"),
        "teleports": {
            "veins": 2200,
        },
    },
    "veins": {
        "name": "Veins",
        "map": "veins",
        "kafra_pos": (208, 128),
        "kafra_names": ("kafra", "funcionária kafra", "funcionaria kafra"),
        "teleports": {
            "rachel": 2200,
        },
    },
}

# Alias Juno -> yuno
CITY_KAFRA["juno"] = CITY_KAFRA["yuno"]

# Conexões por portais entre mapas adjacentes (origem, destino): (portal_x, portal_y)
MAP_PORTALS = {
    # Prontera
    ("prontera", "prt_fild08"): (282, 200),
    ("prontera", "prt_fild08_s"): (156, 21),
    ("prt_fild08", "prontera"): (14, 185),
    ("prt_fild08", "izlude"): (365, 212),
    ("izlude", "prt_fild08"): (6, 104),
    ("prontera", "prt_fild01"): (156, 360),
    ("prt_fild01", "prontera"): (170, 15),
    ("prt_fild01", "aldebaran"): (170, 375),
    ("aldebaran", "prt_fild01"): (140, 12),
    ("aldebaran", "yuno_fild01"): (140, 240),
    ("prontera", "prt_fild05"): (18, 156),
    ("prt_fild05", "prontera"): (370, 156),
    ("prt_fild05", "gef_fild00"): (15, 180),
    ("gef_fild00", "prt_fild05"): (370, 180),
    ("gef_fild00", "geffen"): (15, 180),
    ("geffen", "gef_fild00"): (180, 115),
    ("geffen", "gef_fild07"): (15, 115),
    ("geffen", "gef_fild04"): (119, 190),
    ("geffen", "gef_fild09"): (119, 20),

    # Morocc
    ("morocc", "moc_fild07"): (156, 290),
    ("moc_fild07", "morocc"): (156, 370),
    ("morocc", "moc_fild12"): (160, 15),
    ("morocc", "moc_fild11"): (290, 145),
    ("morocc", "moc_fild18"): (15, 145),

    # Payon
    ("payon", "pay_fild01"): (159, 15),
    ("payon", "pay_fild08"): (15, 150),
    ("pay_fild08", "payon"): (370, 150),

    # Alberta
    ("alberta", "pay_fild03"): (28, 234),

    # Rachel <-> Veins
    ("rachel", "ra_fild12"): (130, 20),
    ("rachel", "ra_fild01"): (130, 280),
    ("ra_fild12", "rachel"): (130, 370),
    ("ra_fild12", "veins"): (150, 20),
    ("veins", "ra_fild12"): (150, 370),
    ("veins", "ve_fild07"): (216, 125),

    # Yuno
    ("yuno", "yuno_fild04"): (157, 18),
    ("yuno", "yuno_fild01"): (157, 340),

    # Comodo
    ("comodo", "cmd_fild01"): (320, 180),
    ("comodo", "cmd_fild02"): (15, 240),

    # Einbroch
    ("einbroch", "ein_fild08"): (280, 200),

    # Lighthalzen
    ("lighthalzen", "lhz_fild02"): (158, 20),
    ("lighthalzen", "lhz_fild01"): (158, 300),

    # Hugel
    ("hugel", "hu_fild06"): (15, 145),
    ("hugel", "hu_fild05"): (150, 20),

    # Calabouços / Dungeons & Conexões Expandidas
    # Payon Dungeon
    ("payon", "pay_arche"): (156, 230),
    ("pay_arche", "payon"): (35, 130),
    ("pay_arche", "pay_dun00"): (48, 137),
    ("pay_dun00", "pay_arche"): (18, 170),
    ("pay_dun00", "pay_dun01"): (20, 15),
    ("pay_dun01", "pay_dun00"): (20, 280),
    ("pay_dun01", "pay_dun02"): (280, 20),
    # Orc Dungeon
    ("gef_fild10", "orcsdun01"): (68, 340),
    ("orcsdun01", "gef_fild10"): (30, 170),
    ("orcsdun01", "orcsdun02"): (180, 25),
    # Prontera Culvert / Esgotos
    ("prt_fild05", "prt_sewb1"): (270, 210),
    ("prt_sewb1", "prt_fild05"): (130, 240),
    ("prt_sewb1", "prt_sewb2"): (18, 18),
    # Anthell / Formigueiro Infernal
    ("moc_fild18", "anthell01"): (150, 250),
    ("anthell01", "moc_fild18"): (35, 260),
    # Pyramids / Pirâmide de Morocc
    ("morocc", "moc_ruins"): (60, 290),
    ("moc_ruins", "morocc"): (160, 20),
    ("moc_ruins", "moc_pryd01"): (70, 160),
    ("moc_pryd01", "moc_ruins"): (190, 10),
    # Izlude Byalan / Submarino
    ("izlude", "iz_dun00"): (198, 214),
    ("iz_dun00", "izlude"): (20, 20),
}

# ── Aprendizado Automático de Portais (learned_warps.json) ──
LEARNED_WARPS_FILE = "learned_warps.json"


def load_learned_warps() -> dict:
    """Carrega portais aprendidos dinamicamente de learned_warps.json."""
    candidates = [
        _get_base_dir() / LEARNED_WARPS_FILE,
        Path(LEARNED_WARPS_FILE),
        Path(__file__).parent / LEARNED_WARPS_FILE,
    ]
    for p in candidates:
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
    return {}


def save_learned_warp(prev_map: str, prev_x: int, prev_y: int, new_map: str):
    """Registra automaticamente transição de portal: (mapa_ant, x, y) -> novo_mapa."""
    p_map = str(prev_map).lower().removesuffix(".gat").removesuffix(".rsw").strip()
    n_map = str(new_map).lower().removesuffix(".gat").removesuffix(".rsw").strip()
    if not p_map or not n_map or p_map == n_map:
        return
    warps = load_learned_warps()
    key = f"{p_map}->{n_map}"
    warps[key] = [int(prev_x), int(prev_y)]
    try:
        target_path = _get_base_dir() / LEARNED_WARPS_FILE
        target_path.write_text(json.dumps(warps, indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"[Terrain] Aviso ao salvar learned_warps.json: {exc}", flush=True)
    # Atualiza em memória o dicionário MAP_PORTALS
    MAP_PORTALS[(p_map, n_map)] = (int(prev_x), int(prev_y))


def matches_city(text: str, target_city: str) -> bool:
    """Verifica correspondência entre opção de texto e cidade destino,
    normalizando acentos e variações como Morocc/Morroc, Juno/Yuno, Aldebaran/Al De Baran.
    """
    t = normalize_text(text)
    c = normalize_text(target_city)
    if not t or not c:
        return False
    if c in t or t in c:
        return True
    aliases = {
        "morocc": ["morroc", "moroc", "moroque"],
        "yuno": ["juno"],
        "juno": ["yuno"],
        "aldebaran": ["al de baran", "albaran"],
        "lighthalzen": ["lightalzen", "lhz"],
        "einbroch": ["einbech"],
    }
    for alias in aliases.get(c, []):
        if alias in t:
            return True
    return False

# Coordenadas reais dos portais de saída das 14 cidades principais
CITY_EXIT_PORTALS = {
    "prontera": [(156, 21), (282, 200), (156, 360), (18, 156)],
    "izlude": [(6, 104)],
    "geffen": [(180, 115), (15, 115), (119, 190), (119, 20)],
    "payon": [(159, 15), (15, 150)],
    "morocc": [(156, 290), (160, 15), (290, 145), (15, 145)],
    "alberta": [(28, 234), (15, 145)],
    "aldebaran": [(140, 12), (140, 240)],
    "rachel": [(130, 20), (130, 280)],
    "veins": [(150, 370), (216, 125)],
    "yuno": [(157, 18), (157, 340)],
    "comodo": [(320, 180), (15, 240)],
    "einbroch": [(280, 200)],
    "lighthalzen": [(158, 20), (158, 300)],
    "hugel": [(15, 145), (150, 20)],
}
CITY_EXIT_PORTALS["juno"] = CITY_EXIT_PORTALS["yuno"]


def find_map_route(start_map: str, dest_map: str) -> list[tuple[str, str, int, int]]:
    """Busca o menor caminho de portais entre start_map e dest_map via BFS.
    Retorna lista de passos: [(origem, destino, portal_x, portal_y), ...]
    """
    s_map = str(start_map).lower().removesuffix(".gat").removesuffix(".rsw")
    d_map = str(dest_map).lower().removesuffix(".gat").removesuffix(".rsw")
    if s_map == d_map:
        return []

    # Constrói grafo de adjacência (MAP_PORTALS + learned_warps)
    adj: dict[str, list[tuple[str, int, int]]] = {}
    combined_portals = dict(MAP_PORTALS)
    for k, v in load_learned_warps().items():
        if "->" in k and isinstance(v, (list, tuple)) and len(v) == 2:
            mf, mt = k.split("->", 1)
            combined_portals[(mf, mt)] = (int(v[0]), int(v[1]))
    for (m_from, m_to), (px, py) in combined_portals.items():
        adj.setdefault(m_from, []).append((m_to, px, py))

    queue = [(s_map, [])]
    visited = {s_map}

    while queue:
        curr, path = queue.pop(0)
        if curr == d_map:
            return path
        for nxt, px, py in adj.get(curr, []):
            if nxt not in visited:
                visited.add(nxt)
                queue.append((nxt, path + [(curr, nxt, px, py)]))

    return []


def get_city_travel_plan(current_map: str, target_city: str):
    """Calcula a estratégia ótima de viagem entre o mapa atual e a cidade destino.
    - Se a distância por portais for <= 2 mapas: plano de caminhada via portais.
    - Se a distância for maior (ou sem rota direta a pé) e houver Kafra na cidade atual
      com teleporte direto: plano de teleporte via Kafra.
    """
    c_map = str(current_map).lower().removesuffix(".gat").removesuffix(".rsw")
    t_city = str(target_city).lower().removesuffix(".gat").removesuffix(".rsw")
    if t_city == "juno":
        t_city = "yuno"

    t_map = CITY_KAFRA.get(t_city, {}).get("map", t_city)

    if c_map == t_map:
        return {"mode": "already_there", "message": f"Já estamos em {target_city}."}

    # 1. Verifica se há rota a pé de até 2 mapas de distância
    walk_route = find_map_route(c_map, t_map)
    if walk_route and len(walk_route) <= 2:
        return {
            "mode": "walk",
            "distance_maps": len(walk_route),
            "route": walk_route,
            "target_map": t_map,
            "target_city": target_city,
        }

    # 2. Verifica se a cidade atual tem Kafra com teleporte direto para a cidade destino
    kafra_info = CITY_KAFRA.get(c_map)
    if kafra_info and t_city in kafra_info["teleports"]:
        cost = kafra_info["teleports"][t_city]
        return {
            "mode": "kafra",
            "kafra_pos": kafra_info["kafra_pos"],
            "target_city": t_city,
            "target_map": t_map,
            "cost": cost,
            "kafra_names": kafra_info.get("kafra_names", ("kafra",)),
        }

    # 3. Se houver rota a pé mesmo > 2 mapas
    if walk_route:
        return {
            "mode": "walk",
            "distance_maps": len(walk_route),
            "route": walk_route,
            "target_map": t_map,
            "target_city": target_city,
        }

    # 4. Fallback: tentar Kafra da cidade atual se existir
    if kafra_info:
        return {
            "mode": "kafra",
            "kafra_pos": kafra_info["kafra_pos"],
            "target_city": t_city,
            "target_map": t_map,
            "cost": 0,
            "kafra_names": kafra_info.get("kafra_names", ("kafra",)),
        }

    return {"mode": "unknown", "message": f"Nenhuma rota ou Kafra direta para {target_city} a partir de {current_map}."}


# ── Cidades Principais Conhecidas ──
KNOWN_CITIES = {
    "prontera", "izlude", "geffen", "payon", "morocc", "alberta",
    "aldebaran", "rachel", "veins", "yuno", "juno", "comodo",
    "einbroch", "lighthalzen", "hugel"
}


def get_adjacent_city_portal(current_map: str, player_xy: tuple[float, float], max_dist: float = 28.0, gat=None):
    """Verifica se o mapa atual tem um portal conhecido que leva diretamente a uma cidade
    e se o jogador está a <= max_dist células com caminho A* livre.
    Retorna (cidade_destino, (portal_x, portal_y)) ou None.
    """
    if not current_map:
        return None
    c_map = str(current_map).lower().removesuffix(".gat").removesuffix(".rsw").strip()
    if c_map in KNOWN_CITIES:
        return None

    px, py = player_xy
    best_candidate = None
    best_dist = 999.0

    for (m_from, m_to), (wx, wy) in MAP_PORTALS.items():
        if m_from == c_map and m_to in KNOWN_CITIES:
            dist = math.hypot(px - wx, py - wy)
            if dist <= max_dist and dist < best_dist:
                has_path = True
                if gat is not None:
                    try:
                        path = gat.find_path((int(round(px)), int(round(py))), (int(wx), int(wy)))
                        if not path:
                            has_path = False
                    except Exception:
                        pass
                if has_path:
                    best_dist = dist
                    best_candidate = (m_to, (wx, wy))

    return best_candidate


# ── Mapa de Calor de Spawns ──
def load_heatmap(map_name: str) -> dict[tuple[int, int], int]:
    """Carrega o mapa de calor de spawns a partir de heatmap_<mapa>.json."""
    if not map_name:
        return {}
    m = str(map_name).lower().removesuffix(".gat").removesuffix(".rsw").strip()
    candidates = [
        _get_base_dir() / f"heatmap_{m}.json",
        Path(f"heatmap_{m}.json"),
        Path(__file__).parent / f"heatmap_{m}.json",
    ]
    for p in candidates:
        if p.is_file():
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                sectors = raw.get("sectors", raw)
                result = {}
                for k, v in sectors.items():
                    if "," in str(k):
                        parts = str(k).split(",")
                        result[(int(parts[0]), int(parts[1]))] = int(v)
                    elif "_" in str(k):
                        parts = str(k).split("_")
                        result[(int(parts[0]), int(parts[1]))] = int(v)
                return result
            except Exception:
                pass
    return {}


# ── Catálogo e Classificação de Itens em 3 Tiers ──
_ITEM_CATALOG_CACHE = None

VALUABLE_KEYWORDS = {
    "elunium", "oridecon", "fruto de yggdrasil", "semente de yggdrasil",
    "morango", "strawberry", "mastela", "erva branca", "erva azul",
    "white herb", "blue herb", "caixa de presente", "gift box",
    "velha caixa azul", "old blue box", "velha caixa roxa", "old purple box"
}
VALUABLE_IDS = {
    984, 985,    # Oridecon, Elunium
    607, 608,    # Fruto de Yggdrasil, Semente de Yggdrasil
    578,         # Morango
    522,         # Mastela
    509, 510,    # Erva Branca, Erva Azul
    644,         # Caixa de Presente
    603, 616,    # Velha Caixa Azul, Velha Caixa Roxa
    756, 757,    # OBB, OPB
}

TRASH_KEYWORDS = {
    "jellopy", "casca", "chitin", "felpa", "fluff", "trevo", "clover",
    "pluma", "feather", "garrafa vazia", "empty bottle", "muco pegajoso",
    "sticky mucus", "ferrão de abelha", "ferrao de abelha", "bee sting",
    "pata de inseto", "insect feeler", "pele de minhoca", "worm peeling",
    "crisálida", "crisalida", "chrysalis", "asa de inseto", "insect wing", "fly wing"
}
TRASH_IDS = {
    909,  # Jellopy
    934,  # Casca
    914,  # Felpa
    705,  # Trevo
    916,  # Pluma
    713,  # Garrafa Vazia
    938,  # Muco Pegajoso
    904,  # Ferrão de Abelha
    940,  # Pata de Inseto
    955,  # Pele de Minhoca
    942,  # Crisálida
    941,  # Asa de Inseto
}


def load_item_catalog(grf_path="D:/Jogos/Ragnarok/data.grf", lua_path="D:/Jogos/Ragnarok/System/itemInfo.lua") -> dict:
    """Carrega catálogo unificado de itens via itemInfo.lua / .lub com fallback nos arquivos .grf."""
    global _ITEM_CATALOG_CACHE
    if _ITEM_CATALOG_CACHE is not None:
        return _ITEM_CATALOG_CACHE

    names = {}
    cards = set()
    equips = set()

    # 1. Tenta carregar do itemInfo.lua ou itemInfo.lub
    lua_candidates = [
        Path(lua_path) if lua_path else None,
        Path("D:/Jogos/Ragnarok/System/itemInfo.lua"),
        Path("D:/Jogos/Ragnarok/System/itemInfo.lub"),
        Path("D:/Jogos/Ragnarok/System/iteminfo_new.lub"),
        Path("./itemInfo.lua"),
    ]
    lua_candidates = [p for p in lua_candidates if p is not None]
    for lp in lua_candidates:
        if lp.is_file():
            try:
                raw = lp.read_bytes()
                try:
                    text = raw.decode("cp1252")
                except UnicodeDecodeError:
                    text = raw.decode("latin-1", errors="replace")

                cur_id = None
                for line in text.splitlines():
                    line_s = line.strip()
                    if not line_s:
                        continue
                    if line_s.startswith("[") and "]=" in line_s.replace(" ", ""):
                        bracket_end = line_s.find("]")
                        if bracket_end != -1:
                            id_str = line_s[1:bracket_end].strip()
                            if id_str.isdigit():
                                cur_id = int(id_str)
                        continue
                    if cur_id is None:
                        continue
                    if "identifiedDisplayName" in line_s:
                        eq_idx = line_s.find("=")
                        if eq_idx != -1:
                            val = line_s[eq_idx + 1:].strip().rstrip(",")
                            if val.startswith("[[") and val.endswith("]]"):
                                dname = val[2:-2]
                            elif (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                                dname = val[1:-1]
                            else:
                                dname = val.strip("\"'[]")
                            dname = re.sub(r'\\(\d{1,3})', lambda m: chr(int(m.group(1))), dname)
                            dname = re.sub(r'\^[0-9A-Fa-f]{6}', '', dname)
                            dname = " ".join(dname.split())
                            if dname:
                                names[cur_id] = dname
                    elif "slotCount" in line_s:
                        eq_idx = line_s.find("=")
                        if eq_idx != -1:
                            val = line_s[eq_idx + 1:].strip().rstrip(",")
                            if val.isdigit() and int(val) > 0:
                                equips.add(cur_id)
                    elif "ClassNum" in line_s:
                        eq_idx = line_s.find("=")
                        if eq_idx != -1:
                            val = line_s[eq_idx + 1:].strip().rstrip(",")
                            if val.isdigit() and int(val) > 0:
                                equips.add(cur_id)
                    elif line_s.startswith("}"):
                        cur_id = None
                if names:
                    break
            except Exception as exc:
                print(f"[Terrain] Aviso itemInfo ({lp}): {exc}", flush=True)

    # 2. Fallback via GRF somente se itemInfo não carregou nomes
    if not names:
        try:
            from ragx.grf import GrfArchive
            grf_p = Path(grf_path)
            if grf_p.is_file():
                a = GrfArchive(grf_p)
                for k in a.namelist():
                    kl = k.lower().replace("/", "\\")
                    if "idnum2itemdisplaynametable.txt" in kl and not kl.endswith(".txt.txt"):
                        try:
                            raw = a.read(k).decode("cp1252", errors="replace")
                            for line in raw.splitlines():
                                parts = line.strip().split("#")
                                if len(parts) >= 2:
                                    try:
                                        nid = int(parts[0])
                                        n = parts[1].replace("_", " ").strip()
                                        if nid not in names and n:
                                            names[nid] = n
                                    except ValueError:
                                        pass
                        except Exception:
                            pass
                    elif "cardprefixnametable.txt" in kl and not kl.endswith(".txt.txt"):
                        try:
                            raw = a.read(k).decode("cp1252", errors="replace")
                            for line in raw.splitlines():
                                parts = line.strip().split("#")
                                if len(parts) >= 1:
                                    try:
                                        cards.add(int(parts[0]))
                                    except ValueError:
                                        pass
                        except Exception:
                            pass
                    elif "num2cardillustnametable.txt" in kl:
                        try:
                            raw = a.read(k).decode("cp1252", errors="replace")
                            for line in raw.splitlines():
                                parts = line.strip().split("#")
                                if len(parts) >= 1:
                                    try:
                                        cards.add(int(parts[0]))
                                    except ValueError:
                                        pass
                        except Exception:
                            pass
                    elif "itemslotcounttable.txt" in kl:
                        try:
                            raw = a.read(k).decode("cp1252", errors="replace")
                            for line in raw.splitlines():
                                parts = line.strip().split("#")
                                if len(parts) >= 1:
                                    try:
                                        equips.add(int(parts[0]))
                                    except ValueError:
                                        pass
                        except Exception:
                            pass
        except Exception as exc:
            print(f"[Terrain] Aviso tabelas GRF: {exc}", flush=True)

    _ITEM_CATALOG_CACHE = {
        "names": names,
        "cards": cards,
        "equips": equips,
    }
    return _ITEM_CATALOG_CACHE


def get_item_tier(nameid: int, item_name: str = "") -> int:
    """Classifica cada nameid em 3 tiers com regra estrita de segurança:
    Tier 1 (Valiosos / Essenciais): Cartas, Equipamentos, consumíveis/minérios valiosos.
    Tier 2 (Normal / Desconhecidos — Padrão Seguro): Todo item desconhecido ou não classificado.
    Tier 3 (Lixo Pesado — Lista Estrita): Apenas itens comuns verificados de baixo valor.
    """
    cat = load_item_catalog()
    name = item_name or cat["names"].get(nameid, "")
    norm_name = name.lower().strip()

    # Tier 1: Cartas
    if (nameid in cat["cards"]) or (4001 <= nameid <= 4750) or ("carta" in norm_name) or ("card" in norm_name):
        return 1
    # Tier 1: Equipamentos
    if (nameid in cat["equips"]) or (1100 <= nameid <= 2999) or (5001 <= nameid <= 5999) or (13000 <= nameid <= 19999):
        return 1
    # Tier 1: Minérios e consumíveis valiosos
    if (nameid in VALUABLE_IDS) or any(v in norm_name for v in VALUABLE_KEYWORDS):
        return 1

    # Tier 3: Lixo Pesado Verificado (Lista Estrita)
    if (nameid in TRASH_IDS) or any(t == norm_name or norm_name.startswith(t + " ") or norm_name.endswith(" " + t) for t in TRASH_KEYWORDS):
        return 3

    # Tier 2: Padrão Seguro (Desconhecidos / Não Classificados)
    return 2


# ── Catálogo Unificado de Monstros e Busca ──
_MOB_CATALOG_CACHE = None

IMMOBILE_KEYWORDS = {
    "pupa", "polen", "pólen", "cogumelo", "erva", "planta", "hidra",
    "ovo de andre", "ovo de besouro", "ovo de pecopeco", "ovo de draco",
    "ovo de scaraba", "ovo de chifres", "ovo de galhado", "ovo de ancinho",
    "ovo de buwaya", "ovo de dragao", "ovo de dragão", "ovo de verme",
    "ovo nivel", "ovo nível", "mandragora", "mandrágora", "flora",
    "rafflesia", "nepenthes", "parasita", "drosera", "muscipular"
}

PASSIVE_KEYWORDS = {
    "poring", "drops", "lunatico", "lunático", "fabre", "chonchon", "chon chon",
    "rocker", "esporo", "spore", "salgueiro", "willow", "creamy", "horn",
    "besouro chifre", "smokie", "fumacento", "marin", "poporing", "poring noel",
    "santa poring", "picky", "condor", "sapo de rodda", "rodda frog",
    "filhote de lobo", "baby desert wolf", "yoyo", "caramelo", "koko", "coco",
    "stapo", "magmaring"
}

KNOWN_MOB_PRESETS = {
    1001: {"level": 24, "hp": 1109, "hostile": True, "immobile": False, "spawns": ["moc_fild18", "moc_fild11"], "drops": [{"item_id": 905, "item_name": "Ferrão de Escorpião", "rate": 55.0}, {"item_id": 4009, "item_name": "Carta Escorpião", "rate": 0.01}]},
    1002: {"level": 1, "hp": 50, "hostile": False, "immobile": False, "spawns": ["prt_fild08", "prt_fild01", "pay_fild04"], "drops": [{"item_id": 909, "item_name": "Jellopy", "rate": 70.0}, {"item_id": 512, "item_name": "Maçã", "rate": 10.0}, {"item_id": 4001, "item_name": "Carta Poring", "rate": 0.01}]},
    1007: {"level": 2, "hp": 63, "hostile": False, "immobile": False, "spawns": ["gef_fild00", "prt_fild08"], "drops": [{"item_id": 914, "item_name": "Felpa", "rate": 70.0}, {"item_id": 705, "item_name": "Trevo", "rate": 10.0}, {"item_id": 4002, "item_name": "Carta Fabre", "rate": 0.01}]},
    1008: {"level": 2, "hp": 425, "hostile": False, "immobile": True, "spawns": ["prt_fild08", "gef_fild00"], "drops": [{"item_id": 942, "item_name": "Crisálida", "rate": 55.0}, {"item_id": 4003, "item_name": "Carta Pupa", "rate": 0.01}]},
    1009: {"level": 6, "hp": 148, "hostile": False, "immobile": False, "spawns": ["moc_fild07", "moc_fild12"], "drops": [{"item_id": 916, "item_name": "Pluma", "rate": 60.0}, {"item_id": 713, "item_name": "Garrafa Vazia", "rate": 20.0}, {"item_id": 4012, "item_name": "Carta Condor", "rate": 0.01}]},
    1010: {"level": 8, "hp": 185, "hostile": False, "immobile": False, "spawns": ["prt_fild08", "pay_fild01"], "drops": [{"item_id": 1019, "item_name": "Tronco", "rate": 65.0}, {"item_id": 4013, "item_name": "Carta Salgueiro", "rate": 0.01}]},
    1011: {"level": 4, "hp": 109, "hostile": False, "immobile": False, "spawns": ["prt_fild08", "gef_fild00"], "drops": [{"item_id": 934, "item_name": "Casca", "rate": 70.0}, {"item_id": 601, "item_name": "Asa de Mosca", "rate": 5.0}, {"item_id": 4014, "item_name": "Carta Chonchon", "rate": 0.01}]},
    1012: {"level": 7, "hp": 133, "hostile": False, "immobile": False, "spawns": ["gef_fild00", "prt_fild08"], "drops": [{"item_id": 951, "item_name": "Escama Aquática", "rate": 55.0}, {"item_id": 4015, "item_name": "Carta Sapo de Rodda", "rate": 0.01}]},
    1013: {"level": 25, "hp": 919, "hostile": True, "immobile": False, "spawns": ["pay_fild02", "prt_fild02"], "drops": [{"item_id": 923, "item_name": "Garra de Lobo", "rate": 55.0}, {"item_id": 578, "item_name": "Morango", "rate": 6.0}, {"item_id": 4016, "item_name": "Carta Lobo", "rate": 0.01}]},
    1014: {"level": 16, "hp": 510, "hostile": False, "immobile": False, "spawns": ["pay_fild08", "prt_fild05"], "drops": [{"item_id": 907, "item_name": "Esporo de Cogumelo", "rate": 60.0}, {"item_id": 578, "item_name": "Morango", "rate": 6.0}, {"item_id": 4017, "item_name": "Carta Esporo", "rate": 0.01}]},
    1015: {"level": 15, "hp": 505, "hostile": True, "immobile": False, "spawns": ["pay_dun00"], "drops": [{"item_id": 948, "item_name": "Mandíbula Horrenda", "rate": 55.0}, {"item_id": 4018, "item_name": "Carta Zumbi", "rate": 0.01}]},
    1016: {"level": 30, "hp": 1450, "hostile": True, "immobile": False, "spawns": ["pay_dun01"], "drops": [{"item_id": 1701, "item_name": "Arco", "rate": 1.5}, {"item_id": 4019, "item_name": "Carta Esqueleto Arqueiro", "rate": 0.01}]},
    1018: {"level": 14, "hp": 530, "hostile": False, "immobile": False, "spawns": ["prt_fild05", "gef_fild00"], "drops": [{"item_id": 602, "item_name": "Asa de Borboleta", "rate": 50.0}, {"item_id": 4021, "item_name": "Carta Creamy", "rate": 0.01}]},
    1019: {"level": 19, "hp": 768, "hostile": False, "immobile": False, "spawns": ["moc_fild07", "moc_fild12"], "drops": [{"item_id": 917, "item_name": "Pena de PecoPeco", "rate": 55.0}, {"item_id": 4022, "item_name": "Carta PecoPeco", "rate": 0.01}]},
    1023: {"level": 24, "hp": 1280, "hostile": True, "immobile": False, "spawns": ["gef_fild10", "gef_fild03"], "drops": [{"item_id": 929, "item_name": "Amuleto dos Orcs", "rate": 55.0}, {"item_id": 4026, "item_name": "Carta Guerreiro Orc", "rate": 0.01}]},
    1028: {"level": 27, "hp": 1320, "hostile": True, "immobile": False, "spawns": ["pay_dun01"], "drops": [{"item_id": 932, "item_name": "Osso", "rate": 50.0}, {"item_id": 4031, "item_name": "Carta Esqueleto Soldado", "rate": 0.01}]},
    1033: {"level": 23, "hp": 1030, "hostile": True, "immobile": False, "spawns": ["pay_fild07"], "drops": [{"item_id": 1019, "item_name": "Tronco", "rate": 55.0}, {"item_id": 4036, "item_name": "Carta Salgueiro Ancião", "rate": 0.01}]},
    1049: {"level": 4, "hp": 110, "hostile": False, "immobile": False, "spawns": ["moc_fild07"], "drops": [{"item_id": 916, "item_name": "Pluma", "rate": 60.0}, {"item_id": 4052, "item_name": "Carta Picky", "rate": 0.01}]},
    1052: {"level": 8, "hp": 215, "hostile": False, "immobile": False, "spawns": ["prt_fild05", "gef_fild00"], "drops": [{"item_id": 940, "item_name": "Pata de Inseto", "rate": 60.0}, {"item_id": 4055, "item_name": "Carta Rocker", "rate": 0.01}]},
    1060: {"level": 29, "hp": 1980, "hostile": False, "immobile": False, "spawns": ["pay_fild07"], "drops": [{"item_id": 927, "item_name": "Pata de Urso", "rate": 55.0}, {"item_id": 4063, "item_name": "Carta Pé-Grande", "rate": 0.01}]},
    1063: {"level": 3, "hp": 60, "hostile": False, "immobile": False, "spawns": ["prt_fild08"], "drops": [{"item_id": 705, "item_name": "Trevo", "rate": 70.0}, {"item_id": 4066, "item_name": "Carta Lunático", "rate": 0.01}]},
    1068: {"level": 12, "hp": 380, "hostile": False, "immobile": True, "spawns": ["iz_dun00", "pay_fild08"], "drops": [{"item_id": 963, "item_name": "Tentáculo", "rate": 55.0}, {"item_id": 4071, "item_name": "Carta Hidra", "rate": 0.01}]},
    1076: {"level": 10, "hp": 280, "hostile": True, "immobile": False, "spawns": ["pay_dun00"], "drops": [{"item_id": 932, "item_name": "Osso", "rate": 55.0}, {"item_id": 4079, "item_name": "Carta Esqueleto", "rate": 0.01}]},
    1078: {"level": 1, "hp": 10, "hostile": False, "immobile": True, "spawns": ["prt_fild08"], "drops": [{"item_id": 507, "item_name": "Erva Vermelha", "rate": 70.0}]},
    1079: {"level": 1, "hp": 10, "hostile": False, "immobile": True, "spawns": ["prt_fild08"], "drops": [{"item_id": 510, "item_name": "Erva Azul", "rate": 70.0}]},
    1080: {"level": 1, "hp": 10, "hostile": False, "immobile": True, "spawns": ["prt_fild08"], "drops": [{"item_id": 511, "item_name": "Erva Verde", "rate": 70.0}]},
    1081: {"level": 1, "hp": 10, "hostile": False, "immobile": True, "spawns": ["prt_fild08"], "drops": [{"item_id": 508, "item_name": "Erva Amarela", "rate": 70.0}]},
    1082: {"level": 1, "hp": 10, "hostile": False, "immobile": True, "spawns": ["prt_fild08"], "drops": [{"item_id": 509, "item_name": "Erva Branca", "rate": 70.0}]},
    1084: {"level": 5, "hp": 15, "hostile": False, "immobile": True, "spawns": ["prt_fild08", "gef_fild00"], "drops": [{"item_id": 907, "item_name": "Esporo de Cogumelo", "rate": 70.0}]},
    1085: {"level": 5, "hp": 15, "hostile": False, "immobile": True, "spawns": ["prt_fild08", "gef_fild00"], "drops": [{"item_id": 907, "item_name": "Esporo de Cogumelo", "rate": 70.0}]},
    1097: {"level": 3, "hp": 180, "hostile": False, "immobile": True, "spawns": ["anthell01"], "drops": [{"item_id": 4100, "item_name": "Carta Ovo de Andre", "rate": 0.01}]},
    1149: {"level": 54, "hp": 5985, "hostile": True, "immobile": False, "spawns": ["moc_pryd05"], "drops": [{"item_id": 928, "item_name": "Chifre de Boi", "rate": 50.0}, {"item_id": 4152, "item_name": "Carta Minorous", "rate": 0.01}]},
    1153: {"level": 45, "hp": 3254, "hostile": True, "immobile": False, "spawns": ["gef_dun01", "orcsdun01"], "drops": [{"item_id": 947, "item_name": "Pedaço de Pele", "rate": 55.0}, {"item_id": 1301, "item_name": "Machado", "rate": 1.5}, {"item_id": 4156, "item_name": "Carta Orc Zumbi", "rate": 0.01}]},
    1163: {"level": 65, "hp": 10500, "hostile": True, "immobile": False, "spawns": ["gl_knt01", "gl_knt02"], "drops": [{"item_id": 2314, "item_name": "Cota de Malha", "rate": 1.0}, {"item_id": 4166, "item_name": "Carta Raydric", "rate": 0.01}]},
    1189: {"level": 44, "hp": 3380, "hostile": True, "immobile": False, "spawns": ["gef_dun01", "orcsdun01"], "drops": [{"item_id": 1752, "item_name": "Flecha de Aço", "rate": 30.0}, {"item_id": 4192, "item_name": "Carta Orc Arqueiro", "rate": 0.01}]},
    1273: {"level": 40, "hp": 3500, "hostile": True, "immobile": False, "spawns": ["gef_fild10"], "drops": [{"item_id": 929, "item_name": "Amuleto dos Orcs", "rate": 55.0}, {"item_id": 4276, "item_name": "Carta Senhora Orc", "rate": 0.01}]},
    1782: {"level": 69, "hp": 6800, "hostile": True, "immobile": False, "spawns": ["ra_fild12", "ra_fild01"], "drops": [{"item_id": 7160, "item_name": "Pluma de Ave", "rate": 50.0}, {"item_id": 4385, "item_name": "Carta Rowen", "rate": 0.01}]},
}


def normalize_text(text: str) -> str:
    """Remove acentos, pontuação e normaliza para minúsculas."""
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFKD", str(text))
    clean = "".join(c for c in nfkd if not unicodedata.combining(c))
    return clean.lower().replace("-", " ").replace("_", " ").strip()


def get_mob_catalog(json_path="monstros-pt-br.json") -> dict:
    """Retorna índice unificado de monstros indexado por job_id e por nome normalizado,
    contendo job_id, name, level, hp, hostile, immobile, drops e spawns.
    """
    global _MOB_CATALOG_CACHE
    if _MOB_CATALOG_CACHE is not None:
        return _MOB_CATALOG_CACHE

    by_id = {}
    by_name = {}
    search_index = []

    raw_dict = {}
    candidates = [
        Path(json_path),
        _get_base_dir() / "monstros-pt-br.json",
        getattr(sys, "_MEIPASS", None) and (Path(sys._MEIPASS) / "monstros-pt-br.json"),
        Path(__file__).parent / "monstros-pt-br.json",
        Path("./monstros-pt-br.json"),
    ]
    for cand in candidates:
        if cand.is_file():
            try:
                raw_dict = json.loads(cand.read_text(encoding="utf-8"))
                break
            except Exception:
                pass

    for key_str, name in raw_dict.items():
        try:
            jid = int(key_str)
        except ValueError:
            continue
        norm_name = normalize_text(name)

        if jid in KNOWN_MOB_PRESETS:
            entry = dict(KNOWN_MOB_PRESETS[jid])
            entry["job_id"] = jid
            entry["name"] = name
        else:
            is_immobile = any(k in norm_name for k in IMMOBILE_KEYWORDS)
            if is_immobile:
                is_hostile = False
            elif any(k in norm_name for k in PASSIVE_KEYWORDS):
                is_hostile = False
            else:
                is_hostile = True

            level = min(99, max(1, (jid - 1000) // 18 + 5)) if jid >= 1000 else 10
            hp = level * 110 + 60
            spawns = []
            drops = [
                {"item_id": 4000 + (jid % 750), "item_name": f"Carta {name}", "rate": 0.01},
                {"item_id": 900, "item_name": f"Pele de {name}", "rate": 50.0},
            ]
            entry = {
                "job_id": jid,
                "name": name,
                "level": level,
                "hp": hp,
                "hostile": is_hostile,
                "immobile": is_immobile,
                "drops": drops,
                "spawns": spawns,
            }

        by_id[jid] = entry
        by_name[norm_name] = entry
        search_index.append(entry)

    _MOB_CATALOG_CACHE = {
        "by_id": by_id,
        "by_name": by_name,
        "search_index": search_index,
    }
    return _MOB_CATALOG_CACHE


def search_mob_catalog(query: str, base_level: int = 1) -> list[dict]:
    """Busca inteligente aceitando tanto nome de monstro quanto nome de item/carta.
    Ordena recomendando primeiro monstros mais adequados ao base_level e maior taxa de drop.
    """
    cat = get_mob_catalog()
    q_norm = normalize_text(query)
    if not q_norm:
        return []

    results = []
    seen_jobs = set()

    for mob in cat["search_index"]:
        m_name_norm = normalize_text(mob["name"])
        matched_drop = None
        if q_norm in m_name_norm or m_name_norm in q_norm:
            matched_drop = mob["drops"][0] if mob["drops"] else None
            for d in mob["drops"]:
                if "carta" in normalize_text(d["item_name"]):
                    matched_drop = d
                    break
        else:
            for d in mob["drops"]:
                d_norm = normalize_text(d["item_name"])
                if q_norm in d_norm or d_norm in q_norm:
                    matched_drop = d
                    break

        if matched_drop is not None:
            if mob["job_id"] not in seen_jobs:
                seen_jobs.add(mob["job_id"])
                results.append({
                    "job_id": mob["job_id"],
                    "name": mob["name"],
                    "level": mob["level"],
                    "hp": mob["hp"],
                    "hostile": mob["hostile"],
                    "immobile": mob["immobile"],
                    "spawns": mob["spawns"],
                    "matched_drop": matched_drop,
                    "level_diff": abs(mob["level"] - base_level),
                    "drop_rate": matched_drop["rate"] if matched_drop else 0.0,
                })

    results.sort(key=lambda x: (x["level_diff"], -x["drop_rate"]))
    return results

