"""Logique métier de Minia : extraction d'images via FFmpeg et écriture de la
miniature (cover art MP4 « covr ») sans réencodage de la vidéo."""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from mutagen.mp4 import MP4, MP4Cover

SUPPORTED = {".mp4", ".m4v", ".mov"}
IS_WINDOWS = sys.platform == "win32"
_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0  # CREATE_NO_WINDOW : pas de console qui clignote


class MiniaError(Exception):
    """Erreur affichable telle quelle à l'utilisateur."""


# --------------------------------------------------------------------------- outils

def find_tool(name: str) -> str:
    """Cherche ffmpeg/ffprobe à côté de l'application, puis dans le PATH."""
    exe = name + (".exe" if IS_WINDOWS else "")
    bases: list[Path] = []
    if getattr(sys, "frozen", False):
        bases.append(Path(sys.executable).parent)
        if hasattr(sys, "_MEIPASS"):
            bases.append(Path(sys._MEIPASS))
    else:
        bases.append(Path(__file__).resolve().parent)
    for base in bases:
        candidate = base / exe
        if candidate.is_file():
            return str(candidate)
    found = shutil.which(name)
    if found:
        return found
    raise MiniaError(
        f"{name} est introuvable. Installe FFmpeg et ajoute-le au PATH, "
        f"ou place {exe} à côté de Minia."
    )


def _run(args: list[str], timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, capture_output=True, timeout=timeout, creationflags=_NO_WINDOW
    )


# --------------------------------------------------------------------------- lecture

def probe_duration(path: str | Path) -> float:
    """Durée de la vidéo en secondes."""
    result = _run([
        find_tool("ffprobe"), "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json", str(path),
    ])
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise MiniaError(f"Impossible de lire la vidéo.\n{detail}")
    try:
        return float(json.loads(result.stdout)["format"]["duration"])
    except (KeyError, ValueError, TypeError):
        return 0.0


def extract_frame(path: str | Path, seconds: float, max_width: int = 1280) -> bytes:
    """Renvoie l'image (PNG) à l'instant donné. `0:V:0` ignore les covers déjà
    présentes pour toujours viser le vrai flux vidéo."""
    vf = (
        "scale=trunc(iw*sar/2)*2:ih,setsar=1,"           # pixels carrés
        f"scale='min({max_width},iw)':-2"                 # largeur max
    )
    result = _run([
        find_tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin",
        "-ss", f"{max(0.0, seconds):.3f}", "-i", str(path),
        "-map", "0:V:0", "-frames:v", "1", "-vf", vf,
        "-f", "image2pipe", "-c:v", "png", "-",
    ])
    return result.stdout if result.returncode == 0 else b""


def read_cover(path: str | Path) -> bytes | None:
    """Cover actuellement intégrée au fichier, s'il y en a une."""
    try:
        tags = MP4(str(path)).tags
    except Exception:
        return None
    if tags and tags.get("covr"):
        return bytes(tags["covr"][0])
    return None


# --------------------------------------------------------------------------- écriture

def write_cover(path: str | Path, jpeg: bytes) -> None:
    """Remplace la miniature de la vidéo.

    Le travail se fait sur une copie temporaire dans le même dossier, vérifiée
    puis substituée à l'original : en cas de problème, l'original reste intact.
    Le flux vidéo n'est jamais réencodé, seules les métadonnées changent.
    """
    src = Path(path)
    if not src.is_file():
        raise MiniaError(f"Fichier introuvable :\n{src}")
    if not os.access(src, os.W_OK):
        raise MiniaError("Le fichier est en lecture seule.")

    size = src.stat().st_size
    if shutil.disk_usage(src.parent).free < size + 50 * 1024 * 1024:
        raise MiniaError("Pas assez d'espace disque libre pour travailler en sécurité "
                         f"(il faut environ {size / 1024**3:.1f} Go disponibles).")

    tmp = src.with_name(f"{src.stem}.minia-{uuid.uuid4().hex[:8]}{src.suffix}.tmp")
    try:
        _copy(src, tmp)

        mp4 = MP4(str(tmp))
        if mp4.tags is None:
            mp4.add_tags()
        mp4.tags["covr"] = [MP4Cover(jpeg, imageformat=MP4Cover.FORMAT_JPEG)]
        mp4.save()

        # Vérification : la cover est bien là et la vidéo est toujours lisible.
        check = MP4(str(tmp))
        if not check.tags or not check.tags.get("covr"):
            raise MiniaError("La miniature n'a pas pu être enregistrée.")
        before, after = probe_duration(src), probe_duration(tmp)
        if abs(before - after) > 0.5:
            raise MiniaError("Vérification échouée : le fichier modifié ne semble pas sain. "
                             "L'original n'a pas été touché.")

        _replace(src, tmp)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass

    notify_shell(src)


def _copy(src: Path, dst: Path) -> None:
    if IS_WINDOWS and ctypes.windll.kernel32.CopyFileW(str(src), str(dst), True):
        return
    shutil.copyfile(src, dst)


def _replace(original: Path, replacement: Path) -> None:
    """Substitue le fichier. ReplaceFileW conserve la date de création,
    les attributs et les droits du fichier d'origine."""
    last_error = None
    for _ in range(10):  # l'Explorateur peut verrouiller brièvement le fichier
        if IS_WINDOWS:
            ok = ctypes.windll.kernel32.ReplaceFileW(
                str(original), str(replacement), None, 0x2, None, None
            )  # 0x2 = REPLACEFILE_IGNORE_MERGE_ERRORS
            if ok:
                return
            last_error = ctypes.WinError()
        else:
            try:
                os.replace(replacement, original)
                return
            except OSError as exc:
                last_error = exc
        time.sleep(0.3)
    raise MiniaError(
        "Impossible de remplacer le fichier : il est peut-être ouvert dans un autre "
        f"programme (lecteur vidéo, volet d'aperçu…).\n\n{last_error}"
    )


# --------------------------------------------------------------------------- Explorateur

def notify_shell(path: str | Path) -> None:
    """Prévient l'Explorateur que le fichier a changé pour qu'il régénère la miniature."""
    if not IS_WINDOWS:
        return
    shell32 = ctypes.windll.shell32
    SHCNE_UPDATEITEM, SHCNE_UPDATEDIR = 0x00002000, 0x00001000
    SHCNF_PATHW, SHCNF_FLUSHNOWAIT = 0x0005, 0x3000
    p = Path(path)
    shell32.SHChangeNotify(SHCNE_UPDATEITEM, SHCNF_PATHW | SHCNF_FLUSHNOWAIT,
                           ctypes.c_wchar_p(str(p)), None)
    shell32.SHChangeNotify(SHCNE_UPDATEDIR, SHCNF_PATHW | SHCNF_FLUSHNOWAIT,
                           ctypes.c_wchar_p(str(p.parent)), None)


def clear_thumbnail_cache() -> int:
    """Redémarre l'Explorateur et vide son cache de miniatures. Renvoie le
    nombre de fichiers de cache supprimés."""
    if not IS_WINDOWS:
        return 0
    folder = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Windows" / "Explorer"
    _run(["taskkill", "/f", "/im", "explorer.exe"], timeout=15)
    time.sleep(1.0)
    removed = 0
    for db in folder.glob("thumbcache_*.db"):
        try:
            db.unlink()
            removed += 1
        except OSError:
            pass
    DETACHED_PROCESS = 0x00000008
    subprocess.Popen(["explorer.exe"], creationflags=DETACHED_PROCESS, close_fds=True)
    return removed
