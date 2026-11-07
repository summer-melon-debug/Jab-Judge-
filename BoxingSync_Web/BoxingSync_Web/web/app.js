"use strict";

const $ = id => document.getElementById(id);
const labels = {
  helmet_hit: ["Helmet hit", "#ff8a8b"],
  body_block: ["Body block", "#ffd28a"],
  dodge: ["Dodge", "#6de1bf"]
};
const boxColors = {glove:"#d5f678",helmet:"#82c7fb"};
const boxNames = {
  glove_search:"Glove search area", helmet_search:"Helmet search area",
  glove_templates:"Glove example", helmet_templates:"Helmet example"
};
const preview = $("preview"), boxes = $("boxes"), boxContext = boxes.getContext("2d");
const player = $("result-video"), canvas = $("timeline"), ctx = canvas.getContext("2d");
const fixed = t => Number(t).toFixed(3) + " s";
let job, sample = false, busy = false, profile, undo, boxTool, drag, result, focused;
let lo, hi, fullLo, fullHi, plot;

function message(id, text, error = false) {
  $(id).hidden = !text;
  $(id).textContent = text;
  $(id).dataset.error = error;
}

async function request(url, options) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = Array.isArray(data.detail) ? data.detail[0]?.msg : data.detail;
    throw new Error(detail || "The request could not be completed.");
  }
  return data;
}

const endpoint = path => "/api/jobs/" + job + path;
const jsonPost = data => ({method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(data)});
const sleep = ms => new Promise(resolve => setTimeout(resolve,ms));

async function waitFor(phases, statusId) {
  for (;;) {
    const state = await request(endpoint(""));
    message(statusId,state.message);
    if (phases.includes(state.phase)) return;
    if (state.phase.endsWith("_error") || state.phase === "interrupted") throw new Error(state.message);
    await sleep(950);
  }
}

function setBusy(value) {
  busy = value;
  $("sample").disabled = value;
  $("upload-button").disabled = value;
  $("anchors").disabled = value;
  $("analyze").disabled = value || !canAnalyze();
}

function resetSetup() {
  profile = {glove_templates:[], helmet_templates:[]};
  undo = [];
  drag = null;
  selectBox(null);
  job = null;
  result = null;
  plot = null;
  $("setup").hidden = $("results").hidden = true;
  preview.pause();
  player.pause();
  $("anchors").value = "";
  $("direction").value = "1";
  $("advanced").open = false;
  message("analysis-status","");
}

async function upload(body, isSample) {
  if (busy) return;
  resetSetup();
  setBusy(true);
  message("upload-status",isSample?"Loading the sample…":"Uploading files…");
  try {
    const data = await request(isSample?"/api/sample":"/api/jobs",{method:"POST",body});
    job = data.id;
    sample = isSample;
    await waitFor(["ready"],"upload-status");
    $("calibration").hidden = isSample;
    $("setup").hidden = false;
    if (!isSample) {
      preview.src = endpoint("/preview");
      paintBoxes();
    }
    message("upload-status","");
    $("setup").scrollIntoView({block:"start",behavior:"smooth"});
    await analyzeSession();
  } catch (error) {
    message("upload-status",error.message,true);
  } finally {
    setBusy(false);
  }
}

$("upload").addEventListener("submit", event => {
  event.preventDefault();
  const body = new FormData(event.currentTarget);
  if (body.getAll("signals").length>2) {
    message("upload-status","Select one combined CSV or two sensor CSVs.",true);
    return;
  }
  upload(body,false);
});
$("sample").onclick = () => upload(undefined,true);

function canAnalyze() {
  return !!job;
}

function frameTime() {
  return Math.floor((preview.currentTime+.00001)*30)/30;
}

function stepFrame(delta) {
  if (!preview.duration) return;
  preview.pause();
  const target = (Math.round(frameTime()*30)+delta)/30+.0001;
  preview.currentTime = Math.max(0,Math.min(preview.duration-1/30+.0001,target));
}

