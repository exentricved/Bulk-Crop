# Bulk Crop

Set crops for many videos in one sitting; encoding (lossless H.264) runs in the background.

## Quick start (Windows)

1. Download **BulkCrop-windows.zip** from the [latest release](https://github.com/exentricved/Bulk-Crop/releases/latest) and unzip it anywhere.
2. Double-click **BulkCrop.exe**. No Python or ffmpeg install needed (ffmpeg is included).
3. Pick the top folder that holds your videos. Output goes to a parallel `<folder>_cropped` tree with the same subfolders.
4. For each video: drag a crop box and press **Enter** (or **U** to copy it uncropped). Encoding starts right away while you move on.
5. Close the window whenever you like. If work is still pending, choose **Yes** to keep encoding in the background.

Reopen the same folder at any time to pick up where you left off. Progress is saved in
`<output>/_bulk_crop_state.json`, and a log is kept in `<output>/_bulk_crop_log.txt`.

If Windows SmartScreen warns about an unrecognised app, click **More info → Run anyway**.

## Running from source

Requires Python 3 and ffmpeg/ffprobe on PATH (`winget install Gyan.FFmpeg`).

```
pythonw bulk_crop.py                       choose folders with dialogs
pythonw bulk_crop.py INPUT [-o OUTPUT]     skip the folder dialogs
python  bulk_crop.py --process STATE.json  headless: finish queued jobs, then exit
```

Or double-click `Bulk Crop.bat`.

## Building the exe

```
pip install pyinstaller
pyinstaller --onefile --windowed --name BulkCrop bulk_crop.py
```

Put `ffmpeg.exe` and `ffprobe.exe` next to `BulkCrop.exe`; they are used ahead of any on PATH.
