# Pi 5 detector contract and measurement gate

Status: **hardware gate pending**. User-reported preliminary day and night FP32
results at confidence 0.5 and 0.35 are recorded below. Longer day/night runs, model comparison, agreed accuracy
criteria and live acceptance are still required.
The selected runtime contract is NanoDet 416; the FP32/INT8 artifact choice
must be made after measurements. Runtime loads one local artifact, without downloads.

Current supervised-test setting (2026-09-29): the user selected
`DETECTION_CONFIDENCE=0.35` in the shared `.env`. It loads in default and `prod`
mode in this checkout; later environment overrides on the Pi can change it.
The code fallback remains 0.5 when the setting is absent. Restart the process
after configuration changes. This selection does not complete hardware acceptance.

## Reproducible source

[OpenCV Zoo NanoDet](https://github.com/opencv/opencv_zoo/tree/47534e27c9851bb1128ccc0102f1145e27f23f98/models/object_detection_nanodet),
revision `47534e27c9851bb1128ccc0102f1145e27f23f98`.
The adapted `nanodet.py` and `demo.py` code is Apache-2.0; the original
[license](../licenses/nanodet-LICENSE) is included.

| Candidate | Official Git LFS SHA256 |
| --- | --- |
| `object_detection_nanodet_2022nov.onnx` (FP32) | `4b82da9944b88577175ee23a459dce2e26e6e4be573def65b1055dc2d9720186` |
| `object_detection_nanodet_2022nov_int8.onnx` (INT8) | `8dd32b85f2d273e9047f1d6b59e0b2fd008b1076338107bb547ac28942cdf90b` |

Use the pinned repository's Git LFS artifacts, verify `sha256sum`, then put the
chosen candidate at `system/models/person.onnx`. Both pinned artifacts are bundled:
`system/models/person.onnx` is FP32 and `system/models/person_int8.onnx` is INT8.
Both are Apache-2.0; see [the attribution notice](../../NOTICE). Verify the hashes
before using them. Their inclusion does not complete the measurement gate.

Input: BGR frame converted to RGB as in the pinned demo, aspect-preserving
416x416 letterbox with black padding and INTER_AREA interpolation. Normalize
channels with mean `[103.53,116.28,123.675]`, standard deviation
`[57.375,57.12,58.395]`; float32 NCHW. Six alternating class/regression
outputs at strides 8/16/32, reg_max=7; distance-distribution softmax decoding.
COCO class 0 is person. Keep winning person labels at confidence >=0.5,
top 1000 candidates per level, person NMS IoU 0.6, restore clipped original xyxy
pixel coordinates. Adaptations: stable softmax, person filtering before NMS,
inclusive score threshold, bounded immutable frames, safe faults and timings.

## Measurements to perform

On the actual Pi 5 (4 GB, no accelerator), record OS/architecture/interpreter,
OpenCV version, backend, cooling/power, and model hash. Keep Python >=3.11,<3.15;
the Windows dependency resolution does not establish ARM64 wheel support.

For each candidate run both representative clips of at least 60 seconds:

```sh
poetry run python -m tools.benchmark_detection --video system/samples/day.mp4 --model system/models/person.onnx --confidence 0.5 --output volume/day-benchmark.json
poetry run python -m tools.benchmark_detection --video system/samples/night.mp4 --model system/models/person.onnx --confidence 0.5 --output volume/night-benchmark.json
```

The tool excludes five warmup inferences, reports stage p50/p95, effective rate,
process CPU, peak RSS (Linux), final Pi temperature/throttling and per-frame
presence for comparison with labels. Monitor temperature/throttling during the
run too (`watch -n 1 'vcgencmd measure_temp; vcgencmd get_throttled'`).
Record person/empty truth labels, false positives, missed-person frames and
incident-level false alarms/misses. Review initial measurements with the user;
agree day/night acceptable error criteria before selecting a candidate.

| Gate | Result |
| --- | --- |
| At least 2 fresh results/second, each clip/variant | Preliminary FP32 file rates: day 12.37/12.55 frames/s and night 12.63/12.68 at confidence 0.5/0.35; >=60-second runs, INT8 comparison and live freshness pending |
| Day and night accuracy criteria agreed and met | Day walking interval: 54.4%/93.9% presence recall at 0.5/0.35, excluding the nearly hidden seated person as accepted by the user. Night 25–28-second partial-visibility interval: 3/36 versus 10/36 frames detected; labels and broader acceptance criteria remain incomplete |
| Python/ARM64 dependency and FFmpeg timeout support | Reported ARM64 FP32 run succeeds on Python 3.13.5; full installation and RTSP timeout checks pending |
| RTSP visible clock/movement latency and stale-buffer rejection | Pending |
| Local stop decision to silence <=1 second | Pending |
| Authorized supervised end-to-end acceptance | Pending |

Do not enable unattended automatic alarms until these gates pass. If neither
variant meets the rate and agreed accuracy criteria, revise the model/resolution
with measured evidence before live automatic-audio acceptance.

## Preliminary day FP32 result

Source: user-supplied `day-benchmark.json`, with the user's initial approximate
label that a person is continuously visible from 5 to 20 seconds. The user later
confirmed partial visibility before 5 seconds and a second partly visible person
on a chair who remains visible before and after the walking person's appearance.
The user accepts missing this almost invisible seated person for this test; the
evaluation target is the visible walking person, as explained below.
The original video was not inspected here.
This is a 22.916-second clip, shorter than the required
60-second acceptance clips. Run date, power supply and cooling were not supplied.

Environment: Python 3.13.5, OpenCV 4.14.0, NumPy 2.4.6,
`Linux-6.18.50+rpt-rpi-2712-aarch64-with-glibc2.41`, architecture `aarch64`.
The reported model SHA256 matches the pinned FP32 artifact above.
Video SHA256: `d8c2dc3c548b6693aa37c03794b1175e1a831b0f4c5187842680fb4c8cd671bb`.
Confidence threshold: 0.5.

| Measurement | Reported result |
| --- | --- |
| Frames / clip duration | 275 / 22.916 seconds |
| Processing duration | 22.229 seconds |
| Effective file processing rate | 12.37 frames/s, about 6.19 times the 2 results/s target |
| Preprocessing p50 / p95 | 11.88 / 13.95 ms |
| Inference p50 / p95 | 64.01 / 76.06 ms |
| Postprocessing p50 / p95 | 1.09 / 1.27 ms |
| Process CPU, relative to one core | 222.25%, equivalent to about 2.22 fully busy cores |
| Benchmark process peak RSS | 113.47 MiB |
| Final temperature | 55.1 C |
| Final throttling status | `throttled=0x0` |

The status value has no throttling/undervoltage flags set according to the
[Raspberry Pi bit definitions](https://www.raspberrypi.com/documentation/computers/os.html#get_throttled).
These are short file-benchmark measurements; they do not establish sustained
temperature, full-service memory use, or live RTSP frame freshness.

Using the reported frame timestamps and the interval `5 <= video_seconds <= 20`:

- 98 of 180 frames in the visible interval contain a detection: approximately
  54.4% frame recall, with 82 missed frames. The label boundaries are approximate,
  so these are preliminary presence counts rather than box-level accuracy scores.
- All 95 frames outside that interval have zero detections. The user accepts
  missing the nearly hidden seated person; partial entry of the walking person
  before 5 seconds is discussed below. This clip does not establish a general
  false-alarm rate for prolonged empty-scene operation.
- First detection: frame 70 at 5.750 seconds, approximately 0.75 seconds after
  entry. Last detection: frame 236 at 19.583 seconds.
- Longest gap between positive detections: 1.250 seconds, from 16.166 to 17.416.
  Misses are frequent despite the processing rate exceeding the target.

A local replay of the supplied presence sequence, armed in day mode and using
video timestamps, produces recognition/warning at frame 96 (7.916 seconds).
Under the earlier stop-on-departure policy it also ended the incident at frame
262 (21.749 seconds), consistent with the walking person leaving around 20 seconds
and the accepted exclusion of the nearly hidden seated person. This replay
supplied every frame without processing delay and omitted audio callbacks.
The revised policy finishes warning/countdown sounds and any started siren period;
those historical departure times therefore do not predict current audio-stop
times. Current stop timing requires playback callbacks and a longer recording.

## Confidence comparison: 0.5 versus 0.35

The second user-supplied result uses the same model/video hashes, environment,
275 frames and 22.916-second clip. A frame is counted as positive if `people > 0`.
The original 5-to-20-second interval is retained for a consistent comparison during
the walking person's appearance. It is approximate and excludes partial entry
before 5 seconds. The user accepts missed detections of the almost invisible
seated person in this particular test; that exception does not establish acceptable
behavior for other partly visible people. Counts alone do not identify individuals.

| Measurement | Confidence 0.5 | Confidence 0.35 |
| --- | --- | --- |
| Frames with a detection during the approximate walking interval | 98 / 180 | 169 / 180 |
| Presence recall during the approximate walking interval | 54.4% | 93.9% |
| Missed frames during the approximate walking interval | 82 | 11 |
| Positive frames outside the approximate walking interval | 0 | 7, all before 5 seconds |
| Frames reporting two people | 0 | 10 |
| First / last detection | 5.750 / 19.583 s | 4.333 / 19.916 s |
| Longest gap between positive detections | 1.250 s | 0.333 s |
| Replayed day recognition/warning | 7.916 s | 6.416 s |
| Historical departure under the earlier stop-on-departure policy | 21.749 s | 22.083 s |
| Effective file processing rate | 12.37 frames/s | 12.55 frames/s |
| Processing duration | 22.229 s | 21.917 s |
| Preprocessing p50 / p95 | 11.88 / 13.95 ms | 11.85 / 11.96 ms |
| Inference p50 / p95 | 64.01 / 76.06 ms | 64.39 / 69.05 ms |
| Postprocessing p50 / p95 | 1.09 / 1.27 ms | 1.08 / 1.13 ms |
| Process CPU, relative to one core | 222.25% | 223.61% |
| Benchmark process peak RSS | 113.47 MiB | 113.42 MiB |
| Final temperature | 55.1 C | 55.65 C |
| Final throttling status | `throttled=0x0` | `throttled=0x0` |

At 0.35, the missed frames within the literal 5-to-20-second interval are
121, 159, 198–200, 207–209, 231, 237 and 241. There are about 87% fewer missed
frames than at 0.5 on this clip, with similar processing speed. Both policy
replays maintain one incident throughout the 5-to-20-second interval; the same
limitations of timestamp replay and omitted audio callbacks apply. The historical
departure decisions are consistent with the accepted scope of this test, but
current audio completion must be verified under the revised departure policy.

The early detections are frame 53 (4.333 s) and frames 56–61 (4.583–5.000 s).
Frame 61 is only 0.00015 seconds before the literal 5-second cutoff. The user
confirmed partial visibility between 4.33 and 5 seconds, so these detections are
consistent with the scene and should not be counted as false positives merely
because they precede the approximate label.

Frames 110, 157, 164, 171, 182, 220, 230, 232, 233 and 235 report two people.
The user confirmed a second partly visible person on a chair, seen from behind.
The two-person counts are therefore plausible. The JSON contains counts without
boxes, so correct localization of both people has not been verified. The
controller uses scene occupancy, so a second box does not itself create a
second incident.

The user confirmed that the seated person remains visible before 4.33 seconds
and after 20 seconds, then explicitly accepted missed detections because that
person is almost invisible. The historical departure at 22.083 seconds is acceptable
for this test under that expectation. The 93.9% figure measures presence within
the walking person's approximate interval; it is not recall for every person.

Confidence 0.35 is the stronger candidate for further testing: it improves
detection continuity for the visible person with similar processing speed, and
the earlier detections and occasional two-person counts are consistent with the
user's description. At the time of this comparison, `.env` selected 0.5. Next, evaluate 0.35 on longer
day/night clips and empty scenes, then agree broader error criteria before
adoption. This short clip and the accepted visibility exception do not complete
hardware acceptance.

Command used for the comparison:

```sh
poetry run python -m tools.benchmark_detection --video system/samples/day.mp4 --model system/models/person.onnx --confidence 0.35 --output volume/day-fp32-confidence-035.json
```

Repeat on representative day/night clips of at least 60 seconds, including empty
scenes, and compare INT8 before selecting the artifact and agreeing acceptable
error criteria.

## Preliminary night FP32 comparison (reported 2026-09-29)

Sources: user-supplied `night-benchmark.json` (confidence 0.5) and
`night-fp32-confidence-035.json` (0.35). Both contain 394 frames from the same
32.833-second clip, below the 60-second acceptance length. Environment matches
the reported day runs: Python 3.13.5, OpenCV 4.14.0, NumPy 2.4.6,
`Linux-6.18.50+rpt-rpi-2712-aarch64-with-glibc2.41`, `aarch64`.
The reported model hash matches the pinned FP32 artifact. Power/cooling details
and the actual benchmark execution times were not supplied.

Video SHA256: `7e9abdb75ae39fdbd27f7c56f18ecc0da14ef6bba31f57ea056019f57c0f889a`.
The private sample available during evaluation matched this hash. Selected frames
were inspected locally, including exact frame indices 279, 295 and 337, using the benchmark's
frame-index/FPS timestamps. No inference benchmark was rerun on the Pi here.

| Measurement | Confidence 0.5 | Confidence 0.35 |
| --- | --- | --- |
| Effective file processing rate | 12.63 frames/s | 12.68 frames/s |
| Processing duration | 31.188 s | 31.072 s |
| Preprocessing p50 / p95 | 11.93 / 12.02 ms | 11.58 / 11.69 ms |
| Inference p50 / p95 | 64.08 / 64.58 ms | 64.06 / 68.12 ms |
| Postprocessing p50 / p95 | 1.10 / 1.22 ms | 1.01 / 1.13 ms |
| Process CPU, relative to one core | 223.32% | 223.35% |
| Benchmark process peak RSS | 113.23 MiB | 113.16 MiB |
| Final temperature | 55.65 C | 55.10 C |
| Final throttling status | `throttled=0x0` | `throttled=0x0` |
| Frames with any person detection, whole clip | 111 / 394 | 167 / 394 |
| Frames reporting two people | 0 | 8 |
| First detection | Frame 83, 6.833 s | Frame 74, 6.083 s |
| Last detection | Frame 339, 28.166 s | Frame 341, 28.333 s |

Both rates exceed the 2-results/second target for these short file runs. Their
speed, CPU and memory are similar to the day results. They do not establish live
RTSP latency, sustained temperature, or full-service memory use. A higher positive
frame count alone does not establish better accuracy without scene labels.

### Visibility labels and detection gaps

The user described no person from 19–25 seconds and partial visibility at
25–28 seconds, with the close person difficult to recognize in night vision.
Selected local frames refine that approximate boundary: frame 295 (24.500 s)
already shows part of a person entering at the right edge; frame 337 (28.000 s)
shows part of a person at the left edge. Frame 279 (23.166 s) shows an object/hand
at the right edge. Treat 23–25 seconds as a transition needing frame labels,
rather than counting all detections there as verified false positives.

| Review interval, using literal video timestamps | Confidence 0.5 | Confidence 0.35 |
| --- | --- | --- |
| 19–23 s, user-labelled empty | 0 / 48 frames positive | 0 / 48 frames positive |
| 23–25 s, approximate boundary requiring review | 6 / 24 frames positive | 13 / 24 frames positive |
| 25–28 s, user-labelled partly visible | 3 / 36 frames positive (8.3%) | 10 / 36 frames positive (27.8%) |

The partial-visibility interval improves at 0.35 but remains poorly detected.
Its negative frames number 33 at 0.5 and 26 at 0.35. This is a separate difficult
case; the day clip's accepted seated-person exception does not automatically
exclude it from night evaluation. The short empty interval does not establish a
general false-alarm rate.

For the earlier detection window (frames 74–222, 6.083–18.416 s), 101 of 149
frames are positive at 0.5 and 140 at 0.35. These boundaries come from the
detections, so those counts are not a labelled recall score. The largest gap
between positives in that window is 0.667 s at 0.5 and 0.417 s at 0.35, both
within the controller's 1-second confirmation tolerance. Exact first-pass entry
and exit labels remain to be agreed.

The eight two-person frames at 0.35 are 143, 144, 158, 166, 167, 168, 214 and
341. The JSON provides no boxes, so these are not evidence of a second person;
review localization before judging them correct or duplicated detections.

### Replay through the current night policy

A local replay fed the supplied counts at their video timestamps, with the
controller armed and the night confirmation time set to the current 3-second
default. Warning/countdown completion callbacks were simulated using the current
Czech-plus-English WAV durations: 18.756 seconds in total. Audio was not played,
network services were not contacted, and no real processing or playback startup
delay was simulated. The siren-start callback was immediate in this replay.

| Simulated transition | Confidence 0.5 | Confidence 0.35 |
| --- | --- | --- |
| Recognition / first warning | 9.917 s | 9.167 s |
| Still-there / countdown starts | 15.063 s | 14.313 s |
| All warning/countdown files finish | 28.672 s | 27.922 s |
| Siren | Not started; waits for confirmed absence | Starts at 27.922 s |
| Incident at final frame | Ended at 30.333 s | First siren period still active |

At 0.35, a fresh positive near the end of countdown permits the siren even though
the scene was empty earlier during the prelude. This matches the current rule:
finish all warning/countdown sounds, then decide from fresh presence. At 0.5,
the final countdown finishes during negative observations; confirmed absence
subsequently ends the incident. These outcomes depend on WAV timing and detection
gaps and are not measured live outcomes.

The clip ends before the simulated siren's 60-second limit. Finishing that period
after departure and the three-period repeat limit still need a longer supervised
test. Do not extrapolate fresh camera observations beyond the clip.

Confidence 0.35 remains a candidate for further evaluation: it detects earlier
and more consistently in the first pass, but the close partial pass still has
many missed frames. The user subsequently selected 0.35 for supervised testing,
as recorded above. Complete labels, longer day/night and empty-scene footage,
and the FP32/INT8 comparison before accepting the detector for unattended use.

## Local contract verification (2026-09-28)

Both artifacts were downloaded to ignored review scratch space and their SHA256
hashes matched the table above. On Windows with Python 3.14, OpenCV 4.14.0.94 and
NumPy 2.4.6, both loaded and completed inference on a synthetic blank frame and
a 10-frame synthetic video through the benchmark CLI's underlying function.
Both artifacts expose six output tensors: class/regression pairs with 2704,
676 and 169 anchors. The official example defines an extra stride but its zip
uses only these three actual output levels. Regression fixtures now match them.
These checks establish artifact/decoder compatibility; they do not measure Pi
throughput or recognition accuracy.
