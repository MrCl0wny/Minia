"""Logique métier de Minia : extraction d'images via FFmpeg et écriture de la
miniature (cover art MP4 « covr ») sans réencodage de la vidéo."""

from __future__ import annotations

import ctypes
import io
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from mutagen.mp4 import MP4, MP4Cover, MP4Tags

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
    """Remplace la miniature de la vidéo, sans réencodage.

    Méthode rapide (presque tous les fichiers) : seul l'atome `moov` (l'index,
    quelques Mo au plus) est réécrit, en fin de fichier ; les données audio/vidéo
    ne sont ni lues ni déplacées. Sinon, repli sur une copie complète vérifiée.
    """
    src = Path(path)
    if not src.is_file():
        raise MiniaError(f"Fichier introuvable :\n{src}")
    if not os.access(src, os.W_OK):
        raise MiniaError("Le fichier est en lecture seule.")

    layout = _top_level_atoms(src)
    if layout is not None:
        _write_cover_append(src, *layout, jpeg)
    else:
        _write_cover_copy(src, jpeg)
    notify_shell(src)


class _MoovOnlyTags(MP4Tags):
    """Tags mutagen qui ne touchent pas aux tables d'offsets (stco/co64).

    Utilisé sur un `moov` isolé en mémoire : dans le fichier final, le `moov`
    est placé après les données, dont les positions ne changent donc pas."""

    def _MP4Tags__update_offsets(self, *_args):  # remplace la méthode privée de MP4Tags
        pass


class _MoovOnlyMP4(MP4):
    MP4Tags = _MoovOnlyTags


_ZERO_ATOM = b"\0\0\0\0"


def _top_level_atoms(path: Path) -> tuple[list[tuple[bytes, int, int]], int] | None:
    """Atomes de premier niveau (type, position, taille) et position de fin utile,
    ou None si la structure ne permet pas la méthode rapide (MP4 fragmenté,
    données parasites en fin de fichier, plusieurs `moov`…).

    Les restes d'une écriture interrompue (voir `_write_cover_append`) en fin de
    fichier sont ignorés et seront écrasés."""
    size = path.stat().st_size
    atoms = []
    with open(path, "rb") as f:
        pos = 0
        while pos < size:
            f.seek(pos)
            head = f.read(16)
            if len(head) < 8:
                return None
            length = int.from_bytes(head[:4], "big")
            kind = head[4:8]
            if length == 0 and kind == _ZERO_ATOM:
                break
            if length == 1:
                if len(head) < 16:
                    return None
                length = int.from_bytes(head[8:16], "big")
            if length < 8 or pos + length > size:  # 0 = « jusqu'à la fin », ou fichier tronqué
                return None
            atoms.append((kind, pos, length))
            pos += length
    # Un second `moov` en dernière position est un nouvel index dont l'écriture a été
    # interrompue avant le renommage de l'ancien : les lecteurs prennent le premier,
    # celui-ci est donc ignoré et sera écrasé.
    while len(atoms) > 1 and atoms[-1][0] == b"moov" and [a[0] for a in atoms].count(b"moov") > 1:
        pos = atoms.pop()[1]
    kinds = [a[0] for a in atoms]
    if kinds.count(b"moov") != 1 or b"moof" in kinds:
        return None
    return atoms, pos


def _open_for_update(path: Path):
    last_error = None
    for _ in range(10):  # l'Explorateur peut verrouiller brièvement le fichier
        try:
            return open(path, "r+b")
        except PermissionError as exc:
            last_error = exc
            time.sleep(0.3)
    raise MiniaError(
        "Impossible d'ouvrir le fichier en écriture : il est peut-être ouvert dans un autre "
        f"programme (lecteur vidéo, volet d'aperçu…).\n\n{last_error}"
    )


def _write_cover_append(src: Path, atoms: list[tuple[bytes, int, int]], end: int,
                        jpeg: bytes) -> None:
    """Ajoute un nouveau `moov` (avec la cover) en fin de fichier, puis renomme
    l'ancien en `free` (4 octets). L'ancien index reste dans le fichier comme
    espace libre (quelques centaines de Ko à quelques Mo).

    Ordre choisi pour qu'une coupure à n'importe quel moment laisse un fichier lisible :
    1. corps du nouvel index écrit après un en-tête laissé à zéro : tant que l'en-tête
       est nul, c'est un atome inconnu « jusqu'à la fin » que les lecteurs ignorent ;
    2. en-tête `moov` écrit : deux `moov`, les lecteurs prennent le premier (l'ancien) ;
    3. ancien `moov` renommé en `free` : seul le nouveau reste."""
    _, moov_pos, moov_len = next(a for a in atoms if a[0] == b"moov")
    duration = probe_duration(src)

    with open(src, "rb") as f:
        f.seek(moov_pos)
        buf = io.BytesIO(f.read(moov_len))
    try:
        mp4 = _MoovOnlyMP4(buf)
        if mp4.tags is None:
            mp4.add_tags()
        mp4.tags["covr"] = [MP4Cover(jpeg, imageformat=MP4Cover.FORMAT_JPEG)]
        buf.seek(0)
        mp4.save(buf)
    except Exception as exc:  # noqa: BLE001
        raise MiniaError(f"Impossible de préparer la miniature :\n{exc}") from exc
    new_moov = buf.getvalue()
    if new_moov[4:8] != b"moov":
        raise MiniaError("Structure de la vidéo inattendue. Le fichier n'a pas été modifié.")

    if shutil.disk_usage(src.parent).free < len(new_moov) + 10 * 1024 * 1024:
        raise MiniaError("Pas assez d'espace disque libre.")

    def sync(f):
        f.flush()
        os.fsync(f.fileno())

    with _open_for_update(src) as f:
        try:
            f.truncate(end)  # retire un éventuel reste d'écriture interrompue
            f.seek(end)
            f.write(bytes(8))
            f.write(new_moov[8:])
            sync(f)
            f.seek(end)
            f.write(new_moov[:8])
            sync(f)
            f.seek(moov_pos + 4)
            f.write(b"free")
            sync(f)
        except BaseException:
            f.seek(moov_pos + 4)  # remet le fichier exactement dans son état d'origine
            f.write(b"moov")
            f.truncate(end)
            raise

    # Vérification : la cover est bien là et la vidéo est toujours lisible.
    ok = False
    try:
        tags = MP4(str(src)).tags
        ok = (bool(tags) and bytes(tags.get("covr", [b""])[0]) == jpeg
              and abs(probe_duration(src) - duration) <= 0.5)
    except Exception:  # noqa: BLE001
        ok = False
    if not ok:
        with _open_for_update(src) as f:
            f.seek(moov_pos + 4)
            f.write(b"moov")
            f.truncate(end)
        raise MiniaError("Vérification échouée : la vidéo a été remise dans son état d'origine.")


def _write_cover_copy(src: Path, jpeg: bytes) -> None:
    """Repli pour les structures inhabituelles : copie temporaire dans le même
    dossier → modification → vérification → ReplaceFileW."""
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