function outline(box,key,dashed) {
  const [x,y,x1,y1] = box;
  boxContext.strokeStyle = boxColors[key.split("_")[0]];
  boxContext.lineWidth = 3;
  boxContext.setLineDash(dashed?[10,7]:[]);
  boxContext.strokeRect(x*960,y*540,(x1-x)*960,(y1-y)*540);
  boxContext.setLineDash([]);
}

function paintBoxes() {
  boxContext.clearRect(0,0,960,540);
  for (const [key, value] of Object.entries(profile)) {
    if (key.endsWith("_search")) outline(value,key,true);
    else for (const item of value) if (Math.abs(item.video_s-frameTime())<.016) outline(item.box,key,false);
  }
  if (drag) outline(ordered(drag.start,drag.end),boxTool,false);
  $("frame-time").textContent = fixed(frameTime());
  const summary = [
    profile.glove_search?"glove area set":"glove area needed",
    profile.helmet_search?"helmet area set":"helmet area needed",
    `${profile.glove_templates.length} glove and ${profile.helmet_templates.length} helmet examples`
  ].join(" · ");
  $("boxes-summary").textContent = boxTool ? `Drag a box on the paused video · ${boxNames[boxTool]}` : summary;
}

function selectBox(key) {
  boxTool = key;
  boxes.dataset.edit = Boolean(key);
  for (const button of document.querySelectorAll("[data-box]")) {
    button.setAttribute("aria-pressed",String(button.dataset.box===key));
  }
  paintBoxes();
}

function point(event) {
  const r = boxes.getBoundingClientRect(), clamp = x => Math.max(0,Math.min(1,x));
  return [clamp((event.clientX-r.left)/r.width),clamp((event.clientY-r.top)/r.height)];
}

function ordered(a,b) {
  return [Math.min(a[0],b[0]),Math.min(a[1],b[1]),Math.max(a[0],b[0]),Math.max(a[1],b[1])];
}

for (const button of document.querySelectorAll("[data-box]")) button.onclick = () => {
  if (busy || preview.readyState<2) return;
  preview.pause();
  selectBox(boxTool===button.dataset.box?null:button.dataset.box);
};
boxes.onpointerdown = event => {
  if (!boxTool || busy) return;
  const p = point(event);
  drag = {start:p,end:p};
  boxes.setPointerCapture(event.pointerId);
};
boxes.onpointermove = event => {
  if (!drag) return;
  drag.end = point(event);
  paintBoxes();
};
boxes.onpointerup = event => {
  if (!drag) return;
  const box = ordered(drag.start,point(event));
  drag = null;
  if ((box[2]-box[0])*480<8 || (box[3]-box[1])*270<8) {
    paintBoxes();
    message("analysis-status","Draw a slightly larger box.",true);
    return;
  }
  const key = boxTool;
  if (key.endsWith("_templates") && profile[key].length>=20) {
    selectBox(null);
    message("analysis-status","Use at most 20 examples per object.",true);
    return;
  }
  undo.push(JSON.stringify(profile));
  if (key.endsWith("_search")) profile[key] = box;
  else profile[key].push({video_s:frameTime(),box});
  selectBox(null);
  message("analysis-status","");
  setBusy(false);
};
boxes.onpointercancel = () => { drag = null; selectBox(null); };
document.addEventListener("keydown", event => {
  if (event.key==="Escape" && boxTool) { drag=null; selectBox(null); }
});
$("undo").onclick = () => {
  if (!busy && undo.length) { profile=JSON.parse(undo.pop()); selectBox(null); setBusy(false); }
};
$("previous").onclick = () => stepFrame(-1);
$("next").onclick = () => stepFrame(1);
for (const event of ["timeupdate","seeked","loadeddata"]) preview.addEventListener(event,() => profile && paintBoxes());

