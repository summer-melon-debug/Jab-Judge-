"""Fuse the device triggers with the closest approach and motion evidence."""
import numpy as np

from sensors import associate
from vision import FPS, WIDTH, HEIGHT, build_model, features, find_object

def geometry(tracks, i, direction):
    g, h = tracks[i]
    reach = direction*(g[0]-h[0])
    gap = -reach-(g[2]+h[2])*.5
    return g, h, gap, g[1]-h[1]


def visual_evidence(tracks, approach, direction):
    i = approach['frame']
    start, stop = max(0,i-6), min(len(tracks),i+7)
    j = max(0, i-10)
    before = np.median(tracks[max(0,j-3):j+1,1,:2], axis=0)
    choices = []
    for f in range(start,stop):
        g,h,gap,dy = geometry(tracks,f,direction)
        quality = min(g[4],h[4])
        if quality < .43:
            continue
        head_clearance = max(gap, abs(dy)-.5*(g[3]+h[3]))
        head_move = np.hypot(*(h[:2]-before))
        down = h[1]-before[1]
        back = direction*(h[0]-before[0])
        previous_gap = direction*(before[0]-g[0])-(g[2]+h[2])*.5
        choices.append((f,gap,dy,head_clearance,head_move,down,back,previous_gap,quality))
    return choices


def fuse(tracks, approaches, glove, helmet, pairs, clock, profile):
    direction = int(profile.get('direction',1))
    strong_v = float(profile.get('strong_glove_v',1.6))
    a,b = clock['scale'],clock['offset_s']
    to_video = lambda t: a*t+b
    to_sensor = lambda t: (t-b)/a
    end = len(tracks)/FPS
    visual_times = [v['video_s'] for v in approaches]
    used = set()
    events = []

    def append(label, frame, g=None, h=None, basis='', confidence='review'):
        time = frame/FPS
        gp,hp,_,_ = geometry(tracks,frame,direction)
        location = None
        if label=='helmet_hit':
            location = [hp[0]-direction*hp[2]*.38, (hp[1]+gp[1])*0.5]
        if label=='body_block':
            location = [gp[0]+direction*gp[2]*.37, gp[1]]
        if label=='dodge':
            location = hp[:2]
        estimated_time = h['sensor_s'] if label=='helmet_hit' and h else (
            g['sensor_s'] if g else h['sensor_s'] if h else to_sensor(time))
        events.append({'event_id': f"G{g['seq']}" if g and g['seq'] else (
            f"H{h['seq']}" if h and h['seq'] else f"V{frame:04d}"),
            'label':label, 'video_s':time,'sensor_s':estimated_time,
            'sensor_time_estimated':g is None and h is None,
            'glove_s':g['sensor_s'] if g else None,'helmet_s':h['sensor_s'] if h else None,
            'glove_seq':g['seq'] if g else None,'helmet_seq':h['seq'] if h else None,
            'glove_peak_v':g['peak_v'] if g else None,'helmet_peak_v':h['peak_v'] if h else None,
            'clock_video_s':to_video(estimated_time),'confidence':confidence,'basis':basis,
            'x':float(np.clip(location[0]/WIDTH,0,1)) if location is not None else None,
            'y':float(np.clip(location[1]/HEIGHT,0,1)) if location is not None else None})

    def at_time(t):
        return min(len(tracks)-1,max(0,round(t*FPS)))

    for label, device_rows in (
        ('pair', [(glove[i],helmet[j]) for i,j in pairs.items()]),
        ('glove', [(p,None) for i,p in enumerate(glove)
                   if i not in pairs and p['synced'] and p['peak_v']>=strong_v]),
        ('helmet', [(None,p) for j,p in enumerate(helmet) if j not in pairs.values()]),
    ):
        rows = [(g,h) for g,h in device_rows if 0<=to_video((h or g)['sensor_s'])<end]
        candidates = [j for j in range(len(approaches)) if j not in used]
        mapped = [to_video((h or g)['sensor_s']) for g,h in rows]
        matches = dict(associate(mapped,[visual_times[j] for j in candidates], .33))
        for k,(g,h) in enumerate(rows):
            if k not in matches:
                # A second vibration during the same visible contact is kept
                # in the raw signal chart rather than counted as another punch.
                if any(abs(mapped[k]-e['video_s'])<.28 for e in events):
                    continue
                append('review',at_time(mapped[k]),g,h,'sensor_trigger_without_matched_punch')
                continue
            index = candidates[matches[k]]
            used.add(index)
            v = approaches[index]
            choices = visual_evidence(tracks,v,direction)
            local = [c for c in choices if abs(c[0]/FPS-mapped[k]) <= .26]
            if label=='pair':
                close = [c for c in local if c[1] <= 14 and c[1] >= -26 and abs(c[2]) <= 22]
                if close:
                    f = min(close,key=lambda c:abs(c[0]/FPS-mapped[k])+.002*abs(c[1]))[0]
                    append('helmet_hit',f,g,h,'paired_glove_helmet_and_visual_proximity','supported')
                    continue
            if label=='glove':
                guard = [c for c in local if -45 <= c[1] <= 16 and 22 <= c[2] <= 83]
                head_nearby = any(abs(p['sensor_s']-g['sensor_s'])<=.16 for p in helmet)
                if guard and not head_nearby:
                    f = min(guard,key=lambda c:abs(c[0]/FPS-mapped[k])+.002*abs(c[1]))[0]
                    append('body_block',f,g,None,'strong_glove_trigger_below_helmet','candidate')
                    continue
            append('review',v['frame'],g,h,'punch_and_sensor_evidence_inconclusive')

    start_sensor = min(p['sensor_s'] for p in glove+helmet)
    end_sensor = max(p['sensor_s'] for p in glove+helmet)
    for j,v in enumerate(approaches):
        if j in used or v['extension_px']<35 or v['reach_px']<-72:
            continue
        if any(abs(v['video_s']-e['video_s'])<.46 for e in events):
            continue
        t = to_sensor(v['video_s'])
        no_impact = not any(abs(p['sensor_s']-t)<.30 for p in helmet)
        no_glove_contact = not any(abs(p['sensor_s']-t)<.30 and p['peak_v']>=strong_v for p in glove)
        local = visual_evidence(tracks,v,direction)
        clear = [c for c in local if c[3]>=4 and c[4]>=17 and (c[5]>=13 or c[6]>=15)
                 and c[7]<=18 and -96<=c[2]<=10]
        if no_impact and no_glove_contact and clear and start_sensor<=t<=end_sensor:
            f = max(clear,key=lambda c:c[4]-.3*abs(c[0]-v['frame']))[0]
            append('dodge',f,basis='evasive_helmet_motion_with_visible_clearance',confidence='candidate')
        else:
            # A clear below-helmet contact can exist without a strong glove
            # trigger. Require sustained proximity and visible defender motion.
            contact = [c for c in local if -45<=c[1]<=8 and 23<=c[2]<=70 and c[8]>=.62]
            steady = [c for a,c,b in zip(contact,contact[1:],contact[2:])
                      if c[0]-a[0]==b[0]-c[0]==1 and c[6]>=5]
            if no_impact and v['extension_px']>=35 and steady:
                f = min(steady,key=lambda c:abs(c[0]-v['frame']))[0]
                append('body_block',f,basis='sustained_below_helmet_contact_with_defender_motion',confidence='candidate')
            else:
                append('review',v['frame'],basis='visible_punch_without_resolved_contact')
    return sorted(events,key=lambda e:e['video_s'])


