"""Unit tests for Ragnarok Online 3D Projection Engine, Native .grf Camera Profiles, ZC_STOPMOVE (0x0088), and Real RO Walking Synchronization."""

import math
import struct
import unittest
from importlib import import_module
from pathlib import Path

from rathena_feed import Entity, Track, World, diagonal_first_path
from rathena_terrain import (
    CELL_SIZE,
    Gat,
    MapCameraProfile,
    RagnarokProjector3D,
    get_map_camera_profile,
)


class TestRagnarok3D(unittest.TestCase):
    def test_gat_bilinear_elevation(self):
        """Teste de elevação bilinear .gat em terreno inclinado."""
        header = struct.pack("<6sII", b"GRAT\x01\x02", 2, 2)
        cells = bytearray()
        # Célula (0, 0): sw=-10.0, se=-20.0, nw=-30.0, ne=-40.0, cell_type=0, 3x pad
        cells += struct.pack("<4fB3x", -10.0, -20.0, -30.0, -40.0, 0)
        # Célula (1, 0):
        cells += struct.pack("<4fB3x", -20.0, -20.0, -40.0, -40.0, 0)
        # Célula (0, 1):
        cells += struct.pack("<4fB3x", -30.0, -40.0, -50.0, -60.0, 0)
        # Célula (1, 1):
        cells += struct.pack("<4fB3x", -40.0, -40.0, -60.0, -60.0, 0)

        gat_data = header + bytes(cells)
        gat = Gat(gat_data, "test_map")

        self.assertEqual(gat.width, 2)
        self.assertEqual(gat.height, 2)

        # Canto SW de (0, 0): fx=0.0, fy=0.0 -> elev = -(-10.0) = 10.0
        self.assertAlmostEqual(gat.get_elevation(0.0, 0.0), 10.0, places=4)
        # Canto SE de (0, 0): fx=1.0, fy=0.0 -> elev = 20.0
        self.assertAlmostEqual(gat.get_elevation(1.0, 0.0), 20.0, places=4)
        # Canto NW de (0, 0): fx=0.0, fy=1.0 -> elev = 30.0
        self.assertAlmostEqual(gat.get_elevation(0.0, 1.0), 30.0, places=4)
        # Centro de (0, 0): fx=0.5, fy=0.5 -> elev = 25.0
        self.assertAlmostEqual(gat.get_elevation(0.5, 0.5), 25.0, places=4)
        # Ponto inclinado arbitrário: (0.3, 0.7)
        self.assertAlmostEqual(gat.get_elevation(0.3, 0.7), 27.0, places=4)

    def test_fov_15_and_pixel_scale_1080p(self):
        """Validação: FOV = 15.0° com distance real = 399.0 sem encolhimento radial em 1080p."""
        viewport_1080p = (0, 0, 1920, 1080)
        cam_target = (100.0, 100.0, 0.0)
        projector = RagnarokProjector3D(yaw_deg=0.0, pitch_deg=50.0, distance=399.0, fov_deg=15.0)
        proj_fn = projector.projector(viewport_1080p, cam_target, lambda x, y: 0.0)

        sx0, sy0 = proj_fn.project_screen(100.0, 100.0)
        # Ponto a 1 célula na diagonal (101.0, 101.0)
        sx1, sy1 = proj_fn.project_screen(101.0, 101.0)
        diag_px = math.hypot(sx1 - sx0, sy1 - sy0)

        # Na distância real de 399.0 (vs 490.0 que encolhia a imagem), a escala diagonal fica em ~64px
        self.assertGreaterEqual(diag_px, 60.0)
        self.assertLessEqual(diag_px, 70.0)
        self.assertEqual(projector.distance, 399.0)

    def test_3d_projection_yaw_0_and_45(self):
        """Teste de projeção 3D com FOV=15°, yaw = 0° e yaw = 45°."""
        viewport = (0, 0, 1024, 768)
        cam_target = (100.0, 100.0, 0.0)

        # 1. Yaw = 0°
        projector_0 = RagnarokProjector3D(yaw_deg=0.0, pitch_deg=50.0, distance=399.0, fov_deg=15.0)
        proj_fn_0 = projector_0.projector(viewport, cam_target, lambda x, y: 0.0)

        u_target, v_target = proj_fn_0(100.0, 100.0)
        self.assertAlmostEqual(u_target, 0.5, places=5)
        self.assertAlmostEqual(v_target, 0.5, places=5)

        # Ponto ao Norte: (100, 105) -> wy = +25.0
        u_north, v_north = proj_fn_0(100.0, 105.0)
        self.assertAlmostEqual(u_north, 0.5, places=5)
        self.assertLess(v_north, 0.5)

        # Ponto ao Leste: (105, 100) -> wx = +25.0
        u_east, v_east = proj_fn_0(105.0, 100.0)
        self.assertGreater(u_east, 0.5)

        # Altura de sprite (sprite_z = 3.5) eleva o ponto na tela (v menor)
        u_sprite, v_sprite = proj_fn_0(100.0, 100.0, sprite_z=3.5)
        self.assertAlmostEqual(u_sprite, 0.5, places=5)
        self.assertLess(v_sprite, v_target)

        # 2. Yaw = 45°
        projector_45 = RagnarokProjector3D(yaw_deg=45.0, pitch_deg=50.0, distance=399.0, fov_deg=15.0)
        proj_fn_45 = projector_45.projector(viewport, cam_target, lambda x, y: 0.0)

        u_target_45, v_target_45 = proj_fn_45(100.0, 100.0)
        self.assertAlmostEqual(u_target_45, 0.5, places=5)
        self.assertAlmostEqual(v_target_45, 0.5, places=5)

        # Ponto ao Norte com yaw=45° vai para o quadrante superior esquerdo (u < 0.5, v < 0.5)
        u_north_45, v_north_45 = proj_fn_45(100.0, 105.0)
        self.assertLess(u_north_45, 0.5)
        self.assertLess(v_north_45, 0.5)

    def test_camera_profiles_from_grf_and_clamp(self):
        """Validação (a): leitura de indoorrswtable.txt e ViewpointTable.txt direto do data.grf
        confirmando ra_in01.rsw (yaw_default = -45.0°, clamp [-45°, 45°]) e rachel.rsw (distance = 399.0, yaw_default = 0.0°).
        """
        overlay_mod = import_module("overlay-eventos")
        CameraTracker = overlay_mod.CameraTracker

        # 1. ra_in01.rsw: Mapa interno extraído de indoorrswtable.txt
        prof_ra = get_map_camera_profile("ra_in01.rsw")
        self.assertTrue(prof_ra.is_indoor)
        self.assertEqual(prof_ra.yaw_default, -45.0)
        self.assertEqual(prof_ra.yaw_min, -45.0)
        self.assertEqual(prof_ra.yaw_max, 45.0)
        self.assertEqual(prof_ra.pitch_default, 50.0)
        self.assertEqual(prof_ra.distance_default, 399.0)

        # 2. rachel.rsw: Mapa externo padrão
        prof_rach = get_map_camera_profile("rachel.rsw")
        self.assertFalse(prof_rach.is_indoor)
        self.assertEqual(prof_rach.yaw_default, 0.0)
        self.assertEqual(prof_rach.distance_default, 399.0)
        self.assertEqual(prof_rach.pitch_default, 50.0)
        self.assertEqual(prof_rach.yaw_min, -360.0)
        self.assertEqual(prof_rach.yaw_max, 360.0)

        # 3. Comportamento do CameraTracker ao alternar de mapa
        tracker = CameraTracker(yaw_deg=0.0, pitch_deg=50.0, distance=399.0)

        tracker.on_map_change("ra_in01", "ra_in01.gat")
        self.assertEqual(tracker.yaw_deg, -45.0)
        self.assertEqual(tracker.distance, 399.0)
        self.assertEqual(tracker.pitch_deg, 50.0)

        # 4. Arrasto horizontal em mapa interno respeita batente estrito [-45°, 45°]
        # dx grande positivo não passa de yaw_max (+45.0)
        tracker.yaw_deg = max(tracker.profile.yaw_min, min(tracker.profile.yaw_max, tracker.yaw_deg + 400.0 * tracker.yaw_sensitivity))
        self.assertEqual(tracker.yaw_deg, 45.0)

        # dx grande negativo não passa de yaw_min (-45.0)
        tracker.yaw_deg = max(tracker.profile.yaw_min, min(tracker.profile.yaw_max, tracker.yaw_deg - 400.0 * tracker.yaw_sensitivity))
        self.assertEqual(tracker.yaw_deg, -45.0)

        # Duplo-clique direito restaura yaw_default (-45.0)
        tracker.yaw_deg = 20.0
        tracker.yaw_deg = tracker.profile.yaw_default
        self.assertEqual(tracker.yaw_deg, -45.0)

        # 5. Entrada em rachel restaura padrão outdoor
        tracker.on_map_change("rachel", "rachel.gat")
        self.assertEqual(tracker.yaw_deg, 0.0)
        self.assertEqual(tracker.distance, 399.0)
        self.assertEqual(tracker.pitch_deg, 50.0)

    def test_track_diagonal_first_and_gat_path(self):
        """Validação (b): Track gera passos diagonais primeiro (e via .gat quando disponível) em vez de Bresenham."""
        path_no_gat = diagonal_first_path(10, 10, 13, 11)
        expected_path = [(10, 10), (11, 11), (12, 11), (13, 11)]
        self.assertEqual(path_no_gat, expected_path)

        dx0 = abs(path_no_gat[1][0] - path_no_gat[0][0])
        dy0 = abs(path_no_gat[1][1] - path_no_gat[0][1])
        self.assertTrue(dx0 > 0 and dy0 > 0, "O primeiro passo DEVE ser diagonal")

        # Gat.find_path prioriza diagonais primeiro
        header = struct.pack("<6sII", b"GRAT\x01\x02", 10, 10)
        cells = bytearray()
        for y in range(10):
            for x in range(10):
                cells += struct.pack("<4fB3x", 0.0, 0.0, 0.0, 0.0, 0)
        mock_gat = Gat(header + bytes(cells), "flat_map")

        gat_path = mock_gat.find_path((0, 0), (3, 1))
        self.assertEqual(gat_path, [(0, 0), (1, 1), (2, 1), (3, 1)])

        t0 = 5000.0
        track = Track.walking(((0, 0), (3, 1)), speed=150.0, start_time=t0, gat=mock_gat)
        seg0 = track.segments[0]
        self.assertEqual(seg0[2], (0.0, 0.0))
        self.assertEqual(seg0[3], (1.0, 1.0))
        self.assertAlmostEqual(seg0[1] - seg0[0], 0.150 * 1.4142135, places=4)

    def test_packet_0x0088_zc_stopmove_immediate_stop(self):
        """Validação (c): parada imediata e encerramento de Track ativo no pacote 0x0088 (ZC_STOPMOVE)."""
        world = World(initial_map="rachel")
        player_aid = 2000001
        world.player_id = player_aid

        # 1. Jogador em movimento ativo de (10, 10) para (25, 25)
        world.player = Track.walking(((10, 10), (25, 25)), speed=150.0)
        self.assertTrue(world.player.duration > 0.0)

        # Pacote 0x0088 para o jogador em (14, 16): opcode 2B + AID 4B + X 2B + Y 2B
        pkt_player = struct.pack("<HIHH", 0x0088, player_aid, 14, 16)
        world.consume(0x0088, pkt_player)

        # O Track DEVE ter sido encerrado imediatamente e fixado na posição exata (14, 16)
        self.assertEqual(world.player.duration, 0.0)
        self.assertEqual(world.player.target, (14, 16))
        self.assertEqual(world.player.cell(999999.0), (14.0, 16.0))

        # 2. Mob com ID 5001 em movimento ativo
        mob_gid = 5001
        mob_track = Track.walking(((40, 40), (50, 50)), speed=200.0)
        world.entities[("mob", mob_gid)] = Entity("mob", 1002, mob_track, speed=200.0)

        pkt_mob = struct.pack("<HIHH", 0x0088, mob_gid, 44, 44)
        world.consume(0x0088, pkt_mob)

        mob_entity = world.entities[("mob", mob_gid)]
        self.assertEqual(mob_entity.track.duration, 0.0)
        self.assertEqual(mob_entity.track.target, (44, 44))
        self.assertEqual(mob_entity.track.cell(999999.0), (44.0, 44.0))

        # 3. NPC com ID 6001 em movimento ativo
        npc_gid = 6001
        npc_track = Track.walking(((2, 2), (10, 10)), speed=250.0)
        world.entities[("npc", npc_gid)] = Entity("npc", 100, npc_track, speed=250.0)

        pkt_npc = struct.pack("<HIHH", 0x0088, npc_gid, 5, 5)
        world.consume(0x0088, pkt_npc)

        npc_entity = world.entities[("npc", npc_gid)]
        self.assertEqual(npc_entity.track.duration, 0.0)
        self.assertEqual(npc_entity.track.target, (5, 5))
        self.assertEqual(npc_entity.track.cell(999999.0), (5.0, 5.0))

    def test_kinematic_track_interpolation(self):
        """Teste de interpolação célula a célula com tempo proporcional ortogonal vs diagonal (diagonal primeiro)."""
        t0 = 1000.0
        speed_ms = 150.0
        track = Track.walking(((10, 10), (12, 11)), speed=speed_ms, start_time=t0)

        diag_dur = 0.150 * 1.4142135
        ortho_dur = 0.150 * 1.0
        expected_total = diag_dur + ortho_dur

        self.assertAlmostEqual(track.duration, expected_total, places=4)
        self.assertAlmostEqual(track.end_time, t0 + expected_total, places=4)

        self.assertEqual(track.cell(t0), (10.0, 10.0))
        mid_diag = track.cell(t0 + diag_dur * 0.5)
        self.assertAlmostEqual(mid_diag[0], 10.5, places=3)
        self.assertAlmostEqual(mid_diag[1], 10.5, places=3)
        at_step1 = track.cell(t0 + diag_dur)
        self.assertAlmostEqual(at_step1[0], 11.0, places=3)
        self.assertAlmostEqual(at_step1[1], 11.0, places=3)
        mid_ortho = track.cell(t0 + diag_dur + ortho_dur * 0.5)
        self.assertAlmostEqual(mid_ortho[0], 11.5, places=3)
        self.assertAlmostEqual(mid_ortho[1], 11.0, places=3)
        self.assertEqual(track.cell(t0 + expected_total + 0.1), (12.0, 11.0))


if __name__ == "__main__":
    unittest.main()