function readAnchors() {
  const entries = $("anchors").value.trim().split(/\r?\n/).filter(x=>x.trim());
  return entries.map(line => {
    const values = line.split(",").map(x=>x.trim());
    if (values.length!==2 || values.some(x=>!x || !Number.isFinite(Number(x)))) {
      throw new Error("Use two numeric times separated by a comma on each anchor line.");
    }
    return {video_s:Number(values[0]),sensor_s:Number(values[1])};
  });
}

function trackingProfile() {
  const ready = profile.glove_search && profile.helmet_search &&
    profile.glove_templates.length && profile.helmet_templates.length;
  return sample || !ready ? null : {...profile,direction:Number($("direction").value)};
}

async function analyzeSession() {
  drag = null;
  selectBox(null);
  player.pause();
  preview.pause();
  $("results").hidden = true;
  try {
    message("analysis-status","Starting analysis…");
    const data = {profile:trackingProfile(),anchors:readAnchors()};
    await request(endpoint("/analyze"),jsonPost(data));
    await waitFor(["done"],"analysis-status");
    result = await request(endpoint("/results/result.json"));
    displayResults();
    message("analysis-status","");
  } catch (error) {
    message("analysis-status",error.message,true);
    $("advanced").open = true;
  }
}

$("analyze").onclick = async () => {
  if (busy || !canAnalyze()) return;
  setBusy(true);
  try { await analyzeSession(); } finally { setBusy(false); }
};

function displayResults() {
  const path = name => endpoint("/results/" + name);
  player.src = path("annotated.mp4");
  $("download-video").href = path("annotated.mp4");
  $("events-csv").href = path("events.csv");
  $("signals-csv").href = path("aligned_signals.csv");
  message("audio-status",result.audio.message,result.audio.state==="failed");
  $("marker-detail").textContent = "Select an action marker to see its video time, sensor time and position.";
  $("counts").replaceChildren();
  $("filter").replaceChildren(new Option("All actions",""));
  for (const [key,[label,color]] of Object.entries(labels)) {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.style.color = color;
    chip.textContent = label + " " + result.events.filter(e=>e.label===key).length;
    $("counts").append(chip);
    $("filter").append(new Option(label,key));
  }
  const c = result.clock;
  $("sync-detail").textContent = `Sync: ${c.anchors.length} anchors · ${Math.round(c.rmse_s*1000)} ms fit RMSE · ${c.offset_s.toFixed(3)} s initial offset`;
  focused = null;
  $("results").hidden = false;
  $("axis").value = "video_s";
  renderEvents();
  resetChart();
  $("results").scrollIntoView({block:"start",behavior:"smooth"});
}

function seek(time, selected = null) {
  if (!result || time<0 || time>=result.duration) return;
  focused = selected;
  const event = result.events.find(e=>e.event_id===selected);
  if (event) $("marker-detail").textContent = markerText(event);
  player.currentTime = time;
  highlight();
  draw();
}

function position(e) {
  return e.x == null || e.y == null ? "—" : `(${Math.round(e.x*960)}, ${Math.round(e.y*540)}) px`;
}

function markerText(e) {
  return `${labels[e.label][0]} · Video ${fixed(e.video_s)} · Sensor ${e.sensor_time_estimated?"≈ ":""}${fixed(e.sensor_s)} · ${position(e)}`;
}

function renderEvents() {
  const tbody = $("events");
  tbody.replaceChildren();
  const filter = $("filter").value;
  for (const e of result.events) {
    if (filter && filter!==e.label) continue;
    const row = tbody.insertRow();
    row.dataset.event = e.event_id;
    const time = document.createElement("button");
    time.type = "button";
    time.textContent = fixed(e.video_s);
    time.onclick = () => seek(e.video_s,e.event_id);
    row.insertCell().append(time);
    const action = row.insertCell();
    action.textContent = labels[e.label][0];
    action.style.color = labels[e.label][1];
    row.insertCell().textContent = (e.sensor_time_estimated?"≈ ":"")+fixed(e.sensor_s);
    row.insertCell().textContent = position(e);
  }
  if (!tbody.rows.length) {
    const cell=tbody.insertRow().insertCell();
    cell.colSpan=4;
    cell.textContent="No events in this view.";
  }
  highlight();
}

