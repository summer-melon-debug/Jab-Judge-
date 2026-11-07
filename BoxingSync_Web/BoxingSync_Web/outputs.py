"""Write the event, signal, and subtitle files used by the app."""
import csv
import json

STYLES = {
    'helmet_hit': ('HELMET HIT','#fa727c'),
    'dodge': ('DODGE','#50e7ce'),
    'body_block': ('BODY BLOCK','#ffca78'),
}

def csv_out(path, records, fields):
    with path.open('w', newline='', encoding='utf-8-sig') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for record in records:
            row = {key:record.get(key) for key in fields}
            writer.writerow({key:round(value,6) if isinstance(value,float) else value
                             for key,value in row.items()})


def ass_time(t):
    cs = max(0,round(100*t))
    return f"{cs//360000}:{cs//6000%60:02d}:{cs//100%60:02d}.{cs%100:02d}"


def bgr(color):
    return '&H' + color[5:7] + color[3:5] + color[1:3] + '&'


def annotations(events,duration):
    lines = ["""[Script Info]
ScriptType: v4.00+
PlayResX: 960
PlayResY: 540
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Default,Arial,23,&H00FFFFFF,&H00FFFFFF,&H00181A24,&HA0000000,-1,0,0,0,100,100,0,0,1,2,0,7,0,0,0,1

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text"""]

    def add(start,end,text):
        if end>start:
            lines.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Default,,0,0,0,,{text}")

    for i,e in enumerate(events):
        t = e['video_s']
        start = max(0,t-.09)
        stop = min(t+.67,duration,events[i+1]['video_s'] if i+1<len(events) else duration)
        label,color = STYLES[e['label']]
        color = bgr(color)
        approx = '~' if e['sensor_time_estimated'] else ''
        add(start,stop,rf"{{\an7\pos(24,22)\1c{color}}}{label}  |  VIDEO {t:05.2f}s  |  SIGNAL {approx}{e['sensor_s']:05.2f}s")
        if e.get('x') is not None and e.get('y') is not None:
            x,y = round(960*e['x']),round(540*e['y'])
            label_x = min(899,max(60,x))
            # The crosshair is brief because the object keeps moving after contact.
            add(start,min(stop,t+.30),rf"{{\an5\pos({x},{y})\1c{color}\bord2\fs36}}+")
            add(start,min(stop,t+.30),rf"{{\an8\pos({label_x},{min(505,y+21)})\1c{color}\fs16}}{label}")
    return '\n'.join(lines)+'\n'


def write_outputs(out, events, points, clock, duration):
    out.mkdir(parents=True,exist_ok=True)
    fields = ['event_id','label','video_s','sensor_s','sensor_time_estimated',
              'clock_video_s','glove_s','helmet_s','glove_seq','helmet_seq',
              'glove_peak_v','helmet_peak_v','x','y','confidence','basis']
    csv_out(out/'events.csv',events,fields)
    (out/'alignment.json').write_text(json.dumps(clock,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    by_point = { (device,e[f'{device}_s']):e for e in events
                for device in ('glove','helmet') if e[f'{device}_s'] is not None}
    signals = []
    for p in points:
        e = by_point.get((p['device'],p['sensor_s']))
        video_s = clock['scale']*p['sensor_s']+clock['offset_s']
        signals.append({**p,'video_s':video_s, 'in_video':0<=video_s<duration,
                        'event_id':e['event_id'] if e else '', 'label':e['label'] if e else '',
                        'event_video_s':e['video_s'] if e else None,
                        'event_sensor_s':e['sensor_s'] if e else None,
                        'sensor_time_estimated':e['sensor_time_estimated'] if e else None,
                        'x':e['x'] if e else None,'y':e['y'] if e else None})
    markers = [{'record_type':'action','sensor_s':e['sensor_s'],'video_s':e['video_s'],
                'in_video':True,'event_id':e['event_id'],'label':e['label'],
                'event_video_s':e['video_s'],'event_sensor_s':e['sensor_s'],
                'sensor_time_estimated':e['sensor_time_estimated'],
                'x':e['x'],'y':e['y']} for e in events]
    triggers = [{'record_type':'trigger',**p} for p in signals]
    csv_out(out/'aligned_signals.csv',sorted(triggers+markers,key=lambda p:p['sensor_s']),[
        'record_type','device','seq','sensor_s','video_s','in_video','peak_v','synced',
        'event_id','label','event_video_s','event_sensor_s','sensor_time_estimated','x','y'])
    (out/'overlay.ass').write_text(annotations(events,duration),encoding='utf-8')
    return signals
