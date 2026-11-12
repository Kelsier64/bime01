"""Crop a fixed front camera view and encode a browser-playable 15 fps preview."""

import argparse
from pathlib import Path

import cv2


def prepare(source, output, roi):
    x, y, width, height = roi
    video = cv2.VideoCapture(str(source))
    try:
        fps = video.get(cv2.CAP_PROP_FPS)
        if not video.isOpened() or not fps > 0:
            raise ValueError(f"Unable to read video: {source}")
        if (min(x, y) < 0 or min(width, height) < 2 or
                x + width > video.get(cv2.CAP_PROP_FRAME_WIDTH) or
                y + height > video.get(cv2.CAP_PROP_FRAME_HEIGHT)):
            raise ValueError("ROI must fit inside the video frame")

        out_width = min(width, 560) // 2 * 2
        out_height = max(2, round(height * out_width / width / 2) * 2)
        size = (out_width, out_height)
        step = max(1, round(fps / 15))
        writer = cv2.VideoWriter(str(output), cv2.VideoWriter_fourcc(*"VP80"), fps / step, size)
        try:
            if not writer.isOpened():
                raise ValueError("Unable to encode VP8 WebM with the installed OpenCV")
            frame, written = 0, 0
            while video.grab():
                if frame % step == 0:
                    valid, image = video.retrieve()
                    if not valid:
                        raise ValueError(f"Unable to decode frame {frame + 1}")
                    crop = image[y:y + height, x:x + width]
                    writer.write(cv2.resize(crop, size, interpolation=cv2.INTER_AREA))
                    written += 1
                frame += 1
        finally:
            writer.release()
        if not written:
            raise ValueError("No frames decoded")
    finally:
        video.release()
    print(f"Wrote {written} frames, {out_width}x{out_height} at {fps / step:g} fps (source stride {step})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--roi", type=int, nargs=4, required=True, metavar=("X", "Y", "WIDTH", "HEIGHT"))
    args = parser.parse_args()
    if args.output.suffix.lower() != ".webm" or args.source.resolve() == args.output.resolve():
        parser.error("use a different file with a .webm extension for the output")
    try:
        prepare(args.source, args.output, args.roi)
    except (OSError, ValueError, cv2.error) as error:
        parser.exit(1, f"{error}\n")
