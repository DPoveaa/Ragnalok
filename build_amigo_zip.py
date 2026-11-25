"""Script de automação para compilar e empacotar o Ragnalok para envio.
Gera dist/Ragnalok_Overlay.exe e dist/Ragnalok-Amigo.zip com o Npcap e manual inclusos.
"""

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent
    dist = root / "dist"
    spec = root / "Ragnalok_Overlay.spec"
    npcap_setup = root / "npcap-setup.exe"
    leia_me = root / "LEIA-ME.txt"

    if not spec.is_file():
        print(f"Erro: Arquivo spec não encontrado em {spec}")
        sys.exit(1)

    # Limpa build e exe anterior para evitar locks de arquivo no .pkg e no .exe
    build_dir = root / "build"
    if build_dir.exists():
        shutil.rmtree(build_dir, ignore_errors=True)

    target_exe = dist / "Ragnalok_Overlay.exe"
    if target_exe.exists():
        try:
            target_exe.unlink()
        except OSError:
            try:
                temp_old = dist / f"Ragnalok_Overlay_old_{os.getpid()}.exe"
                target_exe.rename(temp_old)
                temp_old.unlink(missing_ok=True)
            except OSError:
                pass

    print("==================================================")
    print(" 1. Compilando com PyInstaller...")
    print("==================================================")
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", str(spec)]
    res = subprocess.run(cmd, cwd=str(root))
    if res.returncode != 0:
        print(f"Erro na compilação (código {res.returncode})")
        sys.exit(res.returncode)

    exe_path = dist / "Ragnalok_Overlay.exe"
    if not exe_path.is_file():
        print(f"Erro: Executável não gerado em {exe_path}")
        sys.exit(1)

    print("\n==================================================")
    print(" 2. Criando pacote ZIP para o amigo...")
    print("==================================================")
    zip_path = dist / "Ragnalok-Amigo.zip"

    files_to_pack = [
        ("1. Instalar_Npcap.exe", npcap_setup),
        ("2. Ragnalok_Overlay.exe", exe_path),
        ("LEIA-ME.txt", leia_me),
        ("filters_config.json", root / "filters_config.json"),
        ("kafra_network.json", root / "kafra_network.json"),
        ("learned_warps.json", root / "learned_warps.json"),
    ]

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for archive_name, src_path in files_to_pack:
            if src_path.is_file():
                print(f"  + Adicionando: {archive_name} ({src_path.stat().st_size:,} bytes)")
                zf.write(src_path, arcname=archive_name)
            else:
                print(f"  - Aviso: {src_path} não encontrado, ignorando.")

    zip_size_mb = zip_path.stat().st_size / (1024 * 1024)
    print(f"\nSucesso! Pacote pronto em:")
    print(f"  -> {zip_path} ({zip_size_mb:.2f} MB)")
    print("Basta enviar esse arquivo .zip para seu amigo!")


if __name__ == "__main__":
    main()
