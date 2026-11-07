# BoxingSync Web

A local app for aligning boxing video and sensor signals, classifying three actions, and exporting a video with synchronized markers and optional Fish Audio voice cues.

## Start on Windows

1. Extract the ZIP. Make sure Python 3.10+ and FFmpeg are available in a new terminal. The FFmpeg build needs FFprobe, the `libx264` encoder, and the `ass` filter.
2. Double-click `Start_BoxingSync.bat`. The first run creates a project environment and installs dependencies. Leave the terminal open and visit [http://127.0.0.1:8765](http://127.0.0.1:8765).
3. In the app, choose the video and its signal CSV files, then click **Upload and analyze**.

If the launcher fails, run `.\Start_BoxingSync.bat` in PowerShell to keep its error visible. The launcher tries to restore a missing `pip` using `ensurepip`. If that fails and you have Conda, run these commands in the extracted project folder:

```powershell
conda create -n boxingsync python=3.12 pip -y
conda activate boxingsync
python -m pip install -r requirements.txt
python app.py --open
```

You can also use a working Python environment in PyCharm. Install `requirements.txt` and run `python app.py` from the project folder.

## Analyze and export

- **Session video** takes one video up to 180 seconds and 500 MiB.
- **Sensor signals** takes either a combined CSV or two CSVs. In a two-file upload, the role column identifies each device. Each CSV can be up to 5 MiB.
- **Upload and analyze** starts automatic tracking, time alignment, action classification, and rendering. **Try sample** reproduces the action locations and labels from the first-edition example.
- The results show **Helmet hit**, **Body block**, and **Dodge**. Click an event or a linked signal point to jump to the annotated video frame. Video coordinates are pixels in the 960 × 540 export.
- Download the annotated MP4, events CSV, and aligned signals CSV. The signals CSV has both original `trigger` records and `action` marker records. It includes the action label, both clocks, and video coordinates. An estimated sensor time is flagged when there is no direct trigger for that action.

## Tracking and alignment

The CSV uses `t_s,kind,role,peak_V`, with optional `seq,synced`. Device 1 is the glove and device 2 is the helmet. Time is in seconds. Both sensor files must share the sensor clock. The app uses raw `EVENT` rows and shows discrete voltage peaks.

Automatic tracking uses the equipment appearance from the included sample. It works best with comparable gloves, helmet, and a fixed side view. In **Advanced setup**, you can provide search areas and example boxes for a different camera or equipment. A body block includes contact with the torso, shoulder, or arm. Inconclusive events are omitted from the action results.

To align the video clock with the sensor clock, the app matches visually detected glove approaches to sensor contacts, estimates an initial offset, and estimates drift when anchors span enough of the recording. The results show the number of anchors and alignment fit error. If matching fails, enter corresponding timestamps under **Advanced setup → Manual sync anchors**, one video and sensor time pair per line, in seconds. Use two or more well-separated contacts where available.

```text
1.467, 2.9019
33.933, 35.4829
```

## Fish Audio

Copy `.env.example` to `.env` in the directory containing `app.py`. Set `FISH_AUDIO_API_KEY` in that file and restart the server. The backend reads the key at startup and adds the voice cues automatically during analysis. The page displays the audio result.

The app requests three spoken labels from the [Fish Audio Text to Speech endpoint](https://docs.fish.audio/api-reference/endpoint/openapi-v1/text-to-speech) and adds the relevant cues at the video action times.

| Action | Cue |
|---|---|
| Helmet hit | Hit! |
| Body block | Blocked! |
| Dodge | Dodge! |

Only the fixed cue text and voice configuration are sent to Fish Audio. Video and sensor data are processed locally. If the service rejects the request, the result shows the provider error and retains the downloadable video. After correcting the key or service issue, restart the app and run the analysis again. You can change `FISH_AUDIO_MODEL` and `FISH_AUDIO_VOICE_ID` in `.env`. Audio requires a working Fish Audio key and service access.

Uploads, results, and synthesized cue clips are stored under `local_data/` by default. The app is intended to run on localhost.
