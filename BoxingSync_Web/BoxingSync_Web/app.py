"""Local HTTP app for uploading, calibrating, and reviewing a boxing session."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4
import webbrowser

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from audio import FishAudioError, cue_track, render
from engine import analyze, normalize
from sensors import read_events
from vision import bounds

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT/'.env')
DATA = Path(os.getenv('BOXINGSYNC_DATA_DIR', str(ROOT/'local_data'))).resolve()
DATA.mkdir(parents=True, exist_ok=True)
WORK = ThreadPoolExecutor(max_workers=1)
LOCK = threading.RLock()
LOG = logging.getLogger(__name__)
MAX_VIDEO = 500 * 1024**2
MAX_CSV = 5 * 1024**2
MAX_DURATION = 180
app = FastAPI(title='BoxingSync', docs_url=None, redoc_url=None, openapi_url=None)
app.mount('/assets', StaticFiles(directory=ROOT/'web'), name='assets')


class Template(BaseModel):
    video_s: float = Field(ge=0, le=MAX_DURATION)
    box: tuple[float,float,float,float]


class Profile(BaseModel):
    direction: Literal[-1,1] = 1
    glove_search: tuple[float,float,float,float]
    helmet_search: tuple[float,float,float,float]
    glove_templates: list[Template] = Field(min_length=1,max_length=20)
    helmet_templates: list[Template] = Field(min_length=1,max_length=20)
    strong_glove_v: float = Field(default=1.6, gt=0, le=100)

    @model_validator(mode='after')
    def check_boxes(self):
        for area,templates in [(self.glove_search,self.glove_templates),(self.helmet_search,self.helmet_templates)]:
            x0,y0,x1,y1 = bounds(area)
            for t in templates:
                a,b,c,d = bounds(t.box)
                if not (x0<=a<c<=x1 and y0<=b<d<=y1):
                    raise ValueError('Each template must fit inside its search area.')
        return self


class Anchor(BaseModel):
    video_s: float = Field(ge=0, le=MAX_DURATION)
    sensor_s: float = Field(allow_inf_nan=False)


class Analysis(BaseModel):
    profile: Profile | None = None
    anchors: list[Anchor] = Field(default_factory=list,max_length=20)
    max_offset: float = Field(default=8,ge=.05,le=120)


@app.middleware('http')
async def local_requests(request, call_next):
    if request.url.hostname not in ('127.0.0.1','localhost','testserver'):
        return JSONResponse({'detail':'Open this app on localhost.'},status_code=403)
    if request.method == 'POST':
        origin = request.headers.get('origin')
        if origin and urlsplit(origin).netloc != request.url.netloc:
            return JSONResponse({'detail':'Open the app directly to submit files.'},status_code=403)
        length = request.headers.get('content-length')
        if length and (not length.isdigit() or int(length) > MAX_VIDEO+2*MAX_CSV+1_000_000):
            return JSONResponse({'detail':'Upload exceeds the size limit.'},status_code=413)
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    return response


def store_json(path, value):
    data = json.dumps(value,ensure_ascii=False,allow_nan=False)
    temporary = path.with_name(f'.{path.name}.{uuid4().hex}.tmp')
    # Windows can block replacement while another process holds the file.
    # Serialize our own reads and writes, then retry brief external locks.
    with LOCK:
        try:
            temporary.write_text(data,encoding='utf-8')
            for attempt in range(6):
                try:
                    temporary.replace(path)
                    break
                except PermissionError as error:
                    if getattr(error,'winerror',None) not in (5,32,33) or attempt == 5:
                        raise
                    time.sleep(.025 * 2**attempt)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                LOG.warning('Could not remove temporary file %s',temporary,exc_info=True)


def load_json(path):
    with LOCK:
        return json.loads(path.read_text(encoding='utf-8'))


def status(job, phase, message, **extra):
    store_json(job/'status.json',{'id':job.name,'phase':phase,'message':message,**extra})


def find_job(id):
    job = DATA/id.hex
    if not (job/'status.json').is_file():
        raise HTTPException(404,'Session not found.')
    return job


def new_job():
    job = DATA/uuid4().hex
    job.mkdir()
    return job


def save_upload(upload, path, limit):
    total = 0
    with path.open('wb') as file:
        for chunk in iter(lambda:upload.file.read(1024**2),b''):
            total += len(chunk)
            if total>limit:
                raise HTTPException(413,'One of the files exceeds its size limit.')
            file.write(chunk)
    if not total:
        raise HTTPException(400,'Uploaded files must not be empty.')


def prepare(job, source):
    try:
        info = json.loads(subprocess.check_output(['ffprobe','-v','error','-protocol_whitelist','file,pipe',
            '-select_streams','v:0','-show_entries','stream=index:format=duration','-of','json',str(source)]))
        if not info.get('streams') or not 0 < float(info['format']['duration']) <= MAX_DURATION:
            raise ValueError('Upload a video between 0 and 180 seconds long.')
        normalize(source,job/'preview.mp4')
        status(job,'ready','Video ready')
    except (ValueError, KeyError, subprocess.CalledProcessError, OSError) as error:
        LOG.warning('Video preparation failed for %s: %s',job.name,error)
        message = str(error) if isinstance(error,ValueError) else 'Video could not be opened. Try an MP4 or MOV clip up to 180 seconds.'
        status(job,'prepare_error',message)
    finally:
        source.unlink(missing_ok=True)


def run_analysis(job, options):
    def progress(message):
        status(job,'processing',message)

    out = job/'output'
    try:
        profile = options.profile.model_dump() if options.profile else None
        if profile is None and (job/'sample').is_file():
            profile = json.loads((ROOT/'example/profile.json').read_text())
        helmet = job/'helmet.csv'
        result = analyze(job/'preview.mp4',job/'signals.csv',helmet if helmet.exists() else None,
                         profile,[p.model_dump() for p in options.anchors],options.max_offset,out,progress,
                         reference=ROOT/'example',sample=(job/'sample').is_file())
        progress('Rendering annotated video')
        render(job/'preview.mp4',out)
        result['audio'] = add_voice(job,result)
        store_json(out/'result.json',result)
        status(job,'done','Analysis complete')
    except (ValueError, TypeError, KeyError) as error:
        status(job,'analysis_error',str(error))
    except PermissionError:
        LOG.exception('Analysis file access failed for %s',job.name)
        status(job,'analysis_error','Windows blocked access to an analysis file. Check the terminal for the file path.')
    except Exception:
        LOG.exception('Analysis failed for %s',job.name)
        status(job,'analysis_error','Processing failed. Check available disk space and the FFmpeg installation.')


def add_voice(job, result):
    key = os.getenv('FISH_AUDIO_API_KEY','').strip()
    if not key:
        return {'state':'missing_key','message':'Voice cues were skipped. Configure the Fish Audio key in the server .env file.'}
    out = job/'output'
    track = out/'cues.wav'
    voiced = out/'voiced.mp4'
    status(job,'processing','Generating Fish Audio cues')
    try:
        if not cue_track(result['events'],result['duration'],track,DATA/'fish_cache',key=key):
            return {'state':'empty','message':'No classified actions needed a voice cue.'}
        status(job,'processing','Mixing voice cues into the MP4')
        render(job/'preview.mp4',out,track,target=voiced.name)
        voiced.replace(out/'annotated.mp4')
        return {'state':'added','message':'Fish Audio cues are included in the MP4.'}
    except FishAudioError as error:
        return {'state':'failed','message':str(error)}
    except (OSError, subprocess.CalledProcessError):
        LOG.exception('Voice mix failed for %s',job.name)
        return {'state':'failed','message':'The voice track could not be mixed. The existing video is available.'}
    finally:
        track.unlink(missing_ok=True)
        voiced.unlink(missing_ok=True)


@app.get('/')
def index():
    return FileResponse(ROOT/'web/index.html')


@app.post('/api/jobs')
def upload(video: Annotated[UploadFile,File()], signals: Annotated[list[UploadFile],File()]):
    if not 1<=len(signals)<=2:
        raise HTTPException(400,'Select one combined CSV or two sensor CSV files.')
    helmet = signals[1] if len(signals)==2 else None
    suffix = Path(video.filename or '').suffix.lower()
    if suffix not in {'.mov','.mp4','.m4v','.webm','.mkv','.avi'}:
        raise HTTPException(400,'Choose an MP4, MOV, M4V, WebM, MKV, or AVI file.')
    job = new_job()
    source = job/('upload'+suffix)
    try:
        save_upload(video,source,MAX_VIDEO)
        save_upload(signals[0],job/'signals.csv',MAX_CSV)
        if helmet:
            save_upload(helmet,job/'helmet.csv',MAX_CSV)
        points = read_events(job/'signals.csv',extra_helmet=job/'helmet.csv' if helmet else None)
        if {p['device'] for p in points} != {'glove','helmet'}:
            raise ValueError('The CSV input must contain both glove and helmet triggers.')
    except (ValueError, KeyError) as error:
        shutil.rmtree(job,ignore_errors=True)
        raise HTTPException(400,f'Signal CSV: {error}') from None
    except Exception:
        shutil.rmtree(job,ignore_errors=True)
        raise
    queue(job,prepare,'preparing','Preparing video preview',source)
    return {'id':job.name}


@app.post('/api/sample')
def sample():
    job = new_job()
    shutil.copyfile(ROOT/'example/sample.mp4',job/'preview.mp4')
    shutil.copyfile(ROOT/'example/signals.csv',job/'signals.csv')
    (job/'sample').touch()
    status(job,'ready','Sample ready')
    return {'id':job.name}


@app.get('/api/jobs/{id}')
def get_job(id: UUID):
    job = find_job(id)
    with LOCK:
        state = load_json(job/'status.json')
        if state['phase'] in ('preparing','processing') and job.name not in ACTIVE:
            state.update(phase='interrupted',message='The server restarted. Start a new session.')
    return state


ACTIVE = set()


def queue(job, handler, phase, message, *args):
    def work():
        try:
            handler(job,*args)
        finally:
            with LOCK:
                ACTIVE.discard(job.name)

    with LOCK:
        status(job,phase,message)
        ACTIVE.add(job.name)
        try:
            WORK.submit(work)
        except RuntimeError:
            ACTIVE.discard(job.name)
            raise


@app.post('/api/jobs/{id}/analyze')
def start_analysis(id: UUID, options: Analysis):
    job = find_job(id)
    with LOCK:
        phase = load_json(job/'status.json')['phase']
        if phase not in ('ready','analysis_error','done') or job.name in ACTIVE:
            raise HTTPException(409,'Wait for the current job to finish.')
        queue(job,run_analysis,'processing','Waiting to analyze',options)
    return {'id':job.name}


@app.get('/api/jobs/{id}/preview')
def preview(id: UUID):
    file = find_job(id)/'preview.mp4'
    if not file.is_file():
        raise HTTPException(404,'Preview is not ready.')
    return FileResponse(file,media_type='video/mp4')


@app.get('/api/jobs/{id}/results/{name}')
def result(id: UUID, name: str):
    job = find_job(id)
    files = {'result.json','annotated.mp4','events.csv','aligned_signals.csv','alignment.json'}
    with LOCK:
        if name not in files or load_json(job/'status.json')['phase']!='done':
            raise HTTPException(404,'Result is not ready.')
        file = job/'output'/name
        if name.endswith('.json'):
            return JSONResponse(load_json(file))
    return FileResponse(file,filename=name if name.endswith('.csv') else None)


if __name__ == '__main__':
    import uvicorn
    parser = argparse.ArgumentParser(description='Start the local BoxingSync app.')
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--open',action='store_true',help='Open the app in your browser.')
    args = parser.parse_args()
    if not all(shutil.which(x) for x in ('ffmpeg','ffprobe')):
        parser.exit(1,'FFmpeg and FFprobe must be available on PATH. See README.md.\n')
    if args.open:
        timer = threading.Timer(1.,webbrowser.open,args=(f'http://127.0.0.1:{args.port}',))
        timer.daemon = True
        timer.start()
    uvicorn.run(app,host='127.0.0.1',port=args.port)
