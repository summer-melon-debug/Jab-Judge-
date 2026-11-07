"""Side-view tracking with user-calibrated glove, helmet and defender regions."""
import numpy as np
from scipy import fft, ndimage, signal

WIDTH, HEIGHT, FPS = 480, 270, 30


def bounds(box):
    if len(box) != 4 or not all(0 <= x <= 1 for x in box):
        raise ValueError("Use normalized [left, top, right, bottom] boxes.")
    x0, y0, x1, y1 = [round(a*b) for a, b in zip(box, (WIDTH, HEIGHT)*2)]
    if x1-x0 < 8 or y1-y0 < 8:
        raise ValueError("Boxes must span at least 8 pixels in analysis space.")
    return x0, y0, x1, y1


def features(frame):
    rgb = frame.astype(np.float32)
    lum = rgb @ np.array([.28, .58, .14], dtype=np.float32)
    olive = (rgb[:, :, 1] - rgb[:, :, 2]) * 1.6
    return np.stack((lum, olive), axis=-1)


def prepare(frames, templates, search):
    patches = []
    for spec in templates:
        tx0, ty0, tx1, ty1 = bounds(spec['box'])
        k = round(float(spec['video_s'])*FPS)
        if not 0 <= k < len(frames):
            raise ValueError("Template timestamp outside video.")
        patches.append(frames[k][ty0:ty1, tx0:tx1])
    return build_model(patches,search)


def build_model(patches, search):
    x0, y0, x1, y1 = bounds(search)
    bank = []
    for rgb in patches:
        patch = features(rgb)
        ph, pw = patch.shape[:2]
        if pw > x1-x0 or ph > y1-y0:
            raise ValueError("Template larger than search region.")
        mean = patch.mean(axis=(0, 1))
        kernel = patch - mean
        energy = np.sum(kernel**2)
        if energy <= 1:
            raise ValueError("Template has too little texture.")
        bank.append((kernel, energy, mean))
    if not bank:
        raise ValueError("Glove and helmet each need a template.")
    fft_size = (fft.next_fast_len(y1-y0+max(k.shape[0] for k, _, _ in bank)-1),
                fft.next_fast_len(x1-x0+max(k.shape[1] for k, _, _ in bank)-1))
    spectra = [(k.shape[:2], fft.rfft2(k[::-1, ::-1], s=fft_size, axes=(0,1)), energy, mean)
               for k, energy, mean in bank]
    return (x0, y0, x1, y1), fft_size, spectra


def find_object(image, model, previous):
    (x0, y0, x1, y1), fft_size, bank = model
    region = image[y0:y1, x0:x1]
    region_fft = fft.rfft2(region, s=fft_size, axes=(0,1))
    precise = region.astype(np.float64)
    def integral(values):
        return np.pad(values.cumsum(axis=0).cumsum(axis=1), ((1,0),(1,0),(0,0)))
    integral_values = integral(precise)
    integral_squares = integral(precise**2)
    best = None
    for (h, w), kernel_fft, energy, template_mean in bank:
        def window_sum(s):
            return s[h:, w:] - s[:-h, w:] - s[h:, :-w] + s[:-h, :-w]

        means = window_sum(integral_values) / (w*h)
        variance = window_sum(integral_squares) - means**2*(w*h)
        corr = fft.irfft2((region_fft*kernel_fft).sum(axis=2), s=fft_size)
        corr = corr[h-1:region.shape[0], w-1:region.shape[1]]
        score = corr / np.sqrt(energy*np.maximum(variance.sum(axis=2), 1))
        score -= .0016*abs(means[:, :, 0]-template_mean[0])
        score -= .0040*abs(means[:, :, 1]-template_mean[1])
        ranked = score
        if previous is not None:
            px, py = previous[:2]
            xs = np.arange(score.shape[1]) + x0 + w/2
            ys = np.arange(score.shape[0]) + y0 + h/2
            penalty = ((xs[None, :]-px)**2 + (ys[:, None]-py)**2)**.5
            ranked = score - .0028*np.minimum(penalty, 105)
        iy, ix = np.unravel_index(np.argmax(ranked), score.shape)
        result = (ix+x0+w/2, iy+y0+h/2, w, h, score[iy, ix], ranked[iy, ix])
        if best is None or result[-1] > best[-1]:
            best = result
    return best[:5]


