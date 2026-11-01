# Bulk Crop

Crop videos and/or black out parts of them (e.g. burned-in patient details) for many videos in one
sitting; encoding (lossless H.264) runs in the background.

## Quick start (Windows)

1. Download **BulkCrop-windows.zip** from the [latest release](https://github.com/exentricved/Bulk-Crop/releases/latest) and unzip it anywhere.
2. Double-click **BulkCrop.exe**. No Python or ffmpeg install needed (ffmpeg is included).
3. Pick the top folder that holds your videos. Output goes to a parallel `<folder>_cropped` tree with the same subfolders.
4. Find the part of the video you want: the **filmstrip** under the slider shows the whole video at a glance.
   Click or drag on it (or the slider) to scrub instantly; mouse wheel or **[ ]** step frame by frame.
5. Draw boxes on the frame:
   - **Crop box** (yellow): the area to keep.
   - **Black-out box** (red outline, filled black): areas to hide. Draw as many as you like; right-click one to delete it.
   - Press **B** to switch between the two. Boxes are always stored in the original video's pixels.
6. Press **Enter** to save and go to the next video (or **U** to copy it unchanged). Encoding starts right away.
   - Your boxes carry over to the next video, so the same layout is just **Enter**.
   - **Apply to all remaining [A]** saves the boxes on every undecided video with the same frame size in one go.
7. Close the window whenever you like. If work is still pending, choose **Yes** to keep encoding in the background.

Reopen the same folder at any time to pick up where you left off. Progress is saved in
`<output>/_bulk_crop_state.json`, and a log is kept in `<output>/_bulk_crop_log.txt`.

If Windows SmartScreen warns about an unrecognised app, click **More info → Run anyway**.

### Updating

When a newer release is out, an **⬆ Update to vX** button appears at the top of the window. Click it to
download and install the new version, then restart. Any background encoding keeps going on the old version until
it finishes. (Updating from v1.1.0 or earlier is a one-time manual download.)

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

To release: bump `VERSION` in `bulk_crop.py`, tag it `vX.Y.Z`, and attach `BulkCrop.exe` (the in-app updater
downloads this asset) and `BulkCrop-windows.zip` to a GitHub release with that tag.
