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

def write_cover(path: str | Path, jpeg: bytes | None) -> None:
    """Remplace la miniature de la vidéo, sans réencodage. Avec `jpeg=None`, la
    retire : l'Explorateur revient alors à son choix par défaut.

    Méthode rapide (presque tous les fichiers) : seul l'atome `moov` (l'index,
    quelques Mo au plus) est réécrit, en fin de fichier ; les données audio/vidéo
    ne sont ni lues ni déplacées. Sinon, repli sur une copie complète vérifiée.
    """
    src = Path(path)
    if not src.is_file():
        raise MiniaError(f"Fichier introuvable :\n{src}")
    if not os.access(src, os.W_OK):
        raise MiniaError("Le fichier est en lecture seule.")
    if jpeg is None and read_cover(src) is None:
        return  # rien à retirer

    layout = _top_level_atoms(src)
    if layout is not None:
        _write_cover_append(src, *layout, jpeg)
    else:
        _write_cover_copy(src, jpeg)
    notify_shell(src)


def _set_cover(mp4: MP4, jpeg: bytes | None) -> None:
    if jpeg is None:
        if mp4.tags is not None:
            mp4.tags.pop("covr", None)
        return
    if mp4.tags is None:
        mp4.add_tags()
    mp4.tags["covr"] = [MP4Cover(jpeg, imageformat=MP4Cover.FORMAT_JPEG)]


def _cover_matches(path: Path, jpeg: bytes | None) -> bool:
    """La cover du fichier est bien celle attendue (ou absente si `jpeg` est None)."""
    tags = MP4(str(path)).tags
    covers = tags.get("covr") if tags else None
    if jpeg is None:
        return not covers
    return bool(covers) and bytes(covers[0]) == jpeg


class _MoovOnlyTags(MP4Tags):
    """Tags mutagen qui ne touchent pas aux tables d'offsets (stco/co64).

    Utilisé sur un `moov` isolé en mémoire : dans le fichier final, le `moov`
    est placé après les données, dont les positions ne changent donc pas."""

    def _MP4Tags__update_offsets(self, *_args):  # remplace la méthode privée de MP4Tags
        pass


class _MoovOnlyMP4(MP4):
    MP4Tags = _MoovOnlyTags


_ZERO_ATOM = b"\0\0\0\0"
# Métadonnées XMP (Adobe) : `uuid` + cet identifiant. Windows n'arrive plus à lire une
# vidéo qui en contient une dès que son `moov` est placé après `mdat` (miniature vide,
# même sans cover) ; constaté sur des exports Adobe, quel que soit l'endroit du bloc.
_XMP_UUID = bytes.fromhex("be7acfcb97a942e89c71999491e3afac")
_MAX_TAIL = 1024 * 1024  # données parasites tolérées après le dernier atome


def _top_level_atoms(path: Path) -> tuple[list[tuple[bytes, int, int]], int, int] | None:
    """Analyse les atomes de premier niveau pour la méthode rapide.

    Renvoie (atomes, fin, début_queue) : les atomes (type, position, taille), la
    position où écrire le nouvel index, et le début d'une éventuelle « queue » de
    données parasites (certains sites ajoutent quelques octets après le dernier
    atome) à conserver telle quelle derrière le nouvel index. Renvoie None si la
    structure ne s'y prête pas (MP4 fragmenté, fichier tronqué, plusieurs `moov`…).

    Les restes d'une écriture interrompue (voir `_write_cover_append`) en fin de
    fichier sont ignorés et seront écrasés."""
    size = path.stat().st_size
    atoms = []
    tail = size
    with open(path, "rb") as f:
        pos = 0
        while pos < size:
            f.seek(pos)
            head = f.read(16)
            length = int.from_bytes(head[:4], "big") if len(head) >= 8 else 0
            kind = head[4:8]
            if len(head) >= 8 and length == 0 and kind == _ZERO_ATOM:
                break  # reste d'écriture interrompue : écrasé, avec ce qui le suit
            if length == 1:
                length = int.from_bytes(head[8:16], "big") if len(head) >= 16 else 0
            if len(head) < 8 or length < 8 or pos + length > size:
                if size - pos > _MAX_TAIL:  # pas une simple queue : fichier tronqué ou abîmé
                    return None
                tail = pos
                break
            atoms.append((kind, pos, length))
            pos += length
    end = pos
    # Un second `moov` en dernière position est un nouvel index dont l'écriture a été
    # interrompue avant le renommage de l'ancien : les lecteurs prennent le premier,
    # celui-ci est donc ignoré et sera écrasé.
    while len(atoms) > 1 and atoms[-1][0] == b"moov" and [a[0] for a in atoms].count(b"moov") > 1:
        end = atoms.pop()[1]
    kinds = [a[0] for a in atoms]
    if kinds.count(b"moov") != 1 or b"moof" in kinds:
        return None
    return atoms, end, tail


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
                        tail_start: int, jpeg: bytes | None) -> None:
    """Ajoute un nouveau `moov` (avec la cover) en fin de fichier, puis renomme
    l'ancien en `free` (4 octets). L'ancien index reste dans le fichier comme
    espace libre (quelques centaines de Ko à quelques Mo). Une éventuelle queue
    de données parasites est recopiée telle quelle derrière le nouvel index.

    Ordre choisi pour qu'une coupure à n'importe quel moment laisse un fichier lisible :
    1. corps du nouvel index (et queue) écrits après un en-tête laissé à zéro : tant que
       l'en-tête est nul, c'est un atome inconnu « jusqu'à la fin » que les lecteurs ignorent ;
    2. en-tête `moov` écrit : deux `moov`, les lecteurs prennent le premier (l'ancien) ;
    3. ancien `moov` renommé en `free` : seul le nouveau reste ;
    4. blocs XMP renommés en `free` (contenu conservé), sinon Windows ne lit plus la vidéo
       maintenant que l'index est en fin de fichier (voir `_XMP_UUID`)."""
    _, moov_pos, moov_len = next(a for a in atoms if a[0] == b"moov")
    duration = probe_duration(src)

    with open(src, "rb") as f:
        xmp = []
        for kind, pos, length in atoms:
            if kind == b"uuid" and length >= 24:
                f.seek(pos + 8)
                if f.read(16) == _XMP_UUID:
                    xmp.append(pos)
        f.seek(tail_start)
        tail = f.read()
        f.seek(moov_pos)
        buf = io.BytesIO(f.read(moov_len))
    try:
        mp4 = _MoovOnlyMP4(buf)
        _set_cover(mp4, jpeg)
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

    def restore(f):  # remet le fichier dans son état d'origine
        for pos in xmp:
            f.seek(pos + 4)
            f.write(b"uuid")
        f.seek(moov_pos + 4)
        f.write(b"moov")
        f.truncate(end)
        f.seek(end)
        f.write(tail)

    with _open_for_update(src) as f:
        try:
            f.seek(end)
            f.write(bytes(8))
            f.write(new_moov[8:])
            f.write(tail)
            f.truncate()  # retire un éventuel reste d'écriture interrompue
            sync(f)
            f.seek(end)
            f.write(new_moov[:8])
            sync(f)
            f.seek(moov_pos + 4)
            f.write(b"free")
            sync(f)
            for pos in xmp:
                f.seek(pos + 4)
                f.write(b"free")
            if xmp:
                sync(f)
        except BaseException:
            restore(f)
            raise

    # Vérification : la cover est bien celle attendue et la vidéo est toujours lisible.
    ok = False
    try:
        ok = _cover_matches(src, jpeg) and abs(probe_duration(src) - duration) <= 0.5
    except Exception:  # noqa: BLE001
        ok = False
    if not ok:
        with _open_for_update(src) as f:
            restore(f)
        raise MiniaError("Vérification échouée : la vidéo a été remise dans son état d'origine.")


