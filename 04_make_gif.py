"""Verify recorded spot brightness and make a 10-second GIF from camera pixels."""

import json
from pathlib import Path
import sys
import numpy as np
import tifffile
from PIL import Image, ImageDraw
from configuration import results_path

folder = Path(sys.argv[1]) if len(sys.argv)>1 else sorted(results_path.glob("*_rearrangement"))[-1]
record = json.loads((folder / "run.json").read_text())
frames = tifffile.imread(folder / "frames.tiff")
calibration = json.loads(Path(record["calibration_file"]).read_text())
target = calibration["target_counts"]
calibration_xy = np.array([p["xy"] for p in calibration["points"]]).reshape(7,25,2)
sample_frequencies = np.array(calibration["column_frequencies_hz"])
timestamps = np.array([p["timestamp_ns"] for p in record["timestamps"]],dtype=np.int64)
times = (timestamps-timestamps[0])/1e9
yy,xx = np.indices(frames.shape[1:])


def spots(frame):
    background = float(np.median(frame))
    remaining = frame.astype(float).copy()
    found = []
    for _ in range(25):
        y,x = np.unravel_index(np.argmax(remaining),remaining.shape)
        if remaining[y,x] < background+30:
            break
        radius_squared = (xx-x)**2+(yy-y)**2
        counts = float((frame.astype(float)-background)[radius_squared<=36].sum())
        weight = np.maximum(frame.astype(float)-background,0)*(radius_squared<=25)
        found.append({"x":float((weight*xx).sum()/weight.sum()),
                      "y":float((weight*yy).sum()/weight.sum()),"counts":counts})
        remaining[radius_squared<=64] = background
    return sorted(found,key=lambda spot:spot["x"])


detections = [spots(frame) for frame in frames]
first = next(i for i,detected in enumerate(detections) if len(detected)>=3)
duration = record["cycles"] * record["cycle_seconds"]

# Fit the camera/OPX time offset to the measured moving-spot positions.
# No camera trigger cable is used, so host resume time alone is insufficient.
def timing_loss(start):
    losses = []
    for t,detected in zip(times,detections):
        elapsed = t-start
        if len(detected)!=3 or not 0<=elapsed<duration:
            continue
        cycle = int(elapsed//2)
        local = elapsed%2
        row = int(local//0.260)
        if row>=7:
            losses.append(100)
            continue
        u = np.clip((local-row*.260-.020001)/.220,0,1)
        sites = np.array(record["loadings"][cycle][row])
        frequencies = 95e6+1e6*(sites+(np.array([4,5,6])-sites)*u)
        expected = np.column_stack([np.interp(frequencies,sample_frequencies,calibration_xy[row,:,axis]) for axis in (0,1)])
        observed = np.array([[spot["x"],spot["y"]] for spot in detected])
        losses.append(float(np.minimum(((observed-expected)**2).sum(axis=1),100).mean()))
    return float(np.mean(losses))


offsets = np.arange(times[first]-.030,times[first]+.010,.0002)
start = float(min(offsets,key=timing_loss))
moving_errors, rectangle_errors, missing = [],[],[]
for i,(t,detected) in enumerate(zip(times,detections)):
    elapsed = t-start
    if not 0<=elapsed<duration:
        continue
    local = elapsed%2
    # Exclude exposures within 1 ms of a row change or rectangle transition.
    boundaries = np.array([0,.260,.520,.780,1.040,1.300,1.560,1.820,2.000])
    if np.min(abs(boundaries-local))<.001:
        continue
    expected = 3 if local<1.820 else 21
    if len(detected)!=expected:
        missing.append({"frame":i,"time_s":float(elapsed),"found":len(detected),"expected":expected})
        continue
    errors = moving_errors if expected==3 else rectangle_errors
    errors.extend([spot["counts"]/target-1 for spot in detected])


def summarize(errors):
    values = np.array(errors)
    return {"spot_measurements":len(values),"mean_counts":float(target*(1+values.mean())),
            "rms_nonuniformity":float(np.std(values)/(1+values.mean())),
            "worst_nonuniformity":float(np.max(abs((1+values)/(1+values.mean())-1))),
            "rms_relative_error":float(np.sqrt(np.mean(values**2))),
            "worst_relative_error":float(np.max(abs(values))),"within_5_percent":float(np.mean(abs(values)<=.05)),
            "within_10_percent":float(np.mean(abs(values)<=.10))} if len(values) else {}


verification = {"measured_fps":float(1e9/np.median(np.diff(timestamps))),
    "distinct_loadings_per_row":[len({tuple(loading[row]) for loading in record["loadings"]}) for row in range(7)],
    "missing_frame_ids":int(np.diff([p["frame_id"] for p in record["timestamps"]]).sum()-(len(frames)-1)),
    "camera_opx_offset_s":start,"position_fit_mse_pixels":timing_loss(start),
    "peak":int(frames.max()),"saturated_pixels":int((frames==255).sum()),
    "moving_rows":summarize(moving_errors),"final_rectangles":summarize(rectangle_errors),"unexpected_spot_counts":missing}
(folder / "verification.json").write_text(json.dumps(verification,indent=2))
(folder / "detections.json").write_text(json.dumps(detections))

# Select real frames at their recorded times; keep the same intensity scale throughout.
chosen = np.where((times>=start)&(times<start+duration))[0]
panel_size = 480
divider = 4
scale = panel_size / frames.shape[2]

# One static occupancy diagram per shot, aligned with the camera's site positions.
# Filled cyan circles are simulated occupied sites; gray rings are empty sites.
loading_panels = []
for loading in record["loadings"]:
    panel = Image.new("RGB",(panel_size,panel_size),"#101010")
    draw = ImageDraw.Draw(panel)
    for row, occupied in enumerate(loading):
        for column in range(7):
            x,y = calibration_xy[row,column*4] * scale
            draw.ellipse((x-8,y-8,x+8,y+8),outline="#444444",width=1)
            if column in occupied:
                draw.ellipse((x-6,y-6,x+6,y+6),fill="#6edce8")
    loading_panels.append(panel)

gif_frames = []
for i in chosen:
    elapsed = max(0,times[i]-start)
    cycle = min(record["cycles"]-1,int(elapsed//record["cycle_seconds"]))
    image = Image.new("RGB",(2*panel_size+divider,panel_size),"#444444")
    image.paste(loading_panels[cycle],(0,0))
    image.paste(Image.fromarray(frames[i]).resize((panel_size,panel_size),Image.Resampling.NEAREST),(panel_size+divider,0))
    gif_frames.append(image)
edges = np.round(np.linspace(0,duration*1000,len(gif_frames)+1)/10)*10
durations = np.diff(edges).astype(int).tolist()
gif_frames[0].save(folder / "rearrangement_split.gif",save_all=True,append_images=gif_frames[1:],duration=durations,loop=0,optimize=False)
print(json.dumps(verification,indent=2))
print(folder / "rearrangement_split.gif")