const axis = () => $("axis").value;
const inverse = t => (t-result.clock.offset_s)/result.clock.scale;
const mapped = t => t*result.clock.scale+result.clock.offset_s;
const eventTime = e => axis()==="video_s"?e.video_s:e.sensor_s;
const playerTime = () => axis()==="video_s"?player.currentTime:inverse(player.currentTime);

function resetChart() {
  if (!result) return;
  fullLo = axis()==="video_s"?0:inverse(0);
  fullHi = axis()==="video_s"?result.duration:inverse(result.duration);
  for (const s of result.signals) {
    fullLo = Math.min(fullLo,s[axis()]);
    fullHi = Math.max(fullHi,s[axis()]);
  }
  lo=fullLo; hi=fullHi;
  draw();
}

function windowAt(start,width) {
  width=Math.min(width,fullHi-fullLo);
  lo=Math.max(fullLo,Math.min(fullHi-width,start));
  hi=lo+width;
  draw();
}

function zoom(factor) {
  if (!result) return;
  const t=playerTime(), center=t>=lo&&t<=hi?t:(lo+hi)/2;
  const width=Math.min(fullHi-fullLo,Math.max(1,(hi-lo)*factor));
  windowAt(center-width/2,width);
}

function draw() {
  if (!result || !ctx || !canvas.clientWidth) return;
  const width=canvas.clientWidth, height=canvas.clientHeight, ratio=window.devicePixelRatio||1;
  if (canvas.width!==Math.round(width*ratio) || canvas.height!==Math.round(height*ratio)) {
    canvas.width=Math.round(width*ratio); canvas.height=Math.round(height*ratio);
  }
  ctx.setTransform(ratio,0,0,ratio,0,0);
  ctx.clearRect(0,0,width,height);
  const left=73,right=width-15, bottom=height-25;
  const x=t=>left+(t-lo)/(hi-lo)*(right-left);
  plot={left,right};
  function line(x1,y1,x2,y2,color,w=1) {
    ctx.strokeStyle=color;ctx.lineWidth=w;ctx.beginPath();ctx.moveTo(x1,y1);ctx.lineTo(x2,y2);ctx.stroke();
  }
  function dot(xx,y,color,r) {
    ctx.fillStyle=color;ctx.beginPath();ctx.arc(xx,y,r,0,Math.PI*2);ctx.fill();
  }
  const step=[.1,.25,.5,1,2,5,10,15,30,60,120].find(s=>(hi-lo)/s<8)||300;
  const volt=result.signals.reduce((peak,s)=>Math.max(peak,Math.abs(s.peak_v)),1);
  ctx.save();ctx.beginPath();ctx.rect(left,10,right-left,bottom-10);ctx.clip();
  for(let t=Math.ceil(lo/step)*step;t<=hi+.001;t+=step) line(x(t),10,x(t),bottom,"#29332d");
  for (const [device,y,color] of [["glove",110,"#d5f678"],["helmet",179,"#8cbaff"]]) {
    line(left,y,right,y,"#47584d");
    for (const s of result.signals) {
      if (s.device!==device) continue;
      const xx=x(s[axis()]), tip=y-26*s.peak_v/volt;
      line(xx,y,xx,tip,s.in_video?color:"#829088",2);
      dot(xx,tip,labels[s.label]?.[1] || color,s.label?3.5:2.4);
    }
  }
  for (const e of result.events) {
    const xx=x(eventTime(e)), color=labels[e.label][1];
    line(xx,48,xx,bottom,color+"40");
    if (!e.sensor_time_estimated) {
      const sensorX=x(axis()==="video_s"?e.clock_video_s:e.sensor_s);
      line(xx,37,sensorX,37,color,2);line(sensorX,33,sensorX,41,color);
    }
    dot(xx,37,color,e.event_id===focused?7:4.5);
    if (e.event_id===focused) {
      ctx.font="600 12px system-ui";ctx.textAlign="left";ctx.fillStyle=color;
      ctx.fillText(labels[e.label][0],Math.min(right-85,xx+8),22);
    }
  }
  line(x(playerTime()),10,x(playerTime()),bottom,"#f0f6ed",1.4);
  ctx.restore();
  ctx.font="12px system-ui";ctx.textBaseline="middle";ctx.fillStyle="#b4c0b7";
  ctx.textAlign="start";
  for (const [label,y] of [["Action",37],["Glove (V)",110],["Helmet (V)",179]]) ctx.fillText(label,8,y);
  ctx.textAlign="center";
  for (let t=Math.ceil(lo/step)*step;t<=hi+.001;t+=step) {
    ctx.fillText(t.toFixed(step<1?2:0)+" s",x(t),height-12);
  }
}

