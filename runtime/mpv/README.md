# mpv runtime

The timeline player uses the official mpv `v0.41.0` Windows x86-64 MSVC
release. Binary files are fetched at build time by
`python packaging/fetch_mpv.py` and are intentionally not stored in Git.

- Source: https://github.com/mpv-player/mpv/releases/tag/v0.41.0
- Archive: `mpv-v0.41.0-x86_64-pc-windows-msvc.zip`
- SHA-256: `4e197f729f5071c6772f35fffd96e0f36e3e8a044bd9479b136bb09b7c6a80ff`
- License and notices: https://github.com/mpv-player/mpv/blob/v0.41.0/Copyright

Only `mpv.exe` and `vulkan-1.dll` are included in the packaged application.
Debug symbols and registration scripts are excluded.