def recover_high_blocks(events, frames, tracks, direction, helmet, profile, patches):
    """Recheck strong unmatched glove impacts in the area above a raised guard."""
    for event in events:
        if event['basis']!='sensor_trigger_without_matched_punch' or event['glove_s'] is None:
            continue
        sensor = event['glove_s']
        if event['glove_peak_v'] < profile.get('strong_glove_v',1.6) or any(
            abs(h['sensor_s']-sensor)<=.16 for h in helmet
        ):
            continue

        mapped = event['clock_video_s']
        center = min(len(frames)-1,max(0,round(mapped*FPS)))
        lo,hi = max(0,center-10),min(len(frames),center+9)
        hx,hy = np.median(tracks[lo:hi,1,:2],axis=0)
        left,right = sorted((hx-direction*93,hx-direction*7))
        area = [max(0,left/WIDTH),max(0,(hy-60)/HEIGHT),
                min(1,right/WIDTH),min(1,(hy+24)/HEIGHT)]
        try:
            model = build_model(patches,area)
        except ValueError:
            continue

        approaches = []
        contacts = []
        for i in range(lo,hi):
            h = tracks[i,1]
            g = find_object(features(frames[i]),model,None)
            if min(h[4],g[4])<.76:
                continue
            gap = direction*(h[0]-g[0])-(h[2]+g[2])*.5
            dy = g[1]-h[1]
            if gap>=22:
                approaches.append(i)
            if -9<=gap<=15 and -41<=dy<=-14 and abs(i/FPS-mapped)<=.17:
                contacts.append((i,g,gap))
        stable = [c for a,c in zip(contacts,contacts[1:]) if c[0]-a[0]==1 and
                  any(2<=c[0]-earlier<=10 for earlier in approaches)]
        if stable:
            i,g,gap = min(stable,key=lambda c:abs(c[0]/FPS-mapped)+.001*abs(c[2]))
            event.update(label='body_block',video_s=i/FPS,
                         x=float(np.clip((g[0]+direction*g[2]*.37)/WIDTH,0,1)),
                         y=float(np.clip(g[1]/HEIGHT,0,1)),confidence='candidate',
                         basis='glove_trigger_and_visible_high_guard_contact')
