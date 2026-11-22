"""Unit tests for Gat A* pathfinding, corner cutting prevention, and rathena_feed features."""

import struct
import unittest
from rathena_terrain import Gat
from rathena_feed import World, map_name


def create_dummy_gat(width, height, wall_cells=None, water_cells=None):
    """Cria um buffer binário sintético de arquivo .gat com dimensões e paredes especificadas."""
    # Header: 4b 'GRAT' + 2b version + 4b width + 4b height = 14 bytes
    header = struct.pack("<4sHII", b"GRAT", 0x0102, width, height)
    cells = bytearray()
    wall_set = set(wall_cells) if wall_cells else set()
    water_set = set(water_cells) if water_cells else set()

    for y in range(height):
        for x in range(width):
            # sw, se, nw, ne (4 floats = 16 bytes)
            elevations = struct.pack("<4f", 0.0, 0.0, 0.0, 0.0)
            if (x, y) in wall_set:
                cell_type = 1  # Parede / obstáculo intransitável
            elif (x, y) in water_set:
                cell_type = 3  # Água rasa caminhável
            else:
                cell_type = 0  # Chão caminhável
            type_bytes = struct.pack("<I", cell_type)
            cells.extend(elevations + type_bytes)

    return header + bytes(cells)


class TestGatAStar(unittest.TestCase):
    def test_straight_path(self):
        # Grid 10x10 plano sem paredes
        data = create_dummy_gat(10, 10)
        gat = Gat(data)
        self.assertTrue(gat.is_walkable(0, 0))
        self.assertTrue(gat.is_walkable(9, 9))

        path = gat.find_path((0, 0), (5, 0))
        self.assertEqual(path[0], (0, 0))
        self.assertEqual(path[-1], (5, 0))
        self.assertEqual(len(path), 6)  # 0, 1, 2, 3, 4, 5

    def test_wall_obstacle_avoidance(self):
        # Grid 10x10 com uma parede vertical em x=5 de y=0 até y=7
        walls = [(5, y) for y in range(8)]
        data = create_dummy_gat(10, 10, wall_cells=walls)
        gat = Gat(data)

        # Inicia em (2, 2) e quer ir para (8, 2)
        path = gat.find_path((2, 2), (8, 2))
        self.assertTrue(len(path) > 0, "Deveria encontrar caminho desviando da parede")
        self.assertEqual(path[0], (2, 2))
        self.assertEqual(path[-1], (8, 2))

        # Nenhuma célula no caminho pode ser uma parede
        for cell in path:
            self.assertNotIn(cell, walls, f"Célula {cell} no caminho é uma parede!")
            self.assertTrue(gat.is_walkable(*cell))

        # O caminho deve desviar passando por y >= 8 (onde não há parede)
        max_y = max(y for x, y in path)
        self.assertGreaterEqual(max_y, 8, "O caminho deveria contornar a ponta da parede em y >= 8")

    def test_no_diagonal_corner_cutting(self):
        # Teste estrito de corte de quina:
        # Ponto A: (0, 0), Ponto B: (1, 1)
        # Se (1, 0) for parede, não pode cortar quina diretamente de (0,0) para (1,1)
        # Se (0, 1) for parede, também não pode cortar quina!
        walls = [(1, 0), (0, 1)]
        data = create_dummy_gat(5, 5, wall_cells=walls)
        gat = Gat(data)

        # De (0, 0) para (1, 1), com (1, 0) e (0, 1) bloqueados, (0, 0) está preso sem saída ortogonal
        path = gat.find_path((0, 0), (1, 1))
        self.assertEqual(path, [], "Não deve conseguir cortar a quina diagonal se ambos os lados são paredes")

        # Agora com apenas (1, 0) como parede:
        # Para ir de (0, 0) a (1, 1), deve primeiro andar para (0, 1) e depois para (1, 1)
        walls_single = [(1, 0)]
        data_single = create_dummy_gat(5, 5, wall_cells=walls_single)
        gat_single = Gat(data_single)

        path_single = gat_single.find_path((0, 0), (1, 1))
        self.assertEqual(path_single[0], (0, 0))
        self.assertEqual(path_single[-1], (1, 1))
        # O primeiro passo NÃO pode ser o salto diagonal (1, 1) cortando a quina da parede em (1, 0)
        self.assertNotEqual(path_single[1], (1, 1), "Não deve cortar quina da parede (1, 0)")
        self.assertEqual(path_single[1], (0, 1), "Deve dar passo ortogonal para (0, 1)")

    def test_pick_next_waypoint(self):
        data = create_dummy_gat(30, 30)
        gat = Gat(data)
        visited = [(15, 15), (16, 15), (17, 15)]
        wp = gat.pick_next_waypoint((15, 15), visited_cells=visited, min_dist=5, max_dist=10)
        self.assertIsNotNone(wp)
        self.assertTrue(gat.is_walkable(*wp))
        dist = max(abs(wp[0] - 15), abs(wp[1] - 15))
        self.assertGreaterEqual(dist, 5)


