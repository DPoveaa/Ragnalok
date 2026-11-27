# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [
    ('monstros-pt-br.json', '.'),
    ('rathena-lengths.txt', '.'),
    ('filters_config.json', '.'),
    ('kafra_network.json', '.'),
    ('learned_warps.json', '.'),
    ('camera_calibration.json', '.'),
    ('world_routes.json', '.'),
    ('assets', 'assets'),
]
binaries = []
hiddenimports = [
    'psutil',
    'tkinter',
    'PIL',
    'PIL.Image',
    'PIL.ImageTk',
    'PIL.ImageGrab',
    'PIL.ImageDraw',
    'cv2',
    'numpy',
    'serial',
    'pystray',
    'pytesseract',
    'win32_input',
    'rathena_feed',
    'rathena_terrain',
    'rathena_mock',
]
tmp_ret = collect_all('scapy')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['overlay-eventos.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Ragnalok_Overlay',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    uac_admin=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