def _write_cover_copy(src: Path, jpeg: bytes | None) -> None:
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
        _set_cover(mp4, jpeg)
        mp4.save()
        _fix_fragment_offsets(src, tmp)

        # Vérification : la cover est bien celle attendue et la vidéo est toujours lisible.
        if not _cover_matches(tmp, jpeg):
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


def _child_atoms(f, start: int, end: int):
    """Atomes (type, position, taille) contenus entre start et end."""
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        head = f.read(16)
        length, kind = int.from_bytes(head[:4], "big"), head[4:8]
        if length == 1 and len(head) >= 16:
            length = int.from_bytes(head[8:16], "big")
        if length < 8 or pos + length > end:
            return
        yield kind, pos, length
        pos += length


def _fix_fragment_offsets(original: Path, modified: Path) -> None:
    """MP4 fragmenté : recale les positions absolues des fragments après agrandissement
    de l'index. mutagen ne met à jour que la base (`tfhd`) du premier fragment, ce qui
    rend la suite de la vidéo illisible ; l'index `mfra/tfra` n'est jamais mis à jour.
    Les valeurs sont recalculées depuis l'original (décalage de chaque `moof`)."""
    with open(original, "rb") as fo, open(modified, "r+b") as fm:
        size_o, size_m = original.stat().st_size, modified.stat().st_size
        moofs_o = [a for a in _child_atoms(fo, 0, size_o) if a[0] == b"moof"]
        top_m = list(_child_atoms(fm, 0, size_m))
        moofs_m = [a for a in top_m if a[0] == b"moof"]
        if not moofs_o or len(moofs_o) != len(moofs_m):
            return
        moved = {o[1]: m[1] for o, m in zip(moofs_o, moofs_m)}

        for (_, pos_o, len_o), (_, pos_m, len_m) in zip(moofs_o, moofs_m):
            if len_o != len_m:
                raise MiniaError("Structure de la vidéo inattendue. Le fichier n'a pas été modifié.")
            delta = pos_m - pos_o
            for kind, traf, traf_len in _child_atoms(fo, pos_o + 8, pos_o + len_o):
                if kind != b"traf":
                    continue
                for kind2, tfhd, _ in _child_atoms(fo, traf + 8, traf + traf_len):
                    if kind2 != b"tfhd":
                        continue
                    fo.seek(tfhd + 8)
                    flags = int.from_bytes(fo.read(4)[1:], "big")
                    if flags & 0x1:  # base-data-offset présent (position absolue)
                        fo.seek(tfhd + 16)
                        base = int.from_bytes(fo.read(8), "big")
                        fm.seek(tfhd + delta + 16)
                        fm.write((base + delta).to_bytes(8, "big"))

        for kind, mfra, mfra_len in top_m:
            if kind != b"mfra":
                continue
            for kind2, tfra, _ in _child_atoms(fm, mfra + 8, mfra + mfra_len):
                if kind2 != b"tfra":
                    continue
                fm.seek(tfra + 8)
                version = fm.read(4)[0]
                fm.read(4)  # track_ID
                sizes = int.from_bytes(fm.read(4), "big")
                count = int.from_bytes(fm.read(4), "big")
                width = 8 if version == 1 else 4
                skip = ((sizes >> 4 & 3) + 1) + ((sizes >> 2 & 3) + 1) + ((sizes & 3) + 1)
                pos = tfra + 24
                for _ in range(count):
                    fm.seek(pos + width)
                    old = int.from_bytes(fm.read(width), "big")
                    if old in moved:
                        fm.seek(pos + width)
                        fm.write(moved[old].to_bytes(width, "big"))
                    pos += 2 * width + skip


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
