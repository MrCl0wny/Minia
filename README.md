# Minia

Change la miniature de tes vidéos MP4 dans l'Explorateur Windows en choisissant une image de la vidéo.

## Utilisation

1. Lance `dist\Minia.exe` (ou `python app.py`).
2. Glisse une ou plusieurs vidéos (ou un dossier) dans la fenêtre.
3. Clique sur une des 12 images proposées. **↻ Autres images** (touche `R`) en tire une nouvelle série.
4. **Appliquer comme miniature** (touche `Entrée`). Si plusieurs vidéos ont été déposées, Minia passe automatiquement à la suivante.

On peut aussi glisser des vidéos directement sur `Minia.exe`.

## Ce que fait Minia

- L'image choisie est intégrée au fichier comme « cover art » MP4 (atome `covr`), que l'Explorateur Windows utilise comme miniature.
- La vidéo et le son **ne sont jamais réencodés** : ils restent identiques à l'octet près.
- Le travail se fait sur une copie temporaire dans le même dossier. Elle est vérifiée, puis remplace l'original. En cas d'erreur, l'original n'est pas touché. Il faut donc autant d'espace libre que la taille de la vidéo.
- La date de création du fichier est conservée. La date de modification devient la date du jour, ce qui force Windows à régénérer la miniature.

Si l'Explorateur affiche encore l'ancienne miniature, utilise le lien **« La miniature ne change pas dans l'Explorateur ? »**. Il vide le cache de miniatures de Windows et redémarre l'Explorateur.

## Prérequis

- FFmpeg (`ffmpeg.exe` et `ffprobe.exe`) dans le PATH, ou placés à côté de `Minia.exe`.
- Pour lancer depuis les sources : Python 3.11+ et `pip install -r requirements.txt`.

## Recompiler l'exe

```
build.bat
```

Le résultat est `dist\Minia.exe`.

## Fichiers

- `app.py` : l'interface (PySide6, thème sombre).
- `core.py` : l'extraction des images (FFmpeg), l'écriture de la cover (mutagen) et le rafraîchissement de l'Explorateur.
