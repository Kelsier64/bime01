"""Compare front-view tracks with the 3D-ZeF labels for a MOTChallenge preview."""

import argparse
import csv
import json
from collections import defaultdict
from itertools import product
from math import dist
from pathlib import Path
from zipfile import ZipFile


def annotations(archive, sequence):
    truth = defaultdict(list)
    with ZipFile(archive) as labels:
        content = labels.read(f"3DZeF20Lables/train/{sequence}/gt/gt.txt").decode()
    for row in csv.reader(content.splitlines()):
        frame, fish = map(int, row[:2])
        # MOTChallenge previews put the half-resolution front view on the left.
        x, y, width, height = (float(value) / 2 for value in row[14:18])
        truth[frame].append((fish, (x, y, width, height), bool(int(row[18]))))
    return truth


def matches(predictions, labels, padding):
    candidates = []
    for _, (x, y, width, height), _ in labels:
        center = (x + width / 2, y + height / 2)
        options = [(-1, 0)]
        if width > 0 and height > 0:
            for index, (_, point) in enumerate(predictions):
                px, py = point
                if x - padding <= px <= x + width + padding and y - padding <= py <= y + height + padding:
                    options.append((index, dist(center, point)))
        candidates.append(options)

    best = ()
    best_cost = (0, 0)
    for choices in product(*candidates):
        assigned = [index for index, _ in choices if index >= 0]
        if len(set(assigned)) != len(assigned):
            continue
        cost = (-len(assigned), sum(distance for _, distance in choices))
        if cost < best_cost:
            best, best_cost = choices, cost
    return [(label, index) for label, (index, _) in zip(labels, best) if index >= 0]


def evaluate(tracks, truth, stride, first_frame, padding, source_stride=1, transform=(0, 0, 1, 1)):
    predictions = defaultdict(list)
    x, y, scale_x, scale_y = transform
    with tracks.open(newline="", encoding="utf-8") as file:
        for row in csv.DictReader(file):
            if row["status"] == "tracked":
                point = (x + float(row["x_px"]) * scale_x, y + float(row["y_px"]) * scale_y)
                source_frame = (int(row["frame"]) - 1) * source_stride + 1
                predictions[source_frame].append((int(row["id"]), point))

    frames = range(1, max(truth) + 1, stride)
    visible = matched_visible = occluded = matched_occluded = unmatched = 0
    switches = []
    last_visible_ids = {}
    evaluated_frames = 0
    for frame in frames:
        if frame < first_frame:
            continue
        evaluated_frames += 1
        labels = truth[frame]
        visible += sum(not occlusion for _, _, occlusion in labels)
        occluded += sum(occlusion for _, _, occlusion in labels)
        assignment = matches(predictions[frame], labels, padding)
        unmatched += len(predictions[frame]) - len(assignment)
        for (fish, _, occlusion), index in assignment:
            if occlusion:
                matched_occluded += 1
                continue
            matched_visible += 1
            track_id = predictions[frame][index][0]
            previous = last_visible_ids.get(fish)
            if previous is not None and previous != track_id:
                switches.append({"frame": frame, "fish": fish, "old_id": previous, "new_id": track_id})
            last_visible_ids[fish] = track_id

    return {
        "evaluated_frames": evaluated_frames,
        "visible": visible,
        "matched_visible": matched_visible,
        "occluded": occluded,
        "matched_occluded": matched_occluded,
        "unmatched_predictions": unmatched,
        "id_changes": switches,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tracks", type=Path)
    parser.add_argument("labels", type=Path)
    parser.add_argument("--sequence", default="ZebraFish-03")
    parser.add_argument("--stride", type=int, default=4)
    parser.add_argument("--first-frame", type=int, default=361)
    parser.add_argument("--padding", type=float, default=4)
    parser.add_argument("--source-stride", type=int, default=1, help="Original frames per frame in the cropped video")
    parser.add_argument("--transform", type=float, nargs=4, default=(0, 0, 1, 1),
                        metavar=("X", "Y", "SCALE_X", "SCALE_Y"), help="Map cropped pixels back to source pixels")
    args = parser.parse_args()
    if min(args.stride, args.first_frame, args.source_stride) < 1 or not args.padding >= 0:
        parser.error("invalid stride, first frame or padding")
    truth = annotations(args.labels, args.sequence)
    result = evaluate(args.tracks, truth, args.stride, args.first_frame, args.padding, args.source_stride, args.transform)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
