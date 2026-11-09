# AudioMultiCut

Cut multiple segments out of a long recording — lectures, rehearsals, lessons, shows.
Load the file, mark where the cuts go, fine-tune each segment to the millisecond, then
save them to a folder or download them.

Everything runs on your own Mac. The audio is never uploaded anywhere.

Supports `.m4a`, `.mp3`, `.wav`, `.aac`, `.flac`, `.ogg`, `.opus`, `.m4b`, `.mp4`.

## Setup (once)

```bash
brew install ffmpeg
```

That's the only requirement — no pip packages, and Python 3 is already on your Mac.

## Running it

```bash
cd "~/Desktop/Projects/Trim Audio"
python3 trim_audio.py
```

Your browser opens automatically. `Ctrl+C` in the Terminal stops it.

## Using it

Drop the audio file on the page (or click to browse). The header shows its length,
sample rate and channels, and the waveform appears below.

### Three ways to make segments

| Tab | What it does |
| --- | --- |
| **Manual** | Play the recording and press **M** (or click Mark) wherever you want a cut. |
| **Auto-Cut** | Finds the silent gaps and turns everything between them into segments. |
| **Chunks** | Slices the whole file into equal parts — 15 / 30 / 45 / 50 / 60 min, or your own. |

All three feed the same segment list, so you can start with Chunks and adjust by hand.

### Player

Play/pause with **Space**, click the waveform to seek, drag an orange marker to move a
cut, or drag it up off the waveform to delete it. The speed menu goes from 0.5× to 2×.
Turn **Hotkeys** off if you want Space and M to type normally.

### Fine-Tune Segments

Each segment has an editable name, a duration badge, and **Start** / **End** fields in
seconds to three decimals. **Full** plays the whole segment; **Play the end** jumps to
its last 5 seconds, which is the quick way to check a boundary. **Download** cuts just
that one segment.

**Prevent overlap** (on by default) keeps segments in order: where two segments touch,
moving the boundary moves both sides together. Turn it off to move each edge freely and
allow overlapping segments.

### Getting the files out

- **Cut & save to folder** writes everything to `<filename>_parts/` next to the original,
  then **Show in Finder** opens it.
- **Download all MP3 ZIP** downloads just the segments from that run — older files
  sitting in the folder are not included.

## Notes

- **MP3 output re-encodes**, so cuts land exactly where you set them (within ~25ms).
  **Same as input** stream-copies instead: much faster, but cuts snap to the nearest
  keyframe and can land up to about a second off. Use MP3 when the exact boundary
  matters. Per-segment **Download** follows the format dropdown; **Download all MP3
  ZIP** is always MP3, as its name says.
- **Your session is remembered** — segments, names and settings come back if you reload
  or reopen the page. **Clear session** wipes it and starts over.
- **If a dropped file isn't found automatically**, the page asks you to paste its full
  path. Browsers won't hand a real path to a local app, so the file is matched by name
  and size across Desktop, Downloads, Documents, Movies and Music. For anything
  elsewhere: right-click it in Finder, hold Option, "Copy as Pathname".
- Segment names become filenames; `/` and other illegal characters are stripped, and
  duplicates get `-2`, `-3` suffixes.
- The server listens on `127.0.0.1` only, so it isn't reachable from other machines.
