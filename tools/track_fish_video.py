"""Track up to two fish in a fixed-camera recording and export image coordinates."""

import argparse
import csv
import sys
import time
from pathlib import Path

import cv2
import numpy as np

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aquarium.vision import Tracker, find_fish, prepare


def background_model(video, roi, size, fps, seconds):
    samples = []
    interval = max(1, round(fps / 4))
    for frame in range(round(fps * seconds)):
        if not video.grab():
            break
        if frame % interval == 0:
            valid, image = video.retrieve()
            if valid:
                samples.append(prepare(image, roi, size))
    if len(samples) < 4:
        raise ValueError("Need at least four frames for the background model")
    return np.median(samples, axis=0).astype(np.uint8)


def run(args):
    start = time.monotonic()
    video = cv2.VideoCapture(str(args.video))
    try:
        fps = video.get(cv2.CAP_PROP_FPS)
        frame_width = int(video.get(cv2.CAP_PROP_FRAME_WIDTH))
        frame_height = int(video.get(cv2.CAP_PROP_FRAME_HEIGHT))
        if not video.isOpened() or not fps > 0:
            raise ValueError(f"Unable to read video: {args.video}")
        roi = args.roi or (0, 0, frame_width, frame_height)
        x, y, width, height = roi
        if min(x, y) < 0 or min(width, height) < 1 or x + width > frame_width or y + height > frame_height:
            raise ValueError("ROI must fit inside the video frame")
        scaled_width = min(width, 560)
        size = (scaled_width, max(1, round(height * scaled_width / width)))
        background = background_model(video, roi, size, fps, args.background_seconds)
        video.release()
        video = cv2.VideoCapture(str(args.video))
        if not video.isOpened():
            raise ValueError(f"Unable to reopen video: {args.video}")

        step = max(1, round(fps / 15))
        tracker = Tracker(args.fish, max_gap=max(1, round(fps / step * 0.5)), gate=27)
        frame = -1
        processed = 0
        with args.output.open("w", newline="", encoding="utf-8") as output:
            writer = csv.writer(output)
            writer.writerow(["frame", "time_s", "id", "status", "x_px", "y_px"])
            while video.grab():
                frame += 1
                if frame % step:
                    continue
                valid, image = video.retrieve()
                if not valid:
                    raise ValueError(f"Unable to decode frame {frame + 1}")
                gray = prepare(image, roi, size)
                detections = find_fish(gray, background, args.threshold, args.min_area, args.max_area)
                tracks = tracker.step(detections, processed)
                for track in tracks:
                    tracked = track.frame == processed
                    px = round(x + track.point[0] * width / size[0], 2) if tracked else ""
                    py = round(y + track.point[1] * height / size[1], 2) if tracked else ""
                    writer.writerow([frame + 1, round(frame / fps, 4), track.id,
                                     "tracked" if tracked else "lost", px, py])
                processed += 1
        if not processed:
            raise ValueError("No frames decoded")
    finally:
        video.release()
    print(f"Decoded {frame + 1} frames, processed {processed} at {fps / step:.2f} fps "
          f"in {time.monotonic() - start:.2f} s")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--roi", type=int, nargs=4, metavar=("X", "Y", "WIDTH", "HEIGHT"))
    parser.add_argument("--fish", type=int, choices=(1, 2), default=2)
    parser.add_argument("--background-seconds", type=float, default=6)
    parser.add_argument("--threshold", type=int, default=13)
    parser.add_argument("--min-area", type=int, default=22)
    parser.add_argument("--max-area", type=int, default=550)
    args = parser.parse_args()
    if not 0 < args.background_seconds <= 15:
        parser.error("--background-seconds must be between 0 and 15")
    if not 0 <= args.threshold <= 255 or not 0 < args.min_area <= args.max_area:
        parser.error("invalid threshold or blob area")
    if args.video.resolve() == args.output.resolve():
        parser.error("input video and output CSV must use different paths")
    try:
        run(args)
    except (OSError, ValueError, cv2.error) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