function highlight() {
  for (const row of $("events").rows) {
    row.classList.toggle("active",focused!==null && row.dataset.event===focused);
  }
}

function nearestAt(time, markers, key) {
  return markers.reduce((best, item) => !best || Math.abs(key(item)-time)<Math.abs(key(best)-time)?item:best,null);
}
function chartPoint(event) {
  if (!result || !plot) return null;
  const r=canvas.getBoundingClientRect(), x=event.clientX-r.left, width=plot.right-plot.left;
  if (x<plot.left || x>plot.right) return null;
  return {time:lo+(x-plot.left)/width*(hi-lo), y:event.clientY-r.top, radius:12*(hi-lo)/width};
}

canvas.onclick = event => {
  const p=chartPoint(event);
  if (!p) return;
  const e=nearestAt(p.time,result.events,eventTime);
  if (e && p.y<58 && Math.abs(eventTime(e)-p.time)<p.radius) seek(e.video_s,e.event_id);
  else {
    const channel=p.y<150?"glove":"helmet", key=s=>s[axis()];
    const trigger=nearestAt(p.time,result.signals.filter(s=>s.device===channel),key);
    const event=trigger && Math.abs(key(trigger)-p.time)<p.radius &&
      result.events.find(e=>e.event_id===trigger.event_id);
    if (event) seek(event.video_s,event.event_id);
    else seek(axis()==="video_s"?p.time:mapped(p.time));
  }
};
canvas.onpointermove = event => {
  canvas.title="";
  const p=chartPoint(event);
  if (!p) return;
  if (p.y<58) {
    const e=nearestAt(p.time,result.events,eventTime);
    if (e && Math.abs(eventTime(e)-p.time)<p.radius) canvas.title=markerText(e);
  } else {
    const channel=p.y<150?"glove":"helmet", key=s=>s[axis()];
    const s=nearestAt(p.time,result.signals.filter(s=>s.device===channel),key);
    if (s && Math.abs(key(s)-p.time)<p.radius) {
      const event=result.events.find(e=>e.event_id===s.event_id);
      const details=`${channel==="glove"?"Glove":"Helmet"} · Trigger ${fixed(s.sensor_s)} · ${s.peak_v.toFixed(3)} V`;
      canvas.title=event ? `${details} · ${markerText(event)}` : details;
    }
  }
};
$("filter").onchange=renderEvents;
$("axis").onchange=resetChart;
$("reset").onclick=resetChart;
$("zoom-in").onclick=()=>zoom(.55);
$("zoom-out").onclick=()=>zoom(1.8);
$("left").onclick=()=>windowAt(lo-(hi-lo)*.6,hi-lo);
$("right").onclick=()=>windowAt(lo+(hi-lo)*.6,hi-lo);
player.addEventListener("timeupdate",draw);
player.addEventListener("seeked",draw);
window.addEventListener("resize",draw);
