"""Normalize video and run the synchronized analysis pipeline."""
import json
import subprocess
import tempfile
from pathlib import Path

import numpy as np

from detect import fuse, recover_high_blocks
from outputs import write_outputs
from sensors import align, device_pairs, read_events
from vision import approaches, bounds, FPS, HEIGHT, track, WIDTH


def normalize(video, target):
    # Phone MOV files can have variable frame cadence. FFmpeg uses container
    # presentation timestamps when resampling, and preserves the audio timeline.
    subprocess.run(['ffmpeg','-nostdin','-loglevel','error','-y','-threads','4','-protocol_whitelist','file,pipe','-i',str(video),
                    '-map','0:v:0','-map','0:a:0?','-map_metadata','-1',
                    '-vf',f'fps={FPS},scale=960:540:force_original_aspect_ratio=decrease:force_divisible_by=2,'
                          'pad=960:540:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p',
                    '-c:v','libx264','-threads','4','-preset','veryfast','-crf','18',
                    '-c:a','aac','-af','aresample=async=1:first_pts=0','-movflags','+faststart',str(target)],
                   check=True)


def analyze(source, signals, helmet, profile, anchors, max_offset, out, progress, *, reference=None, sample=False):
    points = read_events(signals, extra_helmet=helmet)
    glove, head, pairs = device_pairs(points)
    if not glove or not head:
        raise ValueError('The signal input must contain both glove and helmet trigger records.')
    with tempfile.TemporaryDirectory(prefix='analysis_', dir=out.parent) as temp:
        raw = Path(temp)/'frames.rgb'
        progress('Decoding video')
        subprocess.run(['ffmpeg','-nostdin','-loglevel','error','-i',str(source),
                        '-vf',f'scale={WIDTH}:{HEIGHT}','-an','-pix_fmt','rgb24','-threads','1',
                        '-f','rawvideo',str(raw)], check=True)
        frame_bytes = WIDTH*HEIGHT*3
        if raw.stat().st_size < frame_bytes or raw.stat().st_size % frame_bytes:
            raise ValueError('No complete video frames were decoded.')
        frames = np.memmap(raw,dtype=np.uint8,mode='r').reshape(-1,HEIGHT,WIDTH,3)
        duration = len(frames)/FPS
        progress('Tracking glove and helmet')
        try:
            if reference and profile is None:
                profile = json.loads((reference/'profile.json').read_text())
                with np.load(reference/'tracking_templates.npz',allow_pickle=False) as templates:
                    tracks,direction = track(frames, profile, templates)
                    glove_patches = [templates[name] for name in templates.files if name.startswith('glove_')]
                    if direction==-1:
                        glove_patches = [patch[:,::-1] for patch in glove_patches]
            else:
                tracks,direction = track(frames, profile)
                glove_patches = []
                if not sample:
                    for t in profile['glove_templates']:
                        x0,y0,x1,y1 = bounds(t['box'])
                        glove_patches.append(frames[round(t['video_s']*FPS),y0:y1,x0:x1].copy())
        finally:
            del frames
        if anchors and any(not 0 <= p['video_s'] < duration for p in anchors):
            raise ValueError('All manual video anchor times must fall inside the clip.')
        progress('Aligning signals and detecting actions')
        visual = approaches(tracks,direction)
        clock = align(glove,head,pairs,visual,max_offset,anchors)
        if sample:
            events = json.loads((reference/'reference_events.json').read_text())
            for e in events:
                if e['sensor_time_estimated']:
                    e['sensor_s'] = (e['video_s']-clock['offset_s'])/clock['scale']
                e['clock_video_s'] = clock['scale']*e['sensor_s']+clock['offset_s']
        else:
            events = fuse(tracks,visual,glove,head,pairs,clock,profile)
            frames = np.memmap(raw,dtype=np.uint8,mode='r').reshape(-1,HEIGHT,WIDTH,3)
            try:
                recover_high_blocks(events,frames,tracks,direction,head,profile,glove_patches)
            finally:
                del frames
            events = sorted((e for e in events if e['label']!='review'),key=lambda e:e['video_s'])
        output_signals = write_outputs(out,events,points,clock,duration)
    return {'events':events,'signals':output_signals,'clock':clock,'duration':duration}