class TestFeedFeatures(unittest.TestCase):
    def test_map_detection_current_map(self):
        world = World(initial_map="prontera.gat")
        self.assertEqual(world.current_map, "prontera.gat")
        self.assertEqual(world.map, "prontera")

        # Simula pacote 0x0091 (ZC_NPCACK_MAPMOVE) com "gef_fild06.gat"
        # 0..2: opcode(2), 2..18: mapName(16), 18..20: x(2), 20..22: y(2)
        map_bytes = b"gef_fild06.gat\x00\x00"
        pkt_0091 = struct.pack("<H16sHH", 0x0091, map_bytes, 150, 200)
        world.consume(0x0091, pkt_0091)
        self.assertEqual(world.map, "gef_fild06")
        self.assertEqual(world.current_map, "gef_fild06.gat")
        self.assertEqual(world.player.target, (150, 200))

        # Simula pacote 0x0AC7 (ZC_MAPPROPERTY_R2 / ZC_NPCACK_SERVERMOVE) com "pay_fild04.gat"
        pkt_0ac7 = struct.pack("<H16sHH", 0x0AC7, b"pay_fild04.gat\x00\x00", 80, 90) + b"\x00" * 30
        world.consume(0x0AC7, pkt_0ac7)
        self.assertEqual(world.map, "pay_fild04")
        self.assertEqual(world.current_map, "pay_fild04.gat")
        self.assertEqual(world.player.target, (80, 90))

    def test_npc_and_warp_tracking(self):
        world = World(initial_map="gef_fild06")

        # Pacote 0x09FF (ZC_NOTIFY_STANDENTRY):
        # opcode(2)=0x09FF, len(2)=90, obj_type(1)=1 (NPC), AID(4)=110001, GID(4)=110001, speed(2)=150, ... job(2)=45 (Warp!)
        # Cria pacote de 90 bytes
        data_warp = bytearray(90)
        struct.pack_into("<HHBI", data_warp, 0, 0x09FF, 90, 1, 110001)
        struct.pack_into("<I", data_warp, 9, 110001)
        struct.pack_into("<H", data_warp, 13, 150)  # speed
        struct.pack_into("<H", data_warp, 23, 45)   # job = 45 (Warp portal!)
        # PosDir at offset 63: x=100, y=100, dir=0
        # (100 << 14) | (100 << 4) | 0
        data_warp[63] = (100 >> 2) & 0xFF
        data_warp[64] = ((100 & 3) << 6) | ((100 >> 4) & 0x3F)
        data_warp[65] = ((100 & 0x0F) << 4)

        world.consume(0x09FF, bytes(data_warp))
        warps = world.get_warps()
        self.assertEqual(len(warps), 1, "Deveria registrar entidade como warp")
        self.assertEqual(warps[0].catalog_id, 45)

        # Agora um NPC com job = 100
        data_npc = bytearray(90)
        struct.pack_into("<HHBI", data_npc, 0, 0x09FF, 90, 1, 110002)
        struct.pack_into("<I", data_npc, 9, 110002)
        struct.pack_into("<H", data_npc, 13, 150)
        struct.pack_into("<H", data_npc, 23, 100)  # job = 100 (NPC)
        data_npc[63] = (105 >> 2) & 0xFF
        data_npc[64] = ((105 & 3) << 6) | ((105 >> 4) & 0x3F)
        data_npc[65] = ((105 & 0x0F) << 4)

        world.consume(0x09FF, bytes(data_npc))
        npcs = world.get_npcs()
        self.assertEqual(len(npcs), 1, "Deveria registrar entidade como npc")
        self.assertEqual(npcs[0].catalog_id, 100)

    def test_npc_dialogue_packets(self):
        world = World(initial_map="gef_fild06")
        self.assertFalse(world.npc_dialog["open"])

        # 0x00B4: ZC_SAY_DIALOG (len 2, NAID 4, text)
        text_bytes = "Olá, aventureiro!".encode("cp1252") + b"\x00"
        pkt_b4 = struct.pack("<HHI", 0x00B4, 8 + len(text_bytes), 12345) + text_bytes
        world.consume(0x00B4, pkt_b4)
        self.assertTrue(world.npc_dialog["open"])
        self.assertEqual(world.npc_dialog["type"], "say")
        self.assertEqual(world.npc_dialog["npc_id"], 12345)
        self.assertEqual(world.npc_dialog["text"], "Olá, aventureiro!")

        # 0x00B5: ZC_WAIT_DIALOG (next)
        pkt_b5 = struct.pack("<HI", 0x00B5, 12345)
        world.consume(0x00B5, pkt_b5)
        self.assertTrue(world.npc_dialog["open"])
        self.assertEqual(world.npc_dialog["type"], "wait")

        # 0x00B7: ZC_MENU_LIST (menu)
        menu_bytes = "Ir para Prontera:Falar sobre missões:Sair".encode("cp1252") + b"\x00"
        pkt_b7 = struct.pack("<HHI", 0x00B7, 8 + len(menu_bytes), 12345) + menu_bytes
        world.consume(0x00B7, pkt_b7)
        self.assertTrue(world.npc_dialog["open"])
        self.assertEqual(world.npc_dialog["type"], "menu")
        self.assertEqual(len(world.npc_dialog["options"]), 3)
        self.assertEqual(world.npc_dialog["options"][0], "Ir para Prontera")

        # 0x00B6: ZC_CLOSE_DIALOG (close)
        pkt_b6 = struct.pack("<HI", 0x00B6, 12345)
        world.consume(0x00B6, pkt_b6)
        self.assertFalse(world.npc_dialog["open"])
        self.assertEqual(world.npc_dialog["type"], "close")


if __name__ == "__main__":
    unittest.main()
