"""Empacota a versão em código-fonte Python (leve, sem .exe) para envio.
Gera dist/Ragnalok-Python-Amigo.zip com os scripts .py, configs, assets,
Npcap e um atalho .bat que já pede Administrador automaticamente.
"""

import zipfile
from pathlib import Path


BAT_LAUNCHER = """@echo off
cd /d "%~dp0"
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Solicitando permissao de Administrador...
    powershell -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
echo Iniciando Ragnalok Overlay...
python overlay-eventos.py --live
if %errorlevel% neq 0 (
    echo.
    echo [Erro] Codigo de saida: %errorlevel%
    pause
)
"""

LEIA_ME_PYTHON = """========================================================================
              RAGNALOK OVERLAY & BOT (VERSAO PYTHON)
========================================================================

1. INSTALACAO DO NPCAP (Apenas na primeira vez):
   - Execute "1. Instalar_Npcap.exe"
   - Marque: [X] "Install Npcap in WinPcap API-compatible Mode"

2. DEPENDENCIAS PYTHON (Apenas na primeira vez, se ainda nao instalou):
   - Abra o terminal na pasta e rode:
     pip install scapy psutil pillow opencv-python numpy pystray pytesseract pyserial

3. INICIAR O OVERLAY:
   - Abra o Ragnarok e entre no mapa com seu personagem.
   - De dois cliques em: "2. Iniciar_Ragnalok_Admin.bat"
   - Clique em "Sim" na janela de Administrador do Windows.
========================================================================
"""


def main():
    root = Path(__file__).resolve().parent
    dist = root / "dist"
    dist.mkdir(exist_ok=True)
    zip_path = dist / "Ragnalok-Python-Amigo.zip"

    files = [
        "overlay-eventos.py",
        "rathena_feed.py",
        "rathena_terrain.py",
        "win32_input.py",
        "rathena_mock.py",
        "rathena-lengths.txt",
        "monstros-pt-br.json",
        "filters_config.json",
        "kafra_network.json",
        "learned_warps.json",
        "camera_calibration.json",
        "world_routes.json",
        "requirements-live.txt",
        "assets/inv_header.png",
        "assets/kafra_pin_ref.png",
    ]

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        npcap = root / "npcap-setup.exe"
        if npcap.is_file():
            zf.write(npcap, arcname="1. Instalar_Npcap.exe")
        for rel in files:
            p = root / rel
            if p.is_file():
                zf.write(p, arcname=rel)
            else:
                print(f"  - Aviso: {rel} nao encontrado, ignorando.")
        zf.writestr("2. Iniciar_Ragnalok_Admin.bat", BAT_LAUNCHER.replace("\n", "\r\n"))
        zf.writestr("LEIA-ME.txt", LEIA_ME_PYTHON.replace("\n", "\r\n"))

    size_mb = zip_path.stat().st_size / (1024 * 1024)
    print(f"Sucesso! Pacote Python pronto em: {zip_path} ({size_mb:.2f} MB)")


if __name__ == "__main__":
    main()