def reference_models(frames, profile, templates):
    """Select the camera orientation and adapt the search when framing shifts."""
    keys = ('glove','helmet')
    banks = {key:[templates[k] for k in sorted(templates.files) if k.startswith(key+'_')] for key in keys}
    indices = np.linspace(0,len(frames)-1,min(14,len(frames)),dtype=int)
    snapshots = [features(frames[i]) for i in indices]

    def evaluate(direction):
        models, points, scores = [], [], []
        for key in keys:
            box = profile[key+'_search']
            patches = banks[key]
            if direction == -1:
                box = [1-box[2],box[1],1-box[0],box[3]]
                patches = [p[:,::-1] for p in patches]
            model = build_model(patches,box)
            rows = np.array([find_object(frame,model,None) for frame in snapshots])
            models.append(model)
            points.append(rows)
            scores.append(np.median(rows[:,4]))
        side_gap = direction*np.median(points[1][:,0]-points[0][:,0])
        return min(scores) - (.25 if side_gap < 15 else 0),direction,models

    quality,direction,models = max(map(evaluate,(1,-1)),key=lambda option:option[0])
    if quality >= .60:
        return direction,models

    # Try a wider search for a translated view of the same equipment.
    models, locations = [], []
    for key in keys:
        patches = banks[key] if direction == 1 else [p[:,::-1] for p in banks[key]]
        broad = build_model(patches,[.03,.05,.97,.88])
        rows = np.array([find_object(frame,broad,None) for frame in snapshots])
        good = rows[rows[:,4] >= .60]
        if len(good) < max(2,len(rows)//2):
            raise ValueError("The glove and helmet could not be tracked automatically. Open Advanced setup and mark them in the preview.")
        x,y = good[:,0]/WIDTH,good[:,1]/HEIGHT
        locations.append(float(np.median(x)))
        pad = .17 if key=='glove' else .11
        box = [max(0,np.quantile(x,.10)-pad), max(0,np.quantile(y,.10)-.17),
               min(1,np.quantile(x,.90)+pad), min(1,np.quantile(y,.90)+.17)]
        models.append(build_model(patches,box))
    if direction*(locations[1]-locations[0]) < .03:
        raise ValueError("The glove and helmet could not be separated. Use Advanced setup to mark each object.")
    return direction,models


def track(frames, profile, templates=None):
    if templates is None:
        direction = profile.get('direction',1)
        glove = prepare(frames, profile['glove_templates'], profile['glove_search'])
        helmet = prepare(frames, profile['helmet_templates'], profile['helmet_search'])
    else:
        direction,(glove,helmet) = reference_models(frames,profile,templates)
    tracks = []
    g = h = None
    for i, frame in enumerate(frames):
        f = features(frame)
        g = find_object(f, glove, g if g and g[-1] > .38 else None)
        h = find_object(f, helmet, h if h and h[-1] > .40 else None)
        tracks.append((g, h))
    return np.array(tracks), direction


def approaches(tracks, direction=1):
    g, h = tracks[:, 0], tracks[:, 1]
    # Use horizontal reach as well as head-relative reach. A slip moves the
    # helmet away and can make the minimum Euclidean gap a poor time anchor.
    reach = direction*(g[:, 0]-h[:, 0])
    reach = ndimage.median_filter(reach, size=3)
    extension = reach-ndimage.percentile_filter(reach, 25, size=45)
    peaks, _ = signal.find_peaks(reach, distance=round(.28*FPS), prominence=17)
    candidates = []
    for p in peaks:
        if extension[p] < 17 or min(g[p, 4], h[p, 4]) < .39 or reach[p] > 17:
            continue
        neighborhood = slice(max(0,p-5), min(len(g),p+6))
        nearest = np.abs(g[neighborhood, 1]-h[neighborhood, 1]).min()
        if reach[p] < -107 or nearest > 92:
            continue
        candidates.append({'frame': int(p), 'video_s': p/FPS, 'reach_px': float(reach[p]),
                           'extension_px': float(extension[p])})
    return candidates
