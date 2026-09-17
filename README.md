# hyde-theme-viewer

A small GTK4 app for browsing [HyDE](https://github.com/HyDE-Project/HyDE) themes: screenshots, wallpapers, and applying a theme, without leaving the GUI.

Pulls the theme list and images from the [hyde-gallery](https://github.com/HyDE-Project/hyde-gallery) repo (the same source `hyde-shell theme.import` uses), so no local git clone is needed just to browse.

## Requirements

- HyDE installed (uses `~/.local/lib/hyde/theme.patch.sh` to apply a theme)
- Python 3, PyGObject, GTK4, libadwaita
- optional: `gh` CLI logged in, to avoid GitHub's low unauthenticated API rate limit

## Running

```
python3 run.py
```

## Layout

- `hyde_theme_viewer/catalog.py` — fetches the theme list and images from GitHub
- `hyde_theme_viewer/window.py` — the GTK window
- `run.py` — entry point
