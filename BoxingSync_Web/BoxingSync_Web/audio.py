"""Get Fish Audio speech cues and mix them onto the exported media timeline."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import wave

import numpy as np

FISH_URL = 'https://api.fish.audio/v1/tts'
CUES = {'helmet_hit':'Hit!', 'body_block':'Blocked!', 'dodge':'Dodge!'}
RATE = 48000
MODELS = {'s2.1-pro-free','s2.1-pro','s2-pro','drama-3','s1'}


class FishAudioError(RuntimeError):
    pass


def fetch_cue(text, cache, key, model, voice):
    payload = {'text':text, 'reference_id':voice, 'format':'wav'}
    fingerprint = hashlib.sha256(json.dumps([model,payload],sort_keys=True).encode()).hexdigest()
    cache.mkdir(parents=True, exist_ok=True)
    file = cache/(fingerprint+'.wav')
    if file.exists():
        return file
    req = Request(FISH_URL, json.dumps(payload).encode(), method='POST',
                  headers={'Authorization':f'Bearer {key}','Content-Type':'application/json','model':model})
    try:
        with urlopen(req, timeout=45) as response:
            data = response.read(6_000_001)
    except HTTPError as error:
        reason = {401:'Check the Fish Audio API key.',402:'Check the Fish Audio account balance.',
                  429:'Fish Audio rate limit reached. Try again shortly.'}.get(error.code,'Check the model and voice settings.')
        raise FishAudioError(f'Fish Audio returned HTTP {error.code}. {reason}') from None
    except (URLError, TimeoutError, OSError):
        raise FishAudioError('Fish Audio could not be reached. Check the connection and try again.') from None
    if not 44 < len(data) <= 6_000_000 or data[:4]!=b'RIFF' or data[8:12]!=b'WAVE':
        raise FishAudioError('Fish Audio returned an invalid WAV response.')
    temp = file.with_suffix('.partial')
    temp.write_bytes(data)
    temp.replace(file)
    return file


def cue_track(events, duration, path, cache, *, key):
    needed = {e['label'] for e in events if e['label'] in CUES}
    if not needed:
        return False
    model = os.getenv('FISH_AUDIO_MODEL','s2.1-pro-free').strip()
    voice = os.getenv('FISH_AUDIO_VOICE_ID','933563129e564b19a115bedd57b7406a').strip()
    if not key or model not in MODELS or not voice:
        raise FishAudioError('Check the Fish Audio key, model, and voice in the server .env file.')
    clips = {}
    for label in sorted(needed):
        source = fetch_cue(CUES[label], cache, key, model, voice)
        try:
            pcm = subprocess.check_output(['ffmpeg','-nostdin','-loglevel','error','-i',str(source),
                '-t','2','-f','f32le','-ac','1','-ar',str(RATE),'pipe:1'])
        except subprocess.CalledProcessError:
            source.unlink(missing_ok=True)
            raise FishAudioError('A Fish Audio cue could not be decoded. Try again.') from None
        clip = np.frombuffer(pcm,dtype='<f4')
        active = np.flatnonzero(abs(clip)>.015)
        if not len(active):
            source.unlink(missing_ok=True)
            raise FishAudioError('Fish Audio returned a silent cue. Try again.')
        clip = clip[max(0,active[0]-round(.006*RATE)):active[-1]+1].copy()
        clip[-min(len(clip),240):] *= np.linspace(1,0,min(len(clip),240))
        clips[label] = clip*(.62/max(abs(clip)))
    mixed = np.zeros(round(duration*RATE), dtype=np.float32)
    for e in events:
        clip = clips.get(e['label'])
        if clip is not None:
            start = max(0,round(e['video_s']*RATE))
            stop = min(start+len(clip),len(mixed))
            mixed[start:stop] += clip[:max(0,stop-start)]
    with wave.open(str(path),'wb') as out:
        out.setparams((1,2,RATE,0,'NONE','not compressed'))
        out.writeframes((np.clip(mixed,-.88,.88)*32767).astype('<i2').tobytes())
    return True


def render(source, out, cues=None, target='annotated.mp4'):
    args = ['ffmpeg','-nostdin','-loglevel','error','-y','-i',str(source)]
    if cues:
        has_audio = subprocess.check_output(['ffprobe','-v','error','-select_streams','a:0',
                     '-show_entries','stream=index','-of','csv=p=0',str(source)]).strip()
        args += ['-i',str(cues),'-map','0:v:0']
        if has_audio:
            args += ['-filter_complex','[0:a:0]aresample=48000,volume=0.65[room];'
                     '[room][1:a:0]amix=inputs=2:duration=longest:normalize=0,alimiter=limit=0.95:latency=1[a]',
                     '-map','[a]']
        else:
            args += ['-map','1:a:0']
        args += ['-c:a','aac','-b:a','160k','-shortest']
    else:
        args += ['-map','0:v:0','-map','0:a:0?','-c:a','copy']
    args += ['-map_metadata','-1','-vf','ass=overlay.ass','-c:v','libx264',
             '-threads','4','-preset','fast','-crf','22','-movflags','+faststart',target]
    subprocess.run(args,cwd=out,check=True)
